"""Framing, quoting and signature tests against the document's own
example packets (BMP5 Transparent Commands rev. 9/08)."""

import pytest

from pakbus.framing import (Deframer, MAX_FRAME_LEN, SYNC, calc_sig,
                            calc_sig_nullifier, frame_packet, quote,
                            unframe_packet, unquote)
from pakbus.errors import FramingError, SignatureError
from tests.fixtures.doc_packets import ALL_WIRE_EXAMPLES


@pytest.mark.parametrize("name,wire", sorted(ALL_WIRE_EXAMPLES.items()))
def test_doc_examples_verify_to_zero(name, wire):
    """Signature over each documented frame's content must be zero."""
    content = wire.strip(bytes([SYNC]))
    assert calc_sig(content) == 0


@pytest.mark.parametrize("name,wire", sorted(ALL_WIRE_EXAMPLES.items()))
def test_doc_examples_nullifier_reproduced(name, wire):
    """We must reproduce each documented nullifier byte-for-byte."""
    content = wire.strip(bytes([SYNC]))
    body, doc_nullifier = content[:-2], content[-2:]
    ours = calc_sig_nullifier(calc_sig(body))
    assert ours.to_bytes(2, "big") == doc_nullifier


@pytest.mark.parametrize("name,wire", sorted(ALL_WIRE_EXAMPLES.items()))
def test_frame_packet_reproduces_doc_examples(name, wire):
    """frame_packet(header+message) must equal the documented frame."""
    content = wire.strip(bytes([SYNC]))
    assert frame_packet(content[:-2]) == wire


def test_signature_seed_chaining():
    """Partial signatures chain: sig(a+b) == sig(b, seed=sig(a))."""
    data = bytes(range(200))
    assert calc_sig(data) == calc_sig(data[50:], seed=calc_sig(data[:50]))


def test_quote_reserved_bytes():
    assert quote(b"\xBD") == b"\xBC\xDD"
    assert quote(b"\xBC") == b"\xBC\xDC"
    assert quote(b"\x01\xBD\x02\xBC\x03") == b"\x01\xBC\xDD\x02\xBC\xDC\x03"
    # a fresh quote byte must not be re-quoted
    assert quote(b"\xBC\xBD") == b"\xBC\xDC\xBC\xDD"


def test_unquote_inverts_quote():
    for payload in (b"", b"\xBD\xBC\xBD\xBC", bytes(range(256)),
                    b"\xBC" * 10, b"\xBD" * 10):
        assert unquote(quote(payload)) == payload


def test_frame_contains_no_raw_reserved_bytes():
    packet = bytes(range(256))  # includes 0xBC and 0xBD
    framed = frame_packet(packet)
    inner = framed[1:-1]
    assert SYNC not in inner


def test_unframe_round_trip():
    packet = b"\xA0\x01\x5F\xFE\x10\x01\x0F\xFE\x17\x01\x00\x00" \
             b"\xBD\xBC\x00\x00\x00\x00\x00\x00"
    assert unframe_packet(frame_packet(packet)) == packet


def test_unframe_rejects_bad_signature():
    framed = bytearray(frame_packet(b"\x90\x01\x0F\xFE"))
    framed[2] ^= 0x01  # corrupt one content byte
    with pytest.raises(SignatureError):
        unframe_packet(bytes(framed))


def test_unframe_rejects_empty_and_runt():
    with pytest.raises(FramingError):
        unframe_packet(b"\xBD\xBD")
    with pytest.raises(FramingError):
        unframe_packet(b"\xBD\x01\x02\xBD")  # 2 bytes < minimum 4


# ----------------------------------------------------------------- Deframer

def test_deframer_single_packet():
    deframer = Deframer()
    wire = ALL_WIRE_EXAMPLES["ring"]
    packets = deframer.feed(wire)
    assert packets == [wire[1:-3]]  # content minus nullifier
    assert deframer.stats.frames_ok == 1
    assert deframer.stats.frames_rejected == 0


def test_deframer_byte_at_a_time():
    """Packets split at arbitrary boundaries must still assemble."""
    deframer = Deframer()
    wire = ALL_WIRE_EXAMPLES["clock_resp"]
    packets = []
    for i in range(len(wire)):
        packets += deframer.feed(wire[i:i + 1])
    assert len(packets) == 1
    assert packets[0] == wire[1:-3]


def test_deframer_multiple_packets_one_feed():
    deframer = Deframer()
    stream = (b"\xBD" * 6  # wake-up preamble must not confuse it
              + ALL_WIRE_EXAMPLES["ring"]
              + ALL_WIRE_EXAMPLES["ready"]
              + ALL_WIRE_EXAMPLES["clock_cmd"])
    packets = deframer.feed(stream)
    assert len(packets) == 3
    assert deframer.stats.frames_ok == 3


def test_deframer_shared_delimiter():
    """A single 0xBD both ends one frame and starts the next."""
    deframer = Deframer()
    a = ALL_WIRE_EXAMPLES["ring"]
    b = ALL_WIRE_EXAMPLES["ready"]
    stream = a + b[1:]  # drop b's leading delimiter: still two packets
    assert len(deframer.feed(stream)) == 2


def test_deframer_counts_bad_signature_and_recovers():
    deframer = Deframer()
    corrupted = bytearray(ALL_WIRE_EXAMPLES["clock_cmd"])
    corrupted[5] ^= 0xFF
    packets = deframer.feed(bytes(corrupted)
                            + ALL_WIRE_EXAMPLES["clock_resp"])
    assert len(packets) == 1  # only the good one
    assert deframer.stats.bad_signature == 1
    assert deframer.stats.frames_ok == 1


def test_deframer_counts_runt_frames():
    deframer = Deframer()
    assert deframer.feed(b"\xBD\x01\x02\xBD") == []
    assert deframer.stats.too_short == 1


def test_deframer_counts_oversize_frames():
    deframer = Deframer()
    junk = b"\x01" * (MAX_FRAME_LEN + 1)
    assert deframer.feed(b"\xBD" + junk + b"\xBD") == []
    assert deframer.stats.too_long == 1


def test_deframer_quoted_content():
    """Reserved bytes inside a message survive the wire intact."""
    deframer = Deframer()
    packet = b"\xA0\x01\x5F\xFE\x10\x01\x0F\xFE\x17\x05\xBD\xBC\xBD\xBC"
    packets = deframer.feed(frame_packet(packet))
    assert packets == [packet]


def test_deframer_reset_drops_partial_frame():
    deframer = Deframer()
    wire = ALL_WIRE_EXAMPLES["ring"]
    deframer.feed(wire[:4])   # partial
    deframer.reset()
    # A fresh complete frame afterwards parses cleanly.
    assert deframer.feed(wire) == [wire[1:-3]]
    assert deframer.stats.frames_rejected == 0
