"""BMP5 message bodies against documented examples plus synthetic
round-trips for the messages the document shows no examples for."""

import datetime as dt
import struct

import pytest

from pakbus import bmp5
from pakbus.errors import (InvalidTableDefsError, PermissionDeniedError,
                           ProtocolError, ResponseError)
from pakbus.framing import unframe_packet
from pakbus.tabledefs import EPOCH, TYPE_BY_NAME, FieldDef, TableDef
from tests.fixtures.doc_packets import (WIRE_CLOCK_CMD, WIRE_CLOCK_RESP,
                                        WIRE_COLLECT_CMD,
                                        WIRE_TDF_UPLOAD_CMD)


def _body(wire: bytes) -> bytes:
    """Message body (after the 8-byte header) of a documented frame."""
    return unframe_packet(wire)[8:]


# -------------------------------------------------------------------- clock

def test_clock_cmd_matches_doc_example():
    """Doc example: clock check (zero adjustment), tran 0x17, security 0."""
    assert bmp5.encode_clock_cmd(0x17, 0, (0, 0)) == _body(WIRE_CLOCK_CMD)


def test_clock_resp_decodes_doc_example():
    decoded = bmp5.decode_clock_resp(_body(WIRE_CLOCK_RESP))
    assert decoded.resp_code == 0
    # Seconds field of the example is 0x1BFA2A61 since 1990-01-01.
    expected_sec = 0x1BFA2A61
    delta = decoded.old_time - EPOCH
    assert abs(delta.total_seconds() - expected_sec) < 1.0


def test_clock_resp_permission_denied_raises():
    body = bytes([bmp5.MSG_CLOCK_RESP, 0x17, 0x01])
    with pytest.raises(PermissionDeniedError):
        bmp5.decode_clock_resp(body)


def test_clock_cmd_negative_adjustment():
    body = bmp5.encode_clock_cmd(1, 0, (-3, -500_000_000))
    assert body[4:8] == (-3).to_bytes(4, "big", signed=True)
    assert body[8:12] == (-500_000_000).to_bytes(4, "big", signed=True)


# ------------------------------------------------------------ file transfer

def test_file_upload_cmd_matches_doc_example():
    """Doc example: upload 'CPU:Def.tdf', offset 0, swath 128, tran 0x1D."""
    ours = bmp5.encode_file_upload_cmd(0x1D, 0, "CPU:Def.tdf",
                                       offset=0, swath=128,
                                       close_flag=False)
    assert ours == _body(WIRE_TDF_UPLOAD_CMD)


def test_file_upload_resp_round_trip():
    payload = b"tabledefbytes"
    body = (bytes([bmp5.MSG_FILE_UPLOAD_RESP, 9, 0x00])
            + (512).to_bytes(4, "big") + payload)
    decoded = bmp5.decode_file_upload_resp(body)
    assert decoded.file_offset == 512
    assert decoded.data == payload


def test_file_upload_resp_error_codes():
    for code, exc in ((0x01, PermissionDeniedError), (0x0D, ResponseError),
                      (0x0E, ResponseError)):
        body = (bytes([bmp5.MSG_FILE_UPLOAD_RESP, 9, code])
                + b"\x00\x00\x00\x00")
        with pytest.raises(exc):
            bmp5.decode_file_upload_resp(body)


def test_file_download_cmd_layout():
    body = bmp5.encode_file_download_cmd(
        5, 0x1234, "CPU:prog.CR6", offset=0x600, data=b"PAYLOAD",
        close_flag=True)
    assert body[0] == bmp5.MSG_FILE_DOWNLOAD_CMD
    assert body[1] == 5
    assert body[2:4] == b"\x12\x34"                      # security code
    assert body[4:17] == b"CPU:prog.CR6\x00"             # ASCIIZ name
    assert body[17] == 0x00                              # reserved attribute
    assert body[18] == 0x01                              # close flag
    assert body[19:23] == (0x600).to_bytes(4, "big")     # offset
    assert body[23:] == b"PAYLOAD"


