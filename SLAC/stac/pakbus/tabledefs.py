"""PakBus data types, .TDF table-definition parsing and record decoding.

Sources:

* Data type codes/sizes: BMP5 Transparent Commands, Appendix A.
* .TDF file layout: BMP5 Transparent Commands sec. 2.3.4.2
  ("Table Definitions File Format" table).
* Table definition signature: sec. 2.3.4.2 -- for CR1000-type loggers
  (which includes the CR6) the signature runs "with the first byte of
  the Table Name" and ends "after the Field List Terminator of that
  table". (The CR200 section 3.9.1 describes a different start point;
  see NOTES.md.)
* FP2 bit layout: not defined in the two primary documents; taken from
  Campbell's published format page
  (help.campbellsci.com .../formats/fp2.htm): sign bit 15, two-bit
  negative decimal exponent bits 14..13, 13-bit mantissa bits 12..0;
  +INF = 0x1FFF, -INF = 0x9FFF, NAN = 0x9FFE.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import struct
from dataclasses import dataclass, field

from .errors import TableDefsError, UnknownFieldError, ValueTypeError
from .framing import calc_sig

log = logging.getLogger(__name__)

#: Datalogger time epoch: 1990-01-01 00:00:00 (logger-local, naive).
EPOCH = _dt.datetime(1990, 1, 1)

FSL_VERSION = 0x01  # sec. 2.3.4.2: "File format version. (Use 0x01)"


# ------------------------------------------------------------- scalar types

@dataclass(frozen=True)
class DataType:
    """One entry of the Appendix A data type table."""

    name: str
    code: int
    size: int | None            # None: variable length (ASCII/ASCIIZ)
    struct_fmt: str | None = None


# Appendix A, verbatim. 'size' in bytes.
DATA_TYPES: dict[int, DataType] = {t.code: t for t in [
    DataType("Byte", 1, 1, ">B"),
    DataType("UInt2", 2, 2, ">H"),
    DataType("UInt4", 3, 4, ">I"),
    DataType("Int1", 4, 1, ">b"),
    DataType("Int2", 5, 2, ">h"),
    DataType("Int4", 6, 4, ">i"),
    DataType("FP2", 7, 2, None),
    DataType("FP3", 15, 3, None),
    DataType("FP4", 8, 4, None),
    DataType("IEEE4B", 9, 4, ">f"),
    DataType("IEEE8B", 18, 8, ">d"),
    DataType("Bool8", 17, 1, ">B"),
    DataType("Bool", 10, 1, ">B"),
    DataType("Bool2", 27, 2, ">H"),
    DataType("Bool4", 28, 4, ">I"),
    DataType("Sec", 12, 4, ">i"),
    DataType("USec", 13, 6, None),
    DataType("NSec", 14, 8, None),
    DataType("ASCII", 11, None, None),
    DataType("ASCIIZ", 16, None, None),
    DataType("Short", 19, 2, "<h"),
    DataType("Long", 20, 4, "<i"),
    DataType("UShort", 21, 2, "<H"),
    DataType("ULong", 22, 4, "<I"),
    DataType("IEEE4L", 24, 4, "<f"),
    DataType("IEEE8L", 25, 8, "<d"),
    DataType("SecNano", 23, 8, "<2i"),
]}

TYPE_BY_NAME: dict[str, DataType] = {t.name: t for t in DATA_TYPES.values()}

# FP2 special values (Campbell FP2 format page; not in the primary docs).
_FP2_POS_INF = 0x1FFF
_FP2_NEG_INF = 0x9FFF
_FP2_NAN = 0x9FFE


def decode_fp2(raw: int) -> float:
    """Decode a 16-bit FP2 word: (-1)^S * 10^-E * M."""
    if raw == _FP2_POS_INF:
        return math.inf
    if raw == _FP2_NEG_INF:
        return -math.inf
    if raw == _FP2_NAN:
        return math.nan
    sign = -1.0 if raw & 0x8000 else 1.0
    exponent = (raw >> 13) & 0x3
    mantissa = raw & 0x1FFF
    return sign * mantissa / (10 ** exponent)


def nsec_to_datetime(seconds: int, nanoseconds: int) -> _dt.datetime:
    """Logger NSec (seconds, ns since 1990-01-01) -> naive datetime."""
    return EPOCH + _dt.timedelta(seconds=seconds,
                                 microseconds=nanoseconds / 1000)


def datetime_to_nsec(when: _dt.datetime) -> tuple[int, int]:
    """Naive datetime -> logger NSec (seconds, nanoseconds since 1990)."""
    delta = when - EPOCH
    seconds = delta.days * 86400 + delta.seconds
    return seconds, delta.microseconds * 1000


def decode_time(time_type: int, buf: bytes, offset: int) \
        -> tuple[_dt.datetime, int]:
    """Decode a timestamp of the table's TimeType; return (time, new offset).

    Sec/USec/NSec per Appendix A. USec resolution is ambiguous between
    Campbell documents (see BLOCKERS.md); decoded here as 10s of
    microseconds with a warning, since CR1000-type loggers (incl. CR6)
    report NSec in practice.
    """
    dtype = DATA_TYPES.get(time_type)
    if dtype is None:
        raise ValueTypeError(f"unknown time type code {time_type}")
    if dtype.name == "NSec":
        sec, ns = struct.unpack_from(">2i", buf, offset)
        return nsec_to_datetime(sec, ns), offset + 8
    if dtype.name == "Sec":
        (sec,) = struct.unpack_from(">i", buf, offset)
        return nsec_to_datetime(sec, 0), offset + 4
    if dtype.name == "USec":
        log.warning("table uses USec timestamps; resolution is ambiguous "
                    "in the protocol documents (see BLOCKERS.md) -- "
                    "decoding as 10s of microseconds")
        raw = int.from_bytes(buf[offset:offset + 6], "big")
        return (EPOCH + _dt.timedelta(microseconds=raw * 10)), offset + 6
    raise ValueTypeError(f"unsupported time type {dtype.name}")


def decode_scalar(type_code: int, buf: bytes, offset: int) \
        -> tuple[object, int]:
    """Decode one scalar of the given type; return (value, new offset)."""
    dtype = DATA_TYPES.get(type_code)
    if dtype is None:
        raise ValueTypeError(f"unknown data type code {type_code}")
    name = dtype.name

    if name in ("FP3", "FP4"):
        # Bit layouts not defined in the primary documents (BLOCKERS.md).
        raise ValueTypeError(
            f"{name} decoding is not implemented: bit layout is not "
            "specified in the available Campbell documents")
    if name == "FP2":
        (raw,) = struct.unpack_from(">H", buf, offset)
        return decode_fp2(raw), offset + 2
    if name == "NSec":
        sec, ns = struct.unpack_from(">2i", buf, offset)
        return nsec_to_datetime(sec, ns), offset + 8
    if name == "SecNano":
        sec, ns = struct.unpack_from("<2i", buf, offset)
        return nsec_to_datetime(sec, ns), offset + 8
    if name == "Sec":
        (sec,) = struct.unpack_from(">i", buf, offset)
        return nsec_to_datetime(sec, 0), offset + 4
    if name == "USec":
        return decode_time(type_code, buf, offset)
    if name in ("Bool", "Bool2", "Bool4"):
        end = offset + dtype.size
        return bool(int.from_bytes(buf[offset:end], "big")), end
    if name == "ASCIIZ":
        nul = buf.index(b"\x00", offset)
        return (buf[offset:nul].decode("ascii", "replace"), nul + 1)
    if name == "ASCII":
        raise ValueTypeError("ASCII needs an explicit length; "
                             "decode it at the field level")
    if dtype.struct_fmt:
        (value,) = struct.unpack_from(dtype.struct_fmt, buf, offset)
        return value, offset + dtype.size
    raise ValueTypeError(f"no decoder for type {name}")


def encode_scalar(type_code: int, value: object) -> bytes:
    """Encode one scalar for Set Values / command bodies."""
    dtype = DATA_TYPES.get(type_code)
    if dtype is None:
        raise ValueTypeError(f"unknown data type code {type_code}")
    name = dtype.name
    try:
        if name in ("IEEE4B", "IEEE8B", "IEEE4L", "IEEE8L"):
            return struct.pack(dtype.struct_fmt, float(value))
        if name in ("Bool", "Bool2", "Bool4"):
            return (1 if value else 0).to_bytes(dtype.size, "big")
        if name == "Bool8":
            return struct.pack(">B", int(value) & 0xFF)
        if name == "NSec":
            sec, ns = value  # (seconds, nanoseconds) tuple
            return struct.pack(">2i", sec, ns)
        if name == "ASCIIZ":
            return str(value).encode("ascii") + b"\x00"
        if name == "ASCII":
            return str(value).encode("ascii")
        if dtype.struct_fmt:
            return struct.pack(dtype.struct_fmt, int(value))
    except (struct.error, OverflowError, TypeError) as exc:
        raise ValueTypeError(
            f"cannot encode {value!r} as {name}: {exc}") from None
    raise ValueTypeError(f"no encoder for type {name} "
                         f"(FP2/FP3/FP4 encoding not implemented)")


# ------------------------------------------------------------ table defs

@dataclass(frozen=True)
class FieldDef:
    """One field of a table (sec. 2.3.4.2 inner braces)."""

    name: str
    type_code: int
    read_only: bool
    processing: str
    units: str
    description: str
    beg_idx: int
    dimension: int
    sub_dims: tuple[int, ...] = ()

    @property
    def type_name(self) -> str:
        dtype = DATA_TYPES.get(self.type_code)
        return dtype.name if dtype else f"type#{self.type_code}"

    @property
    def byte_size(self) -> int:
        """Bytes this field occupies in one record."""
        dtype = DATA_TYPES.get(self.type_code)
        if dtype is None:
            raise ValueTypeError(
                f"field {self.name}: unknown type code {self.type_code}")
        if dtype.name in ("ASCII", "ASCIIZ"):
            # A string field's dimension is its character count.
            return self.dimension
        return dtype.size * self.dimension

    def element_names(self) -> list[str]:
        """Names of the scalar values this field yields in a record."""
        if self.dimension == 1 or self.type_name in ("ASCII", "ASCIIZ"):
            return [self.name]
        return [f"{self.name}({self.beg_idx + i})"
                for i in range(self.dimension)]


@dataclass(frozen=True)
class TableDef:
    """One table of the .TDF file, plus its computed definition signature."""

    number: int              # 1-based position in the .TDF (implied param)
    name: str
    size: int                # records allocated in the datalogger
    time_type: int
    time_into: tuple[int, int]   # (seconds, nanoseconds)
    interval: tuple[int, int]    # (0, 0) => event-driven table
    fields: tuple[FieldDef, ...]
    signature: int           # CSI sig over TableName..field terminator

    @property
    def is_event_driven(self) -> bool:
        return self.interval == (0, 0)

    @property
    def record_size(self) -> int:
        """Bytes of field data in one record (excluding any timestamp)."""
        return sum(f.byte_size for f in self.fields)

    def field_by_name(self, name: str) -> FieldDef:
        """Case-insensitive lookup; accepts bare names ('Batt' for arrays)."""
        wanted = name.lower()
        for fld in self.fields:
            if fld.name.lower() == wanted:
                return fld
        raise UnknownFieldError(
            f"table {self.name} has no field {name!r}; fields: "
            + ", ".join(f.name for f in self.fields))

    def decode_record_fields(self, buf: bytes, offset: int) \
            -> tuple[dict[str, object], int]:
        """Decode one record's field values; return (values, new offset)."""
        values: dict[str, object] = {}
        for fld in self.fields:
            if fld.type_name in ("ASCII", "ASCIIZ"):
                raw = buf[offset:offset + fld.dimension]
                if len(raw) < fld.dimension:
                    raise TableDefsError(
                        f"record truncated in field {fld.name}")
                text = raw.split(b"\x00", 1)[0].decode("ascii", "replace")
                values[fld.name] = text
                offset += fld.dimension
            elif fld.dimension == 1:
                values[fld.name], offset = decode_scalar(
                    fld.type_code, buf, offset)
            else:
                for i in range(fld.dimension):
                    value, offset = decode_scalar(fld.type_code, buf, offset)
                    values[f"{fld.name}({fld.beg_idx + i})"] = value
        return values, offset


