"""Step-1 verification: does the block-ROI coarse fix explain the ~8 px offset?

Runs entirely offline on the existing trial-1 image pair. Three independent
views of the same displacement field:

  1. the DIC pipeline (coarse + per-subset refinement), run both the OLD way
     (full-frame coarse, bench included) and the NEW way (block-ROI coarse);
  2. geometric features -- the block's left/right edges and the printed tick
     bar, each detected independently in the reference and deformed frames, and
     a straight line dx(x) fitted through those three points (trial 1 had a
     pose change, so dx varies linearly with x);
  3. cv2.matchTemplate (normalised cross-correlation, a different algorithm
     family from phase correlation) on 3 subsets.

Pass criterion: NEW per-column dx agrees with the geometric line to within
1 px at each column's x.

Requires: pip install opencv-python numpy scikit-image
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase3_displacement import (GRID_X, GRID_Y, block_roi, coarse_shift,
                                 detect_block_edges, detect_block_rows,
                                 refine_on_subsets, subset_boxes)

HERE = Path(__file__).resolve().parent
TRIALS = HERE / "phase3"

AGREE_LIMIT_PX = 1.0


def load_gray(path):
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise SystemExit("cannot read {}".format(path))
    return img.astype(np.float64)


def tick_center_x(gray, paper_left, block_left, rows):
    """Subpixel x of the printed tick bar, from the margin texture centroid.

    The tick bar is the only textured feature between the sheet's left edge and
    the block's left edge, so the centroid of above-half-max texture columns in
    that window is the bar's position. Detected independently per frame, in
    that frame's own detected coordinates -- no correlation involved.
    """
    x0 = int(paper_left) + 15
    x1 = int(block_left) - 10
    y0, y1 = int(rows[0]), int(rows[1])
    band = gray[y0:y1, x0:x1]
    prof = band.std(axis=0)
    base = float(np.median(prof))
    lifted = np.clip(prof - base, 0.0, None)
    peak = float(lifted.max())
    if peak <= 0:
        return None
    mask = lifted > 0.5 * peak
    idx = np.arange(len(prof), dtype=np.float64)
    w = np.where(mask, lifted, 0.0)
    return x0 + float((idx * w).sum() / w.sum())


def column_means(shifts_dx, used_mask):
    """Per-column mean dx over the GRID_Y rows, honouring skipped subsets."""
    dx_grid = np.full(GRID_Y * GRID_X, np.nan)
    dx_grid[used_mask] = shifts_dx
    dx_grid = dx_grid.reshape(GRID_Y, GRID_X)
    return np.nanmean(dx_grid, axis=0)


def run_pipeline(ref, cur, edges, rows, roi):
    """Coarse (optionally ROI-restricted) + per-subset refinement. Per-col dx."""
    dy_c, dx_c = coarse_shift(ref, cur, roi)
    boxes = subset_boxes(edges, rows, ref.shape)
    shifts, skipped = refine_on_subsets(ref, cur, boxes, dy_c, dx_c)
    # refine_on_subsets drops skipped boxes; rebuild which indices survived.
    used = []
    h, w = ref.shape
    for i, (y0, y1, x0, x1) in enumerate(boxes):
        if not (y0 + dy_c < 0 or x0 + dx_c < 0 or y1 + dy_c > h or x1 + dx_c > w):
            used.append(i)
    used_mask = np.zeros(len(boxes), dtype=bool)
    used_mask[used] = True
    col_dx = column_means(shifts[:, 1], used_mask)
    col_x = np.array([(boxes[i][2] + boxes[i][3]) / 2.0 for i in range(GRID_X)])
    return {"coarse": (dy_c, dx_c), "shifts": shifts, "skipped": skipped,
            "col_dx": col_dx, "col_x": col_x}


def match_template_dx(ref, cur, box, dx_hint, dy_hint, pad_x=80, pad_y=40):
    """Subpixel dx/dy of one subset via cv2.matchTemplate (TM_CCOEFF_NORMED)."""
    y0, y1, x0, x1 = box
    tmpl = ref[y0:y1, x0:x1].astype(np.float32)
    h, w = cur.shape
    sy0 = max(0, y0 + dy_hint - pad_y)
    sy1 = min(h, y1 + dy_hint + pad_y)
    sx0 = max(0, x0 + dx_hint - pad_x)
    sx1 = min(w, x1 + dx_hint + pad_x)
    search = cur[sy0:sy1, sx0:sx1].astype(np.float32)
    res = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, (px, py) = cv2.minMaxLoc(res)

    def parabola(v_m, v_0, v_p):
        denom = v_m - 2 * v_0 + v_p
        if denom == 0:
            return 0.0
        return 0.5 * (v_m - v_p) / denom

    fx = fy = 0.0
    if 0 < px < res.shape[1] - 1:
        fx = parabola(res[py, px - 1], res[py, px], res[py, px + 1])
    if 0 < py < res.shape[0] - 1:
        fy = parabola(res[py - 1, px], res[py, px], res[py + 1, px])
    dx = (sx0 + px + fx) - x0
    dy = (sy0 + py + fy) - y0
    return dx, dy, float(score)


def main():
    scale = json.loads((TRIALS / "scale.json").read_text(encoding="utf-8"))
    ref = load_gray(TRIALS / "trial1_reference.png")
    cur = load_gray(TRIALS / "trial1_deformed.png")

    edges = {"left_px": scale["left_px"], "right_px": scale["right_px"],
             "span_px": scale["span_px"]}
    rows = tuple(scale["block_rows"])

    # --- pipeline, old vs new coarse ---
    old = run_pipeline(ref, cur, edges, rows, roi=None)
    roi = block_roi(edges, rows, ref.shape)
    new = run_pipeline(ref, cur, edges, rows, roi=roi)

    print("=" * 70)
    print("COARSE SHIFT")
    print("  old (full frame, bench included) : dx = {:+d} px, dy = {:+d} px".format(
        old["coarse"][1], old["coarse"][0]))
    print("  new (block ROI {})    : dx = {:+d} px, dy = {:+d} px".format(
        roi, new["coarse"][1], new["coarse"][0]))

    # --- geometric features, detected independently in each frame ---
    e_ref = detect_block_edges(ref)
    e_cur = detect_block_edges(cur)
    if not (e_ref["ok"] and e_cur["ok"]):
        print("GEOMETRIC EDGE DETECTION FAILED: ref ok={} cur ok={}".format(
            e_ref.get("ok"), e_cur.get("ok")))
        return 1
    r_ref = detect_block_rows(ref, e_ref)
    r_cur = detect_block_rows(cur, e_cur)
    t_ref = tick_center_x(ref, e_ref["paper_left_px"], e_ref["left_px"], r_ref)
    t_cur = tick_center_x(cur, e_cur["paper_left_px"], e_cur["left_px"], r_cur)

    span_change = 100.0 * (e_cur["span_px"] - e_ref["span_px"]) / e_ref["span_px"]
    print("\nGEOMETRIC FEATURES (independent detection per frame)")
    print("  block left  : {:8.2f} -> {:8.2f}   dx = {:+7.2f} px".format(
        e_ref["left_px"], e_cur["left_px"], e_cur["left_px"] - e_ref["left_px"]))
    print("  block right : {:8.2f} -> {:8.2f}   dx = {:+7.2f} px".format(
        e_ref["right_px"], e_cur["right_px"], e_cur["right_px"] - e_ref["right_px"]))
    if t_ref is not None and t_cur is not None:
        print("  tick bar    : {:8.2f} -> {:8.2f}   dx = {:+7.2f} px".format(
            t_ref, t_cur, t_cur - t_ref))
    print("  span        : {:8.2f} -> {:8.2f}   ({:+.2f}%)".format(
        e_ref["span_px"], e_cur["span_px"], span_change))

    pts_x = [e_ref["left_px"], e_ref["right_px"]]
    pts_dx = [e_cur["left_px"] - e_ref["left_px"],
              e_cur["right_px"] - e_ref["right_px"]]
    if t_ref is not None and t_cur is not None:
        pts_x.insert(0, t_ref)
        pts_dx.insert(0, t_cur - t_ref)
    slope, intercept = np.polyfit(pts_x, pts_dx, 1)
    resid = np.array(pts_dx) - (slope * np.array(pts_x) + intercept)
    print("  line fit dx(x) = {:+.6f} * x {:+.3f}   (point residuals: {})".format(
        slope, intercept, ", ".join("{:+.2f}".format(r) for r in resid)))

    # A pose change is a projective map, not a shear: dx(x) is curved. With all
    # three anchors available, fit x' = (a x + b) / (c x + 1) exactly through
    # them -- the correct geometric model to hold the speckle field against.
    homog = None
    if len(pts_x) == 3:
        xs = np.array(pts_x)
        x2 = xs + np.array(pts_dx)
        A = np.column_stack([xs, np.ones(3), -xs * x2])
        ha, hb, hc = np.linalg.solve(A, x2)
        homog = (float(ha), float(hb), float(hc))
        print("  homography fit x' = ({:.6f} x {:+.3f}) / ({:.3e} x + 1)".format(
            ha, hb, hc))

    def geom_dx(x):
        if homog is not None:
            ha, hb, hc = homog
            return (ha * x + hb) / (hc * x + 1) - x
        return slope * x + intercept

    # --- per-column comparison against the geometric model ---
    model_name = "homography" if homog is not None else "line"
    print("\nPER-COLUMN dx vs GEOMETRIC {}  (pass: |new - geom| <= {} px)".format(
        model_name.upper(), AGREE_LIMIT_PX))
    print("  {:>8} {:>10} {:>10} {:>10} {:>10} {:>10} {:>8}".format(
        "col x", "old dx", "new dx", "line dx", "geom dx", "new-geom", "pass"))
    diffs = []
    for i in range(GRID_X):
        x = new["col_x"][i]
        line_dx = slope * x + intercept
        d = new["col_dx"][i] - geom_dx(x)
        diffs.append(d)
        print("  {:>8.0f} {:>10.2f} {:>10.2f} {:>10.2f} {:>10.2f} {:>10.2f} {:>8}".format(
            x, old["col_dx"][i], new["col_dx"][i], line_dx, geom_dx(x), d,
            "YES" if abs(d) <= AGREE_LIMIT_PX else "NO"))
    max_abs = float(np.max(np.abs(diffs)))
    old_mean = float(np.nanmean(old["col_dx"]))
    new_mean = float(np.nanmean(new["col_dx"]))
    geom_mean = float(np.mean([geom_dx(x) for x in new["col_x"]]))
    print("  means: old {:+.2f}   new {:+.2f}   geom {:+.2f}   "
          "(old-geom {:+.2f}, new-geom {:+.2f})".format(
              old_mean, new_mean, geom_mean,
              old_mean - geom_mean, new_mean - geom_mean))

    # --- matchTemplate cross-check on 3 subsets (middle row, cols 0/3/5) ---
    boxes = subset_boxes(edges, rows, ref.shape)
    picks = [1 * GRID_X + 0, 1 * GRID_X + 3, 1 * GRID_X + 5]
    dy_c, dx_c = new["coarse"]
    print("\nMATCHTEMPLATE CROSS-CHECK (TM_CCOEFF_NORMED, parabola subpixel)")
    print("  {:>8} {:>10} {:>10} {:>10} {:>10} {:>7}".format(
        "col x", "mt dx", "phase dx", "geom dx", "mt-phase", "score"))
    mt_out = []
    for p in picks:
        box = boxes[p]
        cx = (box[2] + box[3]) / 2.0
        dx_mt, dy_mt, score = match_template_dx(ref, cur, box, dx_c, dy_c)
        col = p % GRID_X
        mt_out.append({"col_x": cx, "dx_px": dx_mt, "dy_px": dy_mt,
                       "score": score, "geom_dx": geom_dx(cx),
                       "phase_dx": float(new["col_dx"][col])})
        print("  {:>8.0f} {:>10.2f} {:>10.2f} {:>10.2f} {:>10.2f} {:>7.3f}".format(
            cx, dx_mt, new["col_dx"][col], geom_dx(cx),
            dx_mt - new["col_dx"][col], score))

    verdict = max_abs <= AGREE_LIMIT_PX
    print("\n" + "=" * 70)
    print("VERDICT: max |new per-column dx - geometric {}| = {:.2f} px -> {}".format(
        model_name, max_abs, "PASS" if verdict else "FAIL"))
    print("=" * 70)

    out = {
        "old_coarse_dx": old["coarse"][1], "old_coarse_dy": old["coarse"][0],
        "new_coarse_dx": new["coarse"][1], "new_coarse_dy": new["coarse"][0],
        "roi": list(roi),
        "geometric": {
            "left_dx": pts_dx[-2], "right_dx": pts_dx[-1],
            "tick_dx": (pts_dx[0] if len(pts_dx) == 3 else None),
            "tick_x_ref": t_ref, "tick_x_cur": t_cur,
            "span_ref_px": e_ref["span_px"], "span_cur_px": e_cur["span_px"],
            "span_change_pct": span_change,
            "line_slope": float(slope), "line_intercept": float(intercept),
            "homography_abc": homog,
        },
        "geom_model": model_name,
        "col_x": new["col_x"].tolist(),
        "old_col_dx": old["col_dx"].tolist(),
        "new_col_dx": new["col_dx"].tolist(),
        "new_minus_geom": [float(d) for d in diffs],
        "max_abs_diff_px": max_abs,
        "match_template": mt_out,
        "pass": bool(verdict),
    }
    (TRIALS / "coarse_fix_verify.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print("\nWrote {}".format(TRIALS / "coarse_fix_verify.json"))
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
