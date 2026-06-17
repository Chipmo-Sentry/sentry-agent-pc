"""High-level discovery operations callable from both CLI and GUI.

Wraps onvif + manual + rtsp_probe + backend_client into pure data-in/data-out
flows so the UI layer never touches sockets or subprocess directly.
"""

from __future__ import annotations

import contextlib
import re
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.discovery import manual as manual_mod
from sentry_agent_pc.discovery import onvif as onvif_mod
from sentry_agent_pc.discovery import rtsp_probe
from sentry_agent_pc.discovery.rtsp_paths import RTSP_PATHS, RTSP_PATHS_PRIORITY
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


# RTSP ports to brute-force, in order. 554 is standard; some cameras (and
# NVR-fronted setups) expose RTSP on 8554/10554. We try 554 fully first and
# only escalate to alt ports if it finds nothing — so the common case stays
# fast and we don't fan out 3× probes unless needed.
_RTSP_PORTS: tuple[int, ...] = (554, 8554, 10554)


# How long ffmpeg gets to pull a keyframe before we call a path dead. Real
# cameras need real time: a 4 MP H.264 main stream answers in ~3s, an H.265
# stream with a long GOP can take 5-6s (measured: UNV 2.9s, Skyworth 5.7s). The
# old 2s budget timed out the CORRECT path and reported "no stream" — the #1
# cause of "scan found the camera but couldn't connect". 7s clears all three.
_RTSP_PROBE_TIMEOUT_SEC = 7

# Tail concurrency — gentle for the long tail of less-common paths.
_RTSP_PROBE_CONCURRENCY = 3

# Priority-batch concurrency. Capped at 4 (not the full 7) for the LOCKOUT
# reason: a wrong password fails identically on every path, so a wide fan-out
# is a simultaneous-auth-failure BURST that can trip a Hikvision/Dahua account
# lockout before our abort-on-401 fires. Hik's default "illegal login" lock is
# 5 attempts; 4 stays under it. 4 is also exactly enough to keep the diverse
# brand mains in ONE batch (the brand-distinct main paths sit in the first 4 of
# RTSP_PATHS_PRIORITY), so a camera whose other paths HANG (Skyworth) still
# resolves in ~one timeout instead of waiting for hung workers to free up.
_RTSP_PRIORITY_CONCURRENCY = 4

# Hard ceiling on the whole RTSP path search. The good case finishes far sooner
# (priority batch ~one timeout). This bound is for the nasty case: some cameras
# (UNV) neither serve nor 401 on a WRONG password — ffmpeg just hangs to the
# timeout on every path, so we can't tell "bad password" from "bad path" and
# would otherwise grind the whole library for ~100s. Better to give up at ~18s
# and tell the user to check the password.
_RTSP_RESOLVE_DEADLINE_SEC = 18.0


def _best_rtsp_path_stream(
    ip: str, username: str, password: str
) -> tuple[ResolvedStream | None, bool]:
    """Find a working RTSP path for `ip`. Returns (stream, auth_error_seen).

    Two-phase, first-hit-wins. Phase 1 probes the brand main-stream paths
    (RTSP_PATHS_PRIORITY) ALL AT ONCE — so whatever the brand, the real path is
    in the very first batch and we return as soon as it answers (~one probe).
    Phase 2 only runs if phase 1 missed, sweeping the long tail with small
    concurrency. The instant any probe reports an auth rejection (401) we abort:
    a wrong password fails the same on every path, and hammering 33× is what
    trips a Hikvision/Dahua account lockout — caller turns the flag into a
    "check your password" message.

    Tries port 554 first, then 8554/10554 only if 554 found nothing.
    """
    u = urllib.parse.quote(username, safe="")
    p = urllib.parse.quote(password, safe="")
    tail = [path for path in RTSP_PATHS if path not in RTSP_PATHS_PRIORITY]
    auth_error_seen = False
    deadline = time.monotonic() + _RTSP_RESOLVE_DEADLINE_SEC

    for port in _RTSP_PORTS:
        for paths, workers in (
            (RTSP_PATHS_PRIORITY, _RTSP_PRIORITY_CONCURRENCY),
            (tail, _RTSP_PROBE_CONCURRENCY),
        ):
            if time.monotonic() >= deadline:
                return None, auth_error_seen
            urls = [f"rtsp://{u}:{p}@{ip}:{port}{path}" for path in paths]
            stream, auth = _probe_until_hit(urls, workers, deadline)
            auth_error_seen = auth_error_seen or auth
            if stream is not None:
                return stream, auth_error_seen
            if auth:
                # Wrong password — fails the same on every path/port. Stop now
                # (and we only fired up to `workers` auth attempts, not 7+).
                return None, True
    return None, auth_error_seen


