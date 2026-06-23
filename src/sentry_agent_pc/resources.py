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


def logo_header_png() -> Path:
    """The 'C' brand mark (white, transparent bg) for the dark in-app header."""
    return asset_path("logo_header.png")


def floorplan_index() -> Path:
    """The bundled floor-plan web editor entry HTML (docs/30). Its konva.min.js +
    app.js sit beside it and load relatively, so pywebview resolves them."""
    return asset_path("floorplan/index.html")


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


def resolve_ffmpeg_exe(configured: str | None = None) -> str:
    """Best path to an ffmpeg binary. ALWAYS returns a string (never None) so the
    caller spawns it and surfaces a clear "not found" if it's truly absent.

    Priority: an explicit absolute ``configured`` path (the founder's .env) → the
    bundled binary (shipped in every release) → ``ffmpeg`` on PATH → the bare name
    as a last resort.

    Unlike MediaMTX (optional fan-out), ffmpeg is REQUIRED — the RTSP probe and the
    cloud push relay both spawn it — so it is bundled with the build and this
    resolver normally hits the bundled copy on a clean store PC.
    """
    if configured and configured not in ("", "ffmpeg") and Path(configured).exists():
        return configured
    bundled = bundled_binary("ffmpeg.exe")
    if bundled is not None:
        return str(bundled)
    return shutil.which(configured or "ffmpeg") or (configured or "ffmpeg")


def ffmpeg_available(configured: str | None = None) -> bool:
    """True if an ffmpeg binary can actually be located (bundled, PATH, or an
    explicit .env path). Lets callers give a precise "ffmpeg not installed"
    message instead of a misleading "stream not found" when the binary is gone."""
    exe = resolve_ffmpeg_exe(configured)
    return Path(exe).exists() or shutil.which(exe) is not None
