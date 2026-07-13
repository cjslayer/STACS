"""PakBusClient: high-level, transaction-oriented API for one datalogger.

Responsibilities:

* link establishment (wake-up SerSyncBytes, ring/ready, hello exchange)
* transaction lifecycle: allocate transaction numbers, match responses,
  honor Please Wait, retry on timeout (with a NEW transaction number,
  per BMP5 Transparent Commands secs. 3.7.4 / 4.7.2 -- except file
  downloads, which must keep their transaction number across fragments)
* answering unsolicited traffic (hello commands, rings) so the logger
  keeps considering us a neighbor
* data-integrity accounting: deduplication by (table, record number),
  last-collected tracking, gap backfill after reconnect

The spec's warning about clock sets (sec. 2.3.2) is enforced: a clock
adjustment is never blindly retried; on timeout the clock is re-read.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import bmp5, linkstate
from .errors import (DeliveryFailureError, FramingError,
                     InvalidTableDefsError, ProtocolError, StacError,
                     TransactionTimeoutError, TransportError,
                     UnknownTableError, UnexpectedMessageError)
from .framing import Deframer, SYNC, frame_packet
from .linkstate import (ExpectMore, Header, LinkState, Neighbor, Protocol,
                        unpack_header)
from .tabledefs import (Record, TableDef, datetime_to_nsec,
                        parse_tabledefs)
from .transport import Transport

log = logging.getLogger(__name__)

#: Wake-up preamble: CR1000-type loggers want >= 6 SerSyncBytes before the
#: first packet (BMP5 Transparent Commands sec. 4.2, auto-baud sync).
WAKEUP = bytes([SYNC]) * 8

#: Bytes requested per File Upload exchange. Must keep the response under
#: the 1000-byte PakBus message limit (sec. 4.12); 512 matches the
#: datalogger's own minimum response allowance for data collection.
UPLOAD_SWATH = 512

#: Payload bytes per File Download fragment (same 1000-byte budget, minus
#: header + filename overhead).
DOWNLOAD_CHUNK = 512


@dataclass
class ClientStats:
    """Visible accounting for everything the client did or rejected."""

    packets_sent: int = 0
    packets_received: int = 0
    retries: int = 0
    timeouts: int = 0
    unexpected_messages: int = 0
    malformed_packets: int = 0
    hellos_answered: int = 0
    duplicates_dropped: int = 0
    records_collected: int = 0
    gaps_detected: int = 0
    records_backfilled: int = 0
    reconnects: int = 0


class PakBusClient:
    """Synchronous client for a single directly-connected PakBus logger."""

    def __init__(self, transport: Transport, *,
                 my_addr: int = 4093,       # STAC default; LoggerNet is 4094
                 logger_addr: int = 1,      # CR6 factory default
                 security_code: int = 0,
                 timeout: float = 3.0,
                 max_retries: int = 2,
                 verify_intv: int = 1800):
        if not (1 <= my_addr <= 4094) or not (1 <= logger_addr <= 4094):
            raise ValueError("PakBus addresses must be within 1..4094")
        self.transport = transport
        self.my_addr = my_addr
        self.logger_addr = logger_addr
        self.security_code = security_code
        self.timeout = timeout
        self.max_retries = max_retries
        self.verify_intv = verify_intv

        self.stats = ClientStats()
        self.deframer = Deframer()
        self.neighbor = Neighbor(address=logger_addr)
        self._tran = 0
        self._table_defs: list[TableDef] | None = None

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> float:
        """Open the transport and establish the link. Returns hello RTT."""
        if not self.transport.is_open:
            self.transport.open()
        self.deframer.reset()
        self._ring()
        return self.hello()

    def close(self) -> None:
        """Send Bye + FINISHED (sec. 2.2.4) and close the transport."""
        if self.transport.is_open:
            try:
                self._send_message(Protocol.PAKCTRL, linkstate.encode_bye(),
                                   expect_more=ExpectMore.LAST,
                                   link_state=LinkState.FINISHED)
                self.transport.write(frame_packet(linkstate.finished_packet(
                    self.logger_addr, self.my_addr)))
            except TransportError as exc:
                log.warning("could not send Bye on close: %s", exc)
            self.transport.close()
        self.neighbor.invalidate()

    def reconnect(self) -> float:
        """Recover after a transport failure (e.g. USB unplug).

        Closes and reopens the transport, then re-establishes the link.
        Raises TransportError if the medium is still unavailable; the
        caller owns the retry cadence.
        """
        self.transport.close()
        self.neighbor.invalidate()
        self.deframer.reset()
        self.transport.open()
        self._ring()
        rtt = self.hello()
        self.stats.reconnects += 1
        return rtt

    # ------------------------------------------------------------- plumbing

    def _next_tran(self) -> int:
        self._tran = (self._tran % 0xFF) + 1  # 1..255, never 0
        return self._tran

    def _send_message(self, hi_proto: Protocol, body: bytes, *,
                      expect_more: ExpectMore = ExpectMore.EXPECT_MORE,
                      link_state: LinkState = LinkState.READY) -> None:
        header = linkstate.pack_header(
            link_state=link_state, dst=self.logger_addr, src=self.my_addr,
            expect_more=expect_more, priority=0x1, hi_proto=hi_proto)
        self.transport.write(frame_packet(header + body))
        self.stats.packets_sent += 1

    def _ring(self) -> None:
        """Wake the logger and confirm the link with ring -> ready."""
        deadline_total = time.monotonic() + self.timeout * (
            self.max_retries + 1)
        attempt = 0
        while True:
            attempt += 1
            self.transport.write(WAKEUP + frame_packet(
                linkstate.ring_packet(self.logger_addr, self.my_addr)))
            self.stats.packets_sent += 1
            deadline = min(time.monotonic() + self.timeout, deadline_total)
            try:
                self._pump_until(deadline, want_ready=True)
                return
            except TransactionTimeoutError:
                self.stats.timeouts += 1
                if time.monotonic() >= deadline_total:
                    raise TransactionTimeoutError(
                        f"no 'ready' from logger {self.logger_addr} after "
                        f"{attempt} ring attempts") from None
                self.stats.retries += 1
                log.info("ring attempt %d got no ready; retrying", attempt)

    def hello(self) -> float:
        """Run a hello transaction; returns round-trip time in seconds."""
        tran = self._next_tran()
        body = linkstate.encode_hello(
            linkstate.MSG_HELLO_CMD, tran, is_router=False,
            verify_intv=self.verify_intv)
        started = time.monotonic()
        resp = self._transact(Protocol.PAKCTRL, body, tran,
                              {linkstate.MSG_HELLO_RESP})
        rtt = time.monotonic() - started
        _, _, info = linkstate.decode_hello(resp)
        self.neighbor.record_hello(info, self.verify_intv)
        log.info("hello exchange complete: rtt=%.1f ms, hop metric 0x%02x, "
                 "verify interval %ds", rtt * 1000, info.hop_metric,
                 self.neighbor.verify_intv)
        return rtt

    def _transact(self, hi_proto: Protocol, body: bytes, tran: int,
                  expected_types: set[int], *,
                  timeout: float | None = None,
                  retries: int | None = None,
                  rebuild=None) -> bytes:
        """Send a command and wait for its matching response body.

        On timeout the command is retried with a NEW transaction number
        (``rebuild(new_tran)`` must return the re-encoded body) unless
        ``rebuild`` is None, in which case the same bytes are re-sent
        with the same transaction number (correct for file downloads and
        idempotent reads).
        """
        timeout = self.timeout if timeout is None else timeout
        retries = self.max_retries if retries is None else retries
        attempt = 0
        while True:
            attempt += 1
            self._send_message(hi_proto, body)
            try:
                return self._pump_until(
                    time.monotonic() + timeout,
                    tran=tran, expected_types=expected_types,
                    expected_proto=hi_proto)
            except TransactionTimeoutError:
                self.stats.timeouts += 1
                if attempt > retries:
                    raise TransactionTimeoutError(
                        f"no response (MsgType in "
                        f"{{{', '.join(f'0x{t:02x}' for t in expected_types)}}}"
                        f", tran {tran}) after {attempt} attempts") from None
                self.stats.retries += 1
                if rebuild is not None:
                    tran = self._next_tran()
                    body = rebuild(tran)
                log.info("transaction timeout, retry %d/%d (tran %d)",
                         attempt, retries, tran)

    def _pump_until(self, deadline: float, *, tran: int | None = None,
                    expected_types: set[int] | None = None,
                    expected_proto: Protocol | None = None,
                    want_ready: bool = False) -> bytes:
        """Receive pump: dispatch traffic until the wanted event arrives."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransactionTimeoutError("deadline expired")
            data = self.transport.read_available(min(remaining, 0.25))
            if not data:
                continue
            for packet in self.deframer.feed(data):
                self.stats.packets_received += 1
                try:
                    outcome = self._dispatch(packet, tran, expected_types,
                                             expected_proto)
                except FramingError as exc:
                    # Valid signature but nonsensical structure: count it
                    # and keep pumping; never let one packet kill a poll.
                    self.stats.malformed_packets += 1
                    log.warning("discarding malformed packet (%s): %s",
                                exc, packet.hex(" "))
                    continue
                if outcome is None:
                    continue
                kind, payload = outcome
                if kind == "response":
                    return payload
                if kind == "ready" and want_ready:
                    return b""
                if kind == "wait":
                    deadline = max(deadline,
                                   time.monotonic() + payload + 1.0)
                    log.info("logger asked us to wait %ds", payload)

    def _dispatch(self, packet: bytes, tran: int | None,
                  expected_types: set[int] | None,
                  expected_proto: Protocol | None = None):
        """Classify one received packet; answer unsolicited traffic.

        Returns None (handled/ignored), ("response", body),
        ("ready", None) or ("wait", seconds).
        """
        header, body = unpack_header(packet)

        # Address filtering (sec. 1.4.4 checklist items 2 and 3).
        if header.dst_phy_addr not in (self.my_addr,
                                       linkstate.BROADCAST_ADDR):
            self.stats.unexpected_messages += 1
            log.warning("ignoring packet addressed to %d (not us): %s",
                        header.dst_phy_addr, packet.hex(" "))
            return None
        if header.src_phy_addr != self.logger_addr:
            self.stats.unexpected_messages += 1
            log.warning("ignoring packet from unexpected node %d: %s",
                        header.src_phy_addr, packet.hex(" "))
            return None

        self.neighbor.touch()

        if header.is_link_state_only or not body:
            return self._dispatch_link_state(header)

        msg_type = body[0]
        msg_tran = body[1] if len(body) > 1 else None

        # PakCtrl delivery failure aborts whatever we were waiting for.
        if (header.hi_proto_code == Protocol.PAKCTRL
                and msg_type == linkstate.MSG_DELIVERY_FAILURE):
            raise DeliveryFailureError(
                linkstate.decode_delivery_failure(body), raw=packet)

        # Please Wait for our pending transaction extends the deadline.
        if msg_type == bmp5.MSG_PLEASE_WAIT and tran is not None:
            wait = bmp5.decode_please_wait(body)
            if wait.tran_nbr == tran:
                return ("wait", wait.wait_sec)
            self.stats.unexpected_messages += 1
            return None

        # The response we are waiting for. MsgType codes overlap between
        # PakCtrl and BMP5 (hello response and collect-data response are
        # both 0x89), so the protocol code must match too.
        if (expected_types is not None and msg_type in expected_types
                and (expected_proto is None
                     or header.hi_proto_code == expected_proto)
                and (tran is None or msg_tran == tran)):
            return ("response", body)

        # Unsolicited PakCtrl commands we must answer (sec. 2.2.2/2.2.3).
        if header.hi_proto_code == Protocol.PAKCTRL:
            if msg_type == linkstate.MSG_HELLO_CMD:
                _, cmd_tran, info = linkstate.decode_hello(body)
                self._send_message(
                    Protocol.PAKCTRL,
                    linkstate.hello_response_for(cmd_tran, info),
                    expect_more=ExpectMore.LAST)
                self.stats.hellos_answered += 1
                return None
            if msg_type == linkstate.MSG_HELLO_REQUEST:
                self._send_message(
                    Protocol.PAKCTRL,
                    linkstate.encode_hello(
                        linkstate.MSG_HELLO_CMD, self._next_tran(),
                        verify_intv=self.verify_intv),
                    expect_more=ExpectMore.LAST)
                return None
            if msg_type == linkstate.MSG_BYE:
                log.info("logger sent Bye; link is going down")
                self.neighbor.invalidate()
                return None

        # Anything else: count it, log it, and tell the logger we do not
        # implement it (sec. 4.1 mandates a delivery-failure reply).
        self.stats.unexpected_messages += 1
        log.warning("unexpected message type 0x%02x (tran %s): %s",
                    msg_type, msg_tran, packet.hex(" "))
        if not (msg_type & 0x80):  # only commands get a failure reply
            self._send_message(
                Protocol.PAKCTRL,
                linkstate.encode_delivery_failure(0x04, packet[4:8], body),
                expect_more=ExpectMore.LAST)
        return None

    def _dispatch_link_state(self, header: Header):
        state = header.link_state
        if state == LinkState.READY:
            return ("ready", None)
        if state == LinkState.RING:
            self.transport.write(frame_packet(
                linkstate.ready_packet(self.logger_addr, self.my_addr)))
            self.stats.packets_sent += 1
            return None
        log.info("link-state packet from logger: %s", state.name)
        if state in (LinkState.OFFLINE, LinkState.FINISHED):
            self.neighbor.invalidate()
        return None

    # ------------------------------------------------------------------ clock

    def get_clock(self) -> "ClockReading":
        """Read the logger clock; returns reading plus PC-clock offset."""
        tran = self._next_tran()
        body = bmp5.encode_clock_cmd(tran, self.security_code, (0, 0))
        resp = self._transact(
            Protocol.BMP5, body, tran, {bmp5.MSG_CLOCK_RESP},
            rebuild=lambda t: bmp5.encode_clock_cmd(
                t, self.security_code, (0, 0)))
        import datetime as dt
        pc_now = dt.datetime.now()
        decoded = bmp5.decode_clock_resp(resp)
        offset = (decoded.old_time - pc_now).total_seconds()
        return ClockReading(decoded.old_time, pc_now, offset)

    def set_clock_to_pc(self) -> tuple["ClockReading", "ClockReading"]:
        """Synchronize the logger clock to the PC clock.

        Reads, adjusts by the measured offset, then re-reads to verify.
        A failed adjustment is NEVER blindly retried (sec. 2.3.2 warning:
        a retried set could double-apply); on timeout we simply re-read
        and report, letting the operator decide.
        """
        before = self.get_clock()
        seconds = int(-before.offset)
        nanoseconds = int((-before.offset - seconds) * 1e9)
        tran = self._next_tran()
        body = bmp5.encode_clock_cmd(tran, self.security_code,
                                     (seconds, nanoseconds))
        try:
            resp = self._transact(Protocol.BMP5, body, tran,
                                  {bmp5.MSG_CLOCK_RESP}, retries=0)
            bmp5.decode_clock_resp(resp)
        except TransactionTimeoutError:
            log.warning("clock-set response lost; re-reading clock instead "
                        "of retrying the adjustment")
        after = self.get_clock()
        return before, after

    # ------------------------------------------------------------ table defs

    def table_definitions(self, refresh: bool = False) -> list[TableDef]:
        """Fetch (or return cached) table definitions from the .TDF file."""
        if self._table_defs is None or refresh:
            raw = self.file_upload(".TDF")
            self._table_defs = parse_tabledefs(raw)
            log.info("parsed %d table definitions: %s",
                     len(self._table_defs),
                     ", ".join(t.name for t in self._table_defs))
        return self._table_defs

    def table_by_name(self, name: str) -> TableDef:
        wanted = name.lower()
        for table in self.table_definitions():
            if table.name.lower() == wanted:
                return table
        raise UnknownTableError(
            f"logger reports no table named {name!r}; available: "
            + ", ".join(t.name for t in self.table_definitions()))

    def _table_by_nbr(self, nbr: int) -> TableDef:
        for table in self.table_definitions():
            if table.number == nbr:
                return table
        raise UnknownTableError(f"no table definition for table #{nbr}")

    # ---------------------------------------------------------- file transfer

    def file_upload(self, filename: str) -> bytes:
        """Retrieve a file from the logger (File Upload, sec. 2.3.3.2).

        Uses stateless offset-addressed reads (CloseFlag=1 each exchange)
        so a retried request can never corrupt position state. EOF is a
        response shorter than the requested swath.
        """
        chunks: list[bytes] = []
        offset = 0
        while True:
            tran = self._next_tran()
            body = bmp5.encode_file_upload_cmd(
                tran, self.security_code, filename, offset, UPLOAD_SWATH,
                close_flag=True)
            resp = self._transact(
                Protocol.BMP5, body, tran, {bmp5.MSG_FILE_UPLOAD_RESP},
                rebuild=lambda t, _o=offset: bmp5.encode_file_upload_cmd(
                    t, self.security_code, filename, _o, UPLOAD_SWATH,
                    close_flag=True))
            decoded = bmp5.decode_file_upload_resp(resp)
            if decoded.file_offset != offset:
                raise ProtocolError(
                    f"file upload: requested offset {offset} but response "
                    f"is for offset {decoded.file_offset}")
            chunks.append(decoded.data)
            if len(decoded.data) < UPLOAD_SWATH:
                return b"".join(chunks)
            offset += len(decoded.data)

    def file_download(self, filename: str, data: bytes,
                      progress=None) -> None:
        """Send a file to the logger (File Download, sec. 2.3.3.1).

        The transaction number is IDENTICAL for every fragment (the
        logger tracks the transfer by it, sec. 2.3.3.1), so a timed-out
        fragment is re-sent verbatim rather than re-numbered.
        """
        tran = self._next_tran()
        offset = 0
        total = len(data)
        while True:
            chunk = data[offset:offset + DOWNLOAD_CHUNK]
            is_last = offset + len(chunk) >= total
            body = bmp5.encode_file_download_cmd(
                tran, self.security_code,
                filename if offset == 0 else "",  # name optional after 1st
                offset, chunk, close_flag=is_last)
            resp = self._transact(
                Protocol.BMP5, body, tran, {bmp5.MSG_FILE_DOWNLOAD_RESP},
                rebuild=None)  # same tran + same bytes on retry
            decoded = bmp5.decode_file_download_resp(resp)
            if decoded.file_offset != offset:
                raise ProtocolError(
                    f"file download: sent fragment at offset {offset} but "
                    f"logger acknowledged offset {decoded.file_offset}")
            offset += len(chunk)
            if progress is not None:
                progress(offset, total)
            if is_last:
                return

    def file_control(self, filename: str, file_cmd: int) -> int:
        """File Control (sec. 2.3.3.4). Returns HoldOff seconds.

        DANGER: compile/run codes wipe data tables. Callers must gate
        this behind explicit operator confirmation (the CLI does).
        """
        tran = self._next_tran()
        body = bmp5.encode_file_control_cmd(tran, self.security_code,
                                            filename, file_cmd)
        resp = self._transact(
            Protocol.BMP5, body, tran, {bmp5.MSG_FILE_CONTROL_RESP},
            rebuild=lambda t: bmp5.encode_file_control_cmd(
                t, self.security_code, filename, file_cmd))
        return bmp5.decode_file_control_resp(resp).hold_off

    def get_prog_stats(self) -> bmp5.ProgStats:
        tran = self._next_tran()
        body = bmp5.encode_get_prog_stat_cmd(tran, self.security_code)
        resp = self._transact(
            Protocol.BMP5, body, tran, {bmp5.MSG_GET_PROG_STAT_RESP},
            rebuild=lambda t: bmp5.encode_get_prog_stat_cmd(
                t, self.security_code))
        return bmp5.decode_get_prog_stat_resp(resp)

    # ------------------------------------------------------------ collection

    def collect_most_recent(self, table_name: str,
                            count: int = 1) -> list[Record]:
        """Collect the newest ``count`` record(s) (mode 0x05)."""
        return self._collect(table_name, bmp5.COLLECT_MOST_RECENT, p1=count)

    def collect_since(self, table_name: str,
                      record_nbr: int) -> list[Record]:
        """Collect from ``record_nbr`` (inclusive) to newest (mode 0x04).

        If the start record no longer exists in ring memory the logger
        returns from its oldest record instead (documented behavior) --
        callers doing gap accounting must inspect the first returned
        record number.
        """
        return self._collect(table_name, bmp5.COLLECT_FROM_RECORD,
                             p1=record_nbr)

    def _collect(self, table_name: str, mode: int, p1=None,
                 p2=None) -> list[Record]:
        """Run a collect transaction to completion (MoreRecsExist loop,
        fragmented-record reassembly, stale-table-defs refresh)."""
        table = self.table_by_name(table_name)
        try:
            return self._collect_loop(table, mode, p1, p2)
        except InvalidTableDefsError:
            log.warning("table definitions are stale; re-fetching .TDF "
                        "and retrying collect on %s", table_name)
            self.table_definitions(refresh=True)
            table = self.table_by_name(table_name)
            return self._collect_loop(table, mode, p1, p2)

    def _collect_loop(self, table: TableDef, mode: int, p1, p2) \
            -> list[Record]:
        records: list[Record] = []
        seen: set[tuple[str, int]] = set()
        while True:
            count_before = len(records)
            tran = self._next_tran()
            body = bmp5.encode_collect_cmd(
                tran, self.security_code, mode, table.number,
                table.signature, p1=p1, p2=p2)
            resp_body = self._transact(
                Protocol.BMP5, body, tran, {bmp5.MSG_COLLECT_DATA_RESP},
                rebuild=lambda t: bmp5.encode_collect_cmd(
                    t, self.security_code, mode, table.number,
                    table.signature, p1=p1, p2=p2))
            resp = bmp5.decode_collect_resp(resp_body, self._table_by_nbr)

            for record in resp.records:
                if record.key() in seen:
                    self.stats.duplicates_dropped += 1
                    continue
                seen.add(record.key())
                records.append(record)
                self.stats.records_collected += 1

            if resp.fragment is not None:
                record = self._reassemble_record(table, resp.fragment)
                if record.key() not in seen:
                    seen.add(record.key())
                    records.append(record)
                    self.stats.records_collected += 1
                else:
                    self.stats.duplicates_dropped += 1

            if not resp.more_records_exist:
                return records
            if len(records) == count_before:
                raise ProtocolError(
                    "logger reports more records exist but the last "
                    "response contained no new ones (no progress)")
            # Continue from the record after the last one we have.
            last = records[-1].record_number
            if mode == bmp5.COLLECT_RECORD_RANGE:
                p1 = (last + 1) & 0xFFFFFFFF          # keep p2 (range end)
            elif mode == bmp5.COLLECT_TIME_RANGE:
                # Resume just past the last collected timestamp, keeping
                # the end bound. Record timestamps within one table are
                # strictly increasing, so +1 ns cannot skip a record
                # (assumption noted in BLOCKERS.md).
                sec, ns = datetime_to_nsec(records[-1].timestamp)
                sec, ns = (sec + (ns + 1) // 1_000_000_000,
                           (ns + 1) % 1_000_000_000)
                p1 = (sec, ns)                        # keep p2 (end time)
            else:
                mode, p1, p2 = (bmp5.COLLECT_FROM_RECORD,
                                (last + 1) & 0xFFFFFFFF, None)

    def _reassemble_record(self, table: TableDef,
                           first: bmp5.RecordFragment) -> Record:
        """Fetch remaining pieces of an oversized record (mode 0x08)."""
        pieces = {first.byte_offset: first.data}
        next_offset = first.byte_offset + len(first.data)
        if first.byte_offset != 0:
            # Restart from zero so we hold the whole record.
            pieces, next_offset = {}, 0
        guard = 0
        while True:
            guard += 1
            if guard > 64:
                raise ProtocolError(
                    f"record #{first.record_nbr} of {table.name}: fragment "
                    "reassembly did not converge after 64 exchanges")
            tran = self._next_tran()
            body = bmp5.encode_collect_cmd(
                tran, self.security_code, bmp5.COLLECT_PARTIAL,
                table.number, table.signature,
                p1=first.record_nbr, p2=next_offset)
            resp_body = self._transact(
                Protocol.BMP5, body, tran, {bmp5.MSG_COLLECT_DATA_RESP},
                rebuild=lambda t: bmp5.encode_collect_cmd(
                    t, self.security_code, bmp5.COLLECT_PARTIAL,
                    table.number, table.signature,
                    p1=first.record_nbr, p2=next_offset))
            resp = bmp5.decode_collect_resp(resp_body, self._table_by_nbr)
            frag = resp.fragment
            if frag is None or frag.record_nbr != first.record_nbr:
                raise ProtocolError(
                    f"expected fragment of record #{first.record_nbr}, "
                    f"got {frag!r}")
            if frag.byte_offset != next_offset:
                raise ProtocolError(
                    f"fragment offset {frag.byte_offset} does not match "
                    f"requested offset {next_offset}")
            pieces[frag.byte_offset] = frag.data
            next_offset += len(frag.data)
            if not resp.more_records_exist or not frag.data:
                break

        data = b"".join(pieces[k] for k in sorted(pieces))
        return bmp5.decode_reassembled_record(table, first.record_nbr, data)

    # ------------------------------------------------------- get/set values

    def get_values(self, table_name: str, field_name: str,
                   type_name: str = "IEEE4B", swath: int = 1) -> list[object]:
        """Get Values against a named table (usually 'Public')."""
        from .tabledefs import TYPE_BY_NAME
        type_code = TYPE_BY_NAME[type_name].code
        tran = self._next_tran()
        body = bmp5.encode_get_values_cmd(
            tran, self.security_code, table_name, type_code, field_name,
            swath)
        resp = self._transact(
            Protocol.BMP5, body, tran, {bmp5.MSG_GET_VALUES_RESP},
            rebuild=lambda t: bmp5.encode_get_values_cmd(
                t, self.security_code, table_name, type_code, field_name,
                swath))
        return bmp5.decode_get_values_resp(resp, type_code, swath)

    def set_values(self, table_name: str, field_name: str,
                   values: list[object], type_name: str = "IEEE4B") -> None:
        """Set Values against a named table (the zero/calibration path)."""
        from .tabledefs import TYPE_BY_NAME
        type_code = TYPE_BY_NAME[type_name].code
        tran = self._next_tran()
        body = bmp5.encode_set_values_cmd(
            tran, self.security_code, table_name, type_code, field_name,
            values)
        resp = self._transact(
            Protocol.BMP5, body, tran, {bmp5.MSG_SET_VALUES_RESP},
            rebuild=lambda t: bmp5.encode_set_values_cmd(
                t, self.security_code, table_name, type_code, field_name,
                values))
        bmp5.decode_set_values_resp(resp)


