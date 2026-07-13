"""Header layout and link-state packets against documented examples."""

import pytest

from pakbus import linkstate
from pakbus.errors import FramingError
from pakbus.framing import frame_packet, unframe_packet
from pakbus.linkstate import (ExpectMore, HelloInfo, LinkState, Neighbor,
                              Protocol, pack_header, ring_packet,
                              ready_packet, unpack_header)
from tests.fixtures.doc_packets import (WIRE_CLOCK_CMD, WIRE_CLOCK_RESP,
                                        WIRE_READY, WIRE_RING)


def test_ring_packet_matches_doc_example():
    """App 4094 -> logger 1 ring must equal BD 90 01 0F FE 71 D2 BD."""
    assert frame_packet(ring_packet(dst=1, src=4094)) == WIRE_RING


def test_ready_packet_matches_doc_example():
    """Logger 1 -> app 4094 ready equals BD AF FE 00 01 5A 89 BD."""
    assert frame_packet(ready_packet(dst=4094, src=1)) == WIRE_READY


def test_unpack_link_state_only_packet():
    header, body = unpack_header(unframe_packet(WIRE_READY))
    assert header.is_link_state_only
    assert header.link_state == LinkState.READY
    assert header.dst_phy_addr == 4094
    assert header.src_phy_addr == 1
    assert body == b""


def test_unpack_full_header_from_doc_clock_command():
    """Header of the documented clock command: A0 01 4F FE 10 01 0F FE."""
    header, body = unpack_header(unframe_packet(WIRE_CLOCK_CMD))
    assert header.link_state == LinkState.READY
    assert header.dst_phy_addr == 1
    assert header.expect_more == ExpectMore.EXPECT_MORE
    assert header.priority == 0
    assert header.src_phy_addr == 4094
    assert header.hi_proto_code == Protocol.BMP5
    assert header.dst_node_id == 1
    assert header.hop_cnt == 0
    assert header.src_node_id == 4094
    assert body[0] == 0x17  # clock command MsgType


def test_pack_header_reproduces_doc_clock_headers():
    cmd_header = pack_header(
        link_state=LinkState.READY, dst=1, src=4094,
        expect_more=ExpectMore.EXPECT_MORE, priority=0,
        hi_proto=Protocol.BMP5)
    assert cmd_header == bytes.fromhex("A0014FFE10010FFE")

    resp_header = pack_header(
        link_state=LinkState.READY, dst=4094, src=1,
        expect_more=ExpectMore.LAST, priority=0,
        hi_proto=Protocol.BMP5)
    assert resp_header == unframe_packet(WIRE_CLOCK_RESP)[:8]


def test_header_round_trip_all_fields():
    packed = pack_header(
        link_state=LinkState.FINISHED, dst=0x123, src=0xABC,
        expect_more=ExpectMore.REVERSE, priority=2,
        hi_proto=Protocol.PAKCTRL, hop_cnt=3,
        dst_node=0x456, src_node=0x789)
    header, rest = unpack_header(packed)
    assert rest == b""
    assert header.link_state == LinkState.FINISHED
    assert header.dst_phy_addr == 0x123
    assert header.src_phy_addr == 0xABC
    assert header.expect_more == ExpectMore.REVERSE
    assert header.priority == 2
    assert header.hi_proto_code == Protocol.PAKCTRL
    assert header.hop_cnt == 3
    assert header.dst_node_id == 0x456
    assert header.src_node_id == 0x789


def test_unpack_rejects_invalid_link_state():
    # high nibble 0x0 is not a defined link state
    with pytest.raises(FramingError):
        unpack_header(bytes.fromhex("00010FFE"))


def test_unpack_rejects_bad_lengths():
    with pytest.raises(FramingError):
        unpack_header(b"\x90\x01")       # shorter than 4
    with pytest.raises(FramingError):
        unpack_header(b"\x90\x01\x0F\xFE\x10\x01")  # 6 bytes: neither form


# -------------------------------------------------------------------- hello

def test_hello_encode_layout():
    """MsgType, TranNbr, IsRouter, HopMetric, VerifyIntv (sec. 2.2.2)."""
    body = linkstate.encode_hello(linkstate.MSG_HELLO_CMD, 0x2A,
                                  is_router=False, hop_metric=0x02,
                                  verify_intv=1800)
    assert body == bytes.fromhex("092A000207 08".replace(" ", ""))


def test_hello_decode_round_trip():
    body = linkstate.encode_hello(linkstate.MSG_HELLO_RESP, 7,
                                  is_router=True, hop_metric=0x05,
                                  verify_intv=300)
    msg_type, tran, info = linkstate.decode_hello(body)
    assert msg_type == linkstate.MSG_HELLO_RESP
    assert tran == 7
    assert info == HelloInfo(True, 0x05, 300)


def test_hello_response_for_copies_command_fields():
    """Sec 2.2.2: copy tran + hop metric, never claim router, /2.5."""
    cmd_info = HelloInfo(is_router=True, hop_metric=0x03, verify_intv=1000)
    body = linkstate.hello_response_for(0x99, cmd_info)
    msg_type, tran, info = linkstate.decode_hello(body)
    assert msg_type == linkstate.MSG_HELLO_RESP
    assert tran == 0x99
    assert info.is_router is False
    assert info.hop_metric == 0x03
    assert info.verify_intv == 400  # 1000 / 2.5


def test_bye_message():
    assert linkstate.encode_bye() == b"\x0d\x00"


def test_delivery_failure_round_trip():
    orig_header = bytes.fromhex("10010FFE")
    body = linkstate.encode_delivery_failure(0x04, orig_header,
                                             b"\x17\x01" + b"x" * 40)
    assert body[0] == 0x81
    assert body[1] == 0x00               # tran always zero
    assert linkstate.decode_delivery_failure(body) == 0x04
    assert body[3:7] == orig_header
    assert len(body) == 3 + 4 + 16       # at most 16 bytes of original


# ----------------------------------------------------------------- neighbor

def test_neighbor_negotiated_verify_interval():
    """Lesser of neighbor's interval and 6/5 of ours (Networking Guide 3.7)."""
    neighbor = Neighbor(address=1)
    neighbor.record_hello(HelloInfo(False, 2, 1500), our_verify_intv=1000)
    assert neighbor.verify_intv == 1200  # 6/5 * 1000 < 1500
    neighbor.record_hello(HelloInfo(False, 2, 300), our_verify_intv=1000)
    assert neighbor.verify_intv == 300


def test_neighbor_lifecycle():
    neighbor = Neighbor(address=1)
    assert neighbor.needs_verification          # never established
    neighbor.record_hello(HelloInfo(False, 2, 1800), 1800)
    assert neighbor.established
    assert not neighbor.needs_verification
    neighbor.invalidate()
    assert neighbor.needs_verification
    assert not neighbor.established
