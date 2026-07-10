"""FloorPlanApi — the pywebview js_api bridge (docs/30). Backend + state mocked."""

from __future__ import annotations

import sys

from sentry_agent_pc.gui import floor_plan_web as fpw
from sentry_agent_pc.gui.floor_plan_web import FloorPlanApi, maybe_run_floor_plan_from_argv
from sentry_agent_pc.state import AgentState, CameraRecord

_PLAN = {"version": 1, "size": [1000, 800], "walls": [], "fixtures": [], "cameras": []}


class _FakeBackend:
    def __init__(self) -> None:
        self.saved: dict | None = None

    def agent_get_floor_plan(self) -> dict:
        return _PLAN

    def agent_update_floor_plan(self, plan: dict) -> dict:
        self.saved = plan
        return plan


def test_list_cameras_from_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state = AgentState(
        cameras=[
            CameraRecord(name="Cam A", ip="1.1.1.1", rtsp_url="rtsp://a", mediamtx_path="cam_a"),
            CameraRecord(name="No Path", ip="2.2.2.2", rtsp_url="rtsp://b"),  # no path → skipped
        ]
    )
    monkeypatch.setattr("sentry_agent_pc.state.load_state", lambda: state)
    cams = FloorPlanApi().list_cameras()
    assert cams == [{"camera_id": "cam_a", "name": "Cam A"}]  # path-less camera dropped


def test_load_plan_ok(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: _FakeBackend())
    assert FloorPlanApi().load_plan() == _PLAN


def test_load_plan_swallows_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Boom:
        def agent_get_floor_plan(self) -> dict:
            raise RuntimeError("offline")

    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: _Boom())
    assert FloorPlanApi().load_plan() == {}  # editor starts blank, never crashes


def test_save_plan_patches_backend(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = _FakeBackend()
    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: fake)
    plan = {**_PLAN, "fixtures": [{"type": "exit", "points": [[0, 0], [1, 0], [0.5, 1]]}]}
    out = FloorPlanApi().save_plan(plan)
    assert fake.saved == plan and out == plan


def test_save_plan_propagates_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Boom:
        def agent_update_floor_plan(self, plan: dict) -> dict:
            raise RuntimeError("422")

    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: _Boom())
    try:
        FloorPlanApi().save_plan(_PLAN)
    except RuntimeError as e:
        assert "422" in str(e)  # surfaces to the JS Promise reject
    else:
        raise AssertionError("save_plan must propagate the backend error")


def test_maybe_run_floor_plan_noop_without_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = {"n": 0}
    monkeypatch.setattr(fpw, "_run_window", lambda: called.__setitem__("n", called["n"] + 1))
    assert maybe_run_floor_plan_from_argv(["gui_main"]) is False
    assert called["n"] == 0
    assert maybe_run_floor_plan_from_argv(["gui_main", "--floor-plan"]) is True
    assert called["n"] == 1


def test_floor_plan_flag_constant() -> None:
    # The spawn + entry must agree on the flag string.
    assert fpw._FLAG == "--floor-plan"
    assert sys is not None  # import-smoke


def test_compute_calibration_identity_scale() -> None:
    # Plan corners → image corners is a pure /1000,/800 scale; a fixture must
    # project to the matching normalized rectangle, reproj error ~0.
    pairs = [
        {"plan": [0, 0], "image": [0, 0]},
        {"plan": [1000, 0], "image": [1, 0]},
        {"plan": [1000, 800], "image": [1, 1]},
        {"plan": [0, 800], "image": [0, 1]},
    ]
    fixtures = [{"type": "shelf", "points": [[250, 200], [750, 200], [750, 600], [250, 600]]}]
    homography, err, zones = fpw._compute_calibration(pairs, fixtures)
    assert len(homography) == 3 and len(homography[0]) == 3
    assert err < 1e-6
    assert len(zones) == 1
    z = zones[0]
    assert z["type"] == "shelf"
    assert abs(z["points"][0][0] - 0.25) < 1e-3 and abs(z["points"][0][1] - 0.25) < 1e-3
    assert abs(z["points"][2][0] - 0.75) < 1e-3 and abs(z["points"][2][1] - 0.75) < 1e-3


