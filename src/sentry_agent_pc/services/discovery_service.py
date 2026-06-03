"""High-level discovery operations callable from both CLI and GUI.

Wraps onvif + manual + rtsp_probe + backend_client into pure data-in/data-out
flows so the UI layer never touches sockets or subprocess directly.
"""

from __future__ import annotations

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.discovery import manual as manual_mod
from sentry_agent_pc.discovery import onvif as onvif_mod
from sentry_agent_pc.discovery import rtsp_probe
from sentry_agent_pc.discovery.rtsp_paths import RTSP_PATHS
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.state import CameraRecord, load_state, save_state

log = get_logger("sentry_agent_pc.services.discovery")


@dataclass(slots=True)
class ResolvedStream:
    """A working RTSP stream found for a camera (creds embedded in rtsp_url)."""

    ok: bool
    rtsp_url: str | None = None
    codec: str | None = None       # "h264" | "hevc" | ...
    width: int | None = None
    height: int | None = None
    via: str | None = None         # "onvif" | "rtsp-path"
    error: str | None = None

    @property
    def is_h264(self) -> bool:
        return (self.codec or "").lower() == "h264"


def _score(codec: str | None, width: int | None, height: int | None) -> tuple[int, int]:
    """Rank a candidate: prefer H.264 (browser-friendly), then larger frame."""
    return (1 if (codec or "").lower() == "h264" else 0, (width or 0) * (height or 0))


def _best_onvif_stream(
    ip: str, username: str, password: str, xaddr: str
) -> ResolvedStream | None:
    """Pick the best (H.264, highest-res) ONVIF profile.

    Trusts ONVIF's codec/resolution metadata rather than probing each profile:
    probing a high-res main stream can exceed the timeout and wrongly fall back
    to the sub stream. register_camera probes the chosen URL anyway.
    """
    device = onvif_mod.OnvifDevice(xaddr=xaddr, ip=ip)
    device = onvif_mod.fetch_profiles(device, username, password)
    cands = [p for p in device.profiles if p.rtsp_uri]
    if not cands:
        return None
    cands.sort(key=lambda p: _score(p.encoding, p.width, p.height), reverse=True)
    best = cands[0]
    return ResolvedStream(
        ok=True,
        rtsp_url=embed_credentials(best.rtsp_uri or "", username, password),
        codec=(best.encoding or "").lower() or None,
        width=best.width,
        height=best.height,
        via="onvif",
    )


def _best_rtsp_path_stream(ip: str, username: str, password: str) -> ResolvedStream | None:
    """Brute-force the RTSP path library (parallel) and pick the best hit.

    Credentials are correct here (wrong path ≠ auth failure), so concurrency
    won't trip account lockouts. Kept modest (5 workers) to be gentle.
    """
    u = urllib.parse.quote(username, safe="")
    p = urllib.parse.quote(password, safe="")
    urls = [f"rtsp://{u}:{p}@{ip}:554{path}" for path in RTSP_PATHS]

    def go(url: str) -> ResolvedStream | None:
        r = rtsp_probe.probe(url, timeout_sec=2)
        if r.ok:
            return ResolvedStream(
                ok=True, rtsp_url=url, codec=r.codec, width=r.width, height=r.height,
                via="rtsp-path",
            )
        return None

    hits: list[ResolvedStream] = []
    # Correct creds + wrong path ≠ auth failure, so 10 concurrent probes won't
    # trip lockouts; this keeps a full-library sweep to ~10-15s.
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(go, urls):
            if res is not None:
                hits.append(res)
    if not hits:
        return None
    hits.sort(key=lambda s: _score(s.codec, s.width, s.height), reverse=True)
    return hits[0]


