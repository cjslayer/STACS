"""Data-type decoding and .TDF parsing tests.

The .TDF bytes here are synthesized to the exact layout of BMP5
Transparent Commands sec. 2.3.4.2 (no complete .TDF example exists in
the document -- its one response example is a truncated fragment).
"""

import datetime as dt
import math
import struct

import pytest

from pakbus.errors import (TableDefsError, UnknownFieldError,
                           ValueTypeError)
from pakbus.framing import calc_sig
from pakbus.tabledefs import (EPOCH, TYPE_BY_NAME, decode_fp2, decode_scalar,
                              datetime_to_nsec, encode_scalar,
                              nsec_to_datetime, parse_tabledefs)


# ---------------------------------------------------------------------- FP2

@pytest.mark.parametrize("raw,expected", [
    (0x0000, 0.0),
    (0x1F3F, 7999.0),          # max magnitude, exponent 0
    (0x9F3F, -7999.0),         # sign bit set
    (0x2FA0, 400.0),           # 4000 / 10^1
    (0x4960, 24.0),            # 2400 / 10^2
    (0xE5DC, -1.5),            # 1500 / 10^3, negative
    (0x0001, 1.0),
])
def test_fp2_values(raw, expected):
    assert decode_fp2(raw) == pytest.approx(expected)


def test_fp2_special_values():
    """+INF/-INF/NAN encodings from Campbell's FP2 format page."""
    assert decode_fp2(0x1FFF) == math.inf
    assert decode_fp2(0x9FFF) == -math.inf
    assert math.isnan(decode_fp2(0x9FFE))


# ------------------------------------------------------------ time and misc

def test_nsec_round_trip():
    when = dt.datetime(2026, 7, 9, 14, 30, 15, 250_000)
    sec, ns = datetime_to_nsec(when)
    assert nsec_to_datetime(sec, ns) == when


def test_nsec_epoch():
    assert nsec_to_datetime(0, 0) == EPOCH
    assert datetime_to_nsec(EPOCH) == (0, 0)


def test_decode_scalar_basics():
    assert decode_scalar(1, b"\xFF", 0) == (255, 1)          # Byte
    assert decode_scalar(2, b"\x01\x02", 0) == (0x0102, 2)   # UInt2
    assert decode_scalar(5, b"\xFF\xFE", 0) == (-2, 2)       # Int2
    value, off = decode_scalar(9, struct.pack(">f", 2.5), 0)  # IEEE4B
    assert (value, off) == (2.5, 4)
    value, off = decode_scalar(24, struct.pack("<f", 2.5), 0)  # IEEE4L
    assert (value, off) == (2.5, 4)
    assert decode_scalar(10, b"\x01", 0) == (True, 1)        # Bool
    assert decode_scalar(16, b"hi\x00rest", 0) == ("hi", 3)  # ASCIIZ


def test_decode_scalar_nsec_and_sec():
    buf = (100).to_bytes(4, "big") + (500_000_000).to_bytes(4, "big")
    value, off = decode_scalar(14, buf, 0)
    assert value == EPOCH + dt.timedelta(seconds=100.5)
    assert off == 8
    value, off = decode_scalar(12, (60).to_bytes(4, "big"), 0)
    assert value == EPOCH + dt.timedelta(seconds=60)


def test_fp3_fp4_raise_not_implemented():
    with pytest.raises(ValueTypeError):
        decode_scalar(15, b"\x00\x00\x00", 0)   # FP3
    with pytest.raises(ValueTypeError):
        decode_scalar(8, b"\x00\x00\x00\x00", 0)  # FP4


def test_encode_scalar():
    assert encode_scalar(9, 2.5) == struct.pack(">f", 2.5)
    assert encode_scalar(3, 70000) == (70000).to_bytes(4, "big")
    assert encode_scalar(10, True) == b"\x01"
    assert encode_scalar(16, "abc") == b"abc\x00"
    with pytest.raises(ValueTypeError):
        encode_scalar(3, -1)          # negative in UInt4
    with pytest.raises(ValueTypeError):
        encode_scalar(7, 1.0)         # FP2 encoding unsupported


# ------------------------------------------------------------------ .TDF

def _field_bytes(type_code, name, *, read_only=False, proc="Smp", units="",
                 desc="", beg_idx=1, dimension=1, sub_dims=()):
    out = bytearray()
    out.append((0x80 if read_only else 0) | type_code)
    out += name.encode() + b"\x00"
    out += b"\x00"                       # empty alias list (terminator)
    out += proc.encode() + b"\x00"
    out += units.encode() + b"\x00"
    out += desc.encode() + b"\x00"
    out += struct.pack(">2I", beg_idx, dimension)
    for sub in sub_dims:
        out += struct.pack(">I", sub)
    out += struct.pack(">I", 0)          # sub-dimension terminator
    return bytes(out)


