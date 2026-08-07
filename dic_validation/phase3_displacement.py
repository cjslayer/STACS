"""Phase 3: known-displacement validation against a 25.4 mm (1 in) tray slide.

Two sub-commands:

  scale   capture a still, find the speckle block's left and right edges, and
          turn the trusted 260.4 mm block width into a mm/px scale factor.

  run     capture a reference frame, wait for the operator to slide the tray,
          capture the deformed frame, and measure the displacement.

The printed scale bar on the target is NOT used: that print came out about 10%
enlarged, so the bar is not the length it claims. The block's edge-to-edge
horizontal width, measured on the bench at 10.25 in = 260.4 mm, is the only
trusted reference.

Edges are found from a per-column TEXTURE profile rather than brightness. The
speckle block is high-variance, the surrounding paper margin is nearly flat, and
that contrast is far more stable than absolute intensity under uneven lighting
and the known target tilt.

'run' waits on a sentinel file instead of exiting between the reference and
deformed captures, so the camera is never reopened mid-trial. A reopen would
re-run the exposure lock and could land on a different operating point, which
would show up as false motion.

Requires: pip install opencv-python numpy scikit-image
"""

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from skimage.registration import phase_cross_correlation

sys.path.insert(0, str(Path(__file__).resolve().parent))
from camera_check import safe_read
from camera_lock import (capture_diagnostics, capture_series,
                         print_capture_diagnostics)
from lock_and_preview import open_locked_camera

HERE = Path(__file__).resolve().parent
TRIALS_DIR = HERE / "phase3"
SCALE_PATH = TRIALS_DIR / "scale.json"
SENTINEL = HERE / "PROCEED_TO_DEFORMED"

#: The one trusted physical reference: block width measured edge-to-edge.
BLOCK_WIDTH_MM = 260.4

#: Known displacement and its reference uncertainty. Day 3: the operator's
#: mark-to-mark slide measured 25.9 mm by calipers (was 25.4 +/- 0.8 by tape).
KNOWN_MM = 25.9
KNOWN_TOL_MM = 0.2

UPSAMPLE = 100
GRID_X = 6
GRID_Y = 4

#: Subset grid is inset from the detected block edges so no subset straddles an
#: edge -- an edge inside a subset is a huge high-contrast feature that would
#: dominate the correlation and hide what the speckle is doing.
BLOCK_INSET = 0.04

#: Rows used for the column texture profile, as fractions of frame height. The
#: extreme top and bottom carry the paper's own edges against the bench, which
#: are not part of the block.
PROFILE_ROW_LO = 0.25
PROFILE_ROW_HI = 0.75

#: Fraction of peak texture defining the block edge. Half-max is the standard
#: choice for a step whose exact transition width is set by focus, not geometry.
EDGE_FRACTION = 0.5

#: An edge whose 20%-80% transition is wider than this many px is called
#: ambiguous and gets escalated to the operator instead of used silently.
EDGE_SHARPNESS_LIMIT_PX = 40.0

#: Columns trimmed inside each paper edge before the block is looked for. The
#: paper-to-bench boundary is a hard intensity step, and a step reads as very
#: high texture -- higher than the speckle itself. Without this trim the block
#: detector locks onto the sheet's own edge instead of the block's.
PAPER_INSET_PX = 25

STILL_FRAMES = 5
SETTLE_SECONDS = 1.0
WAIT_TIMEOUT_S = 1800.0


# --------------------------------------------------------------------------
# capture helpers
# --------------------------------------------------------------------------

def grab_still(cap, n_frames=STILL_FRAMES):
    """Median of n_frames as the still. Returns (still, stats).

    The median across a few frames kills sensor read noise without blurring the
    pattern, because nothing is moving while a still is taken. Median rather
    than mean so a single corrupt frame cannot drag the result.
    """
    frames, stats = capture_series(cap, n_frames, SETTLE_SECONDS, safe_read)
    stack = np.stack(frames).astype(np.float64)
    return np.median(stack, axis=0), stats


# --------------------------------------------------------------------------
# edge detection / scale
# --------------------------------------------------------------------------

