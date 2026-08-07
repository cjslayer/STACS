"""Phase 1 camera bring-up check for the DIC validation session.

Finds the ArduCam, reports what it actually delivers (not what we asked for),
and gives a live preview so the operator can confirm framing and focus.

Probe order is index 0 / CAP_MSMF first -- the built-in webcam is disabled in
Device Manager, so the ArduCam should be index 0, and MSMF is the modern
Windows capture stack. CAP_DSHOW is the fallback that tends to work when MSMF
stalls on cheap UVC bridges.

Requires: pip install opencv-python
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

import cv2

CAPTURE_DIR = Path(__file__).resolve().parent / "captures"

#: Backends to try, in order, at each index.
BACKENDS = [("CAP_MSMF", cv2.CAP_MSMF), ("CAP_DSHOW", cv2.CAP_DSHOW)]

#: Indices to try, in order. 0 first: the built-in webcam is disabled.
INDICES = [0, 1, 2, 3]

#: Candidate modes, largest first, for finding the camera's maximum resolution.
#: The usual trick -- request an absurd size and let the driver clamp down --
#: does not work here: MSMF accepts the request and then throws
#: "(-215:Assertion failed) _step >= minstep" on the next read. So we test
#: real modes instead and keep the largest one that actually delivers a frame.
CANDIDATE_MODES = [
    (4656, 3496),   # ArduCam 16MP
    (4208, 3120),   # 13MP
    (3840, 2160),   # 4K UHD
    (3264, 2448),   # 8MP
    (2592, 1944),   # 5MP
    (2048, 1536),   # 3MP
    (1920, 1080),
    (1600, 1200),
    (1280, 960),
    (1280, 720),
    (800, 600),
    (640, 480),
]

FPS_MEASURE_SECONDS = 5.0


def safe_read(cap):
    """cap.read() that never raises. Returns (ok, frame).

    MSMF can throw cv2.error out of read() when a mode was set that the driver
    reports as valid but cannot actually stream. Treat that as a failed grab.
    """
    try:
        ok, frame = cap.read()
    except cv2.error:
        return False, None
    if not ok or frame is None:
        return False, None
    return True, frame


def try_open(index, backend_flag):
    """Open one index/backend pair. Returns a live VideoCapture or None.

    isOpened() alone is not enough -- MSMF in particular will hand back an
    "open" capture for a device it cannot actually stream from, so we require
    a real frame before calling it a success.
    """
    cap = cv2.VideoCapture(index, backend_flag)
    if not cap.isOpened():
        cap.release()
        return None
    ok, _ = safe_read(cap)
    if not ok:
        cap.release()
        return None
    return cap


def find_camera():
    """Probe index/backend combinations. Returns (cap, index, backend_name, tried).

    ``tried`` is the full list of (index, backend, result) so a total failure
    can be reported precisely instead of just "no camera".
    """
    tried = []
    for index in INDICES:
        for name, flag in BACKENDS:
            cap = try_open(index, flag)
            tried.append((index, name, "OPENED" if cap else "failed"))
            print("  index {} / {:<9} {}".format(index, name, "OPENED" if cap else "failed"))
            if cap is not None:
                return cap, index, name, tried
    return None, None, None, tried


def actual_resolution(cap):
    """Resolution measured from a real frame, falling back to the property query.

    CAP_PROP_FRAME_WIDTH/HEIGHT can lie about what the driver settled on, so
    the frame's own shape is the authority.
    """
    ok, frame = safe_read(cap)
    if ok:
        h, w = frame.shape[:2]
        return w, h
    return int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))


def find_max_resolution(index, backend_flag, verbose=True):
    """Largest CANDIDATE_MODES entry the camera actually delivers.

    Each candidate gets a freshly opened capture: a rejected mode can leave the
    MSMF capture in a state where every later read fails, so reusing one handle
    would make the first failure poison all the smaller modes behind it.
    Returns (cap, width, height) with `cap` still open at that mode.
    """
    for want_w, want_h in CANDIDATE_MODES:
        cap = cv2.VideoCapture(index, backend_flag)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)
        # Two reads: the first may still be a frame from the previous mode.
        safe_read(cap)
        ok, frame = safe_read(cap)
        if ok:
            h, w = frame.shape[:2]
            if verbose:
                print("  requested {}x{} -> got {}x{}".format(want_w, want_h, w, h))
            return cap, w, h
        if verbose:
            print("  requested {}x{} -> failed".format(want_w, want_h))
        cap.release()
    return None, 0, 0


def measure_fps(cap, seconds=FPS_MEASURE_SECONDS):
    """Grab as fast as the camera allows for `seconds`. Returns (fps, n_frames, elapsed).

    This is delivered frame rate, which is what matters for capture timing --
    the driver's advertised CAP_PROP_FPS is reported separately and is often
    wrong.
    """
    frames = 0
    failures = 0
    start = time.perf_counter()
    while time.perf_counter() - start < seconds:
        ok, _ = safe_read(cap)
        if ok:
            frames += 1
        else:
            failures += 1
    elapsed = time.perf_counter() - start
    return frames / elapsed if elapsed > 0 else 0.0, frames, failures, elapsed


def preview(cap, index, backend_name):
    """Live preview. 's' saves a still to captures/, 'q' quits. Returns saved paths."""
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    window = "ArduCam index {} ({}) -- 's' save, 'q' quit".format(index, backend_name)
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    saved = []
    print("\nLive preview open. Focus the preview window, then:")
    print("  's' = save a still to {}".format(CAPTURE_DIR))
    print("  'q' = quit")
    while True:
        ok, frame = safe_read(cap)
        if not ok:
            print("  frame grab failed during preview")
            continue
        cv2.imshow(window, frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path = CAPTURE_DIR / "still_{}.png".format(stamp)
            # PNG, not JPEG: JPEG compression artifacts would corrupt subpixel DIC.
            cv2.imwrite(str(path), frame)
            saved.append(path)
            print("  saved {}".format(path.name))
    cv2.destroyAllWindows()
    return saved


def main():
    parser = argparse.ArgumentParser(description="Phase 1 camera bring-up check")
    parser.add_argument("--no-preview", action="store_true",
                        help="probe and measure only, skip the live preview window")
    args = parser.parse_args()

    print("Probing camera index/backend combinations...")
    cap, index, backend_name, tried = find_camera()

    if cap is None:
        print("\nNO CAMERA OPENED. Tried:")
        for i, name, result in tried:
            print("  index {} / {:<9} {}".format(i, name, result))
        print("\nNext step is a manual check -- open the Windows Camera app and")
        print("Device Manager to confirm the ArduCam enumerates at all.")
        return 1

    default_w, default_h = actual_resolution(cap)
    advertised_fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    print("\nFinding maximum resolution...")
    cap, max_w, max_h = find_max_resolution(index, dict(BACKENDS)[backend_name])
    if cap is None:
        print("Camera opened at {}x{} but no candidate mode could be re-opened.".format(
            default_w, default_h))
        return 1

    print("\nMeasuring delivered frame rate at {}x{} for {:.0f}s...".format(
        max_w, max_h, FPS_MEASURE_SECONDS))
    fps, n_frames, failures, elapsed = measure_fps(cap)

    print("\n" + "=" * 58)
    print("Index/backend    : {} / {}".format(index, backend_name))
    print("Default mode     : {}x{}".format(default_w, default_h))
    print("Max mode         : {}x{}".format(max_w, max_h))
    print("Advertised FPS   : {:.2f} (driver claim, often wrong)".format(advertised_fps))
    print("Measured FPS     : {:.2f}  ({} frames / {:.2f}s, {} failed grabs)".format(
        fps, n_frames, elapsed, failures))
    print("=" * 58)

    if not args.no_preview:
        saved = preview(cap, index, backend_name)
        print("\n{} still(s) saved to {}".format(len(saved), CAPTURE_DIR))

    cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
