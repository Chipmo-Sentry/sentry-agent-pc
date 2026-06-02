"""ONVIF WS-Discovery — find IP cameras on the LAN.

Two-step:
  1. UDP multicast Probe → collect ProbeMatch responses (gives us XAddr URLs)
  2. For each XAddr, ONVIF Media2.GetProfiles + GetStreamUri (with auth)

The first step is a single raw socket — we don't need a SOAP library because
the response XML is tiny and shape-stable. The second step uses onvif-zeep.
"""

from __future__ import annotations

import re
import select
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


def _probe_envelope(types: str = "dn:NetworkVideoTransmitter") -> bytes:
    """Build a WS-Discovery Probe envelope for the given device `types`.

    ONVIF cameras subscribe to either `dn:NetworkVideoTransmitter` (the most
    common) or `tds:Device`. We send one Probe per type so cameras that only
    answer the latter still surface — see `_probe_payloads`.
    """
    message_id = f"uuid:{uuid.uuid4()}"
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <e:Header>
    <w:MessageID>{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>{types}</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""
    return envelope.encode("utf-8")


def _probe_payloads() -> list[bytes]:
    """Probe envelopes to send on every interface (both common ONVIF types)."""
    return [
        _probe_envelope("dn:NetworkVideoTransmitter"),
        _probe_envelope("tds:Device"),
    ]


def _local_ipv4_addresses() -> list[str]:
    """Best-effort list of this host's IPv4 interface addresses.

    WS-Discovery multicast is per-interface: a Probe sent on the OS default
    interface never reaches a camera on a different subnet. On multi-homed
    Windows boxes (Wi-Fi + Ethernet + VPN/virtual adapters) the default is
    often the *wrong* NIC, so we enumerate every local IPv4 and Probe each.

    `getaddrinfo(gethostname())` covers the common cases; we add a UDP-connect
    trick for the primary route and always dedup. Loopback is dropped.
    """
    addrs: set[str] = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(res[4][0])
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except OSError as e:
        log.debug("onvif.getaddrinfo_failed", error=str(e))

    # UDP-connect trick: reveals the IP of the primary outbound interface even
    # when getaddrinfo under-reports (no packets are actually sent).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = str(s.getsockname()[0])
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass

    return sorted(addrs)


def discover(timeout_sec: float = 5.0) -> list[OnvifDevice]:
    """Send WS-Discovery Probes on every local interface, collect responses.

    Returns one OnvifDevice per unique XAddr (deduped by XAddr URL). Probes
    both `NetworkVideoTransmitter` and `Device` types on each interface so
    cameras on any reachable subnet — and either ONVIF profile — show up.
    """
    seen: dict[str, OnvifDevice] = {}
    payloads = _probe_payloads()
    interfaces = _local_ipv4_addresses()
    log.info("onvif.interfaces", addresses=interfaces or ["<default>"])

    socks: list[socket.socket] = []
    # One socket per interface (bound + IP_MULTICAST_IF pinned), plus a default
    # 0.0.0.0 socket as a belt-and-braces fallback for odd routing setups.
    bind_targets: list[str | None] = [*interfaces, None]
    for local_ip in bind_targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            sock.bind((local_ip or "", 0))
            if local_ip:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(local_ip),
                )
        except OSError as e:
            log.debug("onvif.iface_bind_failed", iface=local_ip, error=str(e))
            sock.close()
            continue
        for payload in payloads:
            try:
                sock.sendto(payload, (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))
            except OSError as e:
                log.debug("onvif.send_failed", iface=local_ip, error=str(e))
        socks.append(sock)

    if not socks:
        log.warning("onvif.no_usable_sockets")
        return []

    log.info("onvif.probe_sent", sockets=len(socks), timeout=timeout_sec)

    deadline = time.monotonic() + timeout_sec
    try:
        while time.monotonic() < deadline:
            wait = max(0.05, deadline - time.monotonic())
            ready, _, _ = select.select(socks, [], [], wait)
            if not ready:
                break
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(8192)
                except OSError as e:
                    log.debug("onvif.recv_err", error=str(e))
                    continue
                xml = data.decode("utf-8", errors="replace")
                for xa in _extract_xaddrs(xml):
                    if xa in seen:
                        continue
                    ip = _host_from_url(xa) or addr[0]
                    seen[xa] = OnvifDevice(xaddr=xa, ip=ip, raw_xml=xml)
    finally:
        for sock in socks:
            sock.close()

    log.info("onvif.probe_complete", devices_found=len(seen))
    return list(seen.values())


# Match http(s)://host[:port]/path to extract URLs from XAddrs content
_URL_RE = re.compile(
    r"https?://[\w.-]+(?::\d+)?(?:/[\w.\-/?=&%]*)?",
    re.IGNORECASE,
)
# Find the <...XAddrs>...</...XAddrs> element content (namespace prefix agnostic).
# Only URLs inside this element are real device service addresses — extracting
# from the whole document picks up SOAP/namespace URIs (www.w3.org, xmlsoap).
_XADDRS_ELEM_RE = re.compile(
    r"<(?:\w+:)?XAddrs>(.*?)</(?:\w+:)?XAddrs>",
    re.IGNORECASE | re.DOTALL,
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


def _extract_xaddrs(xml: str) -> list[str]:
    """Extract device service URLs from the <XAddrs> element(s) only.

    ONVIF ProbeMatch carries device addresses in <XAddrs>http://... http://...
    </XAddrs>. Extracting URLs from the whole document would wrongly pick up
    the SOAP envelope + WS-Discovery namespace URIs (www.w3.org, xmlsoap.org).
    """
    seen: set[str] = set()
    out: list[str] = []
    for block in _XADDRS_ELEM_RE.findall(xml):
        for url in _URL_RE.findall(block):
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


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