def column_texture(gray):
    """Per-column texture profile over the central rows.

    Texture = std down the column. The speckle block reads high, the blank
    paper margin reads near zero, and the bench behind the paper sits somewhere
    between -- so the block stands out as a clean plateau.
    """
    h = gray.shape[0]
    lo, hi = int(h * PROFILE_ROW_LO), int(h * PROFILE_ROW_HI)
    band = gray[lo:hi, :].astype(np.float64)
    # Mild vertical blur first: single-pixel speckle noise otherwise adds a
    # floor to every column, compressing the block-vs-margin contrast.
    band = cv2.GaussianBlur(band, (1, 5), 0)
    return band.std(axis=0)


def row_texture(gray, x0, x1):
    """Per-row texture profile within a column range -- the block's vertical extent."""
    band = gray[:, x0:x1].astype(np.float64)
    band = cv2.GaussianBlur(band, (5, 1), 0)
    return band.std(axis=1)


def _crossing(profile, threshold, start, stop, step):
    """First index in [start, stop) where profile crosses threshold, subpixel.

    Linear interpolation between the bracketing samples. The crossing sits
    between two integer columns, and rounding it to the nearer one would put a
    quantisation error straight into the scale factor.
    """
    i = start
    while (i + step) != stop and 0 <= i + step < len(profile):
        a, b = profile[i], profile[i + step]
        if (a < threshold) != (b < threshold):
            if b == a:
                return float(i)
            frac = (threshold - a) / (b - a)
            return float(i) + frac * step
        i += step
    return None


def column_brightness(gray):
    """Per-column 90th-percentile intensity over the central rows.

    The 90th percentile, not the mean. Inside the speckle block the mean sags
    to roughly the level of the bench (~180 vs ~130), because half the block is
    black ink -- so a mean-based threshold cuts the sheet in half. The white
    paper showing between speckles still reaches ~220-250 though, and the bench
    never exceeds ~150, so the upper percentile separates sheet from bench with
    a wide margin.
    """
    h = gray.shape[0]
    lo, hi = int(h * PROFILE_ROW_LO), int(h * PROFILE_ROW_HI)
    prof = np.percentile(gray[lo:hi, :].astype(np.float64), 90, axis=0)
    return cv2.GaussianBlur(prof.reshape(-1, 1), (1, 9), 0).ravel()


def _longest_run(mask, min_len):
    """Longest contiguous True run in mask as (start, end) inclusive, or None.

    Longest-run rather than first-crossing throughout: the frame contains
    several narrow decoys -- the printed tick bar, and the sheet's own edges
    against the bench -- that a scan-inward-until-threshold would stop on. The
    features being measured are hundreds of px wide, so taking the widest run
    ignores every decoy without needing to special-case any of them.
    """
    best, best_len, start = None, 0, None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start > best_len:
                best, best_len = (start, i - 1), i - start
            start = None
    if start is not None and len(mask) - start > best_len:
        best, best_len = (start, len(mask) - 1), len(mask) - start
    if best is None or best_len < min_len:
        return None
    return best


def _refine_boundaries(prof, threshold, run, window=40):
    """Subpixel left/right edges of a run, walking OUTWARD from inside it.

    Direction matters. Searching inward from outside the run can cross an
    unrelated feature first and stop there -- the printed tick bar sits 40 px
    from the block's left edge and is the strongest texture spike in the frame,
    so an inward search returns the tick bar's edge instead of the block's.
    Starting inside the run, where the profile is known to be above threshold,
    the first crossing found on the way out is by construction the run's own
    edge.
    """
    left = _crossing(prof, threshold, run[0], max(-1, run[0] - window), -1)
    right = _crossing(prof, threshold, run[1], min(len(prof), run[1] + window), +1)
    return (float(run[0]) if left is None else left,
            float(run[1]) if right is None else right)


def detect_paper_span(gray):
    """Columns spanned by the white sheet. Returns (left, right) or None."""
    prof = column_brightness(gray)
    dark = float(np.percentile(prof, 5))
    bright = float(np.percentile(prof, 95))
    threshold = 0.5 * (dark + bright)
    run = _longest_run(prof > threshold, min_len=100)
    if run is None:
        return None
    return _refine_boundaries(prof, threshold, run)


