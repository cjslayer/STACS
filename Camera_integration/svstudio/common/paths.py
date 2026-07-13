"""Standard on-disk locations for SVStudio.

Per-machine data (license, logs, config) lives under %PROGRAMDATA%\\SVStudio so it
survives per-user installs and is writable by a locked-down lab account.
"""

import os
from pathlib import Path

from svstudio import APP_NAME


def app_data_dir() -> Path:
    base = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def license_path() -> Path:
    """Default location the app looks for the signed license file."""
    return app_data_dir() / "license.lic"


def config_path() -> Path:
    return app_data_dir() / "config.json"
