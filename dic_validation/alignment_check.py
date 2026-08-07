"""Rail alignment mode: repeated span checks while the rail angle is adjusted.

Trial 1 showed the slide has an out-of-plane component (block span shrank
1.49% over one inch of travel), i.e. the rail is not perpendicular to the
camera axis. This tool gives the operator a live number to align against:

  loop -- press Enter, a still is captured, the block span is detected and
  printed along with its % change from the FIRST capture of the run.

Protocol at the bench: capture at one end of the travel, slide to the other
end, capture again, and adjust the rail/camera angle between passes. When the
span change across full travel stays under 0.3%, the travel is in-plane enough
for the Phase 3 trials.

The camera stays open for the whole run -- reopening would re-run the exposure
lock and could land on a different operating point. A few frames are flushed
before each still so a buffered frame from before the slide cannot be measured.

Requires: pip install opencv-python numpy scikit-image
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from camera_check import safe_read
from camera_lock import settle
from lock_and_preview import open_locked_camera
from phase3_displacement import detect_block_edges, grab_still

#: Frames discarded right before each still, so the still is truly current.
FLUSH_FRAMES = 10

SPAN_CHANGE_LIMIT_PCT = 0.3


def main():
    print("Opening camera and locking automatic controls...")
    cap, info = open_locked_camera()
    if cap is None:
        print("No camera opened. Run lock_and_preview.py to diagnose.")
        return 1

    print("\nALIGNMENT MODE -- span change under {:.1f}% across full travel = good".format(
        SPAN_CHANGE_LIMIT_PCT))
    print("  [Enter]   capture a still and print the span")
    print("  [r Enter] reset the baseline (use after adjusting the angle)")
    print("  [q Enter] quit")

    first = None
    n = 0
    try:
        while True:
            try:
                ans = input("\n> ").strip().lower()
            except EOFError:
                break
            if ans == "q":
                break
            if ans == "r":
                first = None
                print("  baseline reset -- next capture becomes the new baseline")
                continue
            settle(cap, FLUSH_FRAMES, safe_read)
            still, _ = grab_still(cap)
            edges = detect_block_edges(still)
            if not edges["ok"]:
                print("  EDGE DETECTION FAILED: {} -- adjust and press Enter again".format(
                    edges["reason"]))
                continue
            n += 1
            span = edges["span_px"]
            if first is None:
                first = span
                print("  capture {:2d}: span {:8.2f} px   L {:7.2f}  R {:7.2f}   "
                      "(baseline)".format(n, span, edges["left_px"], edges["right_px"]))
            else:
                change = 100.0 * (span - first) / first
                verdict = "OK, under {:.1f}%".format(SPAN_CHANGE_LIMIT_PCT) \
                    if abs(change) < SPAN_CHANGE_LIMIT_PCT else "over limit"
                print("  capture {:2d}: span {:8.2f} px   L {:7.2f}  R {:7.2f}   "
                      "{:+.3f}% vs baseline   [{}]".format(
                          n, span, edges["left_px"], edges["right_px"],
                          change, verdict))
            if edges["ambiguous"]:
                print("  (warning: soft edge transition -- treat this reading with care)")
    finally:
        cap.release()
    print("\nAlignment mode closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