def test_compute_calibration_skips_out_of_view_fixture() -> None:
    # A fixture far outside the camera's view (projects well beyond [0,1]) is not
    # turned into a zone for this camera.
    pairs = [
        {"plan": [0, 0], "image": [0, 0]},
        {"plan": [1000, 0], "image": [1, 0]},
        {"plan": [1000, 800], "image": [1, 1]},
        {"plan": [0, 800], "image": [0, 1]},
    ]
    fixtures = [{"type": "shelf", "points": [[5000, 5000], [5200, 5000], [5200, 5200]]}]
    _h, _e, zones = fpw._compute_calibration(pairs, fixtures)
    assert zones == []


def test_compute_calibration_needs_four_points() -> None:
    import pytest

    with pytest.raises(ValueError):
        fpw._compute_calibration([{"plan": [0, 0], "image": [0, 0]}], [])


def test_preview_calibration_dry_run() -> None:
    # Same geometry as the identity-scale test, but through the bridge: returns
    # the derived zones + error WITHOUT touching backend/state (pure compute).
    pairs = [
        {"plan": [0, 0], "image": [0, 0]},
        {"plan": [1000, 0], "image": [1, 0]},
        {"plan": [1000, 800], "image": [1, 1]},
        {"plan": [0, 800], "image": [0, 1]},
    ]
    plan = {
        "fixtures": [{"type": "shelf", "points": [[250, 200], [750, 200], [750, 600], [250, 600]]}]
    }
    r = FloorPlanApi().preview_calibration(pairs, plan)
    assert r["ok"] is True
    assert r["reproj_err"] < 1e-6
    assert len(r["zones"]) == 1 and r["zones"][0]["type"] == "shelf"


def test_preview_calibration_degrades_not_raises() -> None:
    # Mid-calibration states (too few points) must return {ok: False}, never
    # bubble an exception into the JS Promise — the preview runs on every click.
    r = FloorPlanApi().preview_calibration([{"plan": [0, 0], "image": [0, 0]}], {})
    assert r["ok"] is False and "4" in r["error"]
    assert FloorPlanApi().preview_calibration([], "not-a-dict")["ok"] is False  # type: ignore[arg-type]


def test_set_dirty_mirrors_flag_and_save_clears_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = _FakeBackend()
    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: fake)
    api = FloorPlanApi()
    assert api.dirty is False
    api.set_dirty(True)
    assert api.dirty is True
    api.save_plan(_PLAN)  # successful save stands the close guard down
    assert api.dirty is False


def test_rtsp_host_port_parsing() -> None:
    assert fpw._rtsp_host_port("rtsp://admin:pw@192.168.1.64:554/Streaming") == (
        "192.168.1.64",
        554,
    )
    assert fpw._rtsp_host_port("rtsp://admin:pw@10.0.0.9/stream") == ("10.0.0.9", 554)
    assert fpw._rtsp_host_port("rtsp://cam.local:8554/s") == ("cam.local", 8554)
    assert fpw._rtsp_host_port("192.168.1.50") == ("192.168.1.50", 554)


def test_camera_status_unknown_camera(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sentry_agent_pc.state import AgentState

    monkeypatch.setattr("sentry_agent_pc.state.load_state", lambda: AgentState(cameras=[]))
    r = fpw.FloorPlanApi().camera_status("nope")
    assert r["ok"] is False


def test_save_plan_rejects_non_dict(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The bridge is the only Python gate before a JWT-authed PATCH — a non-object
    # payload must be refused before it can reach the backend.
    fake = _FakeBackend()
    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: fake)
    try:
        FloorPlanApi().save_plan(["not", "a", "dict"])  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("save_plan must reject a non-dict plan")
    assert fake.saved is None  # never reached the backend


def test_save_plan_rejects_oversized(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A runaway shape list (>1 MB serialized) is bounded at the bridge.
    fake = _FakeBackend()
    monkeypatch.setattr("sentry_agent_pc.backend_client.BackendClient", lambda: fake)
    try:
        FloorPlanApi().save_plan({"blob": "x" * (fpw._MAX_PLAN_BYTES + 1)})
    except ValueError:
        pass
    else:
        raise AssertionError("save_plan must reject an oversized plan")
    assert fake.saved is None
