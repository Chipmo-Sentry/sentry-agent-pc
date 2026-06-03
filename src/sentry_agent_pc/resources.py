"""Locate bundled resources (icons) in both dev and frozen (PyInstaller) runs."""

from __future__ import annotations

import sys
from pathlib import Path


def asset_path(name: str) -> Path:
    """Absolute path to a file under the assets/ dir.

    Dev: <package>/assets/<name>.
    Frozen: PyInstaller unpacks --add-data to <_MEIPASS>/assets/<name>.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "assets" / name
    return Path(__file__).parent / "assets" / name


def icon_ico() -> Path:
    return asset_path("icon.ico")


def icon_png() -> Path:
    return asset_path("icon.png")
