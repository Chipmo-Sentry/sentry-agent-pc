"""Self-update from GitHub Releases.

Flow:
  1. `check_for_update()` — GET the repo's latest release, compare its tag
     (e.g. ``v0.2.0``) against the running ``__version__``.
  2. `download_asset()` — stream the ``ChipmoSentryAgent.exe`` asset to a temp
     file, with an optional progress callback.
  3. `apply_update_and_restart()` — on Windows we can't overwrite a running
     .exe, so we spawn a tiny detached .bat that waits for this process to
     exit, swaps the file, and relaunches. Then we quit.

Only the frozen (PyInstaller) build can self-replace. In dev (running from
source) `apply_update_and_restart` raises — the GUI surfaces a "download
manually" link instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sentry_agent_pc import __version__
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.updater")

GITHUB_REPO = "Chipmo-Sentry/sentry-agent-pc"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "ChipmoSentryAgent.exe"


@dataclass(slots=True)
class UpdateInfo:
    """A newer release is available."""

    version: str          # normalized, no leading "v" (e.g. "0.2.0")
    tag: str              # raw tag as published (e.g. "v0.2.0")
    download_url: str     # browser_download_url of the .exe asset
    notes: str            # release body (markdown)
    html_url: str         # release page (manual download fallback)
    size: int = 0         # asset size in bytes (0 if unknown)


def parse_version(s: str) -> tuple[int, ...]:
    """Parse a semver-ish string into a comparable tuple.

    Strips a leading ``v`` and any pre-release/build suffix. ``"v1.2.3-rc1"``
    → ``(1, 2, 3)``. Non-numeric parts are treated as 0 so comparison never
    raises on a malformed tag.
    """
    core = s.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in core.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_frozen() -> bool:
    """True when running as the PyInstaller-built .exe (can self-replace)."""
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Path:
    """Path of the running executable (only meaningful when frozen)."""
    return Path(sys.executable)


def check_for_update(
    current: str = __version__,
    *,
    timeout_sec: float = 10.0,
) -> UpdateInfo | None:
    """Return UpdateInfo if the latest GitHub release is newer, else None.

    Never raises — network/parse errors are logged and return None so the GUI
    can fail silently on a flaky connection.
    """
    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            r = client.get(
                LATEST_RELEASE_API,
                headers={"Accept": "application/vnd.github+json"},
            )
        if r.status_code != 200:
            log.info("updater.check_non_200", status=r.status_code)
            return None
        rel = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("updater.check_failed", error=str(e))
        return None

    tag = str(rel.get("tag_name") or "")
    if not tag:
        return None
    if rel.get("draft") or rel.get("prerelease"):
        log.debug("updater.skip_draft_or_prerelease", tag=tag)
        return None

    if parse_version(tag) <= parse_version(current):
        log.debug("updater.up_to_date", current=current, latest=tag)
        return None

    asset = _pick_exe_asset(rel.get("assets") or [])
    if asset is None:
        log.info("updater.no_exe_asset", tag=tag)
        return None

    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        download_url=str(asset["browser_download_url"]),
        notes=str(rel.get("body") or "").strip(),
        html_url=str(rel.get("html_url") or RELEASES_PAGE),
        size=int(asset.get("size") or 0),
    )


def _pick_exe_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the .exe release asset. Prefer the exact name, else first .exe."""
    for a in assets:
        if a.get("name") == ASSET_NAME:
            return a
    for a in assets:
        if str(a.get("name", "")).lower().endswith(".exe"):
            return a
    return None


def download_asset(
    info: UpdateInfo,
    *,
    progress: Callable[[int, int], None] | None = None,
    timeout_sec: float = 300.0,
) -> Path:
    """Stream the release .exe to a temp file. Returns the downloaded path.

    `progress(downloaded_bytes, total_bytes)` is called as data arrives
    (total is `info.size`, or 0 if the server omits Content-Length).
    """
    tmp_dir = Path(tempfile.gettempdir())
    dest = tmp_dir / f"ChipmoSentryAgent-{info.version}.exe"
    total = info.size
    done = 0

    with (
        httpx.Client(timeout=timeout_sec, follow_redirects=True) as client,
        client.stream("GET", info.download_url) as resp,
    ):
        resp.raise_for_status()
        if total == 0:
            total = int(resp.headers.get("Content-Length", 0))
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    log.info("updater.downloaded", path=str(dest), bytes=done)
    return dest


def apply_update_and_restart(new_exe: Path) -> None:
    """Replace the running .exe with `new_exe` and relaunch, then exit.

    Windows holds a lock on a running executable, so we can't overwrite it in
    place. We write a detached .bat that:
      1. waits for this PID to exit,
      2. moves the downloaded file over the current .exe (retrying on lock),
      3. relaunches the app and deletes itself.

    Raises RuntimeError when not frozen (a Python process can't swap itself).
    """
    if not is_frozen():
        raise RuntimeError(
            "Dev горимд автомат шинэчлэл боломжгүй — GitHub-аас гараар татна уу."
        )

    target = current_exe_path()
    pid = os.getpid()
    bat = Path(tempfile.gettempdir()) / f"chipmo_update_{pid}.bat"

    # %1=pid %2=source(new) %3=target(current exe)
    script = f"""@echo off
setlocal
set "PID={pid}"
set "SRC={new_exe}"
set "DST={target}"

rem Wait for the running agent to exit (lock release).
:waitloop
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
)

rem Swap the executable, retrying while the file is briefly locked.
set /a tries=0
:movloop
move /y "%SRC%" "%DST%" >nul 2>&1
if not errorlevel 1 goto launch
set /a tries+=1
if %tries% geq 20 goto launch
timeout /t 1 /nobreak >nul
goto movloop

:launch
start "" "%DST%"
del "%~f0" >nul 2>&1
"""
    bat.write_text(script, encoding="utf-8")
    log.info("updater.applying", target=str(target), new=str(new_exe))

    # Detached, no console window — survives our exit.
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=creationflags,
        close_fds=True,
    )
    # Hard-exit so the .bat can grab the file lock immediately.
    os._exit(0)
