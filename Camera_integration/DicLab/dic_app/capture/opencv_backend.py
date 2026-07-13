"""OpenCV/UVC camera backend -- any regular webcam.

For developing and testing the live capture -> correlation pipeline before
the Basler acA2440-75um + Computar lens are on the bench, or as a cheap
fallback. Not a substitute for the Basler for real structural tests (rolling
shutter + auto-exposure on most webcams will corrupt DIC results under load),
but the object shape/interface is identical, so swapping backends later is a
one-line change.

Requires: pip install opencv-python
"""

from .base import CameraBackend

try:
    import cv2
except ImportError:  # pragma: no cover - only hit when opencv-python isn't installed
    cv2 = None


class OpenCVCameraBackend(CameraBackend):
    def __init__(self, device_index: int = 0, width: int = None, height: int = None):
        if cv2 is None:
            raise ImportError(
                "opencv-python is not installed. Run: pip install opencv-python"
            )
        self._device_index = device_index
        self._width = width
        self._height = height
        self._cap = None

    def open(self) -> None:
        self._cap = cv2.VideoCapture(self._device_index, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open camera index {self._device_index}")
        if self._width is not None:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        if self._height is not None:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)

    def grab(self):
        ok, frame = self._cap.read()
        if not ok:
            raise RuntimeError("Frame grab failed")
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