def _probe_until_hit(
    urls: list[str], max_workers: int, deadline: float
) -> tuple[ResolvedStream | None, bool]:
    """Probe `urls` concurrently; return (first working stream, auth_seen).

    Returns the MOMENT one probe succeeds (or one reports 401) without waiting
    for the slow ones — some cameras let a wrong path HANG to the full timeout
    rather than 404 quickly, so joining them would throw away all the speed.
    Gives up at `deadline` (monotonic seconds). The abandoned probes are ffmpeg
    subprocesses that self-terminate at their own timeout; we don't block on them.
    """
    def go(url: str) -> ResolvedStream | None:
        r = rtsp_probe.probe(url, timeout_sec=_RTSP_PROBE_TIMEOUT_SEC)
        if r.ok:
            return ResolvedStream(
                ok=True, rtsp_url=url, codec=r.codec, width=r.width, height=r.height,
                via="rtsp-path",
            )
        if r.is_auth_error:
            raise _AuthError
        return None

    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        pending = {ex.submit(go, url) for url in urls}
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, False
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:  # deadline hit before any probe finished
                return None, False
            for fut in done:
                try:
                    res = fut.result()
                except _AuthError:
                    return None, True
                if res is not None:
                    return res, False
        return None, False
    finally:
        # Drop the queued probes and return immediately; don't join the ones
        # already running (a hung wrong-path probe would stall us for seconds).
        ex.shutdown(wait=False, cancel_futures=True)


class _AuthError(Exception):
    """Internal signal: a probe got an RTSP 401 — stop, the password is wrong."""


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
    # ONVIF is best-effort: in the frozen .exe a missing WSDL/XSD can raise a
    # FileNotFoundError ("[Errno 2]") deep in zeep — that must NOT crash the
    # whole scan. Any ONVIF failure falls through to the RTSP path brute-force,
    # which resolves every camera whose path is in the library anyway.
    try:
        best = _best_onvif_stream(ip, username, password, xaddr)
    except Exception as e:  # noqa: BLE001 — onvif/zeep raises a sea of types
        log.info("resolve.onvif_error", ip=ip, error=str(e))
        best = None
    if best is not None:
        log.info("resolve.ok", ip=ip, via=best.via, codec=best.codec,
                 res=f"{best.width}x{best.height}")
        return best

    best, auth_error = _best_rtsp_path_stream(ip, username, password)
    if best is not None:
        log.info("resolve.ok", ip=ip, via=best.via, codec=best.codec,
                 res=f"{best.width}x{best.height}")
        return best

    if auth_error:
        return ResolvedStream(
            ok=False,
            error=(
                "Нэр/нууц үг буруу (401). Нууц үгээ яг таг шалгана уу — тусгай "
                "тэмдэгт (ж: '*') орсон бол анхаарна уу. Hikvision/Dahua дээр "
                "хэд дахин буруу оролдвол данс түр түгждэг тул хэдэн минут "
                "хүлээгээд дахин үзнэ үү."
            ),
        )
    return ResolvedStream(
        ok=False,
        error=(
            "Стрим олдсонгүй. Камер RTSP-г дэмжихгүй, эсвэл стандарт бус зам/порт "
            "ашиглаж байж магадгүй — 'Камер нэмэх'-ээр замыг гараар оруулна уу."
        ),
    )


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


