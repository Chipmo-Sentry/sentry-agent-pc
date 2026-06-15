"""HTTP snapshot fallback — grab a still JPEG over HTTP the way a browser does.

Some cameras (P2P / Tuya / Skyworth ZHCSDB6 and other OEM units) serve a poor or
no RTSP stream, yet you can still SEE them by opening the camera's IP in a
browser — because the browser pulls the picture over **HTTP** (port 80), not
RTSP (port 554). When every RTSP path fails, the offline grid falls back to
polling one of these HTTP snapshot endpoints: lower framerate than RTSP, but
"if the browser can see it, so can we."

Pure + dependency-light: URL building is testable without a network; fetching
uses httpx (already a dep) and tries Basic then Digest auth (IP cameras split
roughly evenly between the two for snapshots).
"""

from __future__ import annotations

import httpx

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.discovery.snapshot")

# Brand-convention HTTP snapshot paths, most-likely-first. Each is tried as
# http://<host>[:port]<path>. Kept short — one or two per brand — so a camera
# that answers none of them fails fast instead of grinding a long tail.
SNAPSHOT_PATHS: list[str] = [
    "/ISAPI/Streaming/channels/101/picture",  # Hikvision
    "/cgi-bin/snapshot.cgi",  # Dahua / Amcrest / many OEM
    "/onvif-http/snapshot",  # ONVIF Profile-S generic
    "/snapshot.jpg",  # generic
    "/tmpfs/auto.jpg",  # XiongMai / Tuya / Skyworth-class OEM
    "/webcapture.jpg?command=snap&channel=1",  # XiongMai
    "/images/snapshot.jpg",  # UNV (Uniview)
    "/jpg/image.jpg",  # Axis-ish / generic
    "/cgi-bin/snapshot.cgi?channel=1",
    "/snap.jpg",
    "/image/jpeg.cgi",
]

# JPEG start-of-image marker — used to reject HTML error pages that 200 OK.
_JPEG_MAGIC = b"\xff\xd8"


def snapshot_urls(host: str, port: int = 80) -> list[str]:
    """Candidate HTTP snapshot URLs for a camera host, most-likely-first."""
    base = f"http://{host}" if port == 80 else f"http://{host}:{port}"
    return [base + p for p in SNAPSHOT_PATHS]


def fetch_snapshot(
    url: str,
    user: str | None,
    password: str | None,
    *,
    timeout: float = 5.0,
) -> bytes | None:
    """GET a JPEG from an HTTP snapshot endpoint. Never raises.

    Tries Basic then Digest auth (skips auth entirely when no user is given).
    Returns the JPEG bytes on a 200 that actually looks like a JPEG, else None —
    so a 401/403/404 or an HTML error body is treated as "this path isn't it",
    letting the caller move to the next candidate.
    """
    auths: list[httpx.Auth | None]
    if user:
        pw = password or ""
        auths = [httpx.BasicAuth(user, pw), httpx.DigestAuth(user, pw)]
    else:
        auths = [None]
    for auth in auths:
        try:
            resp = httpx.get(url, auth=auth, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError:
            return None  # connection refused / timeout — no point trying the other auth
        if resp.status_code == 200 and resp.content[:2] == _JPEG_MAGIC:
            return resp.content
        if resp.status_code not in (401, 403):
            return None  # 404 etc — this path is wrong; auth won't fix it
    return None
