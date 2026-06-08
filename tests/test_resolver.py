"""Multi-protocol resolver scoring + H.265 transcode decision."""

from __future__ import annotations

import pytest

from sentry_agent_pc.discovery import rtsp_probe
from sentry_agent_pc.discovery.rtsp_paths import RTSP_PATHS, RTSP_PATHS_PRIORITY
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.streaming.pusher import PushTarget


def test_score_prefers_h264_over_hevc() -> None:
    # Same resolution → H.264 wins (browser-friendly).
    assert svc._score("h264", 1920, 1080) > svc._score("hevc", 1920, 1080)


def test_score_prefers_higher_resolution() -> None:
    assert svc._score("h264", 2688, 1520) > svc._score("h264", 720, 576)
    # Resolution beats codec only at equal codec; H.264 main vs H.264 sub:
    assert svc._score("h264", 2560, 1440) > svc._score("h264", 352, 288)


def test_rtsp_paths_cover_known_brands() -> None:
    # The paths that actually worked on the live cameras must be present.
    assert "/Streaming/Channels/101" in RTSP_PATHS  # Hikvision
    assert "/media/video1" in RTSP_PATHS            # UNV
    assert "/stream1" in RTSP_PATHS                 # Skyworth (H.265)


def test_push_target_transcodes_only_h265() -> None:
    assert PushTarget("p", "rtsp://x", codec="hevc").needs_transcode is True
    assert PushTarget("p", "rtsp://x", codec="h265").needs_transcode is True
    assert PushTarget("p", "rtsp://x", codec="h264").needs_transcode is False
    assert PushTarget("p", "rtsp://x", codec=None).needs_transcode is False


def test_resolved_stream_is_h264_flag() -> None:
    assert svc.ResolvedStream(ok=True, codec="h264").is_h264 is True
    assert svc.ResolvedStream(ok=True, codec="hevc").is_h264 is False


def test_priority_paths_are_known_brand_mains() -> None:
    # The priority batch must carry one main-stream path per brand we support
    # and be a strict subset of the full library (so the tail = full − priority).
    assert "/Streaming/Channels/101" in RTSP_PATHS_PRIORITY  # Hik
    assert "/media/video1" in RTSP_PATHS_PRIORITY            # UNV
    assert "/stream1" in RTSP_PATHS_PRIORITY                 # Skyworth
    assert set(RTSP_PATHS_PRIORITY).issubset(set(RTSP_PATHS))


def test_rtsp_resolve_aborts_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wrong password (401 on every path) must stop the sweep almost
    # immediately — NOT grind all 33 paths (which is what locks the account).
    calls: list[str] = []

    def fake_probe(url: str, timeout_sec: int | None = None) -> rtsp_probe.ProbeResult:
        calls.append(url)
        return rtsp_probe.ProbeResult(
            ok=False, url=url, error="401 Unauthorized", is_auth_error=True
        )

    monkeypatch.setattr(rtsp_probe, "probe", fake_probe)
    stream, auth_seen = svc._best_rtsp_path_stream("10.0.0.9", "admin", "wrong")
    assert stream is None
    assert auth_seen is True
    # Stopped within the first (priority) batch — never reached the long tail.
    assert len(calls) <= len(RTSP_PATHS_PRIORITY)


def test_rtsp_resolve_returns_first_working_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(url: str, timeout_sec: int | None = None) -> rtsp_probe.ProbeResult:
        if url.endswith("/stream1"):
            return rtsp_probe.ProbeResult(
                ok=True, url=url, codec="hevc", width=2560, height=1920
            )
        return rtsp_probe.ProbeResult(ok=False, url=url, error="404")

    monkeypatch.setattr(rtsp_probe, "probe", fake_probe)
    stream, auth_seen = svc._best_rtsp_path_stream("10.0.0.9", "admin", "pw")
    assert stream is not None and stream.ok
    assert stream.rtsp_url is not None and stream.rtsp_url.endswith("/stream1")
    assert stream.codec == "hevc"
    assert auth_seen is False


def test_register_uses_resolved_without_reprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    # When Scan hands register_camera the stream it already verified, register
    # must NOT pull the URL a second time.
    def boom(*_a: object, **_k: object) -> rtsp_probe.ProbeResult:
        raise AssertionError("register_camera must not re-probe when resolved is given")

    monkeypatch.setattr(rtsp_probe, "probe", boom)
    monkeypatch.setattr(svc, "load_state", lambda: _EmptyState())

    class _FakeBackend:
        def agent_register_camera(self, *, name: str, rtsp_url: str) -> dict[str, str]:
            return {"id": "cam-uuid", "mediamtx_path": "cam_x"}

    resolved = svc.ResolvedStream(
        ok=True, rtsp_url="rtsp://x/stream1", codec="h264", width=1920, height=1080
    )
    monkeypatch.setattr(svc, "save_state", lambda _s: None)
    result = svc.register_camera(
        name="c", ip="10.0.0.9", rtsp_url="rtsp://x/stream1",
        backend=_FakeBackend(), resolved=resolved,  # type: ignore[arg-type]
    )
    assert result.ok and result.codec == "h264"


class _EmptyState:
    cameras: list[object] = []


def _mk_state(cams):  # type: ignore[no-untyped-def]
    from sentry_agent_pc.state import AgentState

    return AgentState(agent_jwt="jwt", cameras=cams)


def test_reconcile_drops_web_deleted_camera(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sentry_agent_pc.state import CameraRecord

    local = _mk_state([
        CameraRecord(uuid="A", name="Cam A", ip="1.1.1.1", rtsp_url="rtsp://a"),
        CameraRecord(uuid="B", name="Cam B", ip="2.2.2.2", rtsp_url="rtsp://b"),
    ])
    saved = {}
    monkeypatch.setattr(svc, "load_state", lambda: local)
    monkeypatch.setattr(svc, "save_state", lambda s: saved.update(c=s.cameras))

    class FakeBackend:
        def agent_list_cameras(self):  # type: ignore[no-untyped-def]
            return [{"id": "A", "name": "Cam A", "mediamtx_path": "a"}]  # B deleted on web

    cams, changed = svc.reconcile_with_backend(backend=FakeBackend())  # type: ignore[arg-type]
    assert changed is True
    assert [c.uuid for c in cams] == ["A"]  # B pruned


def test_reconcile_offline_keeps_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sentry_agent_pc.state import CameraRecord

    local = _mk_state([CameraRecord(uuid="A", name="A", ip="1.1.1.1", rtsp_url="rtsp://a")])
    monkeypatch.setattr(svc, "load_state", lambda: local)
    monkeypatch.setattr(svc, "save_state", lambda s: None)

    class DeadBackend:
        def agent_list_cameras(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("network down")

    cams, changed = svc.reconcile_with_backend(backend=DeadBackend())  # type: ignore[arg-type]
    assert changed is False
    assert [c.uuid for c in cams] == ["A"]  # offline never wipes


def test_reconcile_surfaces_backend_only_camera(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    local = _mk_state([])
    monkeypatch.setattr(svc, "load_state", lambda: local)
    monkeypatch.setattr(svc, "save_state", lambda s: None)

    class FakeBackend:
        def agent_list_cameras(self):  # type: ignore[no-untyped-def]
            return [{"id": "X", "name": "Remote Cam", "mediamtx_path": "x"}]

    cams, changed = svc.reconcile_with_backend(backend=FakeBackend())  # type: ignore[arg-type]
    assert changed is True
    assert len(cams) == 1
    assert cams[0].uuid == "X" and cams[0].rtsp_url == ""  # shown, but can't push
