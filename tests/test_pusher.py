"""StreamPusher URL building + relay reconciliation (no real ffmpeg spawned)."""

from __future__ import annotations

import threading

from sentry_agent_pc.streaming.pusher import PushTarget, StreamPusher, build_push_url


def test_build_push_url_no_creds() -> None:
    assert build_push_url("rtsp://mtx:8554", "cam1", None, None) == "rtsp://mtx:8554/cam1"


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
