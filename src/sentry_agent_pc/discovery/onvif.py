"""ONVIF WS-Discovery — find IP cameras on the LAN.

Two-step:
  1. UDP multicast Probe → collect ProbeMatch responses (gives us XAddr URLs)
  2. For each XAddr, ONVIF Media2.GetProfiles + GetStreamUri (with auth)

The first step is a single raw socket — we don't need a SOAP library because
the response XML is tiny and shape-stable. The second step uses onvif-zeep.
"""

from __future__ import annotations

import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.discovery.onvif")


def _resolve_wsdl_dir() -> str | None:
    """Return the WSDL directory, accounting for the frozen PyInstaller bundle.

    Dev: onvif-zeep installs WSDLs at site-packages/wsdl → use library default
         (return None so ONVIFCamera uses its own default).
    Frozen: PyInstaller --add-data bundles them to <_MEIPASS>/wsdl.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            wsdl = Path(meipass) / "wsdl"
            if wsdl.is_dir():
                return str(wsdl)
    return None

WS_DISCOVERY_ADDR = "239.255.255.250"
WS_DISCOVERY_PORT = 3702


def _probe_envelope() -> bytes:
    """Build a WS-Discovery Probe envelope for `tds:Device` type devices.

    ONVIF cameras subscribe to either `dn:NetworkVideoTransmitter` or
    `tds:Device` — we ask for the latter which is the most common.
    """
    message_id = f"uuid:{uuid.uuid4()}"
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""
    return envelope.encode("utf-8")


# Match http(s)://host[:port]/path to extract XAddrs from response XML
_XADDR_RE = re.compile(
    r"https?://[\w.-]+(?::\d+)?(?:/[\w.\-/?=&%]*)?",
    re.IGNORECASE,
)


@dataclass(slots=True)
class OnvifDevice:
    """One device that responded to our Probe."""

    xaddr: str          # http://host:port/onvif/device_service
    ip: str             # parsed from xaddr
    raw_xml: str = ""   # full SOAP response for debugging
    # Populated by `fetch_profiles` after auth
    manufacturer: str | None = None
    model: str | None = None
    profiles: list[OnvifProfile] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class OnvifProfile:
    token: str
    name: str
    width: int | None = None
    height: int | None = None
    encoding: str | None = None   # "H264", "H265", etc
    rtsp_uri: str | None = None   # auth not yet embedded; agent embeds creds


def discover(timeout_sec: float = 5.0) -> list[OnvifDevice]:
    """Send a single Probe and collect responses for `timeout_sec`.

    Returns one OnvifDevice per unique XAddr. Dedups by XAddr URL.
    """
    seen: dict[str, OnvifDevice] = {}

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout_sec)
        try:
            sock.bind(("", 0))
        except OSError as e:
            log.warning("onvif.bind_failed", error=str(e))
            return []

        sock.sendto(_probe_envelope(), (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))
        log.info("onvif.probe_sent", multicast=WS_DISCOVERY_ADDR, timeout=timeout_sec)

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                sock.settimeout(max(0.1, deadline - time.monotonic()))
                data, addr = sock.recvfrom(8192)
            except TimeoutError:
                break
            except OSError as e:
                log.debug("onvif.recv_err", error=str(e))
                continue

            try:
                xml = data.decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                continue
            xaddrs = _extract_xaddrs(xml)
            for xa in xaddrs:
                if xa in seen:
                    continue
                ip = _host_from_url(xa) or addr[0]
                seen[xa] = OnvifDevice(xaddr=xa, ip=ip, raw_xml=xml)

    log.info("onvif.probe_complete", devices_found=len(seen))
    return list(seen.values())


def _extract_xaddrs(xml: str) -> list[str]:
    """Crude regex-based XAddr extraction.

    ONVIF ProbeMatch includes <XAddrs>http://... http://...</XAddrs>.
    We just grab every URL; for typical replies there's one or two.
    """
    matches = _XADDR_RE.findall(xml)
    # Keep only ones that look like ONVIF device service URLs (port 80 or 8000
    # are common; many cameras include /onvif/device_service in the path).
    filtered: list[str] = []
    seen: set[str] = set()
    for m in matches:
        if m in seen:
            continue
        # Skip XML namespace URIs and meta URLs
        if "schemas" in m or "xmlsoap" in m or "onvif.org/ver10" in m:
            continue
        if "onvif.org/ver20" in m:
            continue
        seen.add(m)
        filtered.append(m)
    return filtered


def _host_from_url(url: str) -> str | None:
    m = re.match(r"https?://([\w.-]+)", url)
    return m.group(1) if m else None


def fetch_profiles(
    device: OnvifDevice,
    username: str,
    password: str,
) -> OnvifDevice:
    """Authenticate to a discovered ONVIF device and fetch profile + stream URIs.

    Mutates and returns the OnvifDevice in-place (sets manufacturer, model,
    profiles).

    On any error sets `device.error` and returns. Never raises.
    """
    try:
        from onvif import ONVIFCamera
    except ImportError as e:
        device.error = f"onvif-zeep not installed: {e}"
        return device

    try:
        host, port = _split_host_port(device.xaddr)
    except ValueError as e:
        device.error = f"bad xaddr: {e}"
        return device

    try:
        wsdl_dir = _resolve_wsdl_dir()
        if wsdl_dir is not None:
            cam = ONVIFCamera(host, port, username, password, wsdl_dir)
        else:
            cam = ONVIFCamera(host, port, username, password)
        info = cam.devicemgmt.GetDeviceInformation()
        device.manufacturer = getattr(info, "Manufacturer", None)
        device.model = getattr(info, "Model", None)
    except Exception as e:  # noqa: BLE001 — onvif-zeep raises a sea of types
        device.error = f"auth/info failed: {e}"
        return device

    # Try Media2 (modern), fall back to Media (legacy ONVIF 1.x)
    profiles_raw: list[object] = []
    media_service = None
    for service_name in ("create_media2_service", "create_media_service"):
        try:
            media_service = getattr(cam, service_name)()
            profiles_raw = media_service.GetProfiles()
            if profiles_raw:
                break
        except Exception as e:  # noqa: BLE001
            log.debug("onvif.profiles_fallback", service=service_name, error=str(e))
            continue

    if not profiles_raw:
        device.error = "no profiles returned (both Media2 and Media failed)"
        return device

    for p in profiles_raw:
        prof = OnvifProfile(
            token=getattr(p, "token", ""),
            name=getattr(p, "Name", "") or "",
        )
        vec = _video_encoder_config(p)
        if vec is not None:
            prof.encoding = getattr(vec, "Encoding", None)
            res = getattr(vec, "Resolution", None)
            if res is not None:
                prof.width = getattr(res, "Width", None)
                prof.height = getattr(res, "Height", None)

        # Stream URI
        try:
            req = {
                "ProfileToken": prof.token,
                "Protocol": "RTSP",
            }
            uri_result = media_service.GetStreamUri(req)  # type: ignore[union-attr]
            prof.rtsp_uri = getattr(uri_result, "Uri", None) or str(uri_result)
        except Exception as e:  # noqa: BLE001
            log.debug("onvif.stream_uri_failed", token=prof.token, error=str(e))

        device.profiles.append(prof)

    return device


def _video_encoder_config(profile: object) -> object | None:
    """Media2 uses Configurations.VideoEncoder; legacy uses VideoEncoderConfiguration."""
    cfgs = getattr(profile, "Configurations", None)
    if cfgs is not None:
        return getattr(cfgs, "VideoEncoder", None)
    return getattr(profile, "VideoEncoderConfiguration", None)


def _split_host_port(xaddr: str) -> tuple[str, int]:
    """Parse http://host[:port]/path → (host, port). Defaults port 80."""
    m = re.match(r"https?://([\w.-]+)(?::(\d+))?", xaddr)
    if not m:
        raise ValueError(f"can't parse {xaddr}")
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else 80
    return host, port
