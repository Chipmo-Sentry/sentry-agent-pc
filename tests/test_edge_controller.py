"""EdgeController reconcile gating — cloud cameras ignored, edge_pc get a worker.

The real _CamWorker (decode thread + OpenVINO load) is replaced with a fake so the
test exercises only the reconcile decision, no camera/model I/O."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sentry_agent_pc.edge.controller as ctrl_mod
from sentry_agent_pc.edge.controller import EdgeController


class _FakeWorker:
    def __init__(
        self,
        _runtime: object,
        camera_id: str,
        src_url: str,
        zones: list[dict[str, object]] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.src_url = src_url
        self.zones = zones
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _cam(
    tier: str,
    path: str | None = "cam_a",
    rtsp: str | None = "rtsp://x/1",
    zones: list[dict[str, object]] | None = None,
) -> object:
    return SimpleNamespace(
        compute_tier=tier, mediamtx_path=path, rtsp_url=rtsp, uuid="u", zones=zones
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ctrl_mod, "_CamWorker", _FakeWorker)
    monkeypatch.setattr(
        ctrl_mod,
        "get_settings",
        lambda: SimpleNamespace(edge_ai_enabled=True, edge_clips_enabled=True),
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


# === docs/33 P0-6 — zones + backend config reach the headless path ===


def test_zones_passed_to_worker(patched) -> None:
    zones = [{"type": "exit", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]}]
    patched([_cam("edge_pc", path="cam_a", zones=zones)])
    c = EdgeController()
    c.refresh()
    assert c._workers["cam_a"].zones == zones  # noqa: SLF001


def test_zone_edit_restarts_worker(patched) -> None:
    """A zone edit must restart the worker (the pipeline takes zones at
    construction) — pre-fix the headless engine never saw zones at all."""
    patched([_cam("edge_pc", path="cam_a", zones=None)])
    c = EdgeController()
    c.refresh()
    old = c._workers["cam_a"]  # noqa: SLF001
    zones = [{"type": "shelf", "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]}]
    patched([_cam("edge_pc", path="cam_a", zones=zones)])
    c.refresh()
    assert old.stopped is True
    new = c._workers["cam_a"]  # noqa: SLF001
    assert new is not old
    assert new.zones == zones


def test_config_poll_applies_to_headless_runtime(patched, monkeypatch) -> None:
    """docs/33 P0-6 regression: the controller must poll backend edge-config into
    its OWN runtime — pre-fix poll_and_apply was wired only to the GUI live-view
    pipelines and superadmin «Edge тохиргоо» had zero effect on the 24/7 engine."""
    import threading

    import sentry_agent_pc.edge.config_poller as poller_mod

    polled = threading.Event()
    seen: dict[str, object] = {}

    def fake_poll(_client: object, pipes: list[object], last_version: int) -> int:
        seen["pipes"] = list(pipes)
        seen["last_version"] = last_version
        polled.set()
        return 7

    monkeypatch.setattr(poller_mod, "poll_and_apply", fake_poll)
    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: SimpleNamespace())
    c = EdgeController()
    assert polled.wait(3.0), "config poll thread never ran"
    assert seen["pipes"] == [c._runtime]  # noqa: SLF001 — applies to the runtime
    assert seen["last_version"] == -1
    for _ in range(50):  # version assignment races the event by a hair
        if c._cfg_version == 7:  # noqa: SLF001
            break
        import time

        time.sleep(0.02)
    assert c._cfg_version == 7  # noqa: SLF001
    c.stop()
