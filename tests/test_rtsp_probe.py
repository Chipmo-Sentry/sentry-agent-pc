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

# A Video line whose codec we recognise but with NO inline WxH token. Some
# cameras print exactly this — ffmpeg identified the codec but couldn't (or
# didn't bother to) report a resolution on the same line.
H264_NO_RES_STDERR = """\
  Input #0, rtsp, from 'rtsp://...':
    Stream #0:0: Video: h264 (Main), yuvj420p(pc, bt709), 25 tbr, 90k tbn
"""


def test_last_error_line_scrubs_credentials() -> None:
    # ffmpeg echoes the full input URL (with the camera password) in its error
    # lines; the returned hint must NEVER carry the credentials (C1 leak).
    stderr = (
        "[rtsp @ 0x..] method DESCRIBE failed: 401 Unauthorized\n"
        "rtsp://admin:S3cr3t@192.168.1.64:554/Streaming: "
        "Server returned 401 Unauthorized\n"
    )
    line = rtsp_probe._last_error_line(stderr)
    assert line is not None
    assert "S3cr3t" not in line
    assert "admin" not in line
    assert "***" in line  # credentials masked
    assert "401" in line  # the useful diagnostic is preserved


def test_codec_regex_h264() -> None:
    m = rtsp_probe._CODEC_RE.search(H264_STDERR)
    assert m is not None
    assert m.group(1).lower() == "h264"
    res = rtsp_probe._RES_RE.search(H264_STDERR)
    assert res is not None
    assert res.group(1) == "1920"
    assert res.group(2) == "1080"


def test_codec_regex_h265() -> None:
    m = rtsp_probe._CODEC_RE.search(H265_STDERR)
    assert m is not None
    assert m.group(1).lower() == "hevc"
    res = rtsp_probe._RES_RE.search(H265_STDERR)
    assert res is not None
    assert res.group(1) == "2688"


def test_codec_regex_matches_without_resolution() -> None:
    # #9: codec must be detected even when the Video line has no WxH token.
    m = rtsp_probe._CODEC_RE.search(H264_NO_RES_STDERR)
    assert m is not None
    assert m.group(1).lower() == "h264"
    assert rtsp_probe._RES_RE.search(H264_NO_RES_STDERR) is None


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
            ok=True,
            url="rtsp://h265",
            codec="hevc",
            is_h264=False,
        ),
        "rtsp://h264": rtsp_probe.ProbeResult(
            ok=True,
            url="rtsp://h264",
            codec="h264",
            is_h264=True,
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
            ok=True,
            url="rtsp://a",
            codec="hevc",
            is_h264=False,
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


def test_probe_first_h264_empty_list_does_not_raise() -> None:
    # #1: empty input must not raise UnboundLocalError.
    out = rtsp_probe.probe_first_h264([])
    assert not out.ok
    assert out.url == ""
    assert out.error is not None


class _FakeProc:
    def __init__(self, stderr: bytes, returncode: int = 0) -> None:
        self.stderr = stderr
        self.returncode = returncode


def test_probe_codec_without_resolution_is_ok(monkeypatch) -> None:
    # #9: a recognised codec with no inline WxH must give ok=True, width/height None.
    monkeypatch.setattr(
        rtsp_probe,
        "subprocess",
        type(
            "S",
            (),
            {
                "run": staticmethod(
                    lambda *a, **k: _FakeProc(H264_NO_RES_STDERR.encode()),
                ),
                "DEVNULL": 0,
            },
        ),
    )
    monkeypatch.setattr(rtsp_probe, "resolve_ffmpeg_exe", lambda _p: "ffmpeg")
    monkeypatch.setattr(
        rtsp_probe,
        "get_settings",
        lambda: type("Cfg", (), {"rtsp_probe_timeout_sec": 5, "ffmpeg_path": ""})(),
    )
    out = rtsp_probe.probe("rtsp://cam", timeout_sec=5)
    assert out.ok
    assert out.codec == "h264"
    assert out.is_h264
    assert out.width is None
    assert out.height is None


def test_probe_codec_with_resolution_keeps_dimensions(monkeypatch) -> None:
    # Preserve original behaviour: WxH present → width/height parsed.
    monkeypatch.setattr(
        rtsp_probe,
        "subprocess",
        type(
            "S",
            (),
            {
                "run": staticmethod(
                    lambda *a, **k: _FakeProc(H264_STDERR.encode()),
                ),
                "DEVNULL": 0,
            },
        ),
    )
    monkeypatch.setattr(rtsp_probe, "resolve_ffmpeg_exe", lambda _p: "ffmpeg")
    monkeypatch.setattr(
        rtsp_probe,
        "get_settings",
        lambda: type("Cfg", (), {"rtsp_probe_timeout_sec": 5, "ffmpeg_path": ""})(),
    )
    out = rtsp_probe.probe("rtsp://cam", timeout_sec=5)
    assert out.ok
    assert out.codec == "h264"
    assert out.width == 1920
    assert out.height == 1080
