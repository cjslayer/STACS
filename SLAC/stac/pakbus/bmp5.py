"""BMP5 application message encode/decode (pure functions, no I/O).

All layouts from Campbell Scientific "BMP5 Transparent Commands",
rev. 9/08, section 2.3:

* Please Wait          0xa1            (sec. 2.3.1)
* Clock                0x17 / 0x97     (sec. 2.3.2)
* File Download        0x1c / 0x9c     (sec. 2.3.3.1)  [PC -> logger]
* File Upload          0x1d / 0x9d     (sec. 2.3.3.2)  [logger -> PC]
* File Control         0x1e / 0x9e     (sec. 2.3.3.4)
* Get Programming Stats 0x18 / 0x98    (sec. 2.3.3.5)
* Collect Data         0x09 / 0x89     (sec. 2.3.4.3)
* Get Values           0x1a / 0x9a     (sec. 2.3.5)
* Set Values           0x1b / 0x9b     (sec. 2.3.5)

Every message body starts with MsgType (1 byte) and TranNbr (1 byte);
decode functions take the body starting at MsgType.
"""

from __future__ import annotations

import datetime as _dt
import functools
import struct
from dataclasses import dataclass
from typing import Callable

from .errors import (ProtocolError, ResponseError, PermissionDeniedError,
                     InvalidTableDefsError, ValueTypeError)
from .tabledefs import (TableDef, Record, decode_scalar, decode_time,
                        encode_scalar, DATA_TYPES)

# message type codes (commands and their 0x80-OR'd responses)
MSG_COLLECT_DATA_CMD = 0x09
MSG_COLLECT_DATA_RESP = 0x89
MSG_CLOCK_CMD = 0x17
MSG_CLOCK_RESP = 0x97
MSG_GET_PROG_STAT_CMD = 0x18
MSG_GET_PROG_STAT_RESP = 0x98
MSG_GET_VALUES_CMD = 0x1A
MSG_GET_VALUES_RESP = 0x9A
MSG_SET_VALUES_CMD = 0x1B
MSG_SET_VALUES_RESP = 0x9B
MSG_FILE_DOWNLOAD_CMD = 0x1C
MSG_FILE_DOWNLOAD_RESP = 0x9C
MSG_FILE_UPLOAD_CMD = 0x1D
MSG_FILE_UPLOAD_RESP = 0x9D
MSG_FILE_CONTROL_CMD = 0x1E
MSG_FILE_CONTROL_RESP = 0x9E
MSG_PLEASE_WAIT = 0xA1

# Collect Data modes (sec. 2.3.4.3)
COLLECT_ALL = 0x03            # oldest -> newest
COLLECT_FROM_RECORD = 0x04    # P1 = start record -> newest
COLLECT_MOST_RECENT = 0x05    # P1 = how many records
COLLECT_RECORD_RANGE = 0x06   # [P1, P2)
COLLECT_TIME_RANGE = 0x07     # P1/P2 NSec, [P1, P2)
COLLECT_PARTIAL = 0x08        # P1 = record nbr, P2 = byte offset

# File Control commands (sec. 2.3.3.4). Names only for the ones STAC uses;
# the full code list lives in the spec.
FC_COMPILE_RUN_POWERUP = 0x01
FC_STOP_DELETE_RUN_POWERUP = 0x0D
FC_STOP_DELETE_RUN = 0x0E


def _decoder(func):
    """Convert truncation-style failures inside a decode function into
    typed ProtocolErrors -- a malformed body must never surface as a
    bare struct.error (data-integrity requirement: explicit failures)."""
    @functools.wraps(func)
    def wrapper(body, *args, **kwargs):
        try:
            return func(body, *args, **kwargs)
        except (struct.error, IndexError, ValueError) as exc:
            raise ProtocolError(
                f"{func.__name__}: truncated or malformed body "
                f"({exc}); raw: {bytes(body).hex(' ')}") from exc
    return wrapper


def _asciiz(text: str) -> bytes:
    encoded = text.encode("ascii")
    if b"\x00" in encoded:
        raise ValueTypeError(f"embedded NUL in string {text!r}")
    return encoded + b"\x00"


def _read_asciiz(body: bytes, offset: int) -> tuple[str, int]:
    nul = body.index(b"\x00", offset)
    return body[offset:nul].decode("ascii", "replace"), nul + 1


