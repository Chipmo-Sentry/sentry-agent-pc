"""Edit-camera flow: URL parse/build helpers + update_camera_connection."""

from __future__ import annotations

from sentry_agent_pc.gui.edit_dialog import build_rtsp, parse_rtsp
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.state import AgentState, CameraRecord

# === parse_rtsp / build_rtsp ===


def test_parse_rtsp_full_url() -> None:
    p = parse_rtsp("rtsp://admin:p%40ss@192.168.1.64:554/Streaming/Channels/101")
    assert p["user"] == "admin"
    assert p["password"] == "p@ss"  # URL-decoded
    assert p["host"] == "192.168.1.64"
    assert p["port"] == "554"
    assert p["path"] == "Streaming/Channels/101"


def test_parse_rtsp_no_creds_default_port() -> None:
    p = parse_rtsp("rtsp://192.168.1.13/media/video1")
    assert p["user"] == "" and p["password"] == ""
    assert p["host"] == "192.168.1.13"
    assert p["port"] == "554"
    assert p["path"] == "media/video1"


def test_parse_rtsp_keeps_query() -> None:
    p = parse_rtsp("rtsp://u:pw@10.0.0.5:8554/cam?channel=1")
    assert p["path"] == "cam?channel=1"


def test_build_rtsp_round_trips_through_parse() -> None:
    url = build_rtsp(
        user="admin", password="p@ss", host="192.168.1.64", port=554,
        path="/Streaming/Channels/101",
    )
    assert url == "rtsp://admin:p%40ss@192.168.1.64:554/Streaming/Channels/101"
    p = parse_rtsp(url)
    assert (p["user"], p["password"], p["host"], p["port"], p["path"]) == (
        "admin", "p@ss", "192.168.1.64", "554", "Streaming/Channels/101",
    )


def test_build_rtsp_no_creds() -> None:
    assert build_rtsp(user="", password="", host="10.0.0.9", port=554, path="s1") == (
        "rtsp://10.0.0.9:554/s1"
    )


# === update_camera_connection ===


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def agent_update_camera(self, uuid: str, **kw: object) -> dict[str, object]:
        self.calls.append({"uuid": uuid, **kw})
        return {"id": uuid}


def _patch_state(monkeypatch, cameras: list[CameraRecord]):  # type: ignore[no-untyped-def]
    state = AgentState(agent_jwt="tok", cameras=cameras)
    saved: dict[str, AgentState] = {}

    def fake_save(s: AgentState) -> None:
        saved["state"] = s

    monkeypatch.setattr(svc, "load_state", lambda: state)
    monkeypatch.setattr(svc, "save_state", fake_save)
    return state, saved


def test_update_connection_patches_backend_and_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cam = CameraRecord(
        uuid="cam-1", name="Old", ip="192.168.1.64",
        rtsp_url="rtsp://admin:pw@192.168.1.64:554/s1", mediamtx_path="cam1",
        codec="h264", resolution=(1920, 1080),
    )
    state, saved = _patch_state(monkeypatch, [cam])
    fake = _FakeBackend()

    res = svc.update_camera_connection(
        camera_uuid="cam-1",
        name="New Name",
        ip="192.168.1.70",
        rtsp_url="rtsp://admin:pw@192.168.1.70:554/s1",
        resolved=svc.ResolvedStream(ok=True, codec="hevc", width=2560, height=1440),
        backend=fake,  # type: ignore[arg-type]
    )

    assert res.ok
    assert fake.calls == [
        {"uuid": "cam-1", "name": "New Name", "rtsp_url": "rtsp://admin:pw@192.168.1.70:554/s1",
         "risk_threshold": None}
    ]
    # Local record updated (path is NOT touched — stream identity stays).
    assert cam.name == "New Name"
    assert cam.ip == "192.168.1.70"
    assert cam.codec == "hevc"
    assert cam.resolution == (2560, 1440)
    assert cam.mediamtx_path == "cam1"
    assert saved.get("state") is state


def test_update_connection_rejects_duplicate_ip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cam_a = CameraRecord(uuid="a", name="A", ip="192.168.1.10", rtsp_url="rtsp://a/s")
    cam_b = CameraRecord(uuid="b", name="B", ip="192.168.1.20", rtsp_url="rtsp://b/s")
    _patch_state(monkeypatch, [cam_a, cam_b])

    res = svc.update_camera_connection(camera_uuid="b", ip="192.168.1.10")
    assert not res.ok
    assert "192.168.1.10" in (res.error or "")


def test_update_connection_unknown_uuid(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_state(monkeypatch, [])
    res = svc.update_camera_connection(camera_uuid="ghost", name="X")
    assert not res.ok


def test_update_connection_backend_failure_keeps_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sentry_agent_pc.backend_client import BackendError

    cam = CameraRecord(uuid="c", name="Keep", ip="1.1.1.1", rtsp_url="rtsp://c/s")
    _patch_state(monkeypatch, [cam])

    class _Boom:
        def agent_update_camera(self, *a: object, **k: object) -> dict[str, object]:
            raise BackendError("500 server error")

    res = svc.update_camera_connection(
        camera_uuid="c", name="Changed", backend=_Boom(),  # type: ignore[arg-type]
    )
    assert not res.ok
    assert cam.name == "Keep"  # local untouched when backend fails