def test_file_control_cmd_layout():
    body = bmp5.encode_file_control_cmd(3, 0, "CPU:prog.CR6",
                                        bmp5.FC_COMPILE_RUN_POWERUP)
    assert body[0] == bmp5.MSG_FILE_CONTROL_CMD
    assert body[4:17] == b"CPU:prog.CR6\x00"
    assert body[17] == 0x01


def test_file_control_resp_holdoff():
    body = bytes([bmp5.MSG_FILE_CONTROL_RESP, 3, 0x00, 0x00, 0x14])
    assert bmp5.decode_file_control_resp(body).hold_off == 20


# --------------------------------------------------------------- prog stats

def test_prog_stats_round_trip():
    body = (bytes([bmp5.MSG_GET_PROG_STAT_RESP, 8, 0x00])
            + b"CR6.Std.12.01\x00"
            + (0xBEEF).to_bytes(2, "big")
            + b"12345\x00"
            + b"CPU:test.CR6\x00"
            + bytes([0x01])
            + b"CPU:test.CR6\x00"
            + (0x1234).to_bytes(2, "big")
            + (800_000_000).to_bytes(4, "big") + (0).to_bytes(4, "big")
            + b"compiled OK\x00")
    stats = bmp5.decode_get_prog_stat_resp(body)
    assert stats.os_version == "CR6.Std.12.01"
    assert stats.os_signature == 0xBEEF
    assert stats.serial_nbr == "12345"
    assert stats.comp_state == 0x01
    assert stats.prog_name == "CPU:test.CR6"
    assert stats.comp_time == EPOCH + dt.timedelta(seconds=800_000_000)
    assert stats.comp_result == "compiled OK"


# ------------------------------------------------------------- collect data

def test_collect_cmd_matches_doc_example():
    """Doc example: mode 5, table 3, sig 0x4315, most recent 60 records."""
    ours = bmp5.encode_collect_cmd(0x09, 0, bmp5.COLLECT_MOST_RECENT,
                                   table_nbr=3, table_sig=0x4315, p1=60)
    assert ours == _body(WIRE_COLLECT_CMD)


def test_collect_cmd_mode_parameters():
    base = bmp5.encode_collect_cmd(1, 0, bmp5.COLLECT_ALL, 2, 0x1111)
    assert len(base) == 9 + 2  # no P fields, terminator only
    from_rec = bmp5.encode_collect_cmd(1, 0, bmp5.COLLECT_FROM_RECORD,
                                       2, 0x1111, p1=500)
    assert from_rec[9:13] == (500).to_bytes(4, "big")
    rng = bmp5.encode_collect_cmd(1, 0, bmp5.COLLECT_RECORD_RANGE,
                                  2, 0x1111, p1=10, p2=20)
    assert rng[9:17] == (10).to_bytes(4, "big") + (20).to_bytes(4, "big")
    tspan = bmp5.encode_collect_cmd(1, 0, bmp5.COLLECT_TIME_RANGE, 2,
                                    0x1111, p1=(100, 0), p2=(200, 0))
    assert len(tspan) == 9 + 16 + 2


def test_collect_cmd_field_list():
    body = bmp5.encode_collect_cmd(1, 0, bmp5.COLLECT_MOST_RECENT, 2,
                                   0x1111, p1=1, field_nbrs=(2, 5))
    assert body[-6:] == b"\x00\x02\x00\x05\x00\x00"


INTERVAL_TABLE = TableDef(
    number=3, name="Fast", size=1000, time_type=14,  # NSec
    time_into=(0, 0), interval=(0, 250_000_000),     # 250 ms
    fields=(
        FieldDef("Load", TYPE_BY_NAME["IEEE4B"].code, False,
                 "Smp", "kN", "", 1, 1),
        FieldDef("Disp", TYPE_BY_NAME["FP2"].code, False,
                 "Smp", "mm", "", 1, 1),
    ),
    signature=0x4315)

