"""Unassisted grid-detection check on a new target.

Captures one still and runs the EXISTING detection chain from
phase3_displacement.py -- detect_block_edges (which itself runs the
brightness-based sheet stage first), detect_block_rows, and subset_boxes --
with no parameter changes, no ROI hints, and no operator input. Draws whatever
the chain returned onto an overlay so the operator can judge it.

This deliberately does NOT write scale.json: the trusted-width constant in the
pipeline belongs to the old printed target and does not apply here.

Requires: pip install opencv-python numpy scikit-image
"""

import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lock_and_preview import open_locked_camera
from phase3_displacement import (TRIALS_DIR, detect_block_edges,
                                 detect_block_rows, grab_still, subset_boxes)


def main():
    print("Opening camera and locking automatic controls...")
    cap, info = open_locked_camera()
    if cap is None:
        print("No camera opened. Run lock_and_preview.py to diagnose.")
        return 1
    still, _ = grab_still(cap)
    cap.release()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    still_path = TRIALS_DIR / "grid_check_{}_still.png".format(stamp)
    cv2.imwrite(str(still_path), still.astype(np.uint8))

    edges = detect_block_edges(still)
    img = cv2.cvtColor(still.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    h, w = still.shape

    if not edges["ok"]:
        print("DETECTION FAILED: {}".format(edges["reason"]))
        out = TRIALS_DIR / "grid_check_{}.png".format(stamp)
        cv2.putText(img, "DETECTION FAILED: {}".format(edges["reason"]),
                    (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)
        cv2.imwrite(str(out), img)
        print("Wrote {}".format(out.name))
        return 1

    rows = detect_block_rows(still, edges)
    boxes = subset_boxes(edges, rows, still.shape)

    print("\nBLOCK DETECTION (unassisted)")
    print("  left edge   : x = {:.2f} px  (20-80% transition {:.1f} px)".format(
        edges["left_px"], edges["left_transition_px"]))
    print("  right edge  : x = {:.2f} px  (20-80% transition {:.1f} px)".format(
        edges["right_px"], edges["right_transition_px"]))
    print("  span        : {:.2f} px".format(edges["span_px"]))
    print("  rows        : y = {:.1f} .. {:.1f}".format(rows[0], rows[1]))
    print("  bright-region stage : x = {:.1f} .. {:.1f}".format(
        edges["paper_left_px"], edges["paper_right_px"]))
    print("  ambiguous   : {}".format(edges["ambiguous"]))
    print("  subset grid : {} boxes".format(len(boxes)))

    for x in (edges["left_px"], edges["right_px"]):
        cv2.line(img, (int(round(x)), 0), (int(round(x)), h), (0, 0, 255), 3)
    for y in rows:
        cv2.line(img, (0, int(round(y))), (w, int(round(y))), (0, 255, 0), 3)
    for x in (edges["paper_left_px"], edges["paper_right_px"]):
        cv2.line(img, (int(round(x)), 0), (int(round(x)), h), (255, 128, 0), 2)
    for (y0, y1, x0, x1) in boxes:
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 255, 255), 2)
    cv2.putText(img, "span {:.1f} px   rows {:.0f}..{:.0f}".format(
        edges["span_px"], rows[0], rows[1]),
        (40, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 0), 3)

    out = TRIALS_DIR / "grid_check_{}.png".format(stamp)
    cv2.imwrite(str(out), img)
    print("\nWrote {}  and  {}".format(out.name, still_path.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
