# BLOCKERS / Flagged Ambiguities

Per the milestone rules: anything the two ground-truth documents leave
ambiguous is recorded here instead of being guessed at silently. None of
these block the milestone's acceptance criteria (all involve types or
paths the bench CR6 does not exercise), but each needs a decision or a
live-hardware confirmation before it can be considered settled.

## 1. USec time resolution contradicts its own name — UNRESOLVED

BMP5 Transparent Commands Appendix A defines `USec` (code 13, 6 bytes)
as "10's of **milliseconds** resolution". The type's name and other
Campbell material suggest 10s of **microseconds**; a definitive official
statement was not found. **Current behavior:** decoded as 10 µs units,
with a warning logged every time a table's TimeType is USec, pointing
here. **Impact:** none for CR6 — CR1000-type loggers report NSec
(code 14) time tags. **To resolve:** if a USec table ever appears,
compare the decoded timestamp against the clock transaction's time; a
1000× discrepancy identifies the correct unit immediately.

## 2. FP3 / FP4 bit layouts undefined — decoding intentionally raises

Appendix A lists FP3 (code 15, 3 bytes) and FP4 (code 8, 4 bytes,
"CSI Format") but neither primary document defines their bit layouts.
**Current behavior:** `decode_scalar` raises a typed `ValueTypeError`
("bit layout is not specified in the available Campbell documents") —
loud, not silent. **Impact:** collection from a table containing FP3/FP4
fields fails explicitly. CR6/CRBasic programs store FP2 or IEEE4, so
this should not occur in STAC's use. **To resolve:** obtain Campbell's
"Binary Data Value Types" definitions for FSF3/FSF4 (LoggerNet manual
appendix) and implement with test vectors.

## 3. FP2 layout comes from a supplementary official source

FP2 (code 7) likewise has no bit layout in the two primary documents.
Implemented from Campbell's own published format page
(help.campbellsci.com, "FP2 data": sign bit 15, 2-bit negative decimal
exponent, 13-bit mantissa; +INF 0x1FFF, −INF 0x9FFF, NAN 0x9FFE), which
matches pycampbellcr1000's arithmetic. Risk considered low (official
Campbell source, two agreeing references), but noted because it is not
from the designated ground-truth documents. **To resolve:** during
hardware validation, store a known constant (e.g. 400.0, −1.5, NAN) into
an FP2 table field and confirm the decoded value.

## 4. Clock-response example contains an out-of-range nanoseconds field

The documented clock response example carries NSec nanoseconds bytes
`C8 00 00 00` = 3.36e9 as unsigned (> 1e9), negative as signed. The spec
never states signedness or normalization of the NSec fraction field.
**Current behavior:** decoded as a signed 32-bit integer per Appendix A
("two four-byte integers"), and our doc-fixture test only asserts the
seconds part. **To resolve:** capture real clock responses from the
bench CR6 and check what the fraction field actually contains.

## 5. Hello-response VerifyIntv rounding unspecified

"The link verification interval from the Hello Command message divided
by 2.5" — integer field, rounding rule unstated. We truncate toward
zero. Harmless (the value only tunes the logger's expectation of when to
re-verify us), noted for completeness.

## 6. Time-range collection (mode 0x07) continuation across packets

When a time-swath collection sets MoreRecsExist, the spec does not say
how to resume the swath. We resume with mode 0x07 and P1 = (last
collected record's timestamp + 1 ns), keeping the original end bound.
This assumes record timestamps within one table are strictly
increasing — true for interval tables and for CR6 event tables in
practice, but not stated by the spec. Duplicate protection by record
number backstops it either way. The milestone's required modes (0x04,
0x05) are unaffected.

## 7. Minimum-frame-length checklist item is self-contradictory

Sec. 1.4.4 says packets shorter than 4 bytes are invalid, but the
shortest meaningful frame (4-byte link-state packet + 2-byte nullifier)
is 6 bytes unquoted. We discard anything under 6 bytes (counted as
`too_short`). A 4–5 byte frame with a valid signature does not exist in
the protocol as documented; if one ever shows up in live captures it
will be counted and logged, not parsed.
