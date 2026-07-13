"""PakBus low-level framing: SerSyncByte framing, byte quoting, signatures.

Every rule in this module is taken from Campbell Scientific
"BMP5 Transparent Commands", rev. 9/08 (sections 1.2, 1.4, Appendix B):

* Frames are delimited by the SerSyncByte 0xBD (sec. 1.2.1). One or more
  0xBD bytes may precede a frame (wake-up / buffer flush).
* Inside a frame, reserved bytes are quoted (sec. 1.2.1 / 1.4.1):
      0xBD -> 0xBC 0xDD
      0xBC -> 0xBC 0xDC
  i.e. QuoteByte 0xBC followed by (original byte + 0x20).
* Integrity uses the CSI running signature (Appendix B), seed 0xAAAA.
  Each frame ends with a two-byte "signature nullifier" chosen so the
  signature over (unquoted content + nullifier) equals zero.
* Packet processing checklist (sec. 1.4.4): discard frames shorter than
  4 bytes or longer than 1010 bytes, and any frame whose signature is
  non-zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .errors import FramingError, SignatureError

log = logging.getLogger(__name__)

SYNC = 0xBD          # SerSyncByte (frame delimiter / wake-up)
QUOTE = 0xBC         # QuoteByte
QUOTE_OFFSET = 0x20  # quoted value = original + 0x20

# sec 1.4.4: total unquoted frame length (including nullifier) bounds
MIN_FRAME_LEN = 4
MAX_FRAME_LEN = 1010


def calc_sig(data: bytes, seed: int = 0xAAAA) -> int:
    """CSI running signature (BMP5 Transparent Commands, Appendix B).

    Verified against the six example packets printed in that document
    (see tests/test_framing.py).
    """
    sig = seed & 0xFFFF
    for byte in data:
        j = sig
        sig = (sig << 1) & 0x01FF
        if sig >= 0x100:
            sig += 1
        sig = ((sig + (j >> 8) + byte) & 0xFF) | ((j << 8) & 0xFF00)
    return sig & 0xFFFF


def calc_sig_nullifier(sig: int) -> int:
    """Two-byte nullifier: appending it drives the running signature to 0.

    Direct transcription of ``calcSigNullifier`` from Appendix B.
    """
    new_seed = (sig << 1) & 0x1FF
    if new_seed >= 0x100:
        new_seed += 1
    null1 = (0x100 - (new_seed + (sig >> 8))) & 0xFF

    new_sig = calc_sig(bytes([null1]), sig)
    new_seed = (new_sig << 1) & 0x1FF
    if new_seed >= 0x100:
        new_seed += 1
    null2 = (0x100 - (new_seed + (new_sig >> 8))) & 0xFF

    return (null1 << 8) | null2


def quote(data: bytes) -> bytes:
    """Quote reserved bytes for transmission (sec. 1.4.1).

    0xBC must be replaced before 0xBD so freshly inserted quote bytes are
    not themselves re-quoted.
    """
    data = data.replace(bytes([QUOTE]), bytes([QUOTE, QUOTE + QUOTE_OFFSET]))
    data = data.replace(bytes([SYNC]), bytes([QUOTE, SYNC + QUOTE_OFFSET]))
    return data


def unquote(data: bytes) -> bytes:
    """Reverse of :func:`quote` (sec. 1.4.2)."""
    out = bytearray()
    it = iter(data)
    for byte in it:
        if byte == QUOTE:
            try:
                nxt = next(it)
            except StopIteration:
                # Dangling quote byte at end of frame: leave it; the
                # signature check will reject the frame.
                out.append(byte)
                break
            out.append((nxt - QUOTE_OFFSET) & 0xFF)
        else:
            out.append(byte)
    return bytes(out)


def frame_packet(content: bytes) -> bytes:
    """Build a complete on-the-wire frame around header+message bytes.

    Appends the signature nullifier, quotes reserved bytes, and wraps in
    SerSyncBytes: ``BD <quoted(content + nullifier)> BD``.
    """
    nullifier = calc_sig_nullifier(calc_sig(content))
    body = content + nullifier.to_bytes(2, "big")
    return bytes([SYNC]) + quote(body) + bytes([SYNC])


def unframe_packet(frame: bytes) -> bytes:
    """Strict one-shot inverse of :func:`frame_packet` (tests/fixtures).

    Expects exactly one framed packet, possibly with extra leading or
    trailing SYNC bytes. Raises FramingError/SignatureError on any
    problem. The receive path uses :class:`Deframer` instead, which
    counts-and-continues.
    """
    inner = frame.strip(bytes([SYNC]))
    if not inner:
        raise FramingError("no frame content between SerSyncBytes", frame)
    content = unquote(inner)
    if not MIN_FRAME_LEN <= len(content) <= MAX_FRAME_LEN:
        raise FramingError(
            f"frame length {len(content)} outside "
            f"[{MIN_FRAME_LEN}, {MAX_FRAME_LEN}]", frame)
    if calc_sig(content) != 0:
        raise SignatureError("frame signature is non-zero", frame)
    return content[:-2]


@dataclass
class DeframerStats:
    """Counters for everything the deframer rejected. Never silent."""

    frames_ok: int = 0
    bad_signature: int = 0
    too_short: int = 0
    too_long: int = 0

    @property
    def frames_rejected(self) -> int:
        return self.bad_signature + self.too_short + self.too_long

    def __str__(self) -> str:  # human-readable for CLI stats output
        return (f"ok={self.frames_ok} bad_signature={self.bad_signature} "
                f"too_short={self.too_short} too_long={self.too_long}")


@dataclass
class Deframer:
    """Incremental frame extractor for a raw byte stream.

    Feed it whatever the transport produced; it returns the validated,
    unquoted packets (signature nullifier stripped). Invalid frames are
    counted in :attr:`stats` and logged with their raw bytes -- they are
    never partially parsed and never raise, so one corrupt frame cannot
    take down a polling loop (data-integrity requirement).

    A 0xBD byte both terminates the current frame and starts the next,
    matching the reference receive loop in BMP5 Transparent Commands
    Appendix D (``GetLine``).
    """

    stats: DeframerStats = field(default_factory=DeframerStats)
    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[bytes]:
        packets: list[bytes] = []
        for byte in data:
            if byte != SYNC:
                self._buf.append(byte)
                continue
            if not self._buf:
                # consecutive SYNC bytes (wake-up stream) -- not an error
                continue
            raw = bytes(self._buf)
            self._buf.clear()
            pkt = self._validate(raw)
            if pkt is not None:
                packets.append(pkt)
        return packets

    def reset(self) -> None:
        """Drop any partial frame (call after reconnecting a transport)."""
        self._buf.clear()

    def _validate(self, raw: bytes) -> bytes | None:
        content = unquote(raw)
        if len(content) < MIN_FRAME_LEN:
            self.stats.too_short += 1
            log.warning("discarding runt frame (%d bytes): %s",
                        len(content), raw.hex(" "))
            return None
        if len(content) > MAX_FRAME_LEN:
            self.stats.too_long += 1
            log.warning("discarding oversize frame (%d bytes): %s...",
                        len(content), raw[:32].hex(" "))
            return None
        if calc_sig(content) != 0:
            self.stats.bad_signature += 1
            log.warning("discarding frame with bad signature: %s",
                        raw.hex(" "))
            return None
        self.stats.frames_ok += 1
        return content[:-2]
