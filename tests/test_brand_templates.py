"""Brand RTSP URL template tests — credential encoding + candidate ordering."""

from __future__ import annotations

import pytest

from sentry_agent_pc.discovery import manual


def test_all_brands_have_required_fields() -> None:
    for b in manual.list_brands():
        assert b.key
        assert b.label
        assert b.main_path.startswith("/")
        assert b.default_port == 554


def test_get_brand_known_and_unknown() -> None:
    assert manual.get_brand("hikvision") is not None
    assert manual.get_brand("nonexistent") is None


def test_build_rtsp_url_basic() -> None:
    t = manual.get_brand("hikvision")
    assert t is not None
    url = manual.build_rtsp_url(t, "192.168.1.64", "admin", "pass123")
    assert url == "rtsp://admin:pass123@192.168.1.64:554/Streaming/Channels/101"


def test_build_rtsp_url_encodes_special_chars() -> None:
    """`*`, `@`, `#`, `:` in password must be percent-encoded."""
    t = manual.get_brand("unv_modern")
    assert t is not None
    url = manual.build_rtsp_url(t, "192.168.1.13", "admin", "Admin123*")
    # * → %2A; the credential block must not contain a literal *
    assert "Admin123%2A" in url
    assert "Admin123*@" not in url


def test_build_rtsp_url_encodes_at_sign() -> None:
    t = manual.get_brand("hikvision")
    assert t is not None
    url = manual.build_rtsp_url(t, "10.0.0.5", "user", "p@ss")
    assert "p%40ss" in url
    # Only ONE @ should separate creds from host
    assert url.count("@") == 1


def test_build_rtsp_url_custom_path_overrides() -> None:
    t = manual.get_brand("hikvision")
    assert t is not None
    url = manual.build_rtsp_url(
        t,
        "1.2.3.4",
        "a",
        "b",
        custom_path="/my/custom/path",
    )
    assert url.endswith("/my/custom/path")


def test_build_rtsp_url_custom_path_without_leading_slash() -> None:
    t = manual.get_brand("hikvision")
    assert t is not None
    url = manual.build_rtsp_url(t, "1.2.3.4", "a", "b", custom_path="onvif1")
    assert url.endswith("/onvif1")


def test_build_rtsp_url_sub_stream() -> None:
    t = manual.get_brand("hikvision")
    assert t is not None
    url = manual.build_rtsp_url(t, "1.2.3.4", "a", "b", use_sub=True)
    assert url.endswith("/Streaming/Channels/102")


def test_build_rtsp_url_custom_port() -> None:
    t = manual.get_brand("hikvision")
    assert t is not None
    url = manual.build_rtsp_url(t, "1.2.3.4", "a", "b", port=8554)
    assert ":8554/" in url


def test_candidate_urls_includes_main_and_sub() -> None:
    t = manual.get_brand("dahua")
    assert t is not None
    urls = manual.candidate_urls(t, "1.2.3.4", "a", "b")
    assert len(urls) == 2
    assert "subtype=0" in urls[0]
    assert "subtype=1" in urls[1]


def test_candidate_urls_main_only_when_no_sub() -> None:
    # onvif_generic has a sub_path, so make a synthetic template with none
    t = manual.BrandTemplate(key="x", label="X", main_path="/only")
    urls = manual.candidate_urls(t, "1.2.3.4", "a", "b")
    assert urls == ["rtsp://a:b@1.2.3.4:554/only"]


@pytest.mark.parametrize("brand_key", ["hikvision", "dahua", "unv_modern", "reolink"])
def test_candidate_urls_main_first(brand_key: str) -> None:
    t = manual.get_brand(brand_key)
    assert t is not None
    urls = manual.candidate_urls(t, "1.2.3.4", "a", "b")
    assert urls[0].endswith(t.main_path)
