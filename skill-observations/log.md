# Skill Observation Log

Observations captured during task-oriented work. Each entry identifies a
potential skill improvement or new skill opportunity.

**Status key:** OPEN = not yet actioned | ACTIONED = skill updated/created |
DECLINED = user decided not to pursue

---

## 2026-07-09

### Observation 1: Spec-anchored binary protocol implementation workflow

**Status:** OPEN
**Date:** 2026-07-09
**Session context:** STAC Milestone 1 — implementing Campbell PakBus/BMP5
datalogger protocol library from vendor PDFs, with a hard "no
implementation from memory" constraint.
**Skill:** New skill candidate: spec-anchored-protocol-implementation
**Type:** open-source
**Phase/Area:** whole-task methodology

**Issue:** A repeatable workflow emerged that made a high-risk protocol
implementation verifiable before any hardware was available: (1) obtain
the authoritative spec PDFs and extract text locally (pypdf) since PDF
page rendering was unavailable; (2) BEFORE writing any code, transcribe
the spec's checksum/signature algorithm into a scratch script and verify
it against the spec's own printed example packets (all six matched);
(3) turn those printed example packets into unit-test fixtures so
encoders are pinned to published bytes rather than to self-round-trips;
(4) treat prior-art libraries strictly as cross-checks, recording each
confirmed/contradicted point in NOTES.md; (5) record every spec
ambiguity in BLOCKERS.md with current behavior + impact + how to resolve,
instead of guessing. Testing also surfaced a real protocol hazard
(PakCtrl and BMP5 share MsgType 0x89) only because the fake peer was
made to discriminate on protocol code like real hardware does.

**Suggested improvement:** Create a skill capturing this workflow for
any "implement a wire protocol / file format from a spec" task: doc
acquisition & text extraction, algorithm-verification-before-code
against in-spec examples, fixture-from-spec-bytes testing, prior-art as
cross-check-only with a written confirmed/wrong ledger, and a BLOCKERS
file discipline for ambiguities.

**Principle:** When a document contains worked examples, they are the
cheapest ground truth available — verify your reading of the algorithm
against them before writing production code, and freeze them as test
fixtures so correctness never rests on code testing itself.