def parse_tabledefs(raw: bytes) -> list[TableDef]:
    """Parse a complete .TDF file into TableDef objects.

    Layout per sec. 2.3.4.2. The per-table signature is computed over the
    raw bytes from the first byte of the table name through the field
    list terminator, inclusive (CR1000-type rule).
    """
    if not raw:
        raise TableDefsError("empty table definition file")
    if raw[0] != FSL_VERSION:
        raise TableDefsError(
            f"unsupported .TDF FslVersion {raw[0]} (expected {FSL_VERSION})")

    tables: list[TableDef] = []
    offset = 1
    try:
        while offset < len(raw):
            table, offset = _parse_one_table(raw, offset, len(tables) + 1)
            tables.append(table)
    except (IndexError, struct.error, ValueError) as exc:
        raise TableDefsError(
            f"malformed .TDF at byte {offset}: {exc}") from exc
    return tables


def _read_asciiz(raw: bytes, offset: int) -> tuple[str, int]:
    nul = raw.index(b"\x00", offset)
    return raw[offset:nul].decode("ascii", "replace"), nul + 1


def _parse_one_table(raw: bytes, offset: int, number: int) \
        -> tuple[TableDef, int]:
    start = offset
    name, offset = _read_asciiz(raw, offset)
    (size,) = struct.unpack_from(">I", raw, offset)
    offset += 4
    time_type = raw[offset]
    offset += 1
    time_into = struct.unpack_from(">2i", raw, offset)
    offset += 8
    interval = struct.unpack_from(">2i", raw, offset)
    offset += 8

    fields: list[FieldDef] = []
    while True:
        type_byte = raw[offset]
        offset += 1
        if type_byte == 0:  # field list terminator (included in signature)
            break
        read_only = bool(type_byte & 0x80)
        type_code = type_byte & 0x7F
        fname, offset = _read_asciiz(raw, offset)
        # Alias name list: ASCIIZ strings until an empty one (terminator).
        while True:
            alias, offset = _read_asciiz(raw, offset)
            if not alias:
                break
        processing, offset = _read_asciiz(raw, offset)
        units, offset = _read_asciiz(raw, offset)
        description, offset = _read_asciiz(raw, offset)
        (beg_idx, dimension) = struct.unpack_from(">2I", raw, offset)
        offset += 8
        sub_dims: list[int] = []
        while True:
            (sub,) = struct.unpack_from(">I", raw, offset)
            offset += 4
            if sub == 0:
                break
            sub_dims.append(sub)
        fields.append(FieldDef(
            name=fname, type_code=type_code, read_only=read_only,
            processing=processing, units=units, description=description,
            beg_idx=beg_idx, dimension=dimension,
            sub_dims=tuple(sub_dims)))

    signature = calc_sig(raw[start:offset])
    table = TableDef(
        number=number, name=name, size=size, time_type=time_type,
        time_into=tuple(time_into), interval=tuple(interval),
        fields=tuple(fields), signature=signature)
    return table, offset


# ---------------------------------------------------------------- records

@dataclass(frozen=True)
class Record:
    """One collected record. Always carries table name, number and time
    (data-integrity requirement: never a value without its identity)."""

    table_name: str
    record_number: int
    timestamp: _dt.datetime
    values: dict[str, object]

    def key(self) -> tuple[str, int]:
        """Deduplication key: (table, record number)."""
        return (self.table_name, self.record_number)
