"""Self-update from GitHub Releases.

Flow:
  1. `check_for_update()` — GET the repo's latest release, compare its tag
     (e.g. ``v0.4.0``) against the running ``__version__``.
  2. `download_asset()` — stream the onedir zip asset to a temp file.
  3. `apply_update_and_restart()` — extract the zip and spawn a windowless .bat
     that robocopies it over the install folder once this process exits
     (releasing the locked .exe/DLLs) and relaunches. Then we quit.

Only the frozen (PyInstaller) build can self-replace. In dev (running from
source) `apply_update_and_restart` raises — the GUI surfaces a "download
manually" link instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
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
# The app is a PyInstaller --onedir folder, published as a zip. Self-update
# downloads the zip and copies it over the install folder (see
# apply_update_and_restart). The installer (Setup.exe) is for fresh installs.
ASSET_NAME = "ChipmoSentryAgent-windows.zip"


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

    asset = _pick_asset(rel.get("assets") or [])
    if asset is None:
        log.info("updater.no_zip_asset", tag=tag)
        return None

    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        download_url=str(asset["browser_download_url"]),
        notes=str(rel.get("body") or "").strip(),
        html_url=str(rel.get("html_url") or RELEASES_PAGE),
        size=int(asset.get("size") or 0),
    )


def _pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the onedir zip asset. Prefer the exact name, else first .zip.

    Never the Setup.exe (that's for fresh installs, not in-place self-update).
    """
    for a in assets:
        if a.get("name") == ASSET_NAME:
            return a
    for a in assets:
        if str(a.get("name", "")).lower().endswith(".zip"):
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
    dest = tmp_dir / f"ChipmoSentryAgent-{info.version}.zip"
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


def _build_update_script(
    extract_dir: Path, install_dir: Path, exe: Path, log_path: Path
) -> str:
    """The copy-over-folder-and-relaunch .bat (for the --onedir layout).

    Crucial Windows details learned the hard way:
      • `timeout` needs a console input handle — it FAILS under a no-console
        process ("Input redirection is not supported"). We use `ping -n`.
      • The retry-`robocopy` loop IS the wait: while the app is running its
        .exe/DLLs are locked and robocopy returns ≥8; the instant the app exits
        the copy succeeds. (robocopy rc < 8 = success.)
      • If the copy ultimately fails we STILL relaunch the existing exe, so the
        app never just vanishes.
      • Everything is logged so a failed update is diagnosable.
    """
    return f"""@echo off
setlocal enableextensions
set "SRC={extract_dir}"
set "DST={install_dir}"
set "EXE={exe}"
set "LOG={log_path}"
echo [start] %date% %time% >> "%LOG%"

rem Kill ANY remaining instances (incl. a "Шууд харах" webview child, same image)
rem so nothing keeps the .exe/DLLs locked during the copy.
taskkill /f /im ChipmoSentryAgent.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul

set /a n=0
:try
robocopy "%SRC%" "%DST%" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% lss 8 (
    echo [ok] copied after %n% retries rc=%errorlevel% >> "%LOG%"
    goto launch
)
set /a n+=1
if %n% geq 60 (
    echo [warn] copy failed after %n% tries rc=%errorlevel%; relaunching >> "%LOG%"
    goto launch
)
ping -n 2 127.0.0.1 >nul
goto try

:launch
echo [launch] %EXE% >> "%LOG%"
start "" "%EXE%"
rmdir /s /q "%SRC%" >nul 2>&1
del "%~f0" >nul 2>&1
"""


def apply_update_and_restart(new_zip: Path, *, on_before_exit: object = None) -> None:
    """Extract `new_zip` over the install folder, relaunch the exe, then exit.

    The app is a --onedir folder (exe + _internal/*). We extract the downloaded
    zip to a temp dir, then a windowless .bat robocopies it over the install
    folder once this process exits (releasing the file locks) and relaunches.
    `on_before_exit`, if callable, runs just before exit (e.g. stop the tray).

    Raises RuntimeError when not frozen (dev can't self-replace).
    """
    if not is_frozen():
        raise RuntimeError(
            "Dev горимд автомат шинэчлэл боломжгүй — GitHub-аас гараар татна уу."
        )

    exe = current_exe_path()
    install_dir = exe.parent
    pid = os.getpid()
    tmp = Path(tempfile.gettempdir())
    extract_dir = tmp / f"chipmo_update_extract_{pid}"
    log_path = tmp / "chipmo_update.log"

    # Extract the zip (flat: exe + _internal/ at the root).
    if extract_dir.exists():
        import shutil

        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(new_zip) as zf:
        zf.extractall(extract_dir)
    # If the zip wrapped everything in a single top folder, descend into it.
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not (extract_dir / exe.name).exists():
        extract_dir = entries[0]

    bat = tmp / f"chipmo_update_{pid}.bat"
    bat.write_text(
        _build_update_script(extract_dir, install_dir, exe, log_path), encoding="utf-8"
    )
    log.info("updater.applying", install=str(install_dir), src=str(extract_dir), bat=str(bat))

    # CREATE_NO_WINDOW (NOT detached): hides the cmd window, but the helper
    # stays attached to the interactive window station so the relaunched GUI is
    # visible to the user. It survives our exit (Windows doesn't kill children
    # on parent exit). DETACHED_PROCESS was the bug — `start` from a detached
    # process can launch with no visible window. We also use `ping` (not
    # `timeout`) for delays, which needs no console input.
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=creationflags,
        close_fds=True,
        cwd=str(tmp),
    )

    if callable(on_before_exit):
        try:
            on_before_exit()
        except Exception as e:  # noqa: BLE001 — never block the exit
            log.debug("updater.on_before_exit_failed", error=str(e))
    # Hard-exit so the OS releases the .exe lock immediately for the swap.
    os._exit(0)