def scan(
    timeout_sec: float = 5.0,
    *,
    on_found: Callable[[DiscoveredCandidate], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> list[DiscoveredCandidate]:
    """Discover cameras by BOTH ONVIF WS-Discovery AND a LAN port-554 sweep.

    ONVIF finds ONVIF-enabled cameras (with their device-service xaddr). The
    RTSP sweep finds every host with port 554 open — so cameras whose ONVIF is
    disabled (e.g. Hikvision out of the box) still show up and can be added via
    the credential-based resolver. Dedups by IP; marks already-registered ones.

    `on_found` fires (on the worker thread) for each NEW unregistered camera as
    it's discovered, so the UI can stream rows in instead of waiting for the
    whole sweep. `on_phase` reports the current step for a live status line.
    Both are best-effort; the full sorted list is still returned.
    """
    state = load_state()
    known_ips = {c.ip for c in state.cameras}
    by_ip: dict[str, DiscoveredCandidate] = {}

    def _emit(cand: DiscoveredCandidate) -> None:
        if on_found is not None and not cand.already_registered:
            with contextlib.suppress(Exception):
                on_found(cand)

    if on_phase is not None:
        with contextlib.suppress(Exception):
            on_phase("ONVIF камер хайж байна…")
    for d in onvif_mod.discover(timeout_sec=timeout_sec):
        cand = DiscoveredCandidate(ip=d.ip, xaddr=d.xaddr, already_registered=d.ip in known_ips)
        by_ip[d.ip] = cand
        _emit(cand)

    if on_phase is not None:
        with contextlib.suppress(Exception):
            on_phase("Сүлжээний RTSP (554) портыг сканердаж байна…")

    def _on_host(ip: str) -> None:
        if ip not in by_ip:
            cand = DiscoveredCandidate(ip=ip, xaddr="", already_registered=ip in known_ips)
            by_ip[ip] = cand
            _emit(cand)

    _sweep_rtsp_hosts(on_host=_on_host)

    # Sort: unregistered first, then by IP.
    return sorted(
        by_ip.values(), key=lambda c: (c.already_registered, _ip_sort_key(c.ip))
    )


def _ip_sort_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(o) for o in ip.split("."))
    except ValueError:
        return (999,)


def _sweep_rtsp_hosts(
    port: int = 554,
    timeout: float = 0.5,
    *,
    on_host: Callable[[str], None] | None = None,
) -> list[str]:
    """Scan every local /24 for hosts with `port` (RTSP) open. Best-effort.

    `on_host` fires for each open host AS it's found (live results for the UI)."""
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
                if on_host is not None:
                    with contextlib.suppress(Exception):
                        on_host(r)
    log.info("sweep.rtsp_hosts", count=len(found))
    return found


def reconcile_with_backend(
    backend: BackendClient | None = None,
) -> tuple[list[CameraRecord], bool]:
    """Make local state agree with the backend (the source of truth for which
    cameras exist). Returns (cameras_to_display, changed).

    - Camera deleted on the web → it's gone from the backend list → DROP it
      locally (so desktop ↔ web always match, and we stop pushing it).
    - Camera still on the backend → keep the local record (it holds the
      rtsp_url + credentials needed to push) and refresh its name/path.
    - Camera on the backend but missing locally (e.g. registered from another
      PC, or local state was lost) → surface it WITHOUT an rtsp_url so the user
      sees it; it can't be pushed until re-added (no stored credentials).

    Best-effort: on any backend error (offline) returns local state unchanged,
    so a flaky uplink never wipes the list. Only a SUCCESSFUL fetch prunes.
    """
    state = load_state()
    if not state.is_paired:
        return state.cameras, False
    client = backend or BackendClient()
    try:
        remote = client.agent_list_cameras()
    except Exception as e:  # noqa: BLE001 — offline / any error must not wipe local
        log.info("reconcile.skipped_offline", error=str(e))
        return state.cameras, False

    remote_by_id = {str(c.get("id")): c for c in remote if c.get("id")}
    kept: list[CameraRecord] = []
    for cam in state.cameras:
        rc = remote_by_id.pop(cam.uuid, None) if cam.uuid else None
        if rc is None:
            continue  # deleted on the web → drop locally
        cam.name = str(rc.get("name") or cam.name)
        cam.mediamtx_path = rc.get("mediamtx_path") or cam.mediamtx_path
        kept.append(cam)
    # Backend cameras we have no local record for: show them (no creds to push).
    for rc in remote_by_id.values():
        kept.append(
            CameraRecord(
                uuid=str(rc.get("id")),
                name=str(rc.get("name") or "?"),
                ip="",
                rtsp_url="",
                mediamtx_path=rc.get("mediamtx_path"),
            )
        )

    changed = [c.uuid for c in kept] != [c.uuid for c in state.cameras]
    if changed:
        state.cameras = kept
        save_state(state)
        log.info("reconcile.applied", kept=len(kept))
    return kept, changed


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
    resolved: ResolvedStream | None = None,
) -> RegisterResult:
    """Probe RTSP, register with the backend (agent-scoped), persist locally.

    The target store is determined by the agent's paired token, so no store_id
    is needed here. Backend is created from settings if not supplied.

    `resolved` lets the Scan flow hand in the stream it JUST verified, so we
    skip a second ffmpeg pull on the same URL (faster, and one fewer RTSP
    session on cameras that limit them). Manual "Add" passes None → we probe.
    """
    # Dedup by IP: scan already skips known IPs, but manual "Add" did not — so
    # adding the same camera twice used to create duplicate backend rows. Bail
    # early (before the ffmpeg probe) if this IP is already registered locally.
    if any(c.ip == ip for c in load_state().cameras):
        return RegisterResult(
            ok=False,
            error=f"Энэ IP ({ip}) аль хэдийн бүртгэлтэй. Дахин нэмэхийн өмнө хуучныг устгана уу.",
        )

    if resolved is not None and resolved.ok:
        # Already verified during resolve — trust it, don't re-pull the stream.
        probe = rtsp_probe.ProbeResult(
            ok=True, url=rtsp_url, codec=resolved.codec,
            width=resolved.width, height=resolved.height,
            is_h264=resolved.is_h264,
        )
    else:
        probe = rtsp_probe.probe(rtsp_url)
    if not probe.ok:
        err = probe.error or ""
        if probe.is_auth_error or "401" in err or "authorization failed" in err.lower():
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


