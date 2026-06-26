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

import base64
import binascii
import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sentry_agent_pc import __version__
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.updater")

# Ed25519 release-signing public key (base64 of the 32-byte raw key), pinned into
# the binary. The release workflow signs each zip's SHA-256 with the matching
# PRIVATE key (held only as the RELEASE_SIGNING_KEY GitHub secret), and the
# updater REFUSES to apply a release whose signature doesn't verify against this
# key — closing the "a compromised GitHub release = fleet-wide RCE" gap that the
# SHA-256 alone (fetched from the same release) can't.
#
# EMPTY = signing not activated yet → the updater keeps verifying the SHA-256
# only (transit integrity), exactly as before. To ACTIVATE, see
# docs/RELEASE_SIGNING.md: (1) run scripts/gen_signing_key.py, (2) add the
# private key as the RELEASE_SIGNING_KEY secret, (3) ship one signed release,
# (4) THEN paste the public key here. Order matters — pinning the key before a
# signed release exists would refuse every update.
_RELEASE_PUBLIC_KEY_B64 = ""

GITHUB_REPO = "Chipmo-Sentry/sentry-agent-pc"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
# Always-latest installer — GitHub redirects /releases/latest/download/<asset>
# to the newest release's asset, so this never needs bumping. Used as the manual
# fallback when an in-app self-update can't download (e.g. a GitHub 504).
SETUP_DOWNLOAD_URL = (
    f"https://github.com/{GITHUB_REPO}/releases/latest/download/ChipmoSentryAgent-Setup.exe"
)
# The app is a PyInstaller --onedir folder, published as a zip. Self-update
# downloads the zip and copies it over the install folder (see
# apply_update_and_restart). The installer (Setup.exe) is for fresh installs.
ASSET_NAME = "ChipmoSentryAgent-windows.zip"
# Sidecar published alongside the zip (see release.yml). Its content is the
# zip's SHA-256 (hex), optionally followed by the filename — `sha256sum` format.
SHA256_ASSET_NAME = ASSET_NAME + ".sha256"
# Detached Ed25519 signature sidecar (base64). Its content is the signature over
# the zip's SHA-256 hex; verified against `_RELEASE_PUBLIC_KEY_B64`. Only present
# on releases built after signing was activated (see release.yml).
SIG_ASSET_NAME = ASSET_NAME + ".sig"


@dataclass(slots=True)
class UpdateInfo:
    """A newer release is available."""

    version: str  # normalized, no leading "v" (e.g. "0.2.0")
    tag: str  # raw tag as published (e.g. "v0.2.0")
    download_url: str  # browser_download_url of the .exe asset
    notes: str  # release body (markdown)
    html_url: str  # release page (manual download fallback)
    size: int = 0  # asset size in bytes (0 if unknown)
    # Integrity: expected SHA-256 (hex) of the zip. Taken from GitHub's asset
    # `digest` field when present, else fetched from the .sha256 sidecar. The
    # download is REFUSED if neither is available (no unsigned auto-update).
    expected_sha256: str | None = None
    sha256_url: str | None = None  # sidecar asset URL (fallback source)
    sig_url: str | None = None  # Ed25519 .sig sidecar URL (authenticity)


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

    assets = rel.get("assets") or []
    asset = _pick_asset(assets)
    if asset is None:
        log.info("updater.no_zip_asset", tag=tag)
        return None

    # Prefer GitHub's own asset digest ("sha256:<hex>") when present; otherwise
    # fall back to the .sha256 sidecar asset, fetched at download time.
    digest = str(asset.get("digest") or "")
    expected_sha256 = digest[7:].lower() if digest.startswith("sha256:") else None
    sidecar = _find_sidecar_sha256(assets)
    sha256_url = str(sidecar["browser_download_url"]) if sidecar else None
    sig = _find_asset_named(assets, SIG_ASSET_NAME)
    sig_url = str(sig["browser_download_url"]) if sig else None

    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        download_url=str(asset["browser_download_url"]),
        notes=str(rel.get("body") or "").strip(),
        html_url=str(rel.get("html_url") or RELEASES_PAGE),
        size=int(asset.get("size") or 0),
        expected_sha256=expected_sha256,
        sha256_url=sha256_url,
        sig_url=sig_url,
    )


