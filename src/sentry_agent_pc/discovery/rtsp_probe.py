"""RTSP probe — does this URL actually return an H.264 stream we can use?

Spawns `ffmpeg -rtsp_transport tcp -i <url> -t 2 -f null -` and parses stderr.
This is the same technique sentry-ingest's MediaMTX uses internally and matches
what the M1 testing playbook uses (см. docs/12-TESTING.md).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.resources import resolve_ffmpeg_exe
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.discovery.rtsp_probe")

# Windows: hide the ffmpeg console window when launched from the GUI .exe.
# Without this, every probe (and a scan fans out dozens at once) pops a
# flashing terminal. Mirrors streaming/pusher.py.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Sample ffmpeg lines we care about:
#   Stream #0:0: Video: h264 (Main), yuvj420p(pc, bt709), 1920x1080, 25 tbr, 90k tbn
#   Stream #0:0: Video: hevc (Main), yuv420p(tv, bt709), 2688x1520, 20 fps, 50 tbr, 90k tbn
_CODEC_RE = re.compile(
    r"Video:\s+(h264|hevc|h265|mjpeg)\b.*?(\d{3,5})x(\d{3,5})",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    url: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None
    is_h264: bool = False
    # True when the camera answered but REJECTED the credentials (RTSP 401 /
    # Unauthorized). Lets the resolver stop brute-forcing paths the instant it
    # learns the password is wrong — a wrong password never gets righter on a
    # different path, and hammering risks a Hikvision/Dahua account lockout.
    is_auth_error: bool = False


def _looks_like_auth_error(text: str) -> bool:
    t = text.lower()
    return "401" in t or "unauthorized" in t or "authorization failed" in t


def probe(url: str, timeout_sec: int | None = None) -> ProbeResult:
    """Run ffmpeg briefly to verify the stream + identify codec/resolution.

    Returns ProbeResult — never raises. On any failure ok=False with error.
    """
    settings = get_settings()
    timeout = timeout_sec or settings.rtsp_probe_timeout_sec

    args = [
        resolve_ffmpeg_exe(settings.ffmpeg_path),
        "-hide_banner",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-t", "2",
        "-f", "null",
        "-",
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout + 5,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as e:
        return ProbeResult(ok=False, url=url, error=f"ffmpeg not found: {e}")
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, url=url, error=f"timeout after {timeout}s")

    stderr = proc.stderr.decode("utf-8", errors="replace")
    m = _CODEC_RE.search(stderr)
    if not m:
        # Maybe auth or connection error — extract last "error" line for hint
        err_line = _last_error_line(stderr)
        return ProbeResult(
            ok=False,
            url=url,
            error=err_line or f"no Stream/Video line (ffmpeg exit {proc.returncode})",
            is_auth_error=_looks_like_auth_error(stderr),
        )

    codec = m.group(1).lower()
    width = int(m.group(2))
    height = int(m.group(3))
    is_h264 = codec == "h264"
    return ProbeResult(
        ok=True,
        url=url,
        codec=codec,
        width=width,
        height=height,
        is_h264=is_h264,
    )


def _last_error_line(stderr: str) -> str | None:
    """Extract the most informative-looking error line from ffmpeg stderr."""
    interesting_keywords = (
        "401", "403", "Connection refused", "timed out",
        "Server returned", "Invalid data", "Unauthorized",
    )
    for line in reversed(stderr.splitlines()):
        for kw in interesting_keywords:
            if kw.lower() in line.lower():
                return line.strip()[-200:]
    return None


def probe_first_h264(urls: list[str]) -> ProbeResult:
    """Try each URL in order, return the first H.264 hit.

    If all are H.265, returns the FIRST H.265 result so caller can give a
    useful error message ("camera served H.265 — change codec in web UI").
    """
    fallback: ProbeResult | None = None
    for url in urls:
        r = probe(url)
        if r.ok and r.is_h264:
            return r
        if r.ok and fallback is None:
            fallback = r
    if fallback is not None:
        return fallback
    # All failed entirely — return the last error
    return r  # noqa: F821 — `r` defined in the loop