EVENT_TABLE = TableDef(
    number=4, name="Events", size=100, time_type=14,
    time_into=(0, 0), interval=(0, 0),               # event-driven
    fields=(
        FieldDef("Flag", TYPE_BY_NAME["Bool"].code, False,
                 "Smp", "", "", 1, 1),
    ),
    signature=0x2222)


def _table_for_nbr(nbr):
    return {3: INTERVAL_TABLE, 4: EVENT_TABLE}[nbr]


def _nsec(seconds, ns=0):
    return seconds.to_bytes(4, "big") + ns.to_bytes(4, "big")


def test_collect_resp_interval_records():
    """Two interval records: one TimeOfRec, later stamps add the interval."""
    body = (bytes([bmp5.MSG_COLLECT_DATA_RESP, 9, 0x00])
            + (3).to_bytes(2, "big")          # table 3
            + (120).to_bytes(4, "big")        # BegRecNbr
            + (2).to_bytes(2, "big")          # NbrOfRecs, IsOffset=0
            + _nsec(1000, 500_000_000)        # TimeOfRec (first record only)
            + struct.pack(">f", 1.25) + bytes.fromhex("2FA0")   # rec 120
            + struct.pack(">f", 2.5) + bytes.fromhex("2FA1")    # rec 121
            + b"\x00")                        # MoreRecsExist = false
    resp = bmp5.decode_collect_resp(body, _table_for_nbr)
    assert resp.more_records_exist is False
    assert resp.fragment is None
    assert [r.record_number for r in resp.records] == [120, 121]
    r0, r1 = resp.records
    assert r0.table_name == "Fast" and r1.table_name == "Fast"
    assert r0.timestamp == EPOCH + dt.timedelta(seconds=1000.5)
    assert r1.timestamp == EPOCH + dt.timedelta(seconds=1000.75)
    assert r0.values["Load"] == 1.25
    assert r0.values["Disp"] == 400.0    # FP2 0x2FA0: 4000 / 10^1
    assert r1.values["Disp"] == 400.1    # FP2 0x2FA1: 4001 / 10^1


def test_collect_resp_event_records_have_per_record_time():
    body = (bytes([bmp5.MSG_COLLECT_DATA_RESP, 9, 0x00])
            + (4).to_bytes(2, "big")
            + (7).to_bytes(4, "big")
            + (2).to_bytes(2, "big")
            + _nsec(50) + b"\x01"             # record 7 @ t=50, Flag=True
            + _nsec(60) + b"\x00"             # record 8 @ t=60, Flag=False
            + b"\x01")                        # more records exist
    resp = bmp5.decode_collect_resp(body, _table_for_nbr)
    assert resp.more_records_exist is True
    assert [r.record_number for r in resp.records] == [7, 8]
    assert resp.records[0].timestamp == EPOCH + dt.timedelta(seconds=50)
    assert resp.records[1].timestamp == EPOCH + dt.timedelta(seconds=60)
    assert resp.records[0].values["Flag"] is True
    assert resp.records[1].values["Flag"] is False


def test_collect_resp_fragment():
    """IsOffset=1: 31-bit byte offset, data runs to the MoreRecs byte."""
    frag_bytes = b"\xAA" * 10
    body = (bytes([bmp5.MSG_COLLECT_DATA_RESP, 9, 0x00])
            + (3).to_bytes(2, "big")
            + (99).to_bytes(4, "big")
            + (0x80000000 | 0).to_bytes(4, "big")   # IsOffset, offset 0
            + frag_bytes
            + b"\x01")
    resp = bmp5.decode_collect_resp(body, _table_for_nbr)
    assert resp.records == []
    assert resp.fragment is not None
    assert resp.fragment.record_nbr == 99
    assert resp.fragment.byte_offset == 0
    assert resp.fragment.data == frag_bytes
    assert resp.more_records_exist is True