def _pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the onedir zip asset — by EXACT name only.

    A loose "first .zip" fallback would let a release that happens to carry any
    other .zip be auto-installed; require the exact published name so the
    updater applies only the artifact release.yml is known to produce. Never the
    Setup.exe (that's for fresh installs, not in-place self-update).
    """
    for a in assets:
        if a.get("name") == ASSET_NAME:
            return a
    return None


def _find_asset_named(assets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The asset with exactly `name`, if the release published one."""
    for a in assets:
        if a.get("name") == name:
            return a
    return None


def _find_sidecar_sha256(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The `<zip>.sha256` checksum sidecar asset, if the release published one."""
    return _find_asset_named(assets, SHA256_ASSET_NAME)


def download_asset(
    info: UpdateInfo,
    *,
    progress: Callable[[int, int], None] | None = None,
    timeout_sec: float = 300.0,
) -> Path:
    """Stream the release zip to a temp file, verify its SHA-256, return the path.

    `progress(downloaded_bytes, total_bytes)` is called as data arrives
    (total is `info.size`, or 0 if the server omits Content-Length).

    Raises RuntimeError when no expected checksum is available or the download's
    digest does not match — the GUI catches it and falls back to a manual
    download link. This is the gate against a tampered/partial release being
    auto-executed on every store PC.
    """
    tmp_dir = Path(tempfile.gettempdir())
    dest = tmp_dir / f"ChipmoSentryAgent-{info.version}.zip"
    total = info.size
    done = 0
    hasher = hashlib.sha256()

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
                hasher.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    actual = hasher.hexdigest().lower()
    _verify_checksum(info, actual, dest)
    _verify_signature(info, actual, dest)
    log.info("updater.downloaded", path=str(dest), bytes=done, sha256=actual)
    return dest


def _resolve_expected_sha256(info: UpdateInfo, *, timeout_sec: float = 30.0) -> str | None:
    """Expected zip SHA-256 — from the asset digest, else the sidecar, else None."""
    if info.expected_sha256:
        return info.expected_sha256.lower()
    if info.sha256_url:
        try:
            with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
                r = client.get(info.sha256_url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.info("updater.sha256_fetch_failed", error=str(e))
            return None
        # `sha256sum` format: "<hex>  filename" — take the first token.
        parts = r.text.strip().split()
        if parts and len(parts[0]) == 64:
            return parts[0].lower()
        log.info("updater.sha256_malformed", body=r.text[:80])
    return None


def _verify_checksum(info: UpdateInfo, actual_hex: str, dest: Path) -> None:
    """Raise (and delete the file) unless `actual_hex` matches the expected digest."""
    expected = _resolve_expected_sha256(info)
    if not expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            "Шинэчлэлийн checksum олдсонгүй — автомат шинэчлэл аюулгүйн үүднээс цуцлагдлаа. "
            "GitHub-аас гараар татна уу."
        )
    if not secrets.compare_digest(actual_hex, expected):
        dest.unlink(missing_ok=True)
        log.warning("updater.checksum_mismatch", expected=expected, actual=actual_hex)
        raise RuntimeError(
            "Татсан файлын checksum таарсангүй — эвдэрсэн эсвэл өөрчлөгдсөн байж магадгүй. "
            "Шинэчлэл цуцлагдлаа."
        )


def _release_public_key() -> Ed25519PublicKey | None:
    """The pinned Ed25519 release key, or None if signing isn't activated yet."""
    if not _RELEASE_PUBLIC_KEY_B64:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(_RELEASE_PUBLIC_KEY_B64))
    except (ValueError, binascii.Error) as e:  # a malformed pin must not crash
        log.error("updater.bad_pinned_key", error=str(e))
        return None


def _fetch_signature(
    info: UpdateInfo, *, timeout_sec: float = 30.0, attempts: int = 3
) -> bytes | None:
    """Download + base64-decode the .sig sidecar. None if absent/unfetchable.

    A transient HTTP failure is RETRIED (a single GitHub blip shouldn't block the
    whole fleet's updates once signing is active); a malformed signature body is
    not retried. ``sig_url is None`` (asset genuinely absent) returns immediately.
    """
    if not info.sig_url:
        return None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
                r = client.get(info.sig_url)
            r.raise_for_status()
            return base64.b64decode(r.text.strip(), validate=True)
        except (ValueError, binascii.Error) as e:
            log.info("updater.sig_decode_failed", error=str(e))
            return None  # malformed content — retrying won't help
        except httpx.HTTPError as e:
            log.info("updater.sig_fetch_retry", attempt=attempt, error=str(e))
            if attempt < attempts:
                time.sleep(1.0)
    return None


def _verify_signature(info: UpdateInfo, sha256_hex: str, dest: Path) -> None:
    """Verify the release's Ed25519 signature (over its SHA-256 hex) against the
    pinned key. No-op when signing isn't activated (EMPTY pin); otherwise a
    missing/invalid signature — or a pin that's set but unparseable — RAISES (and
    deletes the download). An unsigned/tampered release, or a misconfigured key,
    must fail CLOSED once signing is on rather than silently downgrade.
    """
    if not _RELEASE_PUBLIC_KEY_B64:
        log.info("updater.signing_not_enabled")  # SHA-256-only, as before
        return
    pub = _release_public_key()
    if pub is None:
        # Pin is SET but didn't parse — never silently fall back to no-verify.
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            "Шинэчлэлийн нийтийн түлхүүр буруу тохируулагдсан — шинэчлэл аюулгүйн "
            "үүднээс цуцлагдлаа."
        )
    sig = _fetch_signature(info)
    if sig is None:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            "Шинэчлэлийн гарын үсэг олдсонгүй — автомат шинэчлэл аюулгүйн үүднээс "
            "цуцлагдлаа. GitHub-аас гараар татна уу."
        )
    try:
        pub.verify(sig, sha256_hex.encode("ascii"))
    except InvalidSignature:
        dest.unlink(missing_ok=True)
        log.warning("updater.signature_invalid", sha256=sha256_hex)
        raise RuntimeError(
            "Шинэчлэлийн гарын үсэг таарсангүй — найдвартай эх сурвалжаас гараагүй "
            "байж магадгүй. Шинэчлэл цуцлагдлаа."
        ) from None
    log.info("updater.signature_ok")