def resolve_stream(
    ip: str,
    username: str,
    password: str,
    *,
    onvif_xaddr: str | None = None,
) -> ResolvedStream:
    """Find a usable RTSP stream for `ip` by ANY means: ONVIF first (clean,
    no hammering), then a brute-force of known brand RTSP paths. Accepts H.264
    AND H.265, preferring H.264 + the highest resolution (main stream).

    This is what lets one "Scan/Add" connect Hikvision (RTSP, no ONVIF user),
    UNV (ONVIF), and Skyworth (H.265 at /stream1) alike.
    """
    xaddr = onvif_xaddr or f"http://{ip}:80/onvif/device_service"
    best = _best_onvif_stream(ip, username, password, xaddr)
    if best is None:
        best = _best_rtsp_path_stream(ip, username, password)
    if best is None:
        return ResolvedStream(
            ok=False,
            error=(
                "Стрим олдсонгүй. Нэр/нууц үгээ шалгана уу (тусгай тэмдэгт орсныг "
                "анхаар), эсвэл камер RTSP-г дэмжихгүй байж магадгүй."
            ),
        )
    log.info("resolve.ok", ip=ip, via=best.via, codec=best.codec,
             res=f"{best.width}x{best.height}")
    return best


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
    """Discover cameras by BOTH ONVIF WS-Discovery AND a LAN port-554 sweep.

    ONVIF finds ONVIF-enabled cameras (with their device-service xaddr). The
    RTSP sweep finds every host with port 554 open — so cameras whose ONVIF is
    disabled (e.g. Hikvision out of the box) still show up and can be added via
    the credential-based resolver. Dedups by IP; marks already-registered ones.
    """
    state = load_state()
    known_ips = {c.ip for c in state.cameras}

    by_ip: dict[str, DiscoveredCandidate] = {}
    for d in onvif_mod.discover(timeout_sec=timeout_sec):
        by_ip[d.ip] = DiscoveredCandidate(
            ip=d.ip, xaddr=d.xaddr, already_registered=d.ip in known_ips
        )
    for ip in _sweep_rtsp_hosts():
        if ip not in by_ip:
            by_ip[ip] = DiscoveredCandidate(
                ip=ip, xaddr="", already_registered=ip in known_ips
            )

    # Sort: unregistered first, then by IP.
    return sorted(
        by_ip.values(), key=lambda c: (c.already_registered, _ip_sort_key(c.ip))
    )


def _ip_sort_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(o) for o in ip.split("."))
    except ValueError:
        return (999,)


def _sweep_rtsp_hosts(port: int = 554, timeout: float = 0.5) -> list[str]:
    """Scan every local /24 for hosts with `port` (RTSP) open. Best-effort."""
    import ipaddress
    import socket

    targets: set[str] = set()
    for local_ip in onvif_mod._local_ipv4_addresses():
        try:
            net = ipaddress.ip_network(f"{local_ip}/24", strict=False)
        except ValueError:
            continue
        targets.update(str(h) for h in net.hosts())
    if not targets:
        return []

    def check(host: str) -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return host
        except OSError:
            return None
        finally:
            s.close()

    found: list[str] = []
    with ThreadPoolExecutor(max_workers=100) as ex:
        for r in ex.map(check, sorted(targets)):
            if r is not None:
                found.append(r)
    log.info("sweep.rtsp_hosts", count=len(found))
    return found


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
    """Probe RTSP, register with the backend (agent-scoped), persist locally.

    The target store is determined by the agent's paired token, so no store_id
    is needed here. Backend is created from settings if not supplied.
    """
    probe = rtsp_probe.probe(rtsp_url)
    if not probe.ok:
        err = probe.error or ""
        if "401" in err or "Unauthorized" in err or "authorization failed" in err.lower():
            msg = (
                "Нэр/нууц үг буруу (401). Нууц үгээ шалгана уу — тусгай тэмдэгт "
                "(ж: '*') орсон бол яг таг бичих. Hikvision/Dahua дээр хэд дахин "
                "буруу оролдвол данс түр түгжигддэг тул хэдэн минут хүлээгээд дахин үзнэ үү."
            )
        else:
            msg = f"RTSP холбогдсонгүй: {err}"
        return RegisterResult(ok=False, error=msg)
    codec = (probe.codec or "").lower()
    if codec not in ("h264", "hevc", "h265"):
        return RegisterResult(
            ok=False,
            error=f"Камер {probe.codec or '?'} буцаалаа — H.264/H.265 дэмжинэ. Web UI-аас codec солих.",
        )
    # H.265 (hevc) is accepted: AI decodes it fine; the cloud push transcodes
    # it to H.264 for browser viewing (browsers can't play HEVC over WebRTC/HLS).

    client = backend or BackendClient()
    try:
        created = client.agent_register_camera(name=name, rtsp_url=rtsp_url)
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
