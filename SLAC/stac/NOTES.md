# STAC Milestone 1 — Implementation Notes

## Ground-truth documents

Every packet structure, opcode, quoting rule and signature algorithm in
this library was transcribed from official Campbell Scientific documents,
not from memory or inference:

1. **BMP5 Transparent Commands, rev. 9/08** (primary source for almost
   everything). Retrieved from the PyCampbellCR1000 repository's
   `docs/references/bmp5-transparent-commands.pdf` (the copy on
   s.campbellsci.com is no longer at its old URL). 100 pages. Used for:
   framing/quoting (sec. 1.2), header layout (sec. 1.3), signature +
   nullifier algorithm (Appendix B, C source), data types (Appendix A),
   SerPkt link states (sec. 2.1), PakCtrl messages (sec. 2.2), all BMP5
   message bodies (sec. 2.3), CR1000-type procedures (sec. 4), reference
   Java implementation (Appendix D).
2. **PakBus Networking Guide, rev. 10/08**
   (`https://s.campbellsci.com/documents/us/manuals/pakbusnetguide.pdf`).
   Used for: addressing conventions (sec. 3.2), neighbor/verify-interval
   lifecycle (sec. 3.7), software default addresses table.
3. **Campbell FP2 format page**
   (`help.campbellsci.com/.../formats/fp2.htm`). The two documents above
   list FP2 (code 7) but do not define its bit layout; this official page
   does: sign bit 15, two-bit negative decimal exponent (bits 14–13),
   13-bit mantissa; +INF = 0x1FFF, −INF = 0x9FFF, NAN = 0x9FFE.
   Flagged in BLOCKERS.md because it is a supplementary source.

### Verification against the documents' own bytes

BMP5 Transparent Commands prints six complete example frames (ring,
ready, clock command/response, .TDF file-upload command, collect
command). All six are reproduced **byte-for-byte** by this library's
encoders and signature/nullifier implementation — see
`tests/fixtures/doc_packets.py` and the tests that consume it. These are
the anchor for everything else: the framing, header packing, signature,
clock body, file-upload body and collect body are all pinned to
published bytes, not to our own round-trips.

Live request/response captures from the bench CR6 should be added to
`tests/fixtures/` during hardware validation (acceptance criterion 5);
the fixture module has a note marking where they belong.

## Prior art reviewed: PyCampbellCR1000 (and its ancestor PyPak)

Reviewed `pycampbellcr1000/pakbus.py` (master branch, GPL v3 — **no code
was copied**; it was used as a cross-check of our reading of the spec).
What it confirmed, and what it gets wrong or omits for CR6 use:

**Confirmed against our reading:** overall message layouts, big-endian
packing, quote/unquote byte pairs, signature algorithm, table-definition
parse order (name, size, time type, time-into, interval, fields), the
"stateless offset reads with CloseFlag=1" pattern for file upload (their
docstring notes CR1000 OS ≥ 17 wants a consistent transaction id for
continued reads; offset-addressed one-shot reads sidestep the issue).

**Defects/omissions found (not adopted):**

- **FP2 special values missing.** Their decoder does the sign/exponent/
  mantissa arithmetic but returns 799.8/–799.9-style numbers for the
  NAN/±INF encodings instead of NaN/±inf. Ours decodes them per the
  official FP2 page. For structural test data, silently turning NAN into
  a plausible number is exactly the kind of bug this project cannot have.
- **Nanosecond carry bug.** For interval tables they compute record
  timestamps as `(sec + n*int_sec, ns + n*int_ns)` without carrying
  `ns` overflow into seconds — wrong for sub-second intervals (a 250 ms
  table overflows after 4 records). Ours normalizes with divmod.
- **Bad signature ⇒ silent None.** Their `read()` logs and returns
  `None` on signature failure, indistinguishable from a timeout, and
  nothing is counted. Ours counts every rejected frame (by category)
  and logs raw bytes.
- **Fragmented records unsupported.** IsOffset fragments are returned
  raw with no mode-0x08 reassembly (the Appendix D Java throws
  "Partial records are not supported"). Ours reassembles via
  COLLECT_PARTIAL with offset verification.
- **Hello response fields.** They hardcode VerifyIntv=1800 in responses;
  spec says copy the command's hop metric and send its verify interval
  divided by 2.5. Ours follows the spec.