def _table_bytes(name, size, time_type, interval, fields):
    out = bytearray()
    out += name.encode() + b"\x00"
    out += struct.pack(">I", size)
    out.append(time_type)
    out += struct.pack(">2i", 0, 0)          # TblTimeInto
    out += struct.pack(">2i", *interval)     # TblInterval
    for fld in fields:
        out += fld
    out += b"\x00"                           # field list terminator
    return bytes(out)


NSEC = TYPE_BY_NAME["NSec"].code
IEEE4B = TYPE_BY_NAME["IEEE4B"].code
FP2 = TYPE_BY_NAME["FP2"].code
ASCII = TYPE_BY_NAME["ASCII"].code
UINT4 = TYPE_BY_NAME["UInt4"].code


def _sample_tdf() -> bytes:
    fast = _table_bytes("Fast", 100_000, NSEC, (0, 250_000_000), [
        _field_bytes(IEEE4B, "Load", units="kN"),
        _field_bytes(FP2, "Strain", units="uE", beg_idx=1, dimension=3),
        _field_bytes(ASCII, "Label", dimension=8),
    ])
    public = _table_bytes("Public", 1, NSEC, (0, 0), [
        _field_bytes(IEEE4B, "ZeroCmd"),
        _field_bytes(UINT4, "RecNum", read_only=True),
    ])
    return bytes([0x01]) + fast + public


def test_parse_tabledefs_structure():
    tables = parse_tabledefs(_sample_tdf())
    assert [t.name for t in tables] == ["Fast", "Public"]
    fast, public = tables

    assert fast.number == 1
    assert fast.size == 100_000
    assert fast.time_type == NSEC
    assert fast.interval == (0, 250_000_000)
    assert not fast.is_event_driven
    assert [f.name for f in fast.fields] == ["Load", "Strain", "Label"]
    strain = fast.field_by_name("strain")     # case-insensitive
    assert strain.dimension == 3
    assert strain.type_name == "FP2"
    # record: 4 (IEEE4) + 2*3 (FP2 x3) + 8 (ASCII[8]) = 18 bytes
    assert fast.record_size == 18

    assert public.number == 2
    assert public.is_event_driven
    assert public.field_by_name("RecNum").read_only is True


def test_parse_tabledefs_signature_covers_name_to_terminator():
    """Signature spans TableName..field-list terminator (sec. 2.3.4.2)."""
    raw = _sample_tdf()
    tables = parse_tabledefs(raw)
    fast_bytes = _table_bytes("Fast", 100_000, NSEC, (0, 250_000_000), [
        _field_bytes(IEEE4B, "Load", units="kN"),
        _field_bytes(FP2, "Strain", units="uE", beg_idx=1, dimension=3),
        _field_bytes(ASCII, "Label", dimension=8),
    ])
    assert tables[0].signature == calc_sig(fast_bytes)
    # sanity: table starts right after the FslVersion byte
    assert raw[1:1 + len(fast_bytes)] == fast_bytes


def test_parse_tabledefs_rejects_bad_version():
    with pytest.raises(TableDefsError):
        parse_tabledefs(b"\x02junk")
    with pytest.raises(TableDefsError):
        parse_tabledefs(b"")


def test_parse_tabledefs_rejects_truncated():
    raw = _sample_tdf()
    with pytest.raises(TableDefsError):
        parse_tabledefs(raw[:len(raw) // 2])


def test_unknown_field_raises():
    tables = parse_tabledefs(_sample_tdf())
    with pytest.raises(UnknownFieldError):
        tables[0].field_by_name("DoesNotExist")


def test_decode_record_fields():
    tables = parse_tabledefs(_sample_tdf())
    fast = tables[0]
    buf = (struct.pack(">f", 12.5)
           + bytes.fromhex("2FA0") + bytes.fromhex("2FA1")
           + bytes.fromhex("9FFE")                       # NAN strain
           + b"chan-A\x00\x00")
    values, offset = fast.decode_record_fields(buf, 0)
    assert offset == len(buf)
    assert values["Load"] == 12.5
    assert values["Strain(1)"] == 400.0
    assert values["Strain(2)"] == 400.1
    assert math.isnan(values["Strain(3)"])
    assert values["Label"] == "chan-A"


def test_decode_record_fields_truncated_raises():
    tables = parse_tabledefs(_sample_tdf())
    with pytest.raises(Exception):
        tables[0].decode_record_fields(b"\x00\x00", 0)