def detect_block_edges(gray):
    """Left and right edges of the speckle block, subpixel. Returns a dict.

    Two stages. First the sheet is located by brightness; then the block is
    located by texture strictly INSIDE the sheet, inset past its edges. The
    speckle is high-variance and the blank margin is nearly flat, so within the
    sheet that contrast is unambiguous.
    """
    paper = detect_paper_span(gray)
    if paper is None:
        return {"ok": False, "reason": "could not locate the paper sheet"}

    prof = column_texture(gray)
    smooth = cv2.GaussianBlur(prof.reshape(-1, 1), (1, 9), 0).ravel()

    lo = int(paper[0]) + PAPER_INSET_PX
    hi = int(paper[1]) - PAPER_INSET_PX
    if hi - lo < 100:
        return {"ok": False, "reason": "paper too narrow after inset"}

    # Peak texture is measured only inside the sheet, so the paper-to-bench
    # step cannot set the scale the threshold is judged against.
    peak = float(np.percentile(smooth[lo:hi], 99))
    threshold = EDGE_FRACTION * peak

    inside = np.zeros(len(smooth), dtype=bool)
    inside[lo:hi] = smooth[lo:hi] > threshold
    run = _longest_run(inside, min_len=200)
    if run is None:
        return {"ok": False, "reason": "no textured run found inside the paper"}
    left, right = _refine_boundaries(smooth, threshold, run)

    # Widths measured outward from inside the run, for the same reason.
    left_width = _edge_transition_width(smooth, run, "left")
    right_width = _edge_transition_width(smooth, run, "right")
    span = right - left
    ambiguous = (left_width > EDGE_SHARPNESS_LIMIT_PX
                 or right_width > EDGE_SHARPNESS_LIMIT_PX)

    return {
        "ok": True,
        "left_px": left,
        "right_px": right,
        "span_px": span,
        "left_transition_px": left_width,
        "right_transition_px": right_width,
        "peak_texture": peak,
        "paper_left_px": paper[0],
        "paper_right_px": paper[1],
        "ambiguous": bool(ambiguous),
    }


def _edge_transition_width(prof, run, side, plateau_span=150, search=80):
    """20%-80% width of one block edge, in px. Wide means soft/ambiguous.

    Judged against the LOCAL texture plateau just inside that edge, not against
    the frame-wide peak. The peak is set by the tick bar and a few hot spots and
    sits above the block's own texture level, so an 80%-of-peak crossing simply
    does not exist near the edge and the width would come back infinite for a
    perfectly sharp edge.
    """
    n = len(prof)
    if side == "left":
        inner = prof[min(run[0] + 20, n - 1):min(run[0] + plateau_span, n)]
        start, stop, step = min(run[0] + 20, n - 1), max(run[0] - search, -1), -1
    else:
        inner = prof[max(run[1] - plateau_span, 0):max(run[1] - 20, 1)]
        start, stop, step = max(run[1] - 20, 0), min(run[1] + search, n), +1
    if inner.size == 0:
        return float("inf")
    plateau = float(np.median(inner))
    p80 = _crossing(prof, 0.8 * plateau, start, stop, step)
    p20 = _crossing(prof, 0.2 * plateau, start, stop, step)
    if p20 is None or p80 is None:
        return float("inf")
    return abs(p80 - p20)


