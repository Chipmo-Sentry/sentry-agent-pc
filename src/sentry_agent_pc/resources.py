"""Locate bundled resources (icons, binaries) in dev and frozen (PyInstaller) runs."""

from __future__ import annotations

import shutil
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


def bundled_binary(name: str) -> Path | None:
    """Absolute path to a bundled ``bin/<name>``, or None if not present.

    Frozen: PyInstaller unpacks ``--add-data ...;bin`` to ``<_MEIPASS>/bin``.
    Dev:    ``<package>/bin/<name>`` (build_exe.ps1 drops it there; gitignored).
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = Path(meipass) / "bin" / name
            return p if p.exists() else None
    p = Path(__file__).parent / "bin" / name
    return p if p.exists() else None


def resolve_mediamtx_exe(configured: str | None = None) -> str | None:
    """Best path to a MediaMTX binary, or None if unavailable.

    Priority: an explicit absolute ``configured`` path (the founder's .env, e.g.
    pointing at the sentry-ingest copy for a source test) → the bundled binary →
    whatever ``mediamtx`` resolves to on PATH. None → fan-out disabled, callers
    use direct camera connections.
    """
    if configured and configured not in ("", "mediamtx") and Path(configured).exists():
        return configured
    bundled = bundled_binary("mediamtx.exe")
    if bundled is not None:
        return str(bundled)
    return shutil.which(configured or "mediamtx")
