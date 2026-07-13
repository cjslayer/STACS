"""
ISU Civil Engineering Lab — Camera Motion Trigger
Idaho State University — Civil Engineering Department
Version 1.0.0

Watches two PULNiX TM-6701 monochrome analog cameras (presented by an analog
frame grabber as ordinary OpenCV video devices) and runs background-subtraction
motion detection on each one. When motion starts or stops it records a discrete
event to trigger_event.json using the CR6 "elapsed seconds" time base — seconds
since this monitor started, matching the Time (s) column LoggerNet writes — so
the TriggerWatcher in app.py can auto-populate the Time Chop Start/End fields.

It also writes a small PNG preview of each camera (camera_preview_1.png /
camera_preview_2.png) so the analyzer's Camera Monitor window can show a live
feed without contending with this process for the capture device.

Start it at the same moment the CR6 test begins so the elapsed clocks align:

    python camera_trigger.py
    python camera_trigger.py --cam1 0 --cam2 1 --threshold 25 --min-area 1500

Requires: opencv-python, numpy   (these are NOT needed by app.py / the .exe).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

APP_TITLE = "ISU Camera Motion Trigger"
VERSION   = "1.0.0"

def _app_dir() -> Path:
    """Return the folder where shared data files live (next to the .exe when
    frozen, otherwise next to this script). Mirrors app.py so the analyzer and
    this trigger agree on where trigger_event.json / previews are written."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent

TRIGGER_FILE  = _app_dir() / 'trigger_event.json'
PREVIEW_FILES = {1: _app_dir() / 'camera_preview_1.png',
                 2: _app_dir() / 'camera_preview_2.png'}

PREVIEW_WIDTH = 480     # downscaled preview width for the monitor window (px)
POLL_FPS      = 15      # capture + detection loop rate (Hz)
WARMUP_FRAMES = 30      # frames spent learning the background before triggering
BG_ALPHA      = 0.05    # running-average background learning rate (0–1)

# Motion-detection defaults (overridable from the command line)
DEFAULT_THRESHOLD = 25     # per-pixel diff threshold (0–255)
DEFAULT_MIN_AREA  = 1500   # min contour area (px²) that counts as motion
ON_FRAMES         = 3      # consecutive moving frames to latch motion ON
OFF_FRAMES        = 10     # consecutive still frames to latch motion OFF

# Colors reused from the analyzer palette (BGR for OpenCV overlays)
CLR_REC    = (38, 38, 220)    # red  — matches app C['btn_danger'] #DC2626
CLR_OK     = (74, 222, 22)    # green — matches app C['success']   #16A34A
CLR_TEXT   = (255, 255, 255)
CLR_NOSIG  = (96, 96, 96)


# ──────────────────────────────────────────────────────────────────────────────
# Atomic file writers
# ──────────────────────────────────────────────────────────────────────────────

def _atomic_write_text(path: Path, text: str):
    """Write text via a temp file + os.replace so readers never see a partial
    file (os.replace is atomic on Windows and POSIX)."""
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def _atomic_write_image(path: Path, img: np.ndarray):
    """Encode a PNG to a temp file then atomically swap it into place."""
    tmp = str(path) + '.tmp.png'
    cv2.imwrite(tmp, img)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────────
# Camera Channel
# ──────────────────────────────────────────────────────────────────────────────

