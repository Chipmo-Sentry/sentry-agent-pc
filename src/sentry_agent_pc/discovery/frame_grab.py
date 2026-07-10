"""Grab ONE still frame from a camera — the freeze-frame the zone editor draws on.

A single-shot version of the local-view reader: it opens the camera's stream,
reads the first decodable frame, and returns it as a PIL image, then releases the
capture. RTSP first (via the local MediaMTX fan-out when up, else the stored URL),
with the browser-style HTTP-snapshot path as a last resort for OEM cameras that
serve no usable RTSP. Pure-ish + dependency-light: cv2/numpy/PIL are imported
lazily inside the grab so importing this module never pulls OpenCV.

Used by gui/zone_editor.py off the UI thread (it can block for several seconds on
a long-GOP H.265 stream), so it must NEVER raise — it returns a StillResult with
``ok=False`` + a Mongolian error message instead.
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from PIL import Image

    from sentry_agent_pc.state import CameraRecord

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.discovery.frame_grab")

# RTSP over TCP + low-latency demux, same as the live grid. Set before the first
# cv2.VideoCapture; setdefault so we never clobber a value local_view already set.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|reorder_queue_size;0|max_delay;500000",
)

# Long enough for an H.265 long-GOP stream to emit its first decodable keyframe
# under load (local_view uses 12s for the same reason); the editor grab is a
# one-time wait the user is happy to spend before drawing.
_PRIME_TIMEOUT_SEC = 12.0
# Separate bounded budget for the HTTP-snapshot sweep (~11 paths × Basic+Digest ×
# 5s would otherwise be ~110s). Total worst-case grab ≈ _PRIME_TIMEOUT_SEC + this.
_SNAPSHOT_BUDGET_SEC = 8.0
# Cloud-HLS fallback: playlist fetch + first decodable segment through the
# backend proxy. Generous because low-latency HLS still needs a segment or two.
_CLOUD_BUDGET_SEC = 12.0


@dataclass(slots=True)
class StillResult:
    """One grabbed still, or a reason it couldn't be grabbed."""

    ok: bool
    image: Image.Image | None = None
    width: int | None = None
    height: int | None = None
    source: str | None = None  # "rtsp" | "snapshot" | "cloud"
    error: str | None = None


def _rtsp_candidates(cam: CameraRecord) -> list[str]:
    """RTSP URLs to try, local MediaMTX fan-out first (shares the push relay's
    single pull), then the stored direct URL. Empty if the camera has no URL."""
    urls: list[str] = []
    with contextlib.suppress(Exception):  # streaming layer is optional / may be down
        from sentry_agent_pc.streaming.controller import get_stream_controller

        local = get_stream_controller().local_url(cam.mediamtx_path)
        if local:
            urls.append(local)
    if cam.rtsp_url:
        urls.append(cam.rtsp_url)
    return urls


def _grab_rtsp(url: str, deadline: float) -> Image.Image | None:
    """Open `url`, read the first decodable frame as an RGB PIL image, release.

    HW-accel hint first (offloads decode to the iGPU when supported; OpenCV
    silently falls back to software), then a plain software open. Returns None on
    any failure so the caller moves to the next candidate."""
    import cv2

    attempts: list[list[int]] = [
        [int(cv2.CAP_PROP_HW_ACCELERATION), int(cv2.VIDEO_ACCELERATION_ANY)],
        [],  # plain software open — always tried as the fallback
    ]
    for params in attempts:
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
        except Exception:  # noqa: BLE001 — some builds reject the params arg
            continue
        with contextlib.suppress(Exception):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            if cap.isOpened():
                img = _prime(cap, deadline)
                if img is not None:
                    return img
        finally:
            with contextlib.suppress(Exception):
                cap.release()
        if time.monotonic() >= deadline:
            break
    return None


def _prime(cap: object, deadline: float) -> Image.Image | None:
    """Read frames until one decodes (or the deadline passes) → RGB PIL image."""
    import cv2
    import numpy as np
    from PIL import Image

    while time.monotonic() < deadline:
        ok, frame = cap.read()  # type: ignore[attr-defined]
        if ok and frame is not None:
            arr = cast("np.ndarray", frame)
            if not isinstance(arr, np.ndarray) or arr.ndim < 2:
                continue
            ih, iw = arr.shape[:2]
            if iw <= 0 or ih <= 0:
                continue
            if arr.ndim == 2:  # grayscale → BGR
                arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            if arr.ndim != 3 or arr.shape[2] != 3:
                continue
            rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb)
        time.sleep(0.02)  # pre-keyframe reads return empty instantly; don't busy-spin
    return None


