"""PakBusClient behavior tests against a scripted fake transport.

Wire-format correctness is anchored by the document fixtures in the
other test modules; these tests exercise the transaction machinery:
retries, deduplication, gap backfill, please-wait, unsolicited traffic
and signature-failure recovery.
"""

import struct

import pytest

from pakbus import bmp5, linkstate
from pakbus.client import PakBusClient, TableMonitor
from pakbus.errors import (DeliveryFailureError, TransactionTimeoutError,
                           TransportError)
from pakbus.framing import frame_packet
from pakbus.linkstate import (ExpectMore, LinkState, Protocol, pack_header,
                              unpack_header)
from pakbus.tabledefs import TYPE_BY_NAME

LOGGER = 1
ME = 4093


def logger_frame(body: bytes, hi_proto=Protocol.BMP5, *,
                 link_state=LinkState.READY) -> bytes:
    """Frame a message as the logger would send it to us."""
    header = pack_header(link_state=link_state, dst=ME, src=LOGGER,
                         expect_more=ExpectMore.LAST, priority=1,
                         hi_proto=hi_proto)
    return frame_packet(header + body)


READY_FRAME = frame_packet(linkstate.ready_packet(dst=ME, src=LOGGER))


class FakeTransport:
    """Callable-responder transport.

    ``responder(header, body)`` is invoked for every packet the client
    writes and returns a list of items to queue as input: byte strings,
    or floats meaning "deliver nothing for this many seconds". Raising
    TransportError from write/read simulates a yanked cable.
    """

    def __init__(self, responder):
        self.responder = responder
        self.rx_queue: list = []
        self.sent: list[tuple] = []       # (header, body) as decoded
        self.opened = False
        self.fail_reads = False
        self.open_count = 0
        from pakbus.framing import Deframer
        self._deframer = Deframer()

    def open(self):
        self.opened = True
        self.fail_reads = False
        self.open_count += 1

    def close(self):
        self.opened = False

    @property
    def is_open(self):
        return self.opened

    def describe(self):
        return "fake"

    def write(self, data: bytes):
        if not self.opened:
            raise TransportError("write on closed fake transport")
        for packet in self._deframer.feed(data):
            header, body = unpack_header(packet)
            self.sent.append((header, body))
            responses = self.responder(header, body) or []
            self.rx_queue.extend(responses)

    def read_available(self, timeout: float) -> bytes:
        import time
        if self.fail_reads:
            raise TransportError("simulated unplugged cable")
        while self.rx_queue:
            head = self.rx_queue[0]
            if isinstance(head, float):          # delay marker
                self.rx_queue[0] = ("until", time.monotonic() + head)
                return b""
            if isinstance(head, tuple):          # armed delay marker
                if time.monotonic() < head[1]:
                    return b""
                self.rx_queue.pop(0)
                continue
            return self.rx_queue.pop(0)
        return b""


def basic_responder(extra=None):
    """Answers ring and hello; delegates the rest to ``extra``.

    Note: hello command (PakCtrl 0x09) and collect-data command (BMP5
    0x09) share a MsgType code -- discriminate on the protocol code,
    exactly as a real logger does.
    """
    def responder(header, body):
        if header.is_link_state_only:
            if header.link_state == LinkState.RING:
                return [READY_FRAME]
            return []
        if (body and header.hi_proto_code == Protocol.PAKCTRL
                and body[0] == linkstate.MSG_HELLO_CMD):
            _, tran, info = linkstate.decode_hello(body)
            resp = linkstate.encode_hello(
                linkstate.MSG_HELLO_RESP, tran, verify_intv=1800)
            return [logger_frame(resp, Protocol.PAKCTRL)]
        if extra is not None:
            return extra(header, body)
        return []
    return responder


def make_client(responder, **kwargs) -> PakBusClient:
    kwargs.setdefault("timeout", 0.15)
    transport = FakeTransport(responder)
    return PakBusClient(transport, my_addr=ME, logger_addr=LOGGER, **kwargs)


