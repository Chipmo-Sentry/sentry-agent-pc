"""High-level discovery operations callable from both CLI and GUI.

Wraps onvif + manual + rtsp_probe + backend_client into pure data-in/data-out
flows so the UI layer never touches sockets or subprocess directly.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from sentry_agent_pc.backend_client import BackendClient, BackendError, CameraRegistration
from sentry_agent_pc.discovery import manual as manual_mod
from sentry_agent_pc.discovery import onvif as onvif_mod
from sentry_agent_pc.discovery import rtsp_probe
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.state import CameraRecord, load_state, save_state

log = get_logger("sentry_agent_pc.services.discovery")


@dataclass(slots=True)
class DiscoveredCandidate:
    """One ONVIF device shown in the scan results dialog before user approves."""

    ip: str
    xaddr: str
    manufacturer: str | None = None
    model: str | None = None
    # If we've already managed to grab profiles (auth optional), this carries them.
    profiles: list[onvif_mod.OnvifProfile] = field(default_factory=list)
    # Set by post-auth fetch_profiles or post-probe.
    error: str | None = None
    # Was this IP already registered? (skip in default selection)
    already_registered: bool = False


@dataclass(slots=True)
class RegisterResult:
    ok: bool
    camera_uuid: str | None = None
    mediamtx_path: str | None = None
    codec: str | None = None
    resolution: tuple[int, int] | None = None
    error: str | None = None


def scan(timeout_sec: float = 5.0) -> list[DiscoveredCandidate]:
    """Run ONVIF WS-Discovery, return raw candidates (no auth yet).

    Marks already-registered IPs from local state so the UI can default-deselect.
    """
    state = load_state()
    known_ips = {c.ip for c in state.cameras}
    devices = onvif_mod.discover(timeout_sec=timeout_sec)
    out: list[DiscoveredCandidate] = []
    for d in devices:
        out.append(
            DiscoveredCandidate(
                ip=d.ip,
                xaddr=d.xaddr,
                already_registered=d.ip in known_ips,
            ),
        )
    return out


def authenticate_and_fetch(
    candidate: DiscoveredCandidate,
    username: str,
    password: str,
) -> DiscoveredCandidate:
    """Mutate `candidate` in place with manufacturer/model/profiles or error."""
    device = onvif_mod.OnvifDevice(xaddr=candidate.xaddr, ip=candidate.ip)
    device = onvif_mod.fetch_profiles(device, username, password)
    candidate.manufacturer = device.manufacturer
    candidate.model = device.model
    candidate.profiles = device.profiles
    candidate.error = device.error
    return candidate


def pick_h264_profile(
    candidate: DiscoveredCandidate,
) -> onvif_mod.OnvifProfile | None:
    """First profile with rtsp_uri AND H.264 encoding."""
    for p in candidate.profiles:
        enc = (p.encoding or "").lower()
        if p.rtsp_uri and enc == "h264":
            return p
    return None


def embed_credentials(rtsp_uri: str, username: str, password: str) -> str:
    """Inject user:pass into RTSP URL if not already present (URL-encoded)."""
    m = re.match(r"(rtsp://)(?:[^/@]+@)?(.*)", rtsp_uri)
    if not m:
        return rtsp_uri
    user_enc = urllib.parse.quote(username, safe="")
    pass_enc = urllib.parse.quote(password, safe="")
    return f"{m.group(1)}{user_enc}:{pass_enc}@{m.group(2)}"


def register_camera(
    *,
    name: str,
    ip: str,
    rtsp_url: str,
    backend: BackendClient | None = None,
) -> RegisterResult:
    """Probe RTSP, register with backend, persist locally. Pure data in/out.

    Backend is created from settings if not supplied.
    """
    probe = rtsp_probe.probe(rtsp_url)
    if not probe.ok:
        return RegisterResult(ok=False, error=f"RTSP probe failed: {probe.error}")
    if not probe.is_h264:
        return RegisterResult(
            ok=False,
            error=f"Камер {probe.codec} буцаалаа — H.264 шаардлагатай. Web UI-аас codec солих.",
        )

    client = backend or BackendClient()
    try:
        stores = client.list_stores()
    except BackendError as e:
        return RegisterResult(ok=False, error=f"Stores list failed: {e}")
    if not stores:
        return RegisterResult(ok=False, error="Backend дээр store байхгүй.")
    store_id = stores[0]["id"]

    try:
        reg = CameraRegistration(
            store_id=store_id,
            name=name,
            rtsp_url=rtsp_url,
            mediamtx_path=None,  # backend auto-allocates unique slug
        )
        created = client.register_camera(reg)
    except BackendError as e:
        return RegisterResult(ok=False, error=str(e))

    cam_uuid = str(created.get("id"))
    mediamtx_path = created.get("mediamtx_path")

    # Persist locally
    state = load_state()
    state.cameras.append(
        CameraRecord(
            uuid=cam_uuid,
            name=name,
            ip=ip,
            rtsp_url=rtsp_url,
            mediamtx_path=mediamtx_path,
            codec=probe.codec,
            resolution=(probe.width or 0, probe.height or 0),
        ),
    )
    save_state(state)

    return RegisterResult(
        ok=True,
        camera_uuid=cam_uuid,
        mediamtx_path=mediamtx_path,
        codec=probe.codec,
        resolution=(probe.width or 0, probe.height or 0),
    )


def build_manual_url(
    brand_key: str,
    *,
    host: str,
    username: str,
    password: str,
    port: int | None = None,
    custom_path: str | None = None,
    use_sub: bool = False,
) -> tuple[str, list[str]]:
    """Return (preferred_url, all_candidate_urls) for a brand template."""
    template = manual_mod.get_brand(brand_key)
    if template is None:
        raise ValueError(f"unknown brand: {brand_key}")
    if custom_path:
        url = manual_mod.build_rtsp_url(
            template, host, username, password,
            port=port, custom_path=custom_path,
        )
        return url, [url]
    main = manual_mod.build_rtsp_url(
        template, host, username, password, port=port, use_sub=use_sub,
    )
    candidates = manual_mod.candidate_urls(template, host, username, password, port=port)
    return main, candidates


def probe_first_h264(urls: list[str]) -> rtsp_probe.ProbeResult:
    return rtsp_probe.probe_first_h264(urls)