def _grab_snapshot(cam: CameraRecord, deadline: float) -> Image.Image | None:
    """Browser-style HTTP snapshot fallback (Hik/Dahua/ONVIF/OEM conventions).

    Bounded by `deadline` (monotonic seconds): there are ~11 candidate paths and
    each fetch_snapshot tries Basic+Digest at 5s timeout, so an unbounded sweep
    could block the worker thread ~110s with the editor spinner stuck. We stop
    trying paths once the deadline passes."""
    from sentry_agent_pc.discovery.snapshot import fetch_snapshot, snapshot_urls
    from sentry_agent_pc.gui.edit_dialog import parse_rtsp

    parts = parse_rtsp(cam.rtsp_url) if cam.rtsp_url else {}
    host = parts.get("host")
    if not host:
        return None
    import cv2
    import numpy as np
    from PIL import Image

    user = parts.get("user") or None
    pwd = parts.get("password") or None
    for url in snapshot_urls(host):
        if time.monotonic() >= deadline:
            break
        data = fetch_snapshot(url, user, pwd)
        if not data:
            continue
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            continue
        return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
    return None


def _cloud_hls_url(cam: CameraRecord) -> str | None:
    """Backend-proxied HLS URL for the camera's cloud stream, or None.

    The calibration fallback for a machine that can't reach the camera's LAN at
    all — e.g. the camera was registered from ANOTHER store PC (its RTSP
    credentials never leave that machine), or the operator is off-site. The
    camera is usually still streaming to the cloud through that other machine's
    relay, so the backend can serve a frame via its authed HLS proxy."""
    if not cam.uuid or not cam.mediamtx_path:
        return None
    from sentry_agent_pc.backend_client import BackendClient
    from sentry_agent_pc.settings import get_settings

    try:
        res = BackendClient().agent_camera_stream_token(cam.uuid)
    except Exception as e:  # noqa: BLE001 — offline backend just means no fallback
        log.info("frame_grab.cloud_token_failed", error=str(e)[:160])
        return None
    hls = res.get("hls_url")
    if not hls:
        return None
    return f"{get_settings().backend_url.rstrip('/')}{hls}"


def grab_still(cam: CameraRecord, *, timeout_sec: float = _PRIME_TIMEOUT_SEC) -> StillResult:
    """Grab one still frame for `cam`. Never raises — returns ok=False on failure.

    Tries each RTSP candidate (local fan-out first), then the HTTP snapshot path,
    then the camera's CLOUD stream through the backend's HLS proxy (the only
    reachable source when this machine isn't on the camera's LAN).
    The returned image is at the camera's native resolution; the editor scales it
    to fit and normalizes coordinates against the DISPLAYED size (docs/29)."""
    if not cam.rtsp_url and not cam.mediamtx_path:
        return StillResult(
            ok=False,
            error="Энэ камер энэ компьютер дээр стримгүй (өөр компьютероос бүртгэгдсэн).",
        )
    try:
        import cv2  # noqa: F401  — surface a missing OpenCV as a clear error, not a crash
    except Exception as e:  # noqa: BLE001
        log.error("frame_grab.cv2_import_failed", error=str(e))
        return StillResult(ok=False, error="OpenCV ачаалагдсангүй (cv2).")

    deadline = time.monotonic() + max(2.0, timeout_sec)
    for url in _rtsp_candidates(cam):
        if time.monotonic() >= deadline:
            break
        try:
            img = _grab_rtsp(url, deadline)
        except Exception as e:  # noqa: BLE001 — one bad URL must not abort the grab
            log.info("frame_grab.rtsp_error", error=str(e)[:160])
            img = None
        if img is not None:
            return StillResult(
                ok=True, image=img, width=img.width, height=img.height, source="rtsp"
            )

    # RTSP missed → browser-style HTTP snapshot (the camera may have a picture
    # endpoint). Give it its OWN bounded budget so a camera that answers neither
    # RTSP nor snapshot can't leave the editor spinner stuck for ~2 minutes.
    try:
        snap = _grab_snapshot(cam, time.monotonic() + _SNAPSHOT_BUDGET_SEC)
    except Exception as e:  # noqa: BLE001
        log.info("frame_grab.snapshot_error", error=str(e)[:160])
        snap = None
    if snap is not None:
        return StillResult(
            ok=True, image=snap, width=snap.width, height=snap.height, source="snapshot"
        )

    # LAN missed entirely → cloud fallback: pull one frame from the camera's
    # cloud HLS through the backend proxy (ffmpeg reads the m3u8 and follows the
    # proxy's redirect to the relaying agent's tunnel when one is up).
    cloud_url = _cloud_hls_url(cam)
    if cloud_url:
        try:
            img = _grab_rtsp(cloud_url, time.monotonic() + _CLOUD_BUDGET_SEC)
        except Exception as e:  # noqa: BLE001 — fallback must degrade, not crash
            log.info("frame_grab.cloud_error", error=str(e)[:160])
            img = None
        if img is not None:
            return StillResult(
                ok=True, image=img, width=img.width, height=img.height, source="cloud"
            )

    return StillResult(
        ok=False,
        error=(
            "Камераас зураг авч чадсангүй. Камер асаалттай, сүлжээнд холбогдсон "
            "эсэхээ шалгаад дахин оролдоно уу."
        ),
    )
