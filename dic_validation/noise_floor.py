"""Phase 2 static noise-floor test.

Captures a series of frames of a rigidly mounted, stationary target and
correlates every later frame against frame 0. Nothing is moving, so the true
displacement is exactly zero and everything measured is noise -- sensor read
noise, lighting drift, mount vibration, and the subpixel algorithm's own bias.

Displacement is measured with skimage.registration.phase_cross_correlation at
upsample_factor=100 (i.e. 1/100 px quantisation) on a grid of subsets, so the
spread ACROSS subsets is also visible, not just a single global number.

Requires: pip install opencv-python numpy scikit-image
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from skimage.registration import phase_cross_correlation

sys.path.insert(0, str(Path(__file__).resolve().parent))
from camera_check import safe_read
from camera_lock import (capture_diagnostics, capture_series,
                         print_capture_diagnostics)
from lock_and_preview import open_locked_camera

FRAMES_DIR = Path(__file__).resolve().parent / "noise_frames"

N_FRAMES = 10
DURATION_S = 30.0
UPSAMPLE = 100

#: Subsets are laid out as GRID_X x GRID_Y over the ROI. A grid rather than one
#: whole-frame correlation: a single number hides whether error is uniform or
#: concentrated (e.g. one corner out of focus, or a vibrating edge of the mount).
GRID_X = 6
GRID_Y = 4

#: Fraction of the frame trimmed off each edge before gridding. Lens vignetting
#: and distortion are worst at the extreme edges and would bias the noise floor.
EDGE_TRIM = 0.10

#: Settling frames are pulled by open_locked_camera() once the auto controls are
#: disabled -- measuring through the post-lock transient would report the
#: camera's warm-up as if it were the noise floor.


def subset_bounds(shape):
    """Grid of (y0, y1, x0, x1) subset boxes over the trimmed ROI."""
    h, w = shape
    ty, tx = int(h * EDGE_TRIM), int(w * EDGE_TRIM)
    ys = np.linspace(ty, h - ty, GRID_Y + 1).astype(int)
    xs = np.linspace(tx, w - tx, GRID_X + 1).astype(int)
    return [(ys[j], ys[j + 1], xs[i], xs[i + 1])
            for j in range(GRID_Y) for i in range(GRID_X)]


def hann2d(shape):
    """Separable 2-D Hann window.

    Each subset is a hard-cropped rectangle, and its discontinuous edges leak
    across the whole spectrum in the FFT that phase correlation is built on.
    Tapering the edges to zero removes that leakage; without it the measured
    shift picks up a bias that has nothing to do with the actual motion.
    """
    wy = np.hanning(shape[0])
    wx = np.hanning(shape[1])
    return np.outer(wy, wx)


def measure_shifts(ref_gray, cur_gray, boxes):
    """Per-subset (dy, dx) in px between two frames. Returns an (N, 2) array."""
    out = []
    for (y0, y1, x0, x1) in boxes:
        a = ref_gray[y0:y1, x0:x1].astype(np.float64)
        b = cur_gray[y0:y1, x0:x1].astype(np.float64)
        # Remove each subset's mean so a uniform brightness change between
        # frames (flicker, AGC) does not masquerade as motion.
        a -= a.mean()
        b -= b.mean()
        win = hann2d(a.shape)
        result = phase_cross_correlation(a * win, b * win,
                                         upsample_factor=UPSAMPLE,
                                         normalization=None)
        shift = result[0] if isinstance(result, tuple) else result
        out.append((float(shift[0]), float(shift[1])))
    return np.array(out)


def report_subset_scatter(per_subset_dx, per_subset_dy):
    """Per-subset std laid out on the grid, to localise where the error lives.

    A single whole-frame number hides whether the error is uniform or
    concentrated. The target is known to be tilted, so the out-of-focus band
    should show up here as a high-scatter region rather than as a mystery in
    the global std.
    """
    scatter = np.hypot(per_subset_dx.std(axis=0, ddof=1),
                       per_subset_dy.std(axis=0, ddof=1))
    grid = scatter.reshape(GRID_Y, GRID_X)
    print("\nPer-subset scatter (std of |d| across frame pairs, px)")
    print("  rows = top to bottom of frame, cols = left to right")
    print("        " + "".join("{:>9d}".format(i) for i in range(GRID_X)))
    for j in range(GRID_Y):
        print("  row {} ".format(j) + "".join("{:>9.4f}".format(v) for v in grid[j]))
    worst = int(np.argmax(scatter))
    best = int(np.argmin(scatter))
    print("  best  subset {:2d} (row {}, col {}): {:.4f} px".format(
        best, best // GRID_X, best % GRID_X, scatter[best]))
    print("  worst subset {:2d} (row {}, col {}): {:.4f} px  ({:.1f}x the best)".format(
        worst, worst // GRID_X, worst % GRID_X, scatter[worst],
        scatter[worst] / scatter[best] if scatter[best] > 0 else float("inf")))
    return grid


def main():
    parser = argparse.ArgumentParser(description="Phase 2 static noise-floor test")
    parser.add_argument("--frames", type=int, default=N_FRAMES)
    parser.add_argument("--duration", type=float, default=DURATION_S)
    args = parser.parse_args()

    print("Opening camera and locking automatic controls...")
    cap, info = open_locked_camera()
    if cap is None:
        print("No camera opened. Run lock_and_preview.py to diagnose.")
        return 1
    w, h = info["width"], info["height"]

    print("\nCapturing {} frames over {:.0f}s -- NOTHING must move.".format(
        args.frames, args.duration))
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    def on_frame(s):
        print("  frame {:2d}  t = {:6.2f}s  mean intensity {:.1f}  "
              "grabs {:4d}  failed {:d}".format(
                  s["index"], s["t"], s["mean_intensity"], s["grabs"],
                  s["failed_grabs"]))

    frames, stats = capture_series(cap, args.frames, args.duration, safe_read,
                                   on_frame=on_frame)
    cap.release()
    for i, frame in enumerate(frames):
        cv2.imwrite(str(FRAMES_DIR / "frame_{:02d}.png".format(i)), frame)
    stamps = [s["t"] for s in stats]

    print("\nCapture diagnostics")
    diag = capture_diagnostics(stats)
    print_capture_diagnostics(diag)

    boxes = subset_bounds(frames[0].shape)
    print("\nCorrelating frame 0 vs each later frame")
    print("({}x{} = {} subsets of ~{}x{} px, upsample_factor={})\n".format(
        GRID_X, GRID_Y, len(boxes),
        boxes[0][3] - boxes[0][2], boxes[0][1] - boxes[0][0], UPSAMPLE))

    print("  {:>5} {:>8}  {:>9} {:>9} {:>9} {:>9}".format(
        "frame", "t (s)", "mean dx", "mean dy", "mean |d|", "max |d|"))
    all_dy, all_dx = [], []
    for i in range(1, len(frames)):
        shifts = measure_shifts(frames[0], frames[i], boxes)
        dy, dx = shifts[:, 0], shifts[:, 1]
        mag = np.hypot(dx, dy)
        all_dy.append(dy)
        all_dx.append(dx)
        print("  {:>5d} {:>8.2f}  {:>9.4f} {:>9.4f} {:>9.4f} {:>9.4f}".format(
            i, stamps[i], dx.mean(), dy.mean(), mag.mean(), mag.max()))

    per_subset_dy = np.array(all_dy)          # (n_pairs, n_subsets)
    per_subset_dx = np.array(all_dx)
    dy = np.concatenate(all_dy)
    dx = np.concatenate(all_dx)
    mag = np.hypot(dx, dy)

    print("\n" + "=" * 62)
    print("NOISE FLOOR  ({} measurements = {} subsets x {} frame pairs)".format(
        dx.size, len(boxes), len(frames) - 1))
    print("  dx   mean {:+.4f} px   std {:.4f} px".format(dx.mean(), dx.std(ddof=1)))
    print("  dy   mean {:+.4f} px   std {:.4f} px".format(dy.mean(), dy.std(ddof=1)))
    print("  |d|  mean {:.4f} px    max {:.4f} px".format(mag.mean(), mag.max()))
    worst = max(dx.std(ddof=1), dy.std(ddof=1))
    print("  worst-axis std: {:.4f} px".format(worst))
    if worst > 0.1:
        print("\n  *** std > 0.1 px -- WEAK subpixel performance ***")
    else:
        print("\n  std <= 0.1 px -- subpixel performance OK")
    print("=" * 62)

    scatter_grid = report_subset_scatter(per_subset_dx, per_subset_dy)

    np.savez(str(FRAMES_DIR / "noise_floor.npz"), dx=dx, dy=dy,
             per_subset_dx=per_subset_dx, per_subset_dy=per_subset_dy,
             scatter_grid=scatter_grid, stamps=np.array(stamps),
             width=w, height=h,
             drop_rate=diag["drop_rate"], intensity_drift=diag["intensity_drift"])
    print("\nFrames and raw results saved to {}".format(FRAMES_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