def _expect(body: bytes, msg_type: int, what: str) -> None:
    if not body or body[0] != msg_type:
        got = f"0x{body[0]:02x}" if body else "empty body"
        raise ProtocolError(f"{what}: expected MsgType 0x{msg_type:02x}, "
                            f"got {got}")


def check_resp_code(operation: str, resp_code: int,
                    descriptions: dict[int, str]) -> None:
    """Raise a typed error for a non-zero response code."""
    if resp_code == 0:
        return
    if resp_code == 0x01:
        raise PermissionDeniedError(operation)
    if resp_code == 0x07 and operation.startswith("collect"):
        raise InvalidTableDefsError(operation)
    raise ResponseError(operation, resp_code,
                        descriptions.get(resp_code, ""))


# ------------------------------------------------------------- please wait

@dataclass(frozen=True)
class PleaseWait:
    tran_nbr: int
    cmd_msg_type: int
    wait_sec: int


@_decoder
def decode_please_wait(body: bytes) -> PleaseWait:
    _expect(body, MSG_PLEASE_WAIT, "please-wait")
    tran, cmd_type, wait_sec = struct.unpack_from(">xBBH", body)
    return PleaseWait(tran, cmd_type, wait_sec)


# ------------------------------------------------------------------- clock

def encode_clock_cmd(tran_nbr: int, security_code: int,
                     adjustment: tuple[int, int] = (0, 0)) -> bytes:
    """Clock command (sec. 2.3.2). adjustment = (seconds, nanoseconds).

    (0, 0) is a pure clock read. NOTE the spec's warning: never blind-
    retry a clock *set*; re-read the clock instead (client enforces).
    """
    return struct.pack(">BBH2i", MSG_CLOCK_CMD, tran_nbr & 0xFF,
                       security_code & 0xFFFF, adjustment[0], adjustment[1])


@dataclass(frozen=True)
class ClockResponse:
    resp_code: int
    old_time: _dt.datetime | None   # logger time before adjustment


@_decoder
def decode_clock_resp(body: bytes) -> ClockResponse:
    _expect(body, MSG_CLOCK_RESP, "clock response")
    resp_code = body[2]
    check_resp_code("clock", resp_code, {0x01: "permission denied"})
    sec, ns = struct.unpack_from(">2i", body, 3)
    from .tabledefs import nsec_to_datetime
    return ClockResponse(resp_code, nsec_to_datetime(sec, ns))


# ----------------------------------------------------------- file transfer

def encode_file_upload_cmd(tran_nbr: int, security_code: int, filename: str,
                           offset: int, swath: int,
                           close_flag: bool = False) -> bytes:
    """File Upload command (logger -> PC), sec. 2.3.3.2."""
    return (struct.pack(">BBH", MSG_FILE_UPLOAD_CMD, tran_nbr & 0xFF,
                        security_code & 0xFFFF)
            + _asciiz(filename)
            + struct.pack(">BIH", 0x01 if close_flag else 0x00,
                          offset, swath))


@dataclass(frozen=True)
class FileUploadResponse:
    resp_code: int
    file_offset: int
    data: bytes


_FILE_RESP_CODES = {
    0x01: "permission denied",
    0x02: "insufficient resources or memory full",
    0x09: "invalid fragment number",
    0x0D: "invalid file name",
    0x0E: "file is not currently accessible",
}


@_decoder
def decode_file_upload_resp(body: bytes) -> FileUploadResponse:
    _expect(body, MSG_FILE_UPLOAD_RESP, "file upload response")
    resp_code = body[2]
    check_resp_code("file upload", resp_code, _FILE_RESP_CODES)
    (file_offset,) = struct.unpack_from(">I", body, 3)
    return FileUploadResponse(resp_code, file_offset, body[7:])


def encode_file_download_cmd(tran_nbr: int, security_code: int,
                             filename: str, offset: int, data: bytes,
                             close_flag: bool) -> bytes:
    """File Download command (PC -> logger), sec. 2.3.3.1.

    The Attribute byte is reserved and "must currently designate 0x00".
    The same transaction number must be reused for every fragment of a
    multi-packet download (client enforces).
    """
    return (struct.pack(">BBH", MSG_FILE_DOWNLOAD_CMD, tran_nbr & 0xFF,
                        security_code & 0xFFFF)
            + _asciiz(filename)
            + struct.pack(">BBI", 0x00, 0x01 if close_flag else 0x00,
                          offset)
            + data)


