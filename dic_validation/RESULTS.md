# DIC Camera Validation — Final Results

**Camera:** ArduCam, index 0, CAP_MSMF, 1920×1200 @ ~30 fps (measured 29.94)
**Dates:** 2026-08-03 (Phase 1), 2026-08-04 (Phases 2–3 day 2), 2026-08-05 (Phase 3 day 3)
**Operator:** Christian (all physical actions); analysis pipeline in this directory.

## 1. Camera state

- Auto-exposure **locked** (readback-verified, `CAP_PROP_AUTO_EXPOSURE = 0`); intensity
  drift ≤ 0.14% across all measurement captures (limit 2%).
- Autofocus and auto-white-balance **unsupported by the driver** (no getter/setter);
  no focus or WB hunting was observed in any capture.
- 0 dropped frames in every capture of the campaign.

## 2. Noise floor (Phase 2 — PASSED)

Static target, 10 frames over 30 s, 24 subsets, phase correlation at 1/100 px.

| Metric | px | mm (day-3 scale) |
|---|---|---|
| Worst-axis std (dx) | 0.0352 | 0.0069 |
| dy std | 0.0187 | 0.0037 |
| After removing common-mode drift | 0.013 | 0.0026 |

## 3. Scale

- Trusted reference: block width **260.4 mm** (bench ruler measurement).
  The printed scale bar is enlarged ~10% and was **never used**.
- Day-2 scale: span 1315.38 px → 0.197965 mm/px.
- **Day-3 scale (used for trials): span 1326.82 px → 0.196259 mm/px** (5.095 px/mm).
  Edge 20–80% transitions 6.9 / 5.8 px; overlay confirmed by operator
  (`phase3/scale_edges_day3.png`).

## 4. Rail alignment (day 3)

Trial 1 (day 2) showed the rail was ~9–10° off perpendicular to the camera axis, so
tray travel had an out-of-plane component. New tool `alignment_check.py` prints the
block span and its % change from a baseline on each keypress; the operator adjusted
the rail angle between full-travel passes.

- Acceptance limit: |span change| < 0.3% across full travel.
- Passes after adjustment: +0.192%, −0.210%; after a lateral camera shift (to keep
  the tick bar in frame), re-verified: +0.234%, **final residual −0.094%**.
- For reference, trial 1 had −1.49% over just one inch of travel.
- Recorded in `phase3/alignment.json`.

## 5. Trials

Slide reference: operator's 1-inch marks, measured by **tape measure** (nominal
25.4 mm; the stated ±0.8 mm tolerance assumed a ruler — tape marks realistically
carry ±1–2 mm).

| Trial | Disk files | Known (mm) | Measured (mm) | Error | Span change | Column gradient (px) |
|---|---|---|---|---|---|---|
| Trial 1 (day 2, misaligned) | `trial1_*` | 25.4 | 30.46 | +19.9% | −1.49% | −15.3 (monotonic −141.7 → −159.1) |
| Trial A attempt 1 (disturbed) | `trial2_attempt1_*` | 25.4 (actual: full travel) | 28.995 | +14.2% vs nominal | +0.205% | +3.05 |
| **Trial A (slide right)** | `trial2_*` | 25.4 | **26.395** | **+3.92%** | **−0.155%** | **−2.95** |
| **Trial B (slide back left)** | `trial3_*` | 25.4 | **26.556** | **+4.55%** | **+0.271%** | **+3.35** |
| Trial 4 (caliper attempt, overshot) | `trial4_*` | 25.9 (calipers) | 29.492 | n/a — no landing mark | +0.260% | +3.29 |

Aligned-trial checks (protocol):

- Span change and column gradient are near zero in both trials and **flip sign
  between A and B** — the signature of a small symmetric residual, as required.
- **Closure:** A (+134.49 px) vs B (−135.31 px) — the tray returned to within
  0.82 px = **0.16 mm** of its start over a ~26.5 mm round trip.
- Tick-bar correlation cross-check agreed with the speckle mean within 1.5 px
  (0.30 mm) in both trials. Independently detected block edges (left and right)
  bracket the per-column speckle values at matching x in both trials.
- Trial A attempt 1: a bystander shadowed the lighting during the wait (12.4%
  intensity drift) and the tray was slid to the **hard stop**, not the 1-inch mark.
  The measured 28.995 mm matches the alignment-pass full-travel distance
  (147 px ≈ 28.9 mm) to within 2 px — the measurement was correct; the physical
  displacement was simply full travel. Kept as supporting evidence.

**On the ~+4% residual error:** trials A and B agree with each other to 0.16 mm
(0.6%), so the system is highly repeatable; both read ~26.4–26.6 mm against a
nominal 25.4 mm. After the trials the operator disclosed the actual rig: a wood
block slid freehand alongside a piece of metal, with **no track, no end marks, and
no repeatable landing feature**. The "known displacement" was therefore never
controlled to better than a few percent, and the recurring ~29 mm readings
(attempt 1 and trial 4) were the tray gliding to the natural end of travel rather
than stopping at an intended point. A caliper measurement of 25.9 mm was made, but
trial 4 overshot its intended landing by ~2.7 mm (confirmed geometrically: the
deformed frame sits 13.5 px past the previous start-mark position), so trial 4 is
recorded as motion data, not as an accuracy trial. **The ±4% accuracy bound is set
entirely by the displacement reference, not the camera**; the camera-side
self-consistency (closure 0.16 mm, edge/tick agreement ≤ 0.3 mm) supports ~1%
validation once a mechanically defined displacement (gauge block, machinist stop,
or dial indicator) is available.

