"""Basler camera backend (acA2440-75um) via pypylon.

pypylon is Basler's official Python wrapper around the pylon Camera Software
Suite: https://github.com/basler/pypylon (Apache-2.0). Install with:

    pip install pypylon

Requires the pylon runtime to be installed on the machine (Basler pylon
Software Suite / pylon Viewer installer), which also carries the USB3 Vision
driver the acA2440-75um needs.

The acA2440-75um is monochrome, so this backend requests Mono8 frames
directly -- no Bayer demosaicing needed, which keeps grabbing fast and the
frames exactly what µDIC wants (2D grayscale arrays).
"""

from .base import CameraBackend

try:
    from pypylon import pylon
except ImportError:  # pragma: no cover - only hit when pypylon isn't installed
    pylon = None


class BaslerCameraBackend(CameraBackend):
    def __init__(self, device_index: int = 0, exposure_time_us: float = None,
                serial_number: str = None):
        """
        Parameters
        ----------
        device_index : int
            Which enumerated Basler device to open if ``serial_number`` is not given.
        exposure_time_us : float, optional
            Fixed exposure time in microseconds. Structural DIC wants a *fixed*
            exposure across the whole test (auto-exposure would shift image
            brightness frame to frame and hurt correlation) -- set this explicitly
            once you've picked a value in pylon Viewer.
        serial_number : str, optional
            Open a specific camera by serial number instead of by enumeration
            order (recommended once more than one Basler camera is on the bench).
        """
        if pylon is None:
            raise ImportError(
                "pypylon is not installed. Run: pip install pypylon"
            )
        self._device_index = device_index
        self._exposure_time_us = exposure_time_us
        self._serial_number = serial_number
        self._camera = None
        self._converter = None

    def open(self) -> None:
        tl_factory = pylon.TlFactory.GetInstance()
        devices = tl_factory.EnumerateDevices()
        if not devices:
            raise RuntimeError("No Basler camera found (check USB3 connection / pylon driver)")

        if self._serial_number is not None:
            device = next(
                (d for d in devices if d.GetSerialNumber() == self._serial_number), None
            )
            if device is None:
                found = [d.GetSerialNumber() for d in devices]
                raise RuntimeError(
                    f"No Basler camera with serial {self._serial_number!r}; found {found}"
                )
        else:
            device = devices[self._device_index]

        self._camera = pylon.InstantCamera(tl_factory.CreateDevice(device))
        self._camera.Open()

        # Monochrome capture straight off the sensor.
        self._camera.PixelFormat.SetValue("Mono8")

        if self._exposure_time_us is not None:
            self._camera.ExposureAuto.SetValue("Off")
            self._camera.ExposureTime.SetValue(self._exposure_time_us)

        self._converter = pylon.ImageFormatConverter()
        self._converter.OutputPixelFormat = pylon.PixelType_Mono8
        self._converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

        # LatestImageOnly: for live monitoring we want the newest frame, not a
        # backlog -- if processing falls behind, drop old frames rather than lag.
        self._camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

    def grab(self):
        grab_result = self._camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
        try:
            if not grab_result.GrabSucceeded():
                raise RuntimeError(f"Grab failed: {grab_result.ErrorDescription}")
            image = self._converter.Convert(grab_result)
            return image.GetArray()
        finally:
            grab_result.Release()

    def close(self) -> None:
        if self._camera is not None:
            if self._camera.IsGrabbing():
                self._camera.StopGrabbing()
            self._camera.Close()
            self._camera = None
