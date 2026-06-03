"""ONVIF WS-Discovery response parsing — XAddr extraction + host/port split."""

from __future__ import annotations

import pytest

from sentry_agent_pc.discovery import onvif

# A realistic (trimmed) ProbeMatch SOAP response from a Hikvision camera.
SAMPLE_PROBE_MATCH = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:XAddrs>http://192.168.1.64/onvif/device_service</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""


def test_extract_xaddrs_finds_device_service() -> None:
    xaddrs = onvif._extract_xaddrs(SAMPLE_PROBE_MATCH)
    assert "http://192.168.1.64/onvif/device_service" in xaddrs


def test_extract_xaddrs_skips_namespace_uris() -> None:
    """schemas.xmlsoap.org namespace URIs must not be treated as device addrs."""
    xaddrs = onvif._extract_xaddrs(SAMPLE_PROBE_MATCH)
    for xa in xaddrs:
        assert "schemas.xmlsoap.org" not in xa
        assert "www.w3.org" not in xa


def test_extract_xaddrs_with_port() -> None:
    xml = "<d:XAddrs>http://10.0.0.5:8000/onvif/device_service</d:XAddrs>"
    xaddrs = onvif._extract_xaddrs(xml)
    assert "http://10.0.0.5:8000/onvif/device_service" in xaddrs


def test_extract_xaddrs_multiple_dedup() -> None:
    xml = (
        "<d:XAddrs>http://1.1.1.1/onvif/device_service "
        "http://1.1.1.1/onvif/device_service</d:XAddrs>"
    )
    xaddrs = onvif._extract_xaddrs(xml)
    assert xaddrs.count("http://1.1.1.1/onvif/device_service") == 1


def test_host_from_url() -> None:
    assert onvif._host_from_url("http://192.168.1.64/onvif/device_service") == "192.168.1.64"
    assert onvif._host_from_url("https://10.0.0.5:8000/x") == "10.0.0.5"
    assert onvif._host_from_url("not-a-url") is None


def test_split_host_port_default() -> None:
    host, port = onvif._split_host_port("http://192.168.1.64/onvif/device_service")
    assert host == "192.168.1.64"
    assert port == 80


def test_split_host_port_explicit() -> None:
    host, port = onvif._split_host_port("http://10.0.0.5:8000/onvif/device_service")
    assert host == "10.0.0.5"
    assert port == 8000


def test_split_host_port_invalid_raises() -> None:
    with pytest.raises(ValueError, match="can't parse"):
        onvif._split_host_port("garbage")


class _FakeMedia:
    """Mimics onvif-zeep media service GetStreamUri for the two flavors."""

    def __init__(self, *, accept: str) -> None:
        self.accept = accept  # "streamsetup" | "simple" | "none"
        self.calls: list[dict] = []

    def GetStreamUri(self, req: dict) -> object:  # noqa: N802 — ONVIF API name
        self.calls.append(req)
        is_streamsetup = "StreamSetup" in req
        ok = (self.accept == "streamsetup" and is_streamsetup) or (
            self.accept == "simple" and not is_streamsetup
        )
        if not ok:
            raise ValueError("Missing element Stream")

        class _Res:
            Uri = "rtsp://cam/stream"

        return _Res()


def test_get_stream_uri_legacy_streamsetup_tried_first() -> None:
    m = _FakeMedia(accept="streamsetup")
    assert onvif._get_stream_uri(m, "tok") == "rtsp://cam/stream"
    # Legacy StreamSetup form must be the first attempt (the common real case).
    assert "StreamSetup" in m.calls[0]


def test_get_stream_uri_falls_back_to_media2_simple_form() -> None:
    m = _FakeMedia(accept="simple")
    assert onvif._get_stream_uri(m, "tok") == "rtsp://cam/stream"
    assert len(m.calls) == 2  # tried StreamSetup, then simple


def test_get_stream_uri_none_when_all_forms_fail() -> None:
    m = _FakeMedia(accept="none")
    assert onvif._get_stream_uri(m, "tok") is None


def test_probe_envelope_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET

    envelope = onvif._probe_envelope()
    # Should parse without error
    root = ET.fromstring(envelope.decode("utf-8"))
    assert root is not None