def save_edge_overlay(gray, edges, path, extra_lines=()):
    """Annotated PNG so the operator can eyeball the detected edges."""
    img = cv2.cvtColor(gray.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    h = img.shape[0]
    for x, colour, label in ((edges["left_px"], (0, 0, 255), "L"),
                             (edges["right_px"], (0, 0, 255), "R")):
        xi = int(round(x))
        cv2.line(img, (xi, 0), (xi, h), colour, 3)
        cv2.putText(img, "{} x={:.1f}".format(label, x), (max(10, xi - 200), 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, colour, 3)
    for x, colour, label in extra_lines:
        xi = int(round(x))
        cv2.line(img, (xi, 0), (xi, h), colour, 2)
        cv2.putText(img, label, (max(10, xi - 120), h - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, colour, 2)
    cv2.putText(img, "span {:.1f} px  =  {:.1f} mm".format(
        edges["span_px"], BLOCK_WIDTH_MM), (40, h - 100),
        cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 255, 0), 3)
    cv2.imwrite(str(path), img)


# --------------------------------------------------------------------------
# displacement measurement
# --------------------------------------------------------------------------

def hann2d(shape):
    """Separable 2-D Hann window -- kills FFT edge leakage from the hard crop."""
    return np.outer(np.hanning(shape[0]), np.hanning(shape[1]))


def _prep(a):
    """Mean-remove and Hann-window a patch for phase correlation."""
    a = a.astype(np.float64)
    a = a - a.mean()
    return a * hann2d(a.shape)


def correlate(ref, cur, upsample):
    """Displacement of the pattern from ref to cur, in px, as (dy, dx).

    skimage returns the shift needed to register the moving image back onto the
    reference, which is the negative of how far the pattern actually travelled.
    Negating here means every number downstream is a real displacement.
    """
    result = phase_cross_correlation(_prep(ref), _prep(cur),
                                     upsample_factor=upsample,
                                     normalization=None)
    shift = result[0] if isinstance(result, tuple) else result
    return -float(shift[0]), -float(shift[1])


#: Inset of the coarse-estimate ROI inside the detected block edges. Keeps the
#: stationary bench and paper margin out of the coarse correlation entirely.
COARSE_INSET_PX = 30


def block_roi(edges, rows, shape, inset=COARSE_INSET_PX):
    """(y0, y1, x0, x1) strictly inside the detected block, inset by `inset` px."""
    h, w = shape
    y0 = max(0, int(rows[0]) + inset)
    y1 = min(h, int(rows[1]) - inset)
    x0 = max(0, int(edges["left_px"]) + inset)
    x1 = min(w, int(edges["right_px"]) - inset)
    return y0, y1, x0, x1


def coarse_shift(ref, cur, roi=None):
    """Integer displacement (dy, dx), estimated inside `roi` when given.

    The real move is ~128 px, far outside any subset's correlation range, so it
    has to be taken out globally before subpixel refinement means anything.

    The estimate is restricted to the speckle block ROI: a full-frame
    correlation mixes in the stationary wood bench, whose zero-shift peak drags
    the estimate away from the block's true motion.
    """
    if roi is not None:
        y0, y1, x0, x1 = roi
        ref = ref[y0:y1, x0:x1]
        cur = cur[y0:y1, x0:x1]
    dy, dx = correlate(ref, cur, 1)
    return int(round(dy)), int(round(dx))


def subset_boxes(edges, rows, shape):
    """Grid of subset boxes inset inside the detected block."""
    x0b, x1b = edges["left_px"], edges["right_px"]
    y0b, y1b = rows
    inset_x = (x1b - x0b) * BLOCK_INSET
    inset_y = (y1b - y0b) * BLOCK_INSET
    xs = np.linspace(x0b + inset_x, x1b - inset_x, GRID_X + 1).astype(int)
    ys = np.linspace(y0b + inset_y, y1b - inset_y, GRID_Y + 1).astype(int)
    return [(ys[j], ys[j + 1], xs[i], xs[i + 1])
            for j in range(GRID_Y) for i in range(GRID_X)]


def refine_on_subsets(ref, cur, boxes, dy_c, dx_c):
    """Subpixel refinement per subset after removing the coarse shift.

    The deformed patch is pulled from the coarse-shifted location, so what is
    left to measure is a sub-pixel residual. Subsets whose shifted box would
    fall outside the frame are skipped rather than clamped -- a clamped box
    compares different pieces of the pattern and reports nonsense.
    """
    h, w = ref.shape
    out = []
    skipped = 0
    for (y0, y1, x0, x1) in boxes:
        sy0, sy1 = y0 + dy_c, y1 + dy_c
        sx0, sx1 = x0 + dx_c, x1 + dx_c
        if sy0 < 0 or sx0 < 0 or sy1 > h or sx1 > w:
            skipped += 1
            continue
        rdy, rdx = correlate(ref[y0:y1, x0:x1], cur[sy0:sy1, sx0:sx1], UPSAMPLE)
        out.append((dy_c + rdy, dx_c + rdx))
    return np.array(out), skipped


def wait_for_sentinel(cap, timeout_s=WAIT_TIMEOUT_S):
    """Keep the stream alive while waiting for the operator's go-ahead.

    Frames are pulled the whole time. If the stream were left idle the next
    read could hand back a frame buffered from before the slide, which would
    read as zero displacement.
    """
    print("\n  waiting for {} ...".format(SENTINEL.name))
    start = time.perf_counter()
    grabs = failed = 0
    means = []
    while not SENTINEL.exists():
        if time.perf_counter() - start > timeout_s:
            return False, {"grabs": grabs, "failed": failed}
        ok, frame = safe_read(cap)
        grabs += 1
        if not ok:
            failed += 1
            continue
        if grabs % 30 == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            means.append(float(gray.mean()))
    elapsed = time.perf_counter() - start
    drift = 0.0
    if means:
        drift = (max(means) - min(means)) / (sum(means) / len(means))
    print("  go-ahead seen after {:.1f}s ({} grabs, {} failed, "
          "intensity drift during wait {:.2f}%)".format(
              elapsed, grabs, failed, 100 * drift))
    return True, {"grabs": grabs, "failed": failed, "wait_s": elapsed,
                  "wait_intensity_drift": drift}


# --------------------------------------------------------------------------
# sub-commands
# --------------------------------------------------------------------------

def cmd_scale(args):
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    print("Opening camera and locking automatic controls...")
    cap, info = open_locked_camera()
    if cap is None:
        print("No camera opened. Run lock_and_preview.py to diagnose.")
        return 1

    still, stats = grab_still(cap)
    cap.release()
    cv2.imwrite(str(TRIALS_DIR / "scale_still.png"), still.astype(np.uint8))
    print_capture_diagnostics(capture_diagnostics(stats))

    edges = detect_block_edges(still)
    if not edges["ok"]:
        print("EDGE DETECTION FAILED: {}".format(edges["reason"]))
        return 1

    rows = detect_block_rows(still, edges)
    paper_left = edges["paper_left_px"]
    extra = [(paper_left, (255, 128, 0), "paper L"),
             (edges["paper_right_px"], (255, 128, 0), "paper R")]
    save_edge_overlay(still, edges, TRIALS_DIR / "scale_edges.png", extra)

    mm_per_px = BLOCK_WIDTH_MM / edges["span_px"]
    print("\n" + "=" * 62)
    print("BLOCK EDGE DETECTION")
    print("  left edge      : x = {:.2f} px   (20-80% transition {:.1f} px)".format(
        edges["left_px"], edges["left_transition_px"]))
    print("  right edge     : x = {:.2f} px   (20-80% transition {:.1f} px)".format(
        edges["right_px"], edges["right_transition_px"]))
    print("  span           : {:.2f} px  ==  {:.1f} mm (trusted)".format(
        edges["span_px"], BLOCK_WIDTH_MM))
    print("  SCALE          : {:.6f} mm/px   ({:.3f} px/mm)".format(
        mm_per_px, 1.0 / mm_per_px))
    print("  block rows     : y = {:.0f} .. {:.0f}".format(rows[0], rows[1]))
    print("  paper sheet    : x = {:.2f} .. {:.2f} px  (margins {:.0f} px left, "
          "{:.0f} px right)".format(
              edges["paper_left_px"], edges["paper_right_px"],
              edges["left_px"] - edges["paper_left_px"],
              edges["paper_right_px"] - edges["right_px"]))
    print("  {} mm expected : {:.1f} px".format(KNOWN_MM, KNOWN_MM / mm_per_px))
    if edges["ambiguous"]:
        print("\n  *** EDGES AMBIGUOUS -- soft transition, operator must confirm ***")
    print("=" * 62)

    SCALE_PATH.write_text(json.dumps({
        "mm_per_px": mm_per_px,
        "span_px": edges["span_px"],
        "left_px": edges["left_px"],
        "right_px": edges["right_px"],
        "left_transition_px": edges["left_transition_px"],
        "right_transition_px": edges["right_transition_px"],
        "block_rows": [float(rows[0]), float(rows[1])],
        "paper_left_px": paper_left,
        "paper_right_px": edges["paper_right_px"],
        "ambiguous": edges["ambiguous"],
        "block_width_mm": BLOCK_WIDTH_MM,
        "resolution": [info["width"], info["height"]],
    }, indent=2), encoding="utf-8")
    print("\nWrote {}  and  scale_edges.png".format(SCALE_PATH.name))
    return 0


def detect_block_rows(gray, edges):
    """Top and bottom of the block, from the row texture profile inside its columns."""
    x0 = int(edges["left_px"] + 0.1 * edges["span_px"])
    x1 = int(edges["right_px"] - 0.1 * edges["span_px"])
    prof = row_texture(gray, x0, x1)
    smooth = cv2.GaussianBlur(prof.reshape(-1, 1), (1, 9), 0).ravel()
    peak = float(np.percentile(smooth, 99))
    threshold = EDGE_FRACTION * peak
    run = _longest_run(smooth > threshold, min_len=200)
    if run is None:
        return 0.0, float(gray.shape[0])
    return _refine_boundaries(smooth, threshold, run)


def cmd_run(args):
    if not SCALE_PATH.exists():
        print("No scale.json -- run 'scale' first.")
        return 1
    scale = json.loads(SCALE_PATH.read_text(encoding="utf-8"))
    mm_per_px = scale["mm_per_px"]

    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    if SENTINEL.exists():
        SENTINEL.unlink()

    print("Opening camera and locking automatic controls...")
    cap, info = open_locked_camera()
    if cap is None:
        print("No camera opened. Run lock_and_preview.py to diagnose.")
        return 1

    print("\nCapturing REFERENCE frame (trial {})...".format(args.trial))
    ref, ref_stats = grab_still(cap)
    cv2.imwrite(str(TRIALS_DIR / "trial{}_reference.png".format(args.trial)),
                ref.astype(np.uint8))
    ref_diag = capture_diagnostics(ref_stats)
    print_capture_diagnostics(ref_diag)

    print("\n>>> SLIDE THE TRAY {} mm NOW <<<".format(KNOWN_MM))
    ok, wait_stats = wait_for_sentinel(cap)
    if not ok:
        cap.release()
        print("Timed out waiting for the go-ahead. Nothing measured.")
        return 1

    print("\nCapturing DEFORMED frame (trial {})...".format(args.trial))
    cur, cur_stats = grab_still(cap)
    cap.release()
    cv2.imwrite(str(TRIALS_DIR / "trial{}_deformed.png".format(args.trial)),
                cur.astype(np.uint8))
    cur_diag = capture_diagnostics(cur_stats)
    print_capture_diagnostics(cur_diag)

    # --- coarse, then subpixel refinement on subsets ---
    edges = {"left_px": scale["left_px"], "right_px": scale["right_px"],
             "span_px": scale["span_px"]}
    rows = tuple(scale["block_rows"])
    roi = block_roi(edges, rows, ref.shape)
    dy_c, dx_c = coarse_shift(ref, cur, roi)
    print("\nCoarse block-ROI shift: dx = {:+d} px, dy = {:+d} px".format(dx_c, dy_c))

    boxes = subset_boxes(edges, rows, ref.shape)
    shifts, skipped = refine_on_subsets(ref, cur, boxes, dy_c, dx_c)
    if shifts.size == 0:
        print("Every subset fell outside the frame after the shift. Nothing measured.")
        return 1

    dy, dx = shifts[:, 0], shifts[:, 1]
    dx_mm = dx * mm_per_px
    dy_mm = dy * mm_per_px
    mag_mm = np.hypot(dx_mm, dy_mm)

    # --- tick-mark cross-check, strictly inside the paper margin ---
    tick = tick_cross_check(ref, cur, scale, dy_c, dx_c)

    print("\n" + "=" * 66)
    print("TRIAL {} -- known displacement {:.1f} +/- {:.1f} mm".format(
        args.trial, KNOWN_MM, KNOWN_TOL_MM))
    print("  subsets used   : {} of {} ({} skipped at frame edge)".format(
        len(shifts), len(boxes), skipped))
    print("  dx  {:+.3f} px  std {:.3f} px   ->  {:+.3f} mm  std {:.3f} mm".format(
        dx.mean(), dx.std(ddof=1), dx_mm.mean(), dx_mm.std(ddof=1)))
    print("  dy  {:+.3f} px  std {:.3f} px   ->  {:+.3f} mm  std {:.3f} mm".format(
        dy.mean(), dy.std(ddof=1), dy_mm.mean(), dy_mm.std(ddof=1)))
    print("  |d| {:.3f} mm   (horizontal axis is the slide axis)".format(mag_mm.mean()))
    measured = abs(dx_mm.mean())
    error_mm = measured - KNOWN_MM
    error_pct = 100.0 * error_mm / KNOWN_MM
    print("  MEASURED       : {:.3f} mm along x".format(measured))
    print("  ERROR          : {:+.3f} mm  ({:+.2f}%)".format(error_mm, error_pct))
    print("  within ruler tolerance (+/- {:.1f} mm)? {}".format(
        KNOWN_TOL_MM, "YES" if abs(error_mm) <= KNOWN_TOL_MM else "NO"))
    print("  direction      : {}".format("+x (right)" if dx.mean() > 0 else "-x (left)"))
    if tick is not None:
        print("  tick-mark check: dx = {:+.3f} px vs speckle {:+.3f} px "
              "(difference {:+.3f} px = {:+.3f} mm)".format(
                  tick["dx_px"], dx.mean(), tick["dx_px"] - dx.mean(),
                  (tick["dx_px"] - dx.mean()) * mm_per_px))
    else:
        print("  tick-mark check: ROI unavailable")
    print("=" * 66)

    result = {
        "trial": args.trial,
        "known_mm": KNOWN_MM,
        "known_tol_mm": KNOWN_TOL_MM,
        "mm_per_px": mm_per_px,
        "coarse_dx_px": dx_c,
        "coarse_dy_px": dy_c,
        "n_subsets": int(len(shifts)),
        "n_skipped": int(skipped),
        "dx_px_mean": float(dx.mean()), "dx_px_std": float(dx.std(ddof=1)),
        "dy_px_mean": float(dy.mean()), "dy_px_std": float(dy.std(ddof=1)),
        "dx_mm_mean": float(dx_mm.mean()), "dx_mm_std": float(dx_mm.std(ddof=1)),
        "dy_mm_mean": float(dy_mm.mean()), "dy_mm_std": float(dy_mm.std(ddof=1)),
        "measured_mm": float(measured),
        "error_mm": float(error_mm),
        "error_pct": float(error_pct),
        "tick_check": tick,
        "reference_diagnostics": ref_diag,
        "deformed_diagnostics": cur_diag,
        "wait_stats": wait_stats,
        "per_subset_dx_px": dx.tolist(),
        "per_subset_dy_px": dy.tolist(),
    }
    out = TRIALS_DIR / "trial{}_result.json".format(args.trial)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\nWrote {}".format(out.name))
    if SENTINEL.exists():
        SENTINEL.unlink()
    return 0


def tick_cross_check(ref, cur, scale, dy_c, dx_c):
    """Correlate the printed tick-bar margin as an independent witness.

    The tick marks are printed on the same sheet, so they ride with the paper.
    If they report the same pixel shift as the speckle, the measurement is
    tracking the target rather than some artefact of the speckle correlation.

    The ROI is clamped strictly between the paper's left edge and the block's
    left edge: the bench behind the paper stays put while the tray moves, and
    any bench inside the ROI would drag the result toward zero.
    """
    paper_left = scale.get("paper_left_px")
    if paper_left is None:
        return None
    x0 = int(paper_left + 30)
    x1 = int(scale["left_px"] - 10)
    if x1 - x0 < 40:
        return None
    y0, y1 = int(scale["block_rows"][0]), int(scale["block_rows"][1])
    h, w = ref.shape
    sy0, sy1 = y0 + dy_c, y1 + dy_c
    sx0, sx1 = x0 + dx_c, x1 + dx_c
    if sy0 < 0 or sx0 < 0 or sy1 > h or sx1 > w:
        return None
    rdy, rdx = correlate(ref[y0:y1, x0:x1], cur[sy0:sy1, sx0:sx1], UPSAMPLE)
    return {"roi": [x0, x1, y0, y1],
            "dx_px": dx_c + rdx, "dy_px": dy_c + rdy}


def main():
    parser = argparse.ArgumentParser(description="Phase 3 known-displacement test")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scale", help="detect block edges and compute mm/px")
    run = sub.add_parser("run", help="reference + deformed capture and measurement")
    run.add_argument("--trial", type=int, default=1)
    args = parser.parse_args()
    return cmd_scale(args) if args.cmd == "scale" else cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