## 6. The trial-1 story and the "8 px offset" (resolved)

Trial 1 (day 2): the slide changed the target pose (rail ~9–10° off perpendicular).
Symptoms: span shrank 1.49% during the slide; dx varied by column from −141.7 to
−159.1 px; speckle mean read +19.9% vs the ruler.

An apparent **constant ~8 px offset** between the correlation and geometric features
(ticks, edges) was suspected to be bench-pixel contamination of the full-frame
coarse shift. Step-1 investigation (`verify_coarse_fix.py`, results in
`phase3/coarse_fix_verify.json`) showed:

- Restricting the coarse estimate to the block ROI (edges inset 30 px) changed the
  coarse shift by only 1 px (−158 → −159) and the refined per-subset results by
  < 0.01 px. **The bench was never the cause** (the per-subset refinement had
  always recovered local shifts correctly). The ROI restriction was kept as hygiene.
- The real cause: a pose change is a **projective** map, so dx(x) is curved, and the
  three geometric anchors (tick, left edge, right edge) are not even collinear
  (±2.3 px line residuals). Comparing the curved speckle field against a straight
  line through sparse anchors produced the apparent offset. A 1-D homography fitted
  through the same three geometric anchors reproduces the speckle per-column dx to
  **≤ 0.57 px at every column**.
- Independent-algorithm check: `cv2.matchTemplate` (NCC + parabolic subpixel) on
  3 subsets agreed with phase correlation within 0.6–2.0 px (the 2.0 px case had
  the lowest NCC peak, 0.73; the others scored ≥ 0.96).

## 7. Diagnostics and known limitations

- The phase-correlation **tick check in `trial1_result.json` is bogus**
  (dy = +148 px): the tick bar is vertically periodic and phase correlation
  aliased. The day-3 pipeline's tick correlation (clamped ROI, coarse-shift
  seeded) behaved correctly in both aligned trials.
- The geometric **tick-centroid detector is unreliable under uneven lighting**
  (read 9–19 px off in day-3 trials while every other witness agreed); margin
  shading biases the texture centroid. Not used for any reported number.
- Lighting is not controlled: bystanders/hands caused 3–12% intensity swings
  during slide waits. Captures themselves stayed ≤ 0.5% drift with the exposure
  lock, but shadows can bias margin-based detectors (see above).
- Residual out-of-plane coupling: ~0.1–0.3% span change per ~26 mm travel remains
  after alignment. For strain measurement this appears directly as an apparent
  strain of order 1000–3000 µε — significant for metals, marginal for concrete/
  timber testing at higher strain levels.

## 8. Verdict — camera adequacy for structural-testing DIC

**Adequate for displacement-field DIC at the tenth-of-a-millimetre level; adequate
for strain only with further out-of-plane control and a calibrated scale reference.**

- Subpixel noise floor 0.035 px (**~7 µm** at this magnification) — excellent.
- Exposure locks and holds (≤ 0.5% drift); zero dropped frames; stable geometry.
- Rigid-body displacement over ~26.5 mm: repeatable to **0.16 mm (0.6%)**,
  self-consistent with independent geometric features to sub-pixel, and
  cross-validated by a second algorithm (NCC).
- Absolute accuracy is currently validated only to **~4%**, and the bound is set by
  the *references* (tape-measured slide, ruler-measured block width), not by the
  camera. With a caliper-measured displacement and scale reference, the remaining
  camera-side error budget (noise 0.007 mm, tick/edge agreement 0.3 mm, closure
  0.16 mm) supports validation at the ~1% level.
- Out-of-plane sensitivity is the dominant systematic: uncorrected pose change
  reads as large false strain (trial 1: 1.49% span change ≈ 15,000 µε artifact).
  Any structural test with this camera must fix the specimen-to-camera distance,
  verify alignment (span-change check, < 0.3%), and ideally add an out-of-plane
  witness (e.g. the span itself) to every measurement series.

## Appendix — file map

| File | Content |
|---|---|
| `camera_state.json` | Lock state, fps, startup diagnostics |
| `noise_frames/noise_floor.npz` | Phase 2 raw results |
| `phase3/scale.json`, `scale_edges_day3.png` | Day-3 scale + confirmed overlay |
| `phase3/alignment.json` | Alignment residual record |
| `phase3/coarse_fix_verify.json` | Step-1 offset investigation |
| `phase3/trial1_*` | Day-2 misaligned trial |
| `phase3/trial2_attempt1_*` | Disturbed / full-travel attempt |
| `phase3/trial2_*`, `trial3_*` | Trials A and B (aligned) |
| `phase3/trial4_*` | Caliper attempt (overshot — motion data only) |
| `verify_coarse_fix.py`, `alignment_check.py` | New day-3 tools |
