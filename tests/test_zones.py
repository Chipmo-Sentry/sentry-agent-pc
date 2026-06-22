"""Camera detection zones (docs/29 P1a): state model, backend payload, the
save service, reconcile sync, and the still-frame candidate builder."""

from __future__ import annotations

from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.state import AgentState, CameraRecord

_TRI = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]


# === CameraRecord persistence round-trip ===


def test_camera_record_round_trips_zones() -> None:
    cam = CameraRecord(
        uuid="A", name="Cam", ip="1.1.1.1", rtsp_url="rtsp://a",
        zones=[{"id": "z1", "type": "exit", "points": _TRI}],
    )
    reloaded = CameraRecord.model_validate(cam.model_dump())
    assert reloaded.zones is not None
    assert reloaded.zones[0]["type"] == "exit"


def test_camera_record_zones_default_none() -> None:
    assert CameraRecord(name="C", ip="1.1.1.1", rtsp_url="rtsp://a").zones is None


def test_old_state_without_zones_loads() -> None:
    # A v0.7.24 state file has no `zones` key on its cameras — must still load.
    s = AgentState.model_validate(
        {"agent_jwt": "t", "cameras": [{"name": "C", "ip": "1.1.1.1", "rtsp_url": "rtsp://a"}]}
    )
    assert s.cameras[0].zones is None


# === backend_client payload ===


class _RecordingClient:
    """Captures the PATCH body agent_update_camera builds (no network)."""

    def __init__(self) -> None:
        self.body: dict[str, object] | None = None

    def _request(self, method: str, path: str, *, json_body=None, **kw):  # type: ignore[no-untyped-def]
        self.body = json_body

        class _R:
            @staticmethod
            def json() -> dict[str, object]:
                return {"id": "A"}

        return _R()


def test_agent_update_camera_includes_zones_when_given() -> None:
    from sentry_agent_pc.backend_client import BackendClient

    c = BackendClient.__new__(BackendClient)  # skip __init__ (no settings/network)
    rec = _RecordingClient()
    c._request = rec._request  # type: ignore[method-assign]
    c.agent_update_camera("A", zones=[{"type": "exit", "points": _TRI}])
    assert rec.body is not None
    assert rec.body["zones"] == [{"type": "exit", "points": _TRI}]


def test_agent_update_camera_omits_zones_when_none() -> None:
    from sentry_agent_pc.backend_client import BackendClient

    c = BackendClient.__new__(BackendClient)
    rec = _RecordingClient()
    c._request = rec._request  # type: ignore[method-assign]
    c.agent_update_camera("A", name="X")  # zones not passed
    assert rec.body is not None
    assert "zones" not in rec.body


# === save_camera_zones service ===


def _patch_state(monkeypatch, cameras: list[CameraRecord]):  # type: ignore[no-untyped-def]
    state = AgentState(agent_jwt="tok", cameras=cameras)

    def fake_mutate(fn):  # type: ignore[no-untyped-def]
        fn(state)
        return state

    monkeypatch.setattr(svc, "load_state", lambda: state)
    monkeypatch.setattr(svc, "mutate_state", fake_mutate)
    return state


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def agent_update_camera(self, uuid: str, **kw: object) -> dict[str, object]:
        self.calls.append({"uuid": uuid, **kw})
        return {"id": uuid}


