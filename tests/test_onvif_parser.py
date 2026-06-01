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


def test_probe_envelope_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET

    envelope = onvif._probe_envelope()
    # Should parse without error
    root = ET.fromstring(envelope.decode("utf-8"))
    assert root is not None
