"""HTTP snapshot fallback — URL building + auth/validation logic (httpx mocked)."""

from __future__ import annotations

import httpx
import respx

from sentry_agent_pc.discovery.snapshot import (
    SNAPSHOT_PATHS,
    fetch_snapshot,
    snapshot_urls,
)

_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF...."  # starts with the JPEG SOI magic


def test_snapshot_urls_builds_brand_paths() -> None:
    urls = snapshot_urls("192.168.1.217")
    assert len(urls) == len(SNAPSHOT_PATHS)
    assert urls[0] == "http://192.168.1.217" + SNAPSHOT_PATHS[0]
    assert all(u.startswith("http://192.168.1.217/") for u in urls)


def test_snapshot_urls_nonstandard_port() -> None:
    assert snapshot_urls("cam", port=8080)[0].startswith("http://cam:8080/")
    assert snapshot_urls("cam", port=80)[0].startswith("http://cam/")


@respx.mock
def test_fetch_snapshot_returns_jpeg_bytes() -> None:
    url = "http://cam/snapshot.jpg"
    respx.get(url).mock(return_value=httpx.Response(200, content=_JPEG))
    assert fetch_snapshot(url, "admin", "pw") == _JPEG


@respx.mock
def test_fetch_snapshot_rejects_html_200() -> None:
    # A camera that 200s with an HTML error page must NOT be taken as an image.
    url = "http://cam/snapshot.jpg"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"<html>nope</html>"))
    assert fetch_snapshot(url, "admin", "pw") is None


@respx.mock
def test_fetch_snapshot_404_is_none() -> None:
    url = "http://cam/wrong.jpg"
    respx.get(url).mock(return_value=httpx.Response(404))
    assert fetch_snapshot(url, "admin", "pw") is None


@respx.mock
def test_fetch_snapshot_connection_error_is_none() -> None:
    url = "http://cam/x.jpg"
    respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
    assert fetch_snapshot(url, "admin", "pw") is None


@respx.mock
def test_fetch_snapshot_no_credentials() -> None:
    url = "http://cam/snap.jpg"
    respx.get(url).mock(return_value=httpx.Response(200, content=_JPEG))
    assert fetch_snapshot(url, None, None) == _JPEG


@respx.mock
def test_fetch_snapshot_basic_transport_error_falls_back_to_digest() -> None:
    # #19: a Digest-only camera can reset/refuse the Basic attempt. The first
    # httpx.HTTPError must NOT abort the loop — Digest should still be tried.
    url = "http://cam/snapshot.jpg"

    calls = {"n": 0}

    def _side_effect(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt (Basic) is reset at the transport layer.
            raise httpx.ConnectError("reset", request=request)
        # Second attempt (Digest) succeeds.
        return httpx.Response(200, content=_JPEG)

    respx.get(url).mock(side_effect=_side_effect)
    assert fetch_snapshot(url, "admin", "pw") == _JPEG
    assert calls["n"] == 2  # both auth schemes were attempted
