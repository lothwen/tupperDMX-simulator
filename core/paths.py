# ABOUTME: Single source of truth for filesystem locations: the read-only bundle
# ABOUTME: dir (PyInstaller _MEIPASS or the repo root), assets/, and the
# ABOUTME: platform-conventional user-preferences directory.
import os
import sys
from pathlib import Path

import platformdirs

_APP_NAME = "tupperDMX"


def bundle_dir() -> str:
    """Directory containing read-only resources shipped with the app.

    When frozen via PyInstaller, resources are extracted to sys._MEIPASS;
    in development, they sit at the repo root (this file's parent's parent).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def asset_path(*parts: str) -> str:
    """Absolute path to a file inside the assets/ directory."""
    return os.path.join(bundle_dir(), "assets", *parts)


def default_shows_dir() -> Path:
    """Default parent directory for show projects (~/Documents/TupperDMX)."""
    return Path(platformdirs.user_documents_dir()) / "TupperDMX"


def userprefs_dir() -> Path:
    """Platform-conventional directory for writable user data.

    Windows: %APPDATA%\\tupperDMX  (Roaming)
    macOS:   ~/Library/Application Support/tupperDMX
    Linux:   ~/.config/tupperDMX
    """
    if sys.platform == "win32":
        return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False, roaming=True))
    if sys.platform == "darwin":
        return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False))
    return Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))