@dataclass(frozen=True)
class ClockReading:
    logger_time: "datetime.datetime"
    pc_time: "datetime.datetime"
    offset: float  # logger minus PC, seconds


@dataclass
class MonitorStats:
    records_received: int = 0
    polls: int = 0
    poll_failures: int = 0
    gaps_detected: int = 0
    records_backfilled: int = 0
    records_lost: int = 0        # sequence holes the ring buffer overwrote


class TableMonitor:
    """Continuous, loss-accounted polling of one table.

    Tracks the last collected record number; every poll asks for
    "everything since" (mode 0x04), so records produced during an outage
    are backfilled automatically from the logger's ring memory when the
    link returns (data-integrity requirement: a yanked cable costs
    liveness, never data). Deduplicates by (table, record number) so
    transaction retries can never double-count a record.
    """

    def __init__(self, client: PakBusClient, table_name: str):
        self.client = client
        self.table_name = table_name
        self.stats = MonitorStats()
        self.last_record_nbr: int | None = None
        self._in_gap = False

    def prime(self) -> Record | None:
        """Establish the starting position from the newest record."""
        records = self.client.collect_most_recent(self.table_name, 1)
        if records:
            self.last_record_nbr = records[-1].record_number
            return records[-1]
        self.last_record_nbr = None
        return None

    def note_disconnect(self) -> None:
        """Record that the link failed; next successful poll backfills."""
        if not self._in_gap:
            self._in_gap = True
            self.stats.gaps_detected += 1
            self.client.stats.gaps_detected += 1

    def poll(self) -> list[Record]:
        """Collect all records since the last poll (may raise transport/
        protocol errors -- caller handles reconnect cadence)."""
        self.stats.polls += 1
        if self.last_record_nbr is None:
            record = self.prime()
            return [record] if record else []

        start = (self.last_record_nbr + 1) & 0xFFFFFFFF
        records = self.client.collect_since(self.table_name, start)
        fresh = [r for r in records if r.record_number >= start]
        if len(fresh) != len(records):
            # Logger returned records older than requested: mode 0x04
            # falls back to the oldest record when `start` has already
            # been overwritten. Those are re-reads, not new data.
            self.client.stats.duplicates_dropped += len(records) - len(fresh)

        if fresh:
            first_nbr = fresh[0].record_number
            if first_nbr > start:
                lost = first_nbr - start
                self.stats.records_lost += lost
                log.error(
                    "DATA LOSS: table %s records %d..%d were overwritten "
                    "in logger ring memory before collection (%d records)",
                    self.table_name, start, first_nbr - 1, lost)
            self.last_record_nbr = fresh[-1].record_number
            if self._in_gap:
                self.stats.records_backfilled += len(fresh)
                self.client.stats.records_backfilled += len(fresh)
                log.info("backfilled %d records after link outage", len(fresh))
        self._in_gap = False
        self.stats.records_received += len(fresh)
        return fresh
