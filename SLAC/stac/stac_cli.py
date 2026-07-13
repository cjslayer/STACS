#!/usr/bin/env python3
"""STAC Milestone 1 validation harness.

Subcommands:
  ping      hello transaction, report round-trip time
  clock     read logger clock vs PC clock; --set to synchronize
  tables    list tables with fields, types, sizes
  collect   one-shot: newest record(s) from a named table
  monitor   poll a table continuously; Ctrl+C prints integrity stats
  setvar    set a Public-table variable (type-checked before sending)
  sendfile  upload a file to the logger (gated; see --run)

Connection options are shared by all subcommands:
  --port COM7 [--baud 115200]     serial (CR6 over USB virtual COM port)
  --tcp HOST[:PORT]               TCP (default port 6785)
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time

from pakbus import (PakBusClient, SerialTransport, TableMonitor,
                    TcpTransport, StacError, TransportError)
from pakbus.bmp5 import (FC_COMPILE_RUN_POWERUP, FC_STOP_DELETE_RUN,
                         ProgStats)
from pakbus.errors import UnknownFieldError, ValueTypeError
from pakbus.tabledefs import DATA_TYPES, FieldDef

log = logging.getLogger("stac")

WIPE_FLAG = "--i-understand-this-wipes-tables"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stac_cli",
        description="STAC datalogger communication validation harness")
    conn = parser.add_argument_group("connection")
    conn.add_argument("--port", help="serial port (e.g. COM7)")
    conn.add_argument("--baud", type=int, default=115200,
                      help="serial baud rate (ignored on USB, default "
                           "%(default)s)")
    conn.add_argument("--tcp", metavar="HOST[:PORT]",
                      help="TCP address of an Ethernet-connected logger")
    conn.add_argument("--logger-addr", type=int, default=1,
                      help="logger PakBus address (default %(default)s)")
    conn.add_argument("--my-addr", type=int, default=4093,
                      help="STAC's own PakBus address (default %(default)s; "
                           "LoggerNet uses 4094)")
    conn.add_argument("--security-code", type=int, default=0,
                      help="datalogger security code (default %(default)s)")
    conn.add_argument("--timeout", type=float, default=3.0,
                      help="transaction timeout, seconds (default "
                           "%(default)s)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v: protocol info, -vv: byte-level debug")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="hello transaction + round-trip time")

    p_clock = sub.add_parser("clock", help="read (or set) the logger clock")
    p_clock.add_argument("--set", action="store_true",
                         help="synchronize logger clock to this PC")

    sub.add_parser("tables", help="list tables, fields, types, sizes")

    p_collect = sub.add_parser("collect",
                               help="collect newest record(s) from a table")
    p_collect.add_argument("table", help="table name (e.g. Public)")
    p_collect.add_argument("--count", type=int, default=1,
                           help="how many of the most recent records "
                                "(default %(default)s)")
    p_collect.add_argument("--since-record", type=int, default=None,
                           metavar="N",
                           help="instead: collect from record number N "
                                "to newest")

    p_mon = sub.add_parser("monitor", help="poll a table continuously")
    p_mon.add_argument("table", help="table name to poll")
    p_mon.add_argument("--interval", type=float, default=0.25,
                       help="poll interval in seconds (default %(default)s)")
    p_mon.add_argument("--quiet", action="store_true",
                       help="print one status line per poll batch instead "
                            "of every record")

    p_set = sub.add_parser("setvar",
                           help="set a Public variable (type-checked)")
    p_set.add_argument("name", help="variable name, e.g. Zero_Cmd or "
                                    "Offset(3)")
    p_set.add_argument("value", help="new value")
    p_set.add_argument("--table", default="Public",
                       help="table to write (default %(default)s)")

    p_send = sub.add_parser("sendfile", help="upload a file to the logger")
    p_send.add_argument("file", help="local file to send")
    p_send.add_argument("--dest", default=None,
                        help="destination name on the logger (default "
                             "CPU:<basename>)")
    p_send.add_argument("--run", action="store_true",
                        help="compile and RUN the program after upload "
                             f"(DESTROYS DATA TABLES; requires {WIPE_FLAG})")
    p_send.add_argument(WIPE_FLAG, dest="wipe_ack", action="store_true",
                        help="explicit acknowledgement that compiling/"
                             "running a program deletes the logger's "
                             "data tables")
    return parser


def make_client(args) -> PakBusClient:
    if bool(args.port) == bool(args.tcp):
        sys.exit("error: specify exactly one of --port or --tcp")
    if args.port:
        transport = SerialTransport(args.port, args.baud)
    else:
        host, _, port = args.tcp.partition(":")
        transport = TcpTransport(host, int(port) if port else 6785)
    return PakBusClient(
        transport, my_addr=args.my_addr, logger_addr=args.logger_addr,
        security_code=args.security_code, timeout=args.timeout)


# ------------------------------------------------------------- subcommands

def cmd_ping(client: PakBusClient, args) -> int:
    rtts = [client.connect()]
    for _ in range(3):
        rtts.append(client.hello())
    print(f"logger {args.logger_addr} responded to hello "
          f"({client.transport.describe()})")
    print(f"round-trip time: first {rtts[0] * 1000:.1f} ms, "
          f"min {min(rtts) * 1000:.1f} ms, "
          f"avg {sum(rtts) / len(rtts) * 1000:.1f} ms over {len(rtts)} "
          f"exchanges")
    hop = client.neighbor.hop_metric
    print(f"neighbor established: hop metric 0x{hop:02x}, "
          f"negotiated verify interval {client.neighbor.verify_intv}s")
    return 0


def cmd_clock(client: PakBusClient, args) -> int:
    client.connect()
    if args.set:
        before, after = client.set_clock_to_pc()
        print(f"before: logger {before.logger_time}  "
              f"(offset {before.offset:+.3f} s vs PC)")
        print(f"after:  logger {after.logger_time}  "
              f"(offset {after.offset:+.3f} s vs PC)")
        if abs(after.offset) > 1.0:
            print("WARNING: offset is still above 1 s after synchronization")
            return 1
    else:
        reading = client.get_clock()
        print(f"logger time: {reading.logger_time}")
        print(f"PC time:     {reading.pc_time}")
        print(f"offset:      {reading.offset:+.3f} s (logger minus PC)")
    return 0


def cmd_tables(client: PakBusClient, args) -> int:
    client.connect()
    tables = client.table_definitions()
    for table in tables:
        kind = ("event-driven" if table.is_event_driven else
                f"interval {table.interval[0]}s"
                + (f"+{table.interval[1]}ns" if table.interval[1] else ""))
        print(f"\n[{table.number}] {table.name}  --  {table.size} records "
              f"allocated, {kind}, definition signature 0x{table.signature:04X}")
        time_type = DATA_TYPES.get(table.time_type)
        print(f"    time tag: {time_type.name if time_type else table.time_type}"
              f"   record data size: {table.record_size} bytes")
        for fld in table.fields:
            dims = "" if fld.dimension == 1 else f"[{fld.dimension}]"
            extras = ", ".join(x for x in (
                fld.processing, fld.units,
                "read-only" if fld.read_only else "") if x)
            print(f"    {fld.name}{dims}: {fld.type_name}"
                  + (f"  ({extras})" if extras else ""))
    print(f"\n{len(tables)} tables")
    return 0


def _print_record(record) -> None:
    values = ", ".join(f"{k}={_fmt_value(v)}"
                       for k, v in record.values.items())
    print(f"{record.table_name} #{record.record_number} "
          f"@ {record.timestamp}  {values}")


def _fmt_value(value) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return "NAN"
        return f"{value:g}"
    return str(value)


def cmd_collect(client: PakBusClient, args) -> int:
    client.connect()
    if args.since_record is not None:
        records = client.collect_since(args.table, args.since_record)
    else:
        records = client.collect_most_recent(args.table, args.count)
    if not records:
        print(f"table {args.table}: no records")
        return 1
    for record in records:
        _print_record(record)
    return 0


def cmd_monitor(client: PakBusClient, args) -> int:
    client.connect()
    monitor = TableMonitor(client, args.table)
    first = monitor.prime()
    if first is not None:
        print(f"starting from newest record #{first.record_number} "
              f"@ {first.timestamp}")
        if not args.quiet:
            _print_record(first)
    else:
        print(f"table {args.table} is empty; waiting for first record")
    print(f"polling {args.table} every {args.interval * 1000:.0f} ms "
          f"-- Ctrl+C to stop\n")

    started = time.monotonic()
    try:
        while True:
            next_poll = time.monotonic() + args.interval
            try:
                fresh = monitor.poll()
            except TransportError as exc:
                monitor.note_disconnect()
                print(f"! link lost ({exc}); reconnecting...",
                      file=sys.stderr)
                _reconnect_forever(client)
                print("! link restored; backfilling", file=sys.stderr)
                continue
            except StacError as exc:
                monitor.stats.poll_failures += 1
                print(f"! poll failed: {exc}", file=sys.stderr)
                continue
            if args.quiet:
                if fresh:
                    print(f"{time.strftime('%H:%M:%S')} +{len(fresh)} "
                          f"records (last #{fresh[-1].record_number})")
            else:
                for record in fresh:
                    _print_record(record)
            delay = next_poll - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass  # fall through to the stats report

    elapsed = time.monotonic() - started
    s, cs = monitor.stats, client.stats
    print("\n--- monitor statistics ---")
    print(f"runtime:              {elapsed:.1f} s "
          f"({s.polls} polls, {s.poll_failures} poll failures)")
    print(f"records received:     {s.records_received}")
    print(f"gaps detected:        {s.gaps_detected}")
    print(f"records backfilled:   {s.records_backfilled}")
    print(f"records LOST:         {s.records_lost}"
          + ("   <-- ring memory overwrote uncollected records!"
             if s.records_lost else ""))
    print(f"reconnects:           {cs.reconnects}")
    print(f"transaction retries:  {cs.retries} "
          f"(timeouts: {cs.timeouts})")
    print(f"duplicates dropped:   {cs.duplicates_dropped}")
    print(f"unexpected messages:  {cs.unexpected_messages}")
    print(f"frames: {client.deframer.stats}")
    return 2 if s.records_lost else 0


def _reconnect_forever(client: PakBusClient) -> None:
    """Retry until the transport comes back. Ctrl+C still exits."""
    delay = 1.0
    while True:
        try:
            client.reconnect()
            return
        except StacError as exc:
            log.debug("reconnect attempt failed: %s", exc)
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)


def _wire_type_for(field: FieldDef, raw_value: str):
    """Type-check a CLI value against the field's declared type and pick
    the wire type for Set Values. Raises ValueTypeError on mismatch."""
    name = field.type_name
    if name in ("ASCII", "ASCIIZ"):
        if len(raw_value) > max(field.dimension - 1, 0):
            raise ValueTypeError(
                f"{field.name} holds at most {field.dimension - 1} "
                f"characters; got {len(raw_value)}")
        return "ASCII", raw_value
    if name.startswith("Bool"):
        lowered = raw_value.strip().lower()
        if lowered in ("1", "true", "on", "yes"):
            return "Bool", True
        if lowered in ("0", "false", "off", "no"):
            return "Bool", False
        raise ValueTypeError(
            f"{field.name} is {name}; value must be true/false/1/0, "
            f"got {raw_value!r}")
    if name in ("Byte", "UInt2", "UInt4", "Int1", "Int2", "Int4",
                "Short", "Long", "UShort", "ULong"):
        try:
            value = int(raw_value, 0)
        except ValueError:
            raise ValueTypeError(
                f"{field.name} is {name}; {raw_value!r} is not an "
                "integer") from None
        limits = {"Byte": (0, 255), "UInt2": (0, 65535),
                  "UInt4": (0, 2**32 - 1), "UShort": (0, 65535),
                  "ULong": (0, 2**32 - 1), "Int1": (-128, 127),
                  "Int2": (-32768, 32767), "Short": (-32768, 32767),
                  "Int4": (-2**31, 2**31 - 1), "Long": (-2**31, 2**31 - 1)}
        lo, hi = limits[name]
        if not lo <= value <= hi:
            raise ValueTypeError(
                f"{field.name} is {name}; {value} is outside [{lo}, {hi}]")
        return ("UInt4" if lo == 0 else "Int4"), value
    if name in ("FP2", "FP3", "FP4", "IEEE4B", "IEEE4L", "IEEE8B", "IEEE8L"):
        try:
            value = float(raw_value)
        except ValueError:
            raise ValueTypeError(
                f"{field.name} is {name}; {raw_value!r} is not a "
                "number") from None
        wire = "IEEE8B" if name in ("IEEE8B", "IEEE8L") else "IEEE4B"
        return wire, value
    raise ValueTypeError(
        f"setvar does not support fields of type {name} ({field.name})")


def cmd_setvar(client: PakBusClient, args) -> int:
    client.connect()
    table = client.table_by_name(args.table)

    base_name = args.name.split("(", 1)[0]
    field = table.field_by_name(base_name)   # raises UnknownFieldError
    if field.read_only:
        print(f"error: {table.name}.{field.name} is read-only")
        return 1
    wire_type, value = _wire_type_for(field, args.value)

    client.set_values(table.name, args.name, [value], type_name=wire_type)
    print(f"set {table.name}.{args.name} = {value} "
          f"(field type {field.type_name}, sent as {wire_type})")

    # Read back through Get Values so the operator sees what stuck.
    read_type = "ASCII" if wire_type == "ASCII" else wire_type
    swath = field.dimension if wire_type == "ASCII" else 1
    got = client.get_values(table.name, args.name, type_name=read_type,
                            swath=swath)
    print(f"read-back: {table.name}.{args.name} = {_fmt_value(got[0])}")
    return 0


def cmd_sendfile(client: PakBusClient, args) -> int:
    if not args.wipe_ack:
        print(f"error: sendfile requires {WIPE_FLAG}\n"
              "Uploading (and especially compiling/running) a program can "
              "destroy the logger's data tables. Re-run with the flag once "
              "you have collected everything you need.")
        return 1
    if args.run and not args.wipe_ack:
        print(f"error: --run requires {WIPE_FLAG}")
        return 1

    with open(args.file, "rb") as fh:
        data = fh.read()
    dest = args.dest
    if dest is None:
        import os
        dest = "CPU:" + os.path.basename(args.file)

    client.connect()

    def progress(sent: int, total: int) -> None:
        print(f"\r  sent {sent}/{total} bytes", end="", flush=True)

    print(f"uploading {args.file} -> {dest} ({len(data)} bytes)")
    client.file_download(dest, data, progress=progress)
    print("\nupload complete")

    if args.run:
        print("sending compile-and-run (File Control 0x01) -- THIS DELETES "
              "EXISTING DATA TABLES")
        hold_off = client.file_control(dest, FC_COMPILE_RUN_POWERUP)
        if hold_off:
            print(f"logger asked for {hold_off}s before next transaction; "
                  "waiting")
            time.sleep(hold_off)
        stats: ProgStats = client.get_prog_stats()
        state = ProgStats.COMP_STATES.get(stats.comp_state,
                                          f"0x{stats.comp_state:02x}")
        print(f"program: {stats.prog_name}  state: {state}")
        if stats.comp_result.strip():
            print(f"compile result:\n{stats.comp_result.strip()}")
        return 0 if stats.comp_state == 0x01 else 2
    return 0


# ------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = (logging.WARNING, logging.INFO, logging.DEBUG)[
        min(args.verbose, 2)]
    logging.basicConfig(
        level=level, stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    client = make_client(args)
    handler = {
        "ping": cmd_ping,
        "clock": cmd_clock,
        "tables": cmd_tables,
        "collect": cmd_collect,
        "monitor": cmd_monitor,
        "setvar": cmd_setvar,
        "sendfile": cmd_sendfile,
    }[args.command]

    try:
        return handler(client, args)
    except (UnknownFieldError, ValueTypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StacError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            client.close()
        except StacError:
            pass


if __name__ == "__main__":
    sys.exit(main())