# --------------------------------------------------------------- link setup

def test_connect_establishes_neighbor():
    client = make_client(basic_responder())
    rtt = client.connect()
    assert rtt >= 0
    assert client.neighbor.established
    assert client.neighbor.verify_intv > 0


def test_connect_retries_ring_then_fails():
    client = make_client(lambda hdr, body: [], max_retries=1)
    client.transport.open()
    with pytest.raises(TransactionTimeoutError):
        client.connect()
    assert client.stats.timeouts >= 1


# -------------------------------------------------------------------- clock

def clock_responder(header, body):
    if body[0] == bmp5.MSG_CLOCK_CMD:
        resp = (bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                + struct.pack(">2i", 1_000_000_000, 0))
        return [logger_frame(resp)]
    return []


def test_get_clock():
    client = make_client(basic_responder(clock_responder))
    client.connect()
    reading = client.get_clock()
    assert reading.logger_time.year == 2021  # 1e9 s after 1990-01-01
    assert isinstance(reading.offset, float)


def test_transaction_retry_uses_new_tran_number():
    """First command is swallowed; retry must carry a new TranNbr."""
    seen = []

    def flaky(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            seen.append(body[1])
            if len(seen) == 1:
                return []  # swallow first attempt
            return clock_responder(header, body)
        return []

    client = make_client(basic_responder(flaky))
    client.connect()
    reading = client.get_clock()
    assert reading is not None
    assert len(seen) == 2
    assert seen[0] != seen[1]          # new transaction number on retry
    assert client.stats.retries == 1
    assert client.stats.timeouts == 1


def test_clock_set_is_never_blind_retried():
    """A timed-out adjustment must NOT be re-sent (sec. 2.3.2 warning)."""
    adjustments = []

    def responder(header, body):
        if body[0] != bmp5.MSG_CLOCK_CMD:
            return []
        sec, ns = struct.unpack_from(">2i", body, 4)
        if (sec, ns) == (0, 0):
            return clock_responder(header, body)
        adjustments.append((sec, ns))
        return []  # adjustment response is lost

    client = make_client(basic_responder(responder))
    client.connect()
    before, after = client.set_clock_to_pc()
    assert len(adjustments) == 1       # exactly one set attempt, no retry


# ----------------------------------------------------------- please wait

def test_please_wait_extends_deadline():
    """Response arrives after our timeout, but a please-wait announced
    the delay -- the client must wait it out instead of retrying."""
    state = {"asked": 0}

    def responder(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            state["asked"] += 1
            wait = bytes([bmp5.MSG_PLEASE_WAIT, body[1],
                          bmp5.MSG_CLOCK_CMD]) + (1).to_bytes(2, "big")
            resp = (bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                    + struct.pack(">2i", 5, 0))
            # please-wait now, real answer 0.3 s later (> 0.15 s timeout)
            return [logger_frame(wait), 0.3, logger_frame(resp)]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    reading = client.get_clock()
    assert reading is not None
    assert state["asked"] == 1         # no retry happened
    assert client.stats.retries == 0


# ------------------------------------------------- unsolicited traffic

def test_logger_hello_command_is_answered_mid_transaction():
    def responder(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            hello_cmd = linkstate.encode_hello(
                linkstate.MSG_HELLO_CMD, 0x55, verify_intv=1000)
            resp = (bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                    + struct.pack(">2i", 5, 0))
            return [logger_frame(hello_cmd, Protocol.PAKCTRL),
                    logger_frame(resp)]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    client.get_clock()
    assert client.stats.hellos_answered == 1
    # our hello response went out with the logger's transaction number
    hello_resps = [
        (h, b) for h, b in client.transport.sent
        if not h.is_link_state_only and b
        and b[0] == linkstate.MSG_HELLO_RESP and b[1] == 0x55]
    assert len(hello_resps) == 1
    _, resp_body = hello_resps[0]
    _, _, info = linkstate.decode_hello(resp_body)
    assert info.verify_intv == 400     # 1000 / 2.5


def test_delivery_failure_raises_typed_error():
    def responder(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            failure = linkstate.encode_delivery_failure(
                0x04, b"\x10\x01\x0f\xfe", body[:16])
            return [logger_frame(failure, Protocol.PAKCTRL)]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    with pytest.raises(DeliveryFailureError) as excinfo:
        client.get_clock()
    assert excinfo.value.err_code == 0x04


def test_unknown_command_gets_delivery_failure_reply():
    def responder(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            bogus = bytes([0x42, 0x01, 0xAA])   # unknown command type
            resp = (bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                    + struct.pack(">2i", 5, 0))
            return [logger_frame(bogus), logger_frame(resp)]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    client.get_clock()
    assert client.stats.unexpected_messages == 1
    failures = [
        (h, b) for h, b in client.transport.sent
        if not h.is_link_state_only and b
        and b[0] == linkstate.MSG_DELIVERY_FAILURE]
    assert len(failures) == 1
    assert failures[0][1][2] == 0x04   # "unimplemented command" code


def test_corrupt_frame_is_counted_then_recovered():
    def responder(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            good = logger_frame(bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                                + struct.pack(">2i", 5, 0))
            corrupt = bytearray(good)
            corrupt[3] ^= 0xFF
            return [bytes(corrupt), good]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    reading = client.get_clock()
    assert reading is not None
    assert client.deframer.stats.bad_signature == 1


def test_packets_for_other_nodes_are_ignored():
    def responder(header, body):
        if body[0] == bmp5.MSG_CLOCK_CMD:
            wrong_dst = frame_packet(pack_header(
                link_state=LinkState.READY, dst=0x123, src=LOGGER,
                expect_more=ExpectMore.LAST, priority=1,
                hi_proto=Protocol.BMP5)
                + bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                + struct.pack(">2i", 99, 0))
            good = logger_frame(bytes([bmp5.MSG_CLOCK_RESP, body[1], 0x00])
                                + struct.pack(">2i", 5, 0))
            return [wrong_dst, good]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    reading = client.get_clock()
    assert (reading.logger_time - __import__("datetime").datetime(
        1990, 1, 1)).total_seconds() == 5
    assert client.stats.unexpected_messages == 1


def test_pakctrl_response_cannot_satisfy_bmp5_transaction():
    """Hello response (PakCtrl 0x89) must never match a pending collect
    transaction (BMP5 0x89) even with a colliding transaction number."""
    def responder(header, body):
        if body and body[0] == bmp5.MSG_CLOCK_CMD:
            # PakCtrl 0x89 with the SAME transaction number, then nothing:
            # the client must keep waiting (and eventually time out).
            fake = linkstate.encode_hello(
                linkstate.MSG_HELLO_RESP, body[1], verify_intv=1800)
            return [logger_frame(fake, Protocol.PAKCTRL)]
        return []

    client = make_client(basic_responder(responder), max_retries=0)
    client.connect()
    with pytest.raises(TransactionTimeoutError):
        client.get_clock()


# --------------------------------------------------------------- collection

def tdf_bytes() -> bytes:
    """Minimal .TDF: one interval table 'Fast' (Load IEEE4B), NSec time."""
    out = bytearray([0x01])
    out += b"Fast\x00"
    out += struct.pack(">I", 1000)          # table size
    out.append(TYPE_BY_NAME["NSec"].code)   # time type
    out += struct.pack(">2i", 0, 0)         # time into
    out += struct.pack(">2i", 1, 0)         # interval 1 s
    out.append(TYPE_BY_NAME["IEEE4B"].code)
    out += b"Load\x00"
    out += b"\x00"                          # alias terminator
    out += b"Smp\x00" + b"kN\x00" + b"\x00"
    out += struct.pack(">2I", 1, 1)
    out += struct.pack(">I", 0)             # sub-dim terminator
    out += b"\x00"                          # field list terminator
    return bytes(out)


class CollectingLogger:
    """Responder with a .TDF and a growing set of records."""

    def __init__(self):
        self.tdf = tdf_bytes()
        from pakbus.tabledefs import parse_tabledefs
        self.table = parse_tabledefs(self.tdf)[0]
        self.newest = 100          # newest existing record number
        self.oldest = 1
        self.duplicate_next_response = False

    def record_bytes(self, nbr: int) -> bytes:
        return struct.pack(">f", float(nbr))

    def collect_body(self, tran, start, end) -> bytes:
        """Records [start, end] in one response."""
        count = end - start + 1
        body = bytearray([bmp5.MSG_COLLECT_DATA_RESP, tran, 0x00])
        if count > 0:
            body += struct.pack(">HIH", 1, start, count)
            body += struct.pack(">2i", start * 10, 0)   # TimeOfRec
            for nbr in range(start, end + 1):
                body += self.record_bytes(nbr)
        body += b"\x01" if self.duplicate_next_response else b"\x00"
        return bytes(body)

    def __call__(self, header, body):
        msg_type, tran = body[0], body[1]
        if msg_type == bmp5.MSG_FILE_UPLOAD_CMD:
            # parse offset/swath out of the command
            name_end = body.index(b"\x00", 4)
            close_flag, offset, swath = struct.unpack_from(
                ">BIH", body, name_end + 1)
            chunk = self.tdf[offset:offset + swath]
            resp = (bytes([bmp5.MSG_FILE_UPLOAD_RESP, tran, 0x00])
                    + struct.pack(">I", offset) + chunk)
            return [logger_frame(resp)]
        if msg_type == bmp5.MSG_COLLECT_DATA_CMD:
            mode = body[4]
            if mode == bmp5.COLLECT_MOST_RECENT:
                (count,) = struct.unpack_from(">I", body, 9)
                start = max(self.oldest, self.newest - count + 1)
                return [logger_frame(self.collect_body(
                    tran, start, self.newest))]
            if mode == bmp5.COLLECT_FROM_RECORD:
                (start,) = struct.unpack_from(">I", body, 9)
                if start > self.newest:
                    return [logger_frame(self.collect_body(tran, 1, 0))]
                start = max(start, self.oldest)
                if self.duplicate_next_response:
                    # claim more data exists, then repeat the same batch
                    return [logger_frame(self.collect_body(
                        tran, start, self.newest))]
                return [logger_frame(self.collect_body(
                    tran, start, self.newest))]
        return []


def make_collect_client():
    responder = CollectingLogger()
    client = make_client(basic_responder(responder))
    client.connect()
    return client, responder


def test_collect_most_recent():
    client, logger = make_collect_client()
    records = client.collect_most_recent("Fast", 3)
    assert [r.record_number for r in records] == [98, 99, 100]
    assert all(r.table_name == "Fast" for r in records)
    assert records[0].values["Load"] == 98.0
    assert records[0].timestamp is not None


def test_collect_since():
    client, logger = make_collect_client()
    records = client.collect_since("Fast", 95)
    assert [r.record_number for r in records] == list(range(95, 101))


def test_collect_deduplicates_overlapping_batches():
    """If the logger re-serves records across batches, no double insert."""
    client, logger = make_collect_client()
    logger.duplicate_next_response = True

    # The logger will claim MoreRecsExist and then serve overlapping
    # data; the client continues from last+1, gets nothing new, and the
    # no-progress guard ends the loop with an error -- OR the second
    # batch is disjoint. Model the realistic overlap case: second batch
    # starts at last+1 but logger has no new records -> empty + done.
    def scripted(header, body):
        msg_type, tran = body[0], body[1]
        if msg_type == bmp5.MSG_COLLECT_DATA_CMD:
            mode = body[4]
            if mode == bmp5.COLLECT_MOST_RECENT:
                logger.duplicate_next_response = True
                resp = logger.collect_body(tran, 99, 100)  # more=1
                logger.duplicate_next_response = False
                return [logger_frame(resp)]
            if mode == bmp5.COLLECT_FROM_RECORD:
                (start,) = struct.unpack_from(">I", body, 9)
                assert start == 101
                # logger re-serves 100 (overlap) and 101 (new), done
                return [logger_frame(logger.collect_body(tran, 100, 101))]
        return logger(header, body)

    client2 = make_client(basic_responder(scripted))
    client2.connect()
    records = client2.collect_most_recent("Fast", 2)
    assert [r.record_number for r in records] == [99, 100, 101]
    assert client2.stats.duplicates_dropped == 1


# ------------------------------------------------------------- monitor

def test_monitor_backfills_after_outage():
    client, logger = make_collect_client()
    monitor = TableMonitor(client, "Fast")

    primed = monitor.prime()
    assert primed.record_number == 100

    logger.newest = 102
    fresh = monitor.poll()
    assert [r.record_number for r in fresh] == [101, 102]
    assert monitor.stats.gaps_detected == 0

    # --- outage: cable yanked; logger keeps producing records ---
    client.transport.fail_reads = True
    with pytest.raises(TransportError):
        monitor.poll()
    monitor.note_disconnect()
    logger.newest = 110              # 8 records produced during outage

    client.reconnect()               # fake transport reopens cleanly
    fresh = monitor.poll()
    assert [r.record_number for r in fresh] == list(range(103, 111))
    assert monitor.stats.gaps_detected == 1
    assert monitor.stats.records_backfilled == 8
    assert monitor.stats.records_lost == 0
    assert client.stats.reconnects == 1


def test_monitor_detects_ring_memory_loss():
    """If the ring buffer overwrote uncollected records, say so loudly."""
    client, logger = make_collect_client()
    monitor = TableMonitor(client, "Fast")
    monitor.prime()

    logger.newest = 200
    logger.oldest = 150              # records 101..149 gone forever
    fresh = monitor.poll()
    assert fresh[0].record_number == 150
    assert monitor.stats.records_lost == 49
    assert monitor.stats.records_received == 51


def test_monitor_poll_empty_when_no_new_records():
    client, logger = make_collect_client()
    monitor = TableMonitor(client, "Fast")
    monitor.prime()
    assert monitor.poll() == []      # nothing new
    assert monitor.stats.records_lost == 0


# ------------------------------------------------------------ get/set values

def test_set_and_get_values_round_trip():
    store = {"ZeroCmd": 0.0}

    def responder(header, body):
        msg_type, tran = body[0], body[1]
        if msg_type == bmp5.MSG_SET_VALUES_CMD:
            # Public\0 <type> ZeroCmd\0 <swath> <value>
            offset = body.index(b"\x00", 4) + 1
            type_code = body[offset]
            offset += 1
            name_end = body.index(b"\x00", offset)
            name = body[offset:name_end].decode()
            (value,) = struct.unpack_from(">f", body, name_end + 3)
            store[name] = value
            return [logger_frame(bytes([bmp5.MSG_SET_VALUES_RESP, tran,
                                        0x00]))]
        if msg_type == bmp5.MSG_GET_VALUES_CMD:
            offset = body.index(b"\x00", 4) + 2
            name_end = body.index(b"\x00", offset)
            name = body[offset:name_end].decode()
            resp = (bytes([bmp5.MSG_GET_VALUES_RESP, tran, 0x00])
                    + struct.pack(">f", store[name]))
            return [logger_frame(resp)]
        return []

    client = make_client(basic_responder(responder))
    client.connect()
    client.set_values("Public", "ZeroCmd", [42.5])
    assert store["ZeroCmd"] == 42.5
    assert client.get_values("Public", "ZeroCmd") == [42.5]