def update_camera_connection(
    *,
    camera_uuid: str | None,
    name: str | None = None,
    ip: str | None = None,
    rtsp_url: str | None = None,
    risk_threshold: float | None = None,
    resolved: ResolvedStream | None = None,
    backend: BackendClient | None = None,
) -> RegisterResult:
    """Edit an existing camera: PATCH the backend + update local state.

    Only the provided fields change. When `rtsp_url` changes the backend
    re-points its live worker at the new source, and `resolved` (the freshly
    verified stream) refreshes the local codec/resolution. `mediamtx_path` is
    never changed here — it is the stream identity the live pipeline keys on.
    """
    state = load_state()
    target = next((c for c in state.cameras if c.uuid == camera_uuid), None)
    if target is None:
        return RegisterResult(ok=False, error="Камер дотоод бүртгэлд олдсонгүй.")

    # Guard: don't let an edit move this camera onto another camera's IP.
    if ip and any(c.ip == ip and c.uuid != camera_uuid for c in state.cameras):
        return RegisterResult(
            ok=False,
            error=f"Энэ IP ({ip}) өөр камер дээр бүртгэлтэй байна.",
        )

    # Backend PATCH first — if it fails we keep local state untouched so the
    # desktop list never diverges from the server.
    if camera_uuid:
        client = backend or BackendClient()
        try:
            client.agent_update_camera(
                camera_uuid,
                name=name,
                rtsp_url=rtsp_url,
                risk_threshold=risk_threshold,
            )
        except BackendError as e:
            return RegisterResult(ok=False, error=str(e))

    # Apply to local state.
    if name is not None:
        target.name = name
    if ip:
        target.ip = ip
    if rtsp_url is not None:
        target.rtsp_url = rtsp_url
    if resolved is not None and resolved.ok:
        target.codec = resolved.codec or target.codec
        if resolved.width and resolved.height:
            target.resolution = (resolved.width, resolved.height)
    save_state(state)

    return RegisterResult(
        ok=True,
        camera_uuid=camera_uuid,
        mediamtx_path=target.mediamtx_path,
        codec=target.codec,
        resolution=target.resolution,
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
