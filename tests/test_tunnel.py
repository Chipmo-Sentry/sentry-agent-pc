"""Cloudflared quick-tunnel URL parsing + safe no-op without a binary."""

from __future__ import annotations

from sentry_agent_pc.streaming.tunnel import _URL_RE, CloudflaredTunnel


def test_url_regex_matches_quick_tunnel_line() -> None:
    line = "2026-... INF |  https://calm-river-1234.trycloudflare.com  |"
    m = _URL_RE.search(line)
    assert m is not None
    assert m.group(0) == "https://calm-river-1234.trycloudflare.com"


def test_url_regex_ignores_other_urls() -> None:
    assert _URL_RE.search("connecting to https://api.cloudflare.com/x") is None


def test_no_binary_is_safe_noop() -> None:
    t = CloudflaredTunnel(exe_path=None, target_url="http://127.0.0.1:18888")
    t.start()  # must not raise / spawn anything
    assert t.url is None
    t.stop()
