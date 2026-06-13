"""StreamPusher URL building + relay reconciliation (no real ffmpeg spawned)."""

from __future__ import annotations

import threading

from sentry_agent_pc.streaming.pusher import (
    PushTarget,
    StreamPusher,
    build_push_url,
    build_relay_cmd,
)


def test_build_push_url_no_creds() -> None:
    assert build_push_url("rtsp://mtx:8554", "cam1", None, None) == "rtsp://mtx:8554/cam1"


def test_relay_cmd_h264_copies_single_video_track() -> None:
    """H.264 → remux only (-c:v copy), but ALWAYS map a single video track +
    drop audio so a camera's extra 'Generic'/data track can't trip WebRTC."""
    t = PushTarget("cam1", "rtsp://lan/1", codec="h264")
    cmd = build_relay_cmd("ffmpeg", t, "rtsp://mtx:8554/cam1")
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "-an" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "libx264" not in cmd  # no re-encode for H.264
    assert cmd[-1] == "rtsp://mtx:8554/cam1"


def test_relay_cmd_h265_transcodes_to_h264() -> None:
    for codec in ("hevc", "h265", "HEVC"):
        t = PushTarget("cam1", "rtsp://lan/1", codec=codec)
        cmd = build_relay_cmd("ffmpeg", t, "rtsp://mtx:8554/cam1")
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v:0"


def test_relay_cmd_unknown_codec_defaults_to_copy() -> None:
    t = PushTarget("cam1", "rtsp://lan/1", codec=None)
    cmd = build_relay_cmd("ffmpeg", t, "rtsp://mtx:8554/cam1")
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_build_push_url_with_creds_urlencoded() -> None:
    url = build_push_url("rtsp://mtx:8554/", "cam1", "pub", "p@ss/word")
    assert url == "rtsp://pub:p%40ss%2Fword@mtx:8554/cam1"


def test_build_push_url_user_only() -> None:
    assert build_push_url("rtsp://mtx:8554", "c", "pub", None) == "rtsp://pub@mtx:8554/c"


def test_sync_starts_and_stops_relays(monkeypatch) -> None:
    """sync() should spawn a relay thread per target and stop removed ones —
    we stub the relay loop so no ffmpeg is launched."""
    started: list[str] = []
    release = threading.Event()

    def fake_relay(self, st) -> None:  # noqa: ANN001
        started.append(st.target.mediamtx_path)
        st.running = True
        release.wait(2.0)  # block until test tears down

    monkeypatch.setattr(StreamPusher, "_run_relay", fake_relay)

    pusher = StreamPusher("rtsp://mtx:8554")
    pusher.sync([PushTarget("cam1", "rtsp://lan/1"), PushTarget("cam2", "rtsp://lan/2")])
    # Both relays registered.
    paths = {s["path"] for s in pusher.status()}
    assert paths == {"cam1", "cam2"}

    # Removing cam2 from targets stops its relay.
    pusher.sync([PushTarget("cam1", "rtsp://lan/1")])
    paths = {s["path"] for s in pusher.status()}
    assert paths == {"cam1"}

    release.set()
    pusher.stop_all()
    assert pusher.status() == []
    assert "cam1" in started and "cam2" in started
