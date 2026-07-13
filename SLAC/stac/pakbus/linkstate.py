"""SerPkt link-state sub-protocol and PakCtrl link management messages.

Sources (all field layouts verified against the documents, not memory):

* PakBus header layout: BMP5 Transparent Commands sec. 1.3 and the
  Link-State Sub-protocol table in sec. 2.1.
* Link states, ring/ready exchange: sec. 2.1 and the worked examples in
  secs. 3.2 / 4.2 (``BD 90 01 0F FE 71 D2 BD`` etc., reproduced in the
  unit tests).
* Hello transaction (PakCtrl 0x09/0x89): sec. 2.2.2.
* Hello request (0x0e): sec. 2.2.3. Bye (0x0d): sec. 2.2.4.
* Delivery failure (0x81): sec. 2.2.1.
"""

from __future__ import annotations

import enum
import struct
import time
from dataclasses import dataclass

from .errors import FramingError

BROADCAST_ADDR = 0x0FFF  # 4095, sec. 3.3/4.3 (hello request broadcast)


class LinkState(enum.IntEnum):
    """High nibble of the first header byte (sec. 1.3 / 2.1)."""

    OFFLINE = 0b1000   # 8
    RING = 0b1001      # 9
    READY = 0b1010     # 10
    FINISHED = 0b1011  # 11
    PAUSE = 0b1100     # 12


class ExpectMore(enum.IntEnum):
    """ExpMoreCode, bits 7..6 of header byte 2 (sec. 1.3)."""

    LAST = 0b00
    EXPECT_MORE = 0b01
    NEUTRAL = 0b10
    REVERSE = 0b11


class Protocol(enum.IntEnum):
    """HiProtoCode, high nibble of header byte 4 (sec. 1.3)."""

    PAKCTRL = 0x0
    BMP5 = 0x1


@dataclass(frozen=True)
class Header:
    """Decoded 8-byte PakBus header (or 4-byte link-state-only packet).

    For a 4-byte SerPkt link-state packet, ``hi_proto_code`` and the
    node-id fields are ``None`` (sec. 2.1: "Packets with four bytes of
    data will only contain the state of the communications link").
    """

    link_state: LinkState
    dst_phy_addr: int
    expect_more: ExpectMore
    priority: int
    src_phy_addr: int
    hi_proto_code: Protocol | None = None
    dst_node_id: int | None = None
    hop_cnt: int | None = None
    src_node_id: int | None = None

    @property
    def is_link_state_only(self) -> bool:
        return self.hi_proto_code is None


def pack_header(*, link_state: LinkState, dst: int, src: int,
                expect_more: ExpectMore = ExpectMore.EXPECT_MORE,
                priority: int = 0x1,
                hi_proto: Protocol | None = Protocol.PAKCTRL,
                hop_cnt: int = 0,
                dst_node: int | None = None,
                src_node: int | None = None) -> bytes:
    """Encode a header. ``hi_proto=None`` gives a 4-byte link-state packet.

    When talking directly to a single datalogger, node ids equal the
    physical addresses and hop count is zero (sec. 1.3, "HopCnt: always
    zero when connected directly").
    """
    words = [
        ((link_state & 0xF) << 12) | (dst & 0x0FFF),
        ((expect_more & 0x3) << 14) | ((priority & 0x3) << 12)
        | (src & 0x0FFF),
    ]
    if hi_proto is not None:
        dst_node = dst if dst_node is None else dst_node
        src_node = src if src_node is None else src_node
        words.append(((hi_proto & 0xF) << 12) | (dst_node & 0x0FFF))
        words.append(((hop_cnt & 0xF) << 12) | (src_node & 0x0FFF))
    return struct.pack(f">{len(words)}H", *words)


def unpack_header(packet: bytes) -> tuple[Header, bytes]:
    """Decode the header; return (header, message bytes after header).

    ``packet`` is an unquoted frame with the signature nullifier already
    stripped (i.e. what the Deframer yields).
    """
    if len(packet) < 4:
        raise FramingError(f"packet too short for header: {len(packet)}",
                           packet)
    w1, w2 = struct.unpack(">2H", packet[:4])
    link_state_raw = w1 >> 12
    try:
        link_state = LinkState(link_state_raw)
    except ValueError:
        raise FramingError(
            f"invalid link state 0x{link_state_raw:x}", packet) from None

    common = dict(
        link_state=link_state,
        dst_phy_addr=w1 & 0x0FFF,
        expect_more=ExpectMore((w2 & 0xC000) >> 14),
        priority=(w2 & 0x3000) >> 12,
        src_phy_addr=w2 & 0x0FFF,
    )
    if len(packet) == 4:
        return Header(**common), b""
    if len(packet) < 8:
        raise FramingError(
            f"packet length {len(packet)} is neither a 4-byte link-state "
            "packet nor holds a full 8-byte header", packet)
    w3, w4 = struct.unpack(">2H", packet[4:8])
    hdr = Header(
        **common,
        hi_proto_code=Protocol(w3 >> 12),
        dst_node_id=w3 & 0x0FFF,
        hop_cnt=w4 >> 12,
        src_node_id=w4 & 0x0FFF,
    )
    return hdr, packet[8:]


# ------------------------------------------------------- link-state packets

def ring_packet(dst: int, src: int) -> bytes:
    """4-byte SerPkt 'ring' content (before framing).

    Matches the documented example ``BD 90 01 0F FE 71 D2 BD``
    (sec. 4.2, app addr 4094 -> logger addr 1): expect-more and
    priority are both zero in that example, so they are zero here.
    """
    return pack_header(link_state=LinkState.RING, dst=dst, src=src,
                       expect_more=ExpectMore.LAST, priority=0,
                       hi_proto=None)


