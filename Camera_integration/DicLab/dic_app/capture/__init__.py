"""Camera capture backends.

``core.correlation.run_correlation_from_images`` accepts any list of 2D numpy
frames, so any CameraBackend below is a drop-in source for live capture.
"""

from .base import CameraBackend
from .basler_backend import BaslerCameraBackend
from .opencv_backend import OpenCVCameraBackend

__all__ = ["CameraBackend", "BaslerCameraBackend", "OpenCVCameraBackend"]
