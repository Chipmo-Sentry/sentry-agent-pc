"""StreamController routes relays through the local hub when it's healthy."""

from __future__ import annotations

from sentry_agent_pc.state import CameraRecord
from sentry_agent_pc.streaming import controller as ctrl_mod
from sentry_agent_pc.streaming.controller import StreamController
from sentry_agent_pc.streaming.pusher import PushTarget, StreamPusher


class _FakeState:
    is_paired = True

    def __init__(self, cameras: list[CameraRecord]) -> None:
        self.cameras = cameras


class _FakeLocal:
    """Stand-in hub: serves cam1 over loopback, knows nothing about cam2."""

    def __init__(self) -> None:
        self.synced: list[tuple[str, str]] | None = None

    def sync(self, cams: list[tuple[str, str]]) -> bool:
        self.synced = cams
        return True

    def local_url(self, path: str | None) -> str | None:
        return "rtsp://127.0.0.1:18554/cam1" if path == "cam1" else None

    def stop(self) -> None:  # pragma: no cover - teardown only
        pass


def _cam(name: str, path: str, rtsp: str) -> CameraRecord:
    return CameraRecord(name=name, ip="10.0.0.1", rtsp_url=rtsp, mediamtx_path=path)


def test_relays_use_loopback_when_hub_healthy(monkeypatch) -> None:
    cams = [
        _cam("Door", "cam1", "rtsp://u:p@10.0.0.1/1"),
        _cam("Till", "cam2", "rtsp://u:p@10.0.0.2/1"),
    ]
    monkeypatch.setattr(ctrl_mod, "load_state", lambda: _FakeState(cams))
    monkeypatch.setattr(
        ctrl_mod.BackendClient,
        "agent_stream_config",
        lambda self: {"push_enabled": True, "push_rtsp_base": "rtsp://cloud:8554"},
    )

    captured: dict[str, list[PushTarget]] = {}
    monkeypatch.setattr(
        StreamPusher, "sync", lambda self, targets: captured.__setitem__("t", targets)
    )

    controller = StreamController()
    controller._local = _FakeLocal()  # type: ignore[assignment]
    controller.refresh()

    by_path = {t.mediamtx_path: t.lan_rtsp for t in captured["t"]}
    # cam1 is served by the hub → relay reads loopback (shared single pull).
    assert by_path["cam1"] == "rtsp://127.0.0.1:18554/cam1"
    # cam2 not served → relay falls back to the direct camera URL.
    assert by_path["cam2"] == "rtsp://u:p@10.0.0.2/1"
    # The hub was asked to serve both cameras.
    assert controller._local.synced == [  # type: ignore[union-attr]
        ("cam1", "rtsp://u:p@10.0.0.1/1"),
        ("cam2", "rtsp://u:p@10.0.0.2/1"),
    ]


def test_stopped_controller_refresh_is_noop(monkeypatch) -> None:
    """After stop(), a parked refresh() must NOT rebuild relays (orphan-after-quit)."""
    cams = [_cam("Door", "cam1", "rtsp://u:p@10.0.0.1/1")]
    monkeypatch.setattr(ctrl_mod, "load_state", lambda: _FakeState(cams))
    cfg_calls = {"n": 0}

    def _cfg(self) -> dict[str, object]:  # type: ignore[no-untyped-def]
        cfg_calls["n"] += 1
        return {"push_enabled": True, "push_rtsp_base": "rtsp://cloud:8554"}

    monkeypatch.setattr(ctrl_mod.BackendClient, "agent_stream_config", _cfg)

    controller = StreamController()
    controller._local = None
    controller.stop()  # sets _stopped
    controller.refresh()  # must bail before fetching config / building relays
    assert cfg_calls["n"] == 0
    assert controller._pusher is None


def test_relays_use_direct_when_no_hub(monkeypatch) -> None:
    cams = [_cam("Door", "cam1", "rtsp://u:p@10.0.0.1/1")]
    monkeypatch.setattr(ctrl_mod, "load_state", lambda: _FakeState(cams))
    monkeypatch.setattr(
        ctrl_mod.BackendClient,
        "agent_stream_config",
        lambda self: {"push_enabled": True, "push_rtsp_base": "rtsp://cloud:8554"},
    )
    captured: dict[str, list[PushTarget]] = {}
    monkeypatch.setattr(
        StreamPusher, "sync", lambda self, targets: captured.__setitem__("t", targets)
    )

    controller = StreamController()
    controller._local = None  # fan-out disabled / unavailable
    controller.refresh()

    assert captured["t"][0].lan_rtsp == "rtsp://u:p@10.0.0.1/1"
