"""EdgeController reconcile gating — cloud cameras ignored, edge_pc get a worker.

The real _CamWorker (decode thread + OpenVINO load) is replaced with a fake so the
test exercises only the reconcile decision, no camera/model I/O."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sentry_agent_pc.edge.controller as ctrl_mod
from sentry_agent_pc.edge.controller import EdgeController


class _FakeWorker:
    def __init__(self, _runtime: object, camera_id: str, src_url: str) -> None:
        self.camera_id = camera_id
        self.src_url = src_url
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _cam(tier: str, path: str | None = "cam_a", rtsp: str | None = "rtsp://x/1") -> object:
    return SimpleNamespace(compute_tier=tier, mediamtx_path=path, rtsp_url=rtsp, uuid="u")


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ctrl_mod, "_CamWorker", _FakeWorker)
    monkeypatch.setattr(
        ctrl_mod, "get_settings", lambda: SimpleNamespace(edge_ai_enabled=True, edge_clips_enabled=True)
    )
    monkeypatch.setattr(
        ctrl_mod, "get_stream_controller", lambda: SimpleNamespace(local_url=lambda _p: None)
    )

    def _set_cameras(cams: list[object]) -> None:
        monkeypatch.setattr(
            ctrl_mod, "load_state", lambda: SimpleNamespace(is_paired=True, cameras=cams)
        )

    return _set_cameras


def test_cloud_camera_ignored(patched) -> None:
    patched([_cam("cloud")])
    c = EdgeController()
    c.refresh()
    assert c._workers == {}  # noqa: SLF001 — white-box reconcile assertion


def test_edge_pc_camera_starts_worker(patched) -> None:
    patched([_cam("edge_pc", path="cam_a"), _cam("cloud", path="cam_b")])
    c = EdgeController()
    c.refresh()
    assert set(c._workers) == {"cam_a"}  # noqa: SLF001
    assert c._workers["cam_a"].started is True  # noqa: SLF001


def test_edge_pc_no_rtsp_but_loopback_starts(patched, monkeypatch) -> None:
    # A camera re-registered + synced from the backend has NO local rtsp_url, but
    # the push relay's loopback exists → the worker must still start off the hub.
    monkeypatch.setattr(
        ctrl_mod,
        "get_stream_controller",
        lambda: SimpleNamespace(local_url=lambda p: f"rtsp://127.0.0.1:18554/{p}"),
    )
    patched([_cam("edge_pc", path="cam_a", rtsp=None)])
    c = EdgeController()
    c.refresh()
    assert set(c._workers) == {"cam_a"}  # noqa: SLF001
    assert c._workers["cam_a"].src_url == "rtsp://127.0.0.1:18554/cam_a"  # noqa: SLF001


def test_edge_pc_no_rtsp_no_loopback_skipped(patched) -> None:
    # Neither a loopback (default fixture local_url→None) nor a stored rtsp → skip.
    patched([_cam("edge_pc", path="cam_a", rtsp=None)])
    c = EdgeController()
    c.refresh()
    assert c._workers == {}  # noqa: SLF001


def test_removed_camera_stops_worker(patched) -> None:
    patched([_cam("edge_pc", path="cam_a")])
    c = EdgeController()
    c.refresh()
    worker = c._workers["cam_a"]  # noqa: SLF001
    patched([])  # camera removed
    c.refresh()
    assert c._workers == {}  # noqa: SLF001
    assert worker.stopped is True