def ready_packet(dst: int, src: int) -> bytes:
    """4-byte SerPkt 'ready' content, per example ``BD AF FE 00 01 ...``."""
    return pack_header(link_state=LinkState.READY, dst=dst, src=src,
                       expect_more=ExpectMore.LAST, priority=0,
                       hi_proto=None)


def finished_packet(dst: int, src: int) -> bytes:
    """4-byte SerPkt 'finished' content."""
    return pack_header(link_state=LinkState.FINISHED, dst=dst, src=src,
                       expect_more=ExpectMore.LAST, priority=0,
                       hi_proto=None)


# --------------------------------------------------------- PakCtrl messages

MSG_HELLO_CMD = 0x09
MSG_HELLO_RESP = 0x89
MSG_HELLO_REQUEST = 0x0E
MSG_BYE = 0x0D
MSG_DELIVERY_FAILURE = 0x81

HOP_METRIC_5S = 0x02  # "5 sec or less (default for RS232 or TCP/IP)"


@dataclass(frozen=True)
class HelloInfo:
    """Body of a hello command/response (sec. 2.2.2)."""

    is_router: bool
    hop_metric: int
    verify_intv: int  # seconds


def encode_hello(msg_type: int, tran_nbr: int, *, is_router: bool = False,
                 hop_metric: int = HOP_METRIC_5S,
                 verify_intv: int = 1800) -> bytes:
    """Hello command (0x09) or response (0x89) message body."""
    if msg_type not in (MSG_HELLO_CMD, MSG_HELLO_RESP):
        raise ValueError(f"not a hello message type: 0x{msg_type:02x}")
    return struct.pack(">BBBBH", msg_type, tran_nbr & 0xFF,
                       0x01 if is_router else 0x00, hop_metric & 0xFF,
                       verify_intv & 0xFFFF)


def decode_hello(body: bytes) -> tuple[int, int, HelloInfo]:
    """Decode hello command/response -> (msg_type, tran_nbr, info)."""
    if len(body) < 6:
        raise FramingError(f"hello body too short: {len(body)}", body)
    msg_type, tran_nbr, is_router, hop_metric, verify = struct.unpack(
        ">BBBBH", body[:6])
    return msg_type, tran_nbr, HelloInfo(bool(is_router), hop_metric, verify)


def hello_response_for(cmd_tran_nbr: int, cmd_info: HelloInfo) -> bytes:
    """Build the response the application must send to a datalogger hello.

    Sec. 2.2.2: copy the exact transaction number and the hop metric from
    the command; never identify as a router; VerifyIntv is the command's
    interval divided by 2.5.
    """
    return encode_hello(MSG_HELLO_RESP, cmd_tran_nbr, is_router=False,
                        hop_metric=cmd_info.hop_metric,
                        verify_intv=int(cmd_info.verify_intv / 2.5))


def encode_bye() -> bytes:
    """Bye message body (sec. 2.2.4). Transaction number is always zero."""
    return struct.pack(">BB", MSG_BYE, 0x00)


def encode_delivery_failure(err_code: int, orig_header: bytes,
                            orig_msg: bytes) -> bytes:
    """Delivery-failure body (sec. 2.2.1) for replying to unknown commands.

    ``orig_header`` must be the last 4 header bytes of the offending
    packet (HiProtoCode/DstPBAddr/HopCnt/SrcPBAddr words); ``orig_msg``
    contributes up to 16 bytes of the original message.
    """
    return (struct.pack(">BBB", MSG_DELIVERY_FAILURE, 0x00, err_code & 0xFF)
            + orig_header[:4] + orig_msg[:16])


def decode_delivery_failure(body: bytes) -> int:
    """Return the ErrCode of a delivery-failure message."""
    if len(body) < 3 or body[0] != MSG_DELIVERY_FAILURE:
        raise FramingError("not a delivery failure message", body)
    return body[2]


# --------------------------------------------------------- neighbor status

@dataclass
class Neighbor:
    """Tracks hello-exchange state with one PakBus node (the datalogger).

    PakBus Networking Guide sec. 3.7: after a hello exchange, nodes
    expect to hear from each other within the negotiated verify interval;
    otherwise the link must be re-verified with a new hello exchange.
    """

    address: int
    verify_intv: int = 0          # negotiated interval, seconds
    is_router: bool = False
    hop_metric: int = HOP_METRIC_5S
    last_heard: float | None = None   # time.monotonic()
    established: bool = False

    def record_hello(self, info: HelloInfo, our_verify_intv: int) -> None:
        """Complete a hello exchange with this node.

        Networking Guide sec. 3.7: the negotiated interval is the lesser
        of the neighbor's verify interval and 6/5 of our own.
        """
        candidates = [iv for iv in (info.verify_intv,
                                    int(our_verify_intv * 6 / 5)) if iv > 0]
        self.verify_intv = min(candidates) if candidates else 0
        self.is_router = info.is_router
        self.hop_metric = info.hop_metric
        self.established = True
        self.touch()

    def touch(self) -> None:
        """Note that we heard a valid packet from this node just now."""
        self.last_heard = time.monotonic()

    @property
    def needs_verification(self) -> bool:
        """True when the verify interval elapsed without traffic."""
        if not self.established:
            return True
        if not self.verify_intv or self.last_heard is None:
            return False
        return (time.monotonic() - self.last_heard) > self.verify_intv

    def invalidate(self) -> None:
        """Drop neighbor status (e.g. after a transport failure)."""
        self.established = False
        self.last_heard = None