- **No source-address check.** They filter on DstNodeId only; the
  sec. 1.4.4 checklist also requires validating the source address.
- **File download (program send) unimplemented** — `NotImplementedError`.
- **MsgType 0x89 ambiguity.** They dispatch hello response and collect
  response by (proto, type) in `decode_packet` but *match transactions
  by number only*. Our client additionally requires the HiProtoCode to
  match the pending transaction (PakCtrl hello response and BMP5 collect
  response share code 0x89) — a unit test locks this in.

The Appendix D Java reference itself has bugs worth knowing about when
using it as a hint source: its priority decode masks the wrong bits
(`0x0300 >> 12`), and its `read_string` can index one past the buffer.
Where the prose tables and the Java disagree, we followed the prose.

## Design decisions

- **Addresses.** STAC defaults to PakBus address **4093** (PC400's
  conventional slot) so it can coexist with LoggerNet at 4094 on the
  same logger during the transition period. Configurable via
  `--my-addr`. Logger address defaults to 1 (CR6 factory default).
  Being ≥ 4000 also means allowed-neighbor lists cannot filter us
  (Networking Guide sec. 3.2).
- **Header fields for commands.** LinkState=ready (0xA), ExpMoreCode=01
  (expect more), priority 1. The doc's examples use priority 0 and 3 in
  different places; sec. 1.3 says "Priority 01 will be sufficient for
  normal communication". Ring/ready packets replicate the documented
  examples exactly (priority 0, expect-more 0).
- **Wake-up.** 8 SerSyncBytes before ring (spec sec. 4.2 requires ≥ 6
  for auto-baud sync).
- **Retry policy.** Timed-out transactions are retried with a **new**
  transaction number (secs. 3.7.4/4.7.2), except (a) file-download
  fragments, which must keep their transaction number, and are re-sent
  verbatim; (b) **clock set**, which is never blindly retried (sec.
  2.3.2 warning about double-applied adjustments) — on timeout we
  re-read the clock and report.
- **Data integrity.** Records always carry (table, record number, logger
  timestamp). Deduplication is by (table, record number) both within a
  collect transaction and across monitor polls. `TableMonitor` tracks
  the last collected record number; after a transport failure it resumes
  with collect-mode 0x04 from last+1, which backfills the outage from
  the logger's ring memory. If the first record returned is *newer* than
  requested, the ring buffer overwrote data before we could collect it —
  this is counted as `records_lost` and logged as an error (and the
  monitor subcommand exits nonzero). Nothing is ever silently dropped:
  the deframer counts bad-signature/runt/oversize frames, the client
  counts malformed and unexpected packets, and all of it is printed in
  the monitor's Ctrl+C stats.
- **Record-number wraparound** (UInt4) is not specially handled; at
  250 ms that is ~34 years of records. Noted here for honesty.
- **.TDF signature span.** For CR1000-type loggers (CR6) the table
  definition signature runs from the first byte of the table name
  through the field-list terminator (sec. 2.3.4.2). The CR200 section
  (3.9.1) describes a different start point (FieldName); we implement
  the CR1000-type rule since STAC targets CR6.
- **File upload swath / download chunk = 512 bytes**, comfortably inside
  the 1000-byte message limit (sec. 4.12) with header + name overhead.
- **TCP transport** (port 6785) is implemented and unit-tested at the
  interface level but has not touched real hardware yet; the bench CR6
  is serial-only for now.
- **`sendfile` safety.** The whole subcommand requires
  `--i-understand-this-wipes-tables`; the compile-and-run File Control
  command additionally requires `--run`. Without `--run` the file is
  only stored on the logger.

## Running

```
pip install pyserial pytest
python -m pytest tests/            # 125 tests, no hardware needed
python stac_cli.py --port COM7 ping
python stac_cli.py --port COM7 clock --set
python stac_cli.py --port COM7 tables
python stac_cli.py --port COM7 collect Public
python stac_cli.py --port COM7 monitor FastData --interval 0.25
python stac_cli.py --port COM7 setvar ZeroCmd 1
python stac_cli.py --port COM7 sendfile prog.CR6 --run \
    --i-understand-this-wipes-tables
```

`-v` shows protocol-level logging, `-vv` adds byte-level detail
(including raw hex of every rejected frame).
