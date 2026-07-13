"""Camera backend interface.

Backends implement open/grab/close and return frames as numpy arrays (2D,
grayscale, dtype uint8), which feed directly into
``core.correlation.run_correlation_from_images``.

Two backends are provided:
  - ``BaslerCameraBackend`` (basler_backend.py) -- the acA2440-75um via pypylon,
    the real hardware path for live structural tests.
  - ``OpenCVCameraBackend`` (opencv_backend.py) -- any UVC/webcam via OpenCV,
    for developing/testing the live pipeline before the Basler camera + lens
    are on the bench, or as a cheap fallback backend.

Use as a context manager (``with backend: ...``) or call open()/close() by hand.
"""

from abc import ABC, abstractmethod


class CameraBackend(ABC):
    @abstractmethod
    def open(self) -> None:
        """Connect to the camera and start grabbing."""

    @abstractmethod
    def grab(self):
        """Block until the next frame is available; return it as a 2D numpy array."""

    @abstractmethod
    def close(self) -> None:
        """Stop grabbing and release the camera."""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
