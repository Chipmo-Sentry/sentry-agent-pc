"""RTSP probe ffmpeg-stderr parsing (no real ffmpeg needed)."""

from __future__ import annotations

from sentry_agent_pc.discovery import rtsp_probe

H264_STDERR = """\
ffmpeg version N-124657 Copyright (c) 2000-2026
  Input #0, rtsp, from 'rtsp://...':
    Stream #0:0: Video: h264 (Main), yuvj420p(pc, bt709), 1920x1080, 25 tbr, 90k tbn
"""

H265_STDERR = """\
  Input #0, rtsp, from 'rtsp://...':
    Stream #0:0: Video: hevc (Main), yuv420p(tv), 2688x1520, 20 fps, 50 tbr, 90k tbn
"""

AUTH_FAIL_STDERR = """\
[rtsp @ 0x...] method DESCRIBE failed: 401 Unauthorized
rtsp://...: Server returned 401 Unauthorized
"""


def test_codec_regex_h264() -> None:
    m = rtsp_probe._CODEC_RE.search(H264_STDERR)
    assert m is not None
    assert m.group(1).lower() == "h264"
    assert m.group(2) == "1920"
    assert m.group(3) == "1080"


def test_codec_regex_h265() -> None:
    m = rtsp_probe._CODEC_RE.search(H265_STDERR)
    assert m is not None
    assert m.group(1).lower() == "hevc"
    assert m.group(2) == "2688"


def test_last_error_line_extracts_401() -> None:
    line = rtsp_probe._last_error_line(AUTH_FAIL_STDERR)
    assert line is not None
    assert "401" in line


def test_last_error_line_none_when_clean() -> None:
    assert rtsp_probe._last_error_line(H264_STDERR) is None


def test_probe_first_h264_prefers_h264(monkeypatch) -> None:
    """Given a list of URLs, returns the first H.264 hit even if H.265 comes first."""
    results = {
        "rtsp://h265": rtsp_probe.ProbeResult(
            ok=True, url="rtsp://h265", codec="hevc", is_h264=False,
        ),
        "rtsp://h264": rtsp_probe.ProbeResult(
            ok=True, url="rtsp://h264", codec="h264", is_h264=True,
        ),
    }
    monkeypatch.setattr(rtsp_probe, "probe", lambda url, **_: results[url])
    out = rtsp_probe.probe_first_h264(["rtsp://h265", "rtsp://h264"])
    assert out.url == "rtsp://h264"
    assert out.is_h264


def test_probe_first_h264_falls_back_to_h265(monkeypatch) -> None:
    """If no H.264, return the first OK (H.265) result so caller can warn."""
    results = {
        "rtsp://a": rtsp_probe.ProbeResult(
            ok=True, url="rtsp://a", codec="hevc", is_h264=False,
        ),
    }
    monkeypatch.setattr(rtsp_probe, "probe", lambda url, **_: results[url])
    out = rtsp_probe.probe_first_h264(["rtsp://a"])
    assert out.ok
    assert not out.is_h264
    assert out.codec == "hevc"


def test_probe_first_h264_all_fail(monkeypatch) -> None:
    monkeypatch.setattr(
        rtsp_probe,
        "probe",
        lambda url, **_: rtsp_probe.ProbeResult(ok=False, url=url, error="refused"),
    )
    out = rtsp_probe.probe_first_h264(["rtsp://x"])
    assert not out.ok
