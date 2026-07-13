"""SVStudio — commercial 2D Digital Image Correlation suite.

A turnkey, hardware-locked desktop product for full-field displacement and strain
measurement from a Basler acA2440-75um camera. Owns its own DIC correlation engine
(NumPy/SciPy/OpenCV); no third-party correlation library.
"""

APP_NAME = "SVStudio"
APP_VENDOR = "SVStudio"
__version__ = "0.1.0"

# Hardware the product is locked to.
TARGET_CAMERA_MODEL = "acA2440-75um"