@dataclass(frozen=True)
class FileDownloadResponse:
    resp_code: int
    file_offset: int


@_decoder
def decode_file_download_resp(body: bytes) -> FileDownloadResponse:
    _expect(body, MSG_FILE_DOWNLOAD_RESP, "file download response")
    resp_code = body[2]
    check_resp_code("file download", resp_code, _FILE_RESP_CODES)
    (file_offset,) = struct.unpack_from(">I", body, 3)
    return FileDownloadResponse(resp_code, file_offset)


def encode_file_control_cmd(tran_nbr: int, security_code: int,
                            filename: str, file_cmd: int) -> bytes:
    """File Control command, sec. 2.3.3.4. DANGEROUS: several FileCmd
    codes stop the running program and destroy data tables. The CLI
    gates these behind an explicit confirmation flag."""
    return (struct.pack(">BBH", MSG_FILE_CONTROL_CMD, tran_nbr & 0xFF,
                        security_code & 0xFFFF)
            + _asciiz(filename)
            + struct.pack(">B", file_cmd & 0xFF))


@dataclass(frozen=True)
class FileControlResponse:
    resp_code: int
    hold_off: int   # seconds to wait before the next transaction


@_decoder
def decode_file_control_resp(body: bytes) -> FileControlResponse:
    _expect(body, MSG_FILE_CONTROL_RESP, "file control response")
    resp_code = body[2]
    check_resp_code("file control", resp_code, {
        0x01: "permission denied",
        0x0D: "invalid file name",
        0x13: "unsupported FileCmd code",
    })
    (hold_off,) = struct.unpack_from(">H", body, 3)
    return FileControlResponse(resp_code, hold_off)


# --------------------------------------------------- programming statistics

def encode_get_prog_stat_cmd(tran_nbr: int, security_code: int) -> bytes:
    return struct.pack(">BBH", MSG_GET_PROG_STAT_CMD, tran_nbr & 0xFF,
                       security_code & 0xFFFF)


@dataclass(frozen=True)
class ProgStats:
    os_version: str
    os_signature: int
    serial_nbr: str
    powerup_prog: str
    comp_state: int
    prog_name: str
    prog_signature: int
    comp_time: _dt.datetime
    comp_result: str

    COMP_STATES = {0x00: "no datalogger program",
                   0x01: "program running",
                   0x02: "program cannot compile",
                   0x03: "program is paused"}


@_decoder
def decode_get_prog_stat_resp(body: bytes) -> ProgStats:
    _expect(body, MSG_GET_PROG_STAT_RESP, "programming stats response")
    resp_code = body[2]
    check_resp_code("get programming statistics", resp_code,
                    {0x01: "permission denied"})
    offset = 3
    os_version, offset = _read_asciiz(body, offset)
    (os_sig,) = struct.unpack_from(">H", body, offset)
    offset += 2
    serial_nbr, offset = _read_asciiz(body, offset)
    powerup_prog, offset = _read_asciiz(body, offset)
    comp_state = body[offset]
    offset += 1
    prog_name, offset = _read_asciiz(body, offset)
    (prog_sig,) = struct.unpack_from(">H", body, offset)
    offset += 2
    sec, ns = struct.unpack_from(">2i", body, offset)
    offset += 8
    comp_result, offset = _read_asciiz(body, offset)
    from .tabledefs import nsec_to_datetime
    return ProgStats(os_version, os_sig, serial_nbr, powerup_prog,
                     comp_state, prog_name, prog_sig,
                     nsec_to_datetime(sec, ns), comp_result)


# ------------------------------------------------------------ collect data