class CameraChannel:
    """
    One camera input: owns the capture device, a running-average background
    model, and a debounced motion latch.

    detect()       — update the background model, return (moving_now, boxes)
    update_latch() — debounce raw motion into a stable ON/OFF state, returning
                     'start' / 'stop' on a transition (else None)
    """

    def __init__(self, index: int, device: int, threshold: int, min_area: int):
        self.index     = index        # logical camera number (1 or 2)
        self.device    = device       # OpenCV capture device id
        self.threshold = threshold
        self.min_area  = min_area
        # DirectShow (CAP_DSHOW) is the reliable capture backend on Windows
        # for analog frame grabbers; the default MSMF backend is unreliable
        # on Windows 7.
        self.cap       = cv2.VideoCapture(device, cv2.CAP_DSHOW)
        self.ok        = self.cap.isOpened()
        self.bg        = None          # float32 running-average background
        self.frames    = 0
        self.motion    = False
        self._on       = 0
        self._off      = 0

    def read(self):
        """Grab the next frame, or None if the device dropped out."""
        if not self.ok:
            return None
        got, frame = self.cap.read()
        if not got or frame is None:
            self.ok = False
            return None
        return frame

    def detect(self, frame: np.ndarray):
        """Update the background model and return (moving_now, bounding_boxes)."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.bg is None:
            self.bg = gray.astype('float32')
            return False, []

        cv2.accumulateWeighted(gray, self.bg, BG_ALPHA)
        self.frames += 1

        diff = cv2.absdiff(gray, cv2.convertScaleAbs(self.bg))
        _, thresh = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        boxes = [cv2.boundingRect(c) for c in contours
                 if cv2.contourArea(c) >= self.min_area]

        # Ignore motion until the background model has settled
        moving = bool(boxes) and self.frames > WARMUP_FRAMES
        return moving, boxes

    def update_latch(self, moving: bool):
        """Debounce raw per-frame motion. Returns 'start', 'stop', or None."""
        if moving:
            self._on += 1
            self._off = 0
            if not self.motion and self._on >= ON_FRAMES:
                self.motion = True
                return 'start'
        else:
            self._off += 1
            self._on = 0
            if self.motion and self._off >= OFF_FRAMES:
                self.motion = False
                return 'stop'
        return None

    def release(self):
        if self.cap is not None:
            self.cap.release()


# ──────────────────────────────────────────────────────────────────────────────
# Preview rendering
# ──────────────────────────────────────────────────────────────────────────────

def _resize_preview(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= 0:
        return frame
    scale = PREVIEW_WIDTH / float(w)
    return cv2.resize(frame, (PREVIEW_WIDTH, max(1, int(h * scale))))


def write_preview(ch: CameraChannel, frame: np.ndarray,
                  boxes: list, elapsed: float):
    """Render and atomically write the PNG preview for one camera."""
    # Draw motion boxes on the full-resolution frame, then downscale
    view = frame.copy()
    if view.ndim == 2:
        view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)
    for (x, y, w, h) in boxes:
        cv2.rectangle(view, (x, y), (x + w, y + h), CLR_REC, 2)

    view = _resize_preview(view)

    # Header overlay: camera label + elapsed seconds (CR6 elapsed-seconds base)
    cv2.putText(view, f"CAMERA {ch.index}   t={elapsed:7.2f}s",
                (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, CLR_TEXT, 1,
                cv2.LINE_AA)
    if ch.motion:
        cv2.putText(view, "REC", (view.shape[1] - 52, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, CLR_REC, 2, cv2.LINE_AA)

    _atomic_write_image(PREVIEW_FILES[ch.index], view)


def write_no_signal_preview(index: int):
    """Write a placeholder preview when a camera is unavailable."""
    img = np.zeros((int(PREVIEW_WIDTH * 0.74), PREVIEW_WIDTH, 3), dtype=np.uint8)
    cv2.putText(img, f"CAMERA {index}", (8, 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, CLR_NOSIG, 1, cv2.LINE_AA)
    cv2.putText(img, "NO SIGNAL", (PREVIEW_WIDTH // 2 - 80, img.shape[0] // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, CLR_NOSIG, 2, cv2.LINE_AA)
    _atomic_write_image(PREVIEW_FILES[index], img)


# ──────────────────────────────────────────────────────────────────────────────
# Status file
# ──────────────────────────────────────────────────────────────────────────────

def write_status(seq: int, last_event: dict, channels: list, elapsed: float):
    """
    Atomically write trigger_event.json.

    The TriggerWatcher in app.py keys off `seq` (a rising counter bumped on each
    discrete start/stop) to fire exactly once per transition, reading `event`
    and `elapsed_s` to fill the chop fields. The `cameras` block carries the
    live per-camera motion state + preview path for the Camera Monitor window.
    """
    data = {
        'seq':       seq,
        'event':     last_event['event'],        # 'start' | 'stop' | 'idle'
        'elapsed_s': last_event['elapsed_s'],     # CR6 elapsed seconds, 2 dp
        'camera':    last_event['camera'],        # camera that triggered it
        'wall_clock': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'running':   True,
        'status_elapsed_s': round(elapsed, 2),
        'cameras': {
            str(c.index): {
                'motion':  bool(c.motion),
                'ok':      bool(c.ok),
                'preview': PREVIEW_FILES[c.index].name,
            }
            for c in channels
        },
    }
    _atomic_write_text(TRIGGER_FILE, json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Motion-trigger logger for two PULNiX TM-6701 cameras.")
    p.add_argument('--cam1', type=int, default=0,
                   help="OpenCV device id for camera 1 (default 0)")
    p.add_argument('--cam2', type=int, default=1,
                   help="OpenCV device id for camera 2 (default 1)")
    p.add_argument('--threshold', type=int, default=DEFAULT_THRESHOLD,
                   help=f"per-pixel diff threshold 0-255 (default {DEFAULT_THRESHOLD})")
    p.add_argument('--min-area', type=int, default=DEFAULT_MIN_AREA,
                   help=f"min contour area in px to count as motion "
                        f"(default {DEFAULT_MIN_AREA})")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"{APP_TITLE} v{VERSION}")
    print(f"Writing events to: {TRIGGER_FILE}")

    channels = [
        CameraChannel(1, args.cam1, args.threshold, args.min_area),
        CameraChannel(2, args.cam2, args.threshold, args.min_area),
    ]
    for c in channels:
        state = "opened" if c.ok else "NOT FOUND"
        print(f"  Camera {c.index} (device {c.device}): {state}")

    if not any(c.ok for c in channels):
        print("ERROR: no camera could be opened. Check the frame grabber and "
              "the --cam1 / --cam2 device ids.")

    print("Monitoring… motion populates the Time Chop fields in the analyzer. "
          "Press Ctrl+C to stop.")

    t0       = time.monotonic()
    seq      = 0
    prev_any = False
    last_event = {'event': 'idle', 'elapsed_s': 0.0, 'camera': 0}
    period   = 1.0 / POLL_FPS

    try:
        while True:
            loop_start = time.monotonic()
            elapsed    = loop_start - t0

            for ch in channels:
                frame = ch.read()
                if frame is None:
                    write_no_signal_preview(ch.index)
                    continue
                moving, boxes = ch.detect(frame)
                ch.update_latch(moving)
                write_preview(ch, frame, boxes, elapsed)

            # Combined state: a "start" fires when the first camera latches on,
            # a "stop" fires once every camera has gone quiet again. This yields
            # a single clean Start/End pair for the test window.
            any_motion = any(c.motion for c in channels)
            if any_motion and not prev_any:
                cam = next((c.index for c in channels if c.motion), 0)
                seq += 1
                last_event = {'event': 'start',
                              'elapsed_s': round(elapsed, 2),
                              'camera': cam}
                print(f"[START] t={elapsed:8.2f}s  camera {cam}  (seq {seq})")
            elif not any_motion and prev_any:
                seq += 1
                last_event = {'event': 'stop',
                              'elapsed_s': round(elapsed, 2),
                              'camera': last_event['camera']}
                print(f"[STOP ] t={elapsed:8.2f}s  camera {last_event['camera']}  (seq {seq})")
            prev_any = any_motion

            write_status(seq, last_event, channels, elapsed)

            dt = time.monotonic() - loop_start
            if dt < period:
                time.sleep(period - dt)

    except KeyboardInterrupt:
        print("\ncamera_trigger: stopped by user.")
    finally:
        for c in channels:
            c.release()


if __name__ == '__main__':
    main()
