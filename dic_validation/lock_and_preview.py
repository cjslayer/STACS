"""Step 1: lock the camera's automatic controls, then show a live preview.

Opens the ArduCam at its maximum working mode, disables autofocus /
auto-exposure / auto-white-balance, reports which of those actually took, and
puts a live window up so the operator can confirm framing and stillness before
any measurement runs.

The lock state is written to camera_state.json so Phase 2 and Phase 3 report
the same settings without re-probing.

Requires: pip install opencv-python numpy
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import cv2

from camera_check import (BACKENDS, INDICES, find_max_resolution, safe_read,
                          try_open)
from camera_lock import (capture_diagnostics, capture_series, lock_auto_controls,
                         print_capture_diagnostics, print_lock_report, settle)

HERE = Path(__file__).resolve().parent
CAPTURE_DIR = HERE / "captures"
STATE_PATH = HERE / "camera_state.json"

SETTLE_FRAMES = 45
FPS_MEASURE_SECONDS = 5.0
DIAG_FRAMES = 10
DIAG_SECONDS = 5.0


def open_locked_camera(verbose=True):
    """Find the camera, take it to max resolution, lock auto controls.

    Returns (cap, info) where info carries index/backend/resolution/fps and the
    per-property lock results. Returns (None, info) if nothing opened, with
    info["tried"] holding the full probe record.

    Probe policy is one pass over indices 0-3 x MSMF/DSHOW and then stop -- no
    retry loops, so a dead camera fails fast and visibly.
    """
    tried = []
    cap = index = backend_name = None
    for idx in INDICES:
        for name, flag in BACKENDS:
            found = try_open(idx, flag)
            tried.append({"index": idx, "backend": name,
                          "result": "OPENED" if found else "failed"})
            if verbose:
                print("  index {} / {:<9} {}".format(
                    idx, name, "OPENED" if found else "failed"))
            if found is not None:
                cap, index, backend_name = found, idx, name
                break
        if cap is not None:
            break
    if cap is None:
        return None, {"tried": tried}
    cap.release()

    cap, w, h = find_max_resolution(index, dict(BACKENDS)[backend_name], verbose=False)
    if cap is None:
        return None, {"tried": tried, "index": index, "backend": backend_name}

    if verbose:
        print("\nOpened index {} / {} at {}x{}".format(index, backend_name, w, h))
        print("\nLocking automatic controls...")
    lock_results = lock_auto_controls(cap)
    if verbose:
        print_lock_report(lock_results)

    settle(cap, SETTLE_FRAMES, safe_read)

    info = {
        "tried": tried,
        "index": index,
        "backend": backend_name,
        "width": w,
        "height": h,
        "advertised_fps": float(cap.get(cv2.CAP_PROP_FPS)),
        "lock_results": lock_results,
    }
    return cap, info


def measure_fps(cap, seconds=FPS_MEASURE_SECONDS):
    """Delivered frame rate -- what the camera actually hands over, not the claim."""
    frames = failures = 0
    start = time.perf_counter()
    while time.perf_counter() - start < seconds:
        ok, _ = safe_read(cap)
        if ok:
            frames += 1
        else:
            failures += 1
    elapsed = time.perf_counter() - start
    return (frames / elapsed if elapsed > 0 else 0.0), frames, failures, elapsed


def preview(cap, info):
    """Live preview so the operator can confirm framing and stillness.

    's' saves a still, 'q' closes the window. The overlay carries mean intensity
    so a drifting exposure is visible live rather than only in the log.
    """
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    window = "DIC preview -- index {} ({}) {}x{}  |  's' save, 'q' continue".format(
        info["index"], info["backend"], info["width"], info["height"])
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 1280, int(1280 * info["height"] / max(1, info["width"])))
    saved = []
    print("\nLive preview open. Confirm the speckle target is framed landscape,")
    print("fully inside the frame with white margin visible on left and right,")
    print("and that nothing is moving.")
    print("  's' = save a still to {}".format(CAPTURE_DIR))
    print("  'q' = close the preview")
    while True:
        ok, frame = safe_read(cap)
        if not ok:
            continue
        shown = frame.copy()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        cv2.putText(shown, "mean {:.1f}   {}x{}".format(
            gray.mean(), info["width"], info["height"]),
            (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 255, 0), 3)
        cv2.imshow(window, shown)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path = CAPTURE_DIR / "locked_{}.png".format(stamp)
            # PNG, never JPEG: JPEG ringing would corrupt subpixel correlation.
            cv2.imwrite(str(path), frame)
            saved.append(str(path))
            print("  saved {}".format(path.name))
    cv2.destroyAllWindows()
    return saved


def main():
    parser = argparse.ArgumentParser(description="Step 1 camera lock + preview")
    parser.add_argument("--no-preview", action="store_true",
                        help="lock and report only, skip the live window")
    args = parser.parse_args()

    print("Probing camera index/backend combinations...")
    cap, info = open_locked_camera()
    if cap is None:
        print("\nNO CAMERA OPENED. Tried:")
        for t in info["tried"]:
            print("  index {} / {:<9} {}".format(t["index"], t["backend"], t["result"]))
        print("\nStopping -- no retry loop. Check Device Manager / Windows Camera app.")
        return 1

    print("\nMeasuring delivered frame rate for {:.0f}s...".format(FPS_MEASURE_SECONDS))
    fps, n_frames, failures, elapsed = measure_fps(cap)
    info["measured_fps"] = fps

    print("\nCapture diagnostics ({} frames over {:.0f}s, locked)...".format(
        DIAG_FRAMES, DIAG_SECONDS))
    _, stats = capture_series(cap, DIAG_FRAMES, DIAG_SECONDS, safe_read)
    diag = capture_diagnostics(stats)
    print_capture_diagnostics(diag)
    info["startup_diagnostics"] = diag

    print("\n" + "=" * 62)
    print("Index/backend  : {} / {}".format(info["index"], info["backend"]))
    print("Resolution     : {}x{}".format(info["width"], info["height"]))
    print("Advertised FPS : {:.2f} (driver claim)".format(info["advertised_fps"]))
    print("Measured FPS   : {:.2f}  ({} frames / {:.2f}s, {} failed grabs)".format(
        fps, n_frames, elapsed, failures))
    print("Auto controls  : " + ", ".join(
        "{}={}".format(r["name"].replace("CAP_PROP_", ""), r["status"])
        for r in info["lock_results"]))
    print("=" * 62)

    if not args.no_preview:
        info["preview_stills"] = preview(cap, info)

    cap.release()
    STATE_PATH.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print("\nCamera state written to {}".format(STATE_PATH.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