def encode_collect_cmd(tran_nbr: int, security_code: int, mode: int,
                       table_nbr: int, table_sig: int,
                       p1: int | tuple[int, int] | None = None,
                       p2: int | tuple[int, int] | None = None,
                       field_nbrs: tuple[int, ...] = ()) -> bytes:
    """Collect Data command (sec. 2.3.4.3).

    P1/P2 presence depends on the mode:
      0x03: no parameters
      0x04, 0x05: P1 (UInt4)
      0x06, 0x08: P1 and P2 (UInt4)
      0x07: P1 and P2 (NSec tuples)
    An empty ``field_nbrs`` collects all fields (list is terminated by a
    UInt2 zero either way).
    """
    parts = [struct.pack(">BBHBHH", MSG_COLLECT_DATA_CMD, tran_nbr & 0xFF,
                         security_code & 0xFFFF, mode & 0xFF,
                         table_nbr & 0xFFFF, table_sig & 0xFFFF)]
    if mode == COLLECT_ALL:
        pass
    elif mode in (COLLECT_FROM_RECORD, COLLECT_MOST_RECENT):
        parts.append(struct.pack(">I", int(p1)))
    elif mode in (COLLECT_RECORD_RANGE, COLLECT_PARTIAL):
        parts.append(struct.pack(">2I", int(p1), int(p2)))
    elif mode == COLLECT_TIME_RANGE:
        parts.append(struct.pack(">2i", *p1))
        parts.append(struct.pack(">2i", *p2))
    else:
        raise ValueTypeError(f"unsupported collect mode 0x{mode:02x}")
    for nbr in field_nbrs:
        if nbr == 0:
            raise ValueTypeError("field number 0 is the list terminator")
        parts.append(struct.pack(">H", nbr))
    parts.append(struct.pack(">H", 0))  # field list terminator
    return b"".join(parts)


@dataclass(frozen=True)
class RecordFragment:
    """IsOffset=1 case: one piece of a record too big for a single packet.

    ``byte_offset`` counts from the beginning of the TimeOfRec field
    (sec. 2.3.4.3); reassemble pieces in offset order, then decode with
    :func:`decode_reassembled_record`.
    """

    table_nbr: int
    record_nbr: int
    byte_offset: int
    data: bytes


@dataclass(frozen=True)
class CollectResponse:
    records: list[Record]
    fragment: RecordFragment | None   # set when IsOffset was flagged
    more_records_exist: bool


@_decoder
def decode_collect_resp(body: bytes,
                        table_for_nbr: Callable[[int], TableDef]) \
        -> CollectResponse:
    """Decode a Collect Data response (sec. 2.3.4.3).

    ``table_for_nbr`` maps a table number to its TableDef (needed to
    know record layout). Raises InvalidTableDefsError on RespCode 0x07
    so the caller can refresh table definitions.
    """
    _expect(body, MSG_COLLECT_DATA_RESP, "collect response")
    resp_code = body[2]
    check_resp_code("collect data", resp_code, {
        0x01: "permission denied",
        0x02: "insufficient resources",
        0x07: "invalid table definitions",
    })

    records: list[Record] = []
    fragment: RecordFragment | None = None
    offset = 3
    # Groups repeat until only the trailing MoreRecsExist byte remains.
    while offset < len(body) - 1:
        table_nbr, beg_rec_nbr, flag_word = struct.unpack_from(
            ">HIH", body, offset)
        offset += 8
        table = table_for_nbr(table_nbr)
        if flag_word & 0x8000:
            # Fragmented record: flag word is the high half of a 31-bit
            # byte offset ("15 or 31 bits", sec. 2.3.4.3).
            (low,) = struct.unpack_from(">H", body, offset)
            offset += 2
            byte_offset = ((flag_word << 16) | low) & 0x7FFFFFFF
            data = body[offset:len(body) - 1]
            offset = len(body) - 1
            fragment = RecordFragment(table_nbr, beg_rec_nbr, byte_offset,
                                      data)
            continue
        nbr_of_recs = flag_word & 0x7FFF
        offset = _decode_records(body, offset, table, beg_rec_nbr,
                                 nbr_of_recs, records)

    if offset != len(body) - 1:
        raise ProtocolError(
            f"collect response mis-parse: ended at byte {offset} of "
            f"{len(body)} (raw: {body.hex(' ')})")
    more = bool(body[-1])
    return CollectResponse(records, fragment, more)