def test_save_zones_patches_backend_and_persists(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cam = CameraRecord(uuid="cam-1", name="C", ip="1.1.1.1", rtsp_url="rtsp://a")
    state = _patch_state(monkeypatch, [cam])
    fake = _FakeBackend()
    zones = [{"type": "exit", "points": _TRI}]

    res = svc.save_camera_zones(camera_uuid="cam-1", zones=zones, backend=fake)  # type: ignore[arg-type]

    assert res.ok
    assert fake.calls == [{"uuid": "cam-1", "zones": zones}]
    assert state.cameras[0].zones == zones


def test_save_zones_empty_clears_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cam = CameraRecord(
        uuid="cam-1", name="C", ip="1.1.1.1", rtsp_url="rtsp://a",
        zones=[{"type": "exit", "points": _TRI}],
    )
    state = _patch_state(monkeypatch, [cam])

    res = svc.save_camera_zones(camera_uuid="cam-1", zones=[], backend=_FakeBackend())  # type: ignore[arg-type]

    assert res.ok
    assert state.cameras[0].zones is None  # [] → cleared → stored as None


def test_save_zones_unknown_uuid(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_state(monkeypatch, [])
    res = svc.save_camera_zones(camera_uuid="ghost", zones=[], backend=_FakeBackend())  # type: ignore[arg-type]
    assert not res.ok


def test_save_zones_backend_failure_keeps_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sentry_agent_pc.backend_client import BackendError

    cam = CameraRecord(uuid="c", name="Keep", ip="1.1.1.1", rtsp_url="rtsp://c", zones=None)
    state = _patch_state(monkeypatch, [cam])

    class _Boom:
        def agent_update_camera(self, *a: object, **k: object) -> dict[str, object]:
            raise BackendError("422 too many points")

    res = svc.save_camera_zones(
        camera_uuid="c", zones=[{"type": "exit", "points": _TRI}], backend=_Boom(),  # type: ignore[arg-type]
    )
    assert not res.ok
    assert state.cameras[0].zones is None  # local untouched when backend rejects


# === reconcile zones sync ===


def test_reconcile_syncs_zones(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A zone edit on another PC syncs down + persists here (docs/29)."""
    local = AgentState(
        agent_jwt="jwt",
        cameras=[CameraRecord(uuid="A", name="A", ip="1.1.1.1", rtsp_url="rtsp://a")],
    )
    assert local.cameras[0].zones is None
    saved: dict[str, object] = {}
    monkeypatch.setattr(svc, "load_state", lambda: local)
    monkeypatch.setattr(svc, "save_state", lambda s: saved.update(c=s.cameras))

    class FakeBackend:
        def agent_list_cameras(self):  # type: ignore[no-untyped-def]
            return [
                {"id": "A", "name": "A", "mediamtx_path": "a",
                 "zones": [{"type": "shelf", "points": _TRI}]}
            ]

    cams, changed = svc.reconcile_with_backend(backend=FakeBackend())  # type: ignore[arg-type]
    assert changed is True  # zones-only change caught by the pre-mutation snapshot
    assert cams[0].zones == [{"type": "shelf", "points": _TRI}]


# === frame_grab candidate builder + no-stream guard ===


def test_dedup_consecutive_collapses_double_click_duplicates() -> None:
    from sentry_agent_pc.gui.zone_editor import _dedup_consecutive

    # A double-click leaves two extra vertices at the close location.
    pts = [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9), (0.5, 0.9), (0.5, 0.9)]
    assert _dedup_consecutive(pts) == [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]


def test_dedup_consecutive_keeps_distinct_points() -> None:
    from sentry_agent_pc.gui.zone_editor import _dedup_consecutive

    pts = [(0.1, 0.1), (0.2, 0.2), (0.3, 0.1)]
    assert _dedup_consecutive(pts) == pts


def test_dedup_consecutive_within_epsilon() -> None:
    from sentry_agent_pc.gui.zone_editor import _dedup_consecutive

    # Two points closer than _DEDUP_EPS collapse to one.
    pts = [(0.10, 0.10), (0.1005, 0.1005), (0.9, 0.9), (0.5, 0.5)]
    assert _dedup_consecutive(pts) == [(0.10, 0.10), (0.9, 0.9), (0.5, 0.5)]


def test_frame_grab_no_stream_returns_error() -> None:
    from sentry_agent_pc.discovery import frame_grab

    cam = CameraRecord(uuid="A", name="A", ip="", rtsp_url="", mediamtx_path=None)
    res = frame_grab.grab_still(cam)
    assert not res.ok
    assert res.image is None


def test_frame_grab_candidates_prefers_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sentry_agent_pc.discovery import frame_grab

    cam = CameraRecord(
        uuid="A", name="A", ip="1.1.1.1",
        rtsp_url="rtsp://admin:pw@1.1.1.1:554/s1", mediamtx_path="cam1",
    )

    class _Ctrl:
        def local_url(self, path: str | None) -> str | None:
            return "rtsp://127.0.0.1:8554/cam1" if path else None

    monkeypatch.setattr(
        "sentry_agent_pc.streaming.controller.get_stream_controller", lambda: _Ctrl()
    )
    urls = frame_grab._rtsp_candidates(cam)
    assert urls[0] == "rtsp://127.0.0.1:8554/cam1"  # local fan-out first
    assert urls[-1] == cam.rtsp_url  # direct URL as fallback