def test_collect_resp_stale_table_defs_raises():
    body = bytes([bmp5.MSG_COLLECT_DATA_RESP, 9, 0x07])
    with pytest.raises(InvalidTableDefsError):
        bmp5.decode_collect_resp(body, _table_for_nbr)


def test_collect_resp_empty():
    body = bytes([bmp5.MSG_COLLECT_DATA_RESP, 9, 0x00, 0x00])
    resp = bmp5.decode_collect_resp(body, _table_for_nbr)
    assert resp.records == [] and not resp.more_records_exist


def test_reassembled_record_decode():

    data = _nsec(1000, 0) + struct.pack(">f", 3.5) + bytes.fromhex("0FA0")
    record = bmp5.decode_reassembled_record(INTERVAL_TABLE, 42, data)
    assert record.record_number == 42
    assert record.timestamp == EPOCH + dt.timedelta(seconds=1000)
    assert record.values["Load"] == 3.5


def test_reassembled_record_rejects_trailing_bytes():

    data = (_nsec(1000, 0) + struct.pack(">f", 3.5)
            + bytes.fromhex("0FA0") + b"\xFF")
    with pytest.raises(ProtocolError):
        bmp5.decode_reassembled_record(INTERVAL_TABLE, 42, data)


# ------------------------------------------------------------ get/set values

def test_get_values_cmd_layout():
    body = bmp5.encode_get_values_cmd(0x1A, 0, "Public", 9, "SaveSite", 16)
    assert body[0] == bmp5.MSG_GET_VALUES_CMD
    assert body[4:11] == b"Public\x00"
    assert body[11] == 9                       # IEEE4B type code
    assert body[12:21] == b"SaveSite\x00"
    assert body[21:23] == (16).to_bytes(2, "big")


def test_get_values_resp_decode():

    body = (bytes([bmp5.MSG_GET_VALUES_RESP, 5, 0x00])
            + struct.pack(">3f", 1.0, 2.5, -3.0))
    values = bmp5.decode_get_values_resp(body, TYPE_BY_NAME["IEEE4B"].code,
                                         swath=3)
    assert values == [1.0, 2.5, -3.0]


def test_get_values_resp_extra_bytes_raise():

    body = (bytes([bmp5.MSG_GET_VALUES_RESP, 5, 0x00])
            + struct.pack(">2f", 1.0, 2.0))
    with pytest.raises(ProtocolError):
        bmp5.decode_get_values_resp(body, TYPE_BY_NAME["IEEE4B"].code,
                                    swath=1)


def test_set_values_cmd_layout():

    body = bmp5.encode_set_values_cmd(7, 0, "Public", 9, "ZeroCmd", [1.0])
    assert body[0] == bmp5.MSG_SET_VALUES_CMD
    assert body[4:11] == b"Public\x00"
    assert body[11] == 9
    assert body[12:20] == b"ZeroCmd\x00"
    assert body[20:22] == (1).to_bytes(2, "big")
    assert body[22:] == struct.pack(">f", 1.0)


def test_set_values_resp_errors():
    for code, exc in ((0x10, ResponseError), (0x11, ResponseError),
                      (0x01, PermissionDeniedError)):
        with pytest.raises(exc):
            bmp5.decode_set_values_resp(
                bytes([bmp5.MSG_SET_VALUES_RESP, 7, code]))
    assert bmp5.decode_set_values_resp(
        bytes([bmp5.MSG_SET_VALUES_RESP, 7, 0x00])) == 0


# -------------------------------------------------------------- please wait

def test_please_wait_decode():
    body = bytes([bmp5.MSG_PLEASE_WAIT, 0x21, 0x09]) + (15).to_bytes(2, "big")
    wait = bmp5.decode_please_wait(body)
    assert wait.tran_nbr == 0x21
    assert wait.cmd_msg_type == 0x09
    assert wait.wait_sec == 15