def _decode_records(body: bytes, offset: int, table: TableDef,
                    beg_rec_nbr: int, nbr_of_recs: int,
                    records: list[Record]) -> int:
    """Decode NbrOfRecs consecutive records into ``records``."""
    interval_sec, interval_ns = table.interval
    first_time: _dt.datetime | None = None
    for i in range(nbr_of_recs):
        if table.is_event_driven:
            # event data: a time field precedes each record
            timestamp, offset = decode_time(table.time_type, body, offset)
        elif i == 0:
            first_time, offset = decode_time(table.time_type, body, offset)
            timestamp = first_time
        else:
            # interval data: TimeOfRec appears once; later records are
            # spaced by the table interval (carry nanoseconds properly)
            total_ns = i * interval_ns
            carry_sec, rem_ns = divmod(total_ns, 1_000_000_000)
            timestamp = first_time + _dt.timedelta(
                seconds=i * interval_sec + carry_sec,
                microseconds=rem_ns / 1000)
        values, offset = table.decode_record_fields(body, offset)
        records.append(Record(
            table_name=table.name,
            record_number=(beg_rec_nbr + i) & 0xFFFFFFFF,
            timestamp=timestamp,
            values=values))
    return offset


def decode_reassembled_record(table: TableDef, record_nbr: int,
                              data: bytes) -> Record:
    """Decode a record rebuilt from COLLECT_PARTIAL fragments.

    The buffer starts at the TimeOfRec field (sec. 2.3.4.3 offset rule),
    for interval and event tables alike.
    """
    timestamp, offset = decode_time(table.time_type, data, 0)
    values, offset = table.decode_record_fields(data, offset)
    if offset != len(data):
        raise ProtocolError(
            f"reassembled record for {table.name}#{record_nbr}: "
            f"{len(data) - offset} unexpected trailing bytes")
    return Record(table.name, record_nbr & 0xFFFFFFFF, timestamp, values)


# ------------------------------------------------------------ get/set values

def encode_get_values_cmd(tran_nbr: int, security_code: int, table_name: str,
                          type_code: int, field_name: str,
                          swath: int) -> bytes:
    """Get Values command (sec. 2.3.5)."""
    return (struct.pack(">BBH", MSG_GET_VALUES_CMD, tran_nbr & 0xFF,
                        security_code & 0xFFFF)
            + _asciiz(table_name)
            + struct.pack(">B", type_code & 0xFF)
            + _asciiz(field_name)
            + struct.pack(">H", swath & 0xFFFF))


_VALUES_RESP_CODES = {
    0x01: "permission denied",
    0x10: "invalid table or field",
    0x11: "data type conversion not supported",
    0x12: "memory bounds violation",
}


@_decoder
def decode_get_values_resp(body: bytes, type_code: int,
                           swath: int) -> list[object]:
    _expect(body, MSG_GET_VALUES_RESP, "get values response")
    resp_code = body[2]
    check_resp_code("get values", resp_code, _VALUES_RESP_CODES)
    dtype = DATA_TYPES.get(type_code)
    if dtype is None:
        raise ValueTypeError(f"unknown type code {type_code}")
    values: list[object] = []
    offset = 3
    if dtype.name == "ASCII":
        # one fixed-length string of `swath` characters
        raw = body[offset:offset + swath]
        return [raw.split(b"\x00", 1)[0].decode("ascii", "replace")]
    for _ in range(swath):
        value, offset = decode_scalar(type_code, body, offset)
        values.append(value)
    if offset != len(body):
        raise ProtocolError(
            f"get values response has {len(body) - offset} extra bytes")
    return values


def encode_set_values_cmd(tran_nbr: int, security_code: int, table_name: str,
                          type_code: int, field_name: str,
                          values: list[object]) -> bytes:
    """Set Values command (sec. 2.3.5). Values are encoded as type_code."""
    dtype = DATA_TYPES.get(type_code)
    if dtype is None:
        raise ValueTypeError(f"unknown type code {type_code}")
    if dtype.name == "ASCII":
        payload = values[0].encode("ascii") if values else b""
        swath = len(payload)
    else:
        payload = b"".join(encode_scalar(type_code, v) for v in values)
        swath = len(values)
    return (struct.pack(">BBH", MSG_SET_VALUES_CMD, tran_nbr & 0xFF,
                        security_code & 0xFFFF)
            + _asciiz(table_name)
            + struct.pack(">B", type_code & 0xFF)
            + _asciiz(field_name)
            + struct.pack(">H", swath & 0xFFFF)
            + payload)


@_decoder
def decode_set_values_resp(body: bytes) -> int:
    _expect(body, MSG_SET_VALUES_RESP, "set values response")
    resp_code = body[2]
    check_resp_code("set values", resp_code, _VALUES_RESP_CODES)
    return resp_code