def _is_safe_mirror_dest(install_dir: Path) -> bool:
    """True only when `install_dir` is safe to /MIR (mirror = purge) into.

    `/MIR` deletes anything in the destination that isn't in the source, so an
    accidentally-wrong DST (a drive root like ``C:\\``, the user-profile root, or
    an empty path) would wipe far more than the app folder. We refuse to mirror
    unless DST is a real, sufficiently-nested directory. The caller falls back to
    a non-purging copy (``/E``) when this returns False — stale files are the
    lesser evil versus nuking the wrong tree.
    """
    raw = str(install_dir).strip()
    if not raw:
        return False
    resolved = install_dir.resolve()
    # A drive root (C:\, D:\) or filesystem root has no parent of its own.
    if resolved == resolved.parent:
        return False
    # The user-profile root (%USERPROFILE%) and its immediate parent (the
    # \Users dir) must never be mirrored into.
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        profile_path = Path(profile).resolve()
        if resolved == profile_path or resolved == profile_path.parent:
            return False
    # Require at least a drive + two path components (e.g. C:\A\B), so the real
    # install dir (…\AppData\Local\ChipmoSentryAgent) passes but shallow paths
    # (C:\, C:\Foo) are rejected.
    return len(resolved.parts) >= 3


def _robocopy_line(install_dir: Path) -> str:
    """The single robocopy command for the update .bat.

    Uses ``/MIR`` (mirror) so the install dir matches the new release EXACTLY —
    files dropped in an older release are purged, killing version-skew. Because
    ``/MIR`` purges, we only mirror when `_is_safe_mirror_dest` vouches for DST;
    otherwise we fall back to ``/E`` (copy without purge) so a misdetected path
    can never wipe the wrong tree.
    """
    mode = "/MIR" if _is_safe_mirror_dest(install_dir) else "/E"
    return f'robocopy "%SRC%" "%DST%" {mode} /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul'


def _build_update_script(extract_dir: Path, install_dir: Path, exe: Path, log_path: Path) -> str:
    """The copy-over-folder-and-relaunch .bat (for the --onedir layout).

    Crucial Windows details learned the hard way:
      • `chcp 65001` FIRST: this .bat is written UTF-8, but cmd.exe otherwise
        reads it in the OEM code page (e.g. CP866). Any non-ASCII path —
        a Cyrillic/Mongolian install folder, or just a Cyrillic Windows
        USERNAME in the default %LOCALAPPDATA% path — would then be mangled,
        so robocopy created a garbage-named phantom folder instead of updating
        the real install (the app "vanished"). Switching cmd to UTF-8 first
        makes the embedded paths resolve correctly.
      • `timeout` needs a console input handle — it FAILS under a no-console
        process ("Input redirection is not supported"). We use `ping -n`.
      • The retry-`robocopy` loop IS the wait: while the app is running its
        .exe/DLLs are locked and robocopy returns ≥8; the instant the app exits
        the copy succeeds. (robocopy rc < 8 = success.)
      • `/MIR` mirrors the new release over the install dir so files removed in
        a newer release don't linger (version-skew) — but only when the DST is a
        vetted, sufficiently-nested path (see `_is_safe_mirror_dest`), since
        `/MIR` purges; otherwise we degrade to `/E` (copy, no purge).
      • If the copy ultimately fails we STILL relaunch the existing exe, so the
        app never just vanishes.
      • Everything is logged so a failed update is diagnosable.
    """
    return f"""@echo off
chcp 65001 >nul
setlocal enableextensions
set "SRC={extract_dir}"
set "DST={install_dir}"
set "EXE={exe}"
set "LOG={log_path}"
echo [start] %date% %time% >> "%LOG%"

rem Kill ANY remaining instances (incl. a "Шууд харах" webview child, same image)
rem so nothing keeps the .exe/DLLs locked during the copy.
taskkill /f /im ChipmoSentryAgent.exe >nul 2>&1
rem ...AND the agent's child processes: the ffmpeg push/decode workers + the local
rem fan-out MediaMTX run from the install's bundled bin and SURVIVE the agent exit,
rem so they keep ffmpeg.exe/mediamtx.exe locked → robocopy returns rc>=8 forever (the
rem multi-day "copy failed after 60 tries" flap that never applied an update). A
rem store PC runs only the agent, so killing by image name is safe; the relaunched
rem agent restarts them.
taskkill /f /im ffmpeg.exe >nul 2>&1
taskkill /f /im mediamtx.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul

set /a n=0
:try
{_robocopy_line(install_dir)}
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
        raise RuntimeError("Dev горимд автомат шинэчлэл боломжгүй — GitHub-аас гараар татна уу.")

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
    bat.write_text(_build_update_script(extract_dir, install_dir, exe, log_path), encoding="utf-8")
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
