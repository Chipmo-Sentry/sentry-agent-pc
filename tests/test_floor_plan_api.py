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


def test_compute_calibration_skips_furniture() -> None:
    # Furniture is scenery: visible on the plan/analytics, but must NOT become a
    # detection zone (the engine has no semantics for it, and backend Zone
    # consumers only know shelf/exit/entrance/checkout).
    pairs = [
        {"plan": [0, 0], "image": [0, 0]},
        {"plan": [1000, 0], "image": [1, 0]},
        {"plan": [1000, 800], "image": [1, 1]},
        {"plan": [0, 800], "image": [0, 1]},
    ]
    fixtures = [
        {"type": "furniture", "points": [[250, 200], [750, 200], [750, 600], [250, 600]]},
        {"type": "shelf", "points": [[250, 200], [750, 200], [750, 600], [250, 600]]},
    ]
    _h, _e, zones = fpw._compute_calibration(pairs, fixtures)
    assert [z["type"] for z in zones] == ["shelf"]


# === zone clipping (docs/30 Phase B) ===

_SCALE_PAIRS = [
    {"plan": [0, 0], "image": [0, 0]},
    {"plan": [1000, 0], "image": [1, 0]},
    {"plan": [1000, 800], "image": [1, 1]},
    {"plan": [0, 800], "image": [0, 1]},
]


def test_partially_visible_fixture_clipped_at_frame_edge() -> None:
    # A shelf half off-frame must be CUT at the frame border (new vertices on
    # x=1), not have its outside corners snapped onto the border.
    fixtures = [{"type": "shelf", "points": [[500, 200], [1500, 200], [1500, 600], [500, 600]]}]
    _h, _e, zones = fpw._compute_calibration(_SCALE_PAIRS, fixtures)
    assert len(zones) == 1
    xs = [p[0] for p in zones[0]["points"]]
    ys = [p[1] for p in zones[0]["points"]]
    assert max(xs) == 1.0 and abs(min(xs) - 0.5) < 1e-3
    assert abs(min(ys) - 0.25) < 1e-3 and abs(max(ys) - 0.75) < 1e-3
    assert abs(fpw._poly_area(zones[0]["points"]) - 0.25) < 1e-3


def test_full_frame_fixture_kept() -> None:
    # Every corner is OFF-frame but the fixture covers the whole view — the old
    # any-vertex-visible gate silently dropped it; clipping keeps the full frame.
    fixtures = [
        {"type": "shelf", "points": [[-500, -500], [1500, -500], [1500, 1300], [-500, 1300]]}
    ]
    _h, _e, zones = fpw._compute_calibration(_SCALE_PAIRS, fixtures)
    assert len(zones) == 1
    assert abs(fpw._poly_area(zones[0]["points"]) - 1.0) < 1e-3


def _horizon_pairs() -> list[dict[str, list[float]]]:
    """Pairs generated from an exact projective map with a horizon inside the
    plan: w = 1 - x/4000, so plan points at x >= 4000 are at/behind the camera."""

    def proj(x: float, y: float) -> list[float]:
        w = 1.0 - x / 4000.0
        return [x / 1000.0 / w, y / 800.0 / w]

    return [
        {"plan": [float(x), float(y)], "image": proj(x, y)}
        for x, y in [(0, 0), (1000, 0), (1000, 800), (0, 800)]
    ]


def test_behind_camera_fixture_dropped() -> None:
    # Entirely behind the camera's principal plane (w < 0): projecting it yields
    # wrapped garbage — the plan-space w-clip must remove it completely.
    fixtures = [{"type": "shelf", "points": [[5000, 0], [6000, 0], [6000, 800], [5000, 800]]}]
    _h, _e, zones = fpw._compute_calibration(_horizon_pairs(), fixtures)
    assert zones == []


def test_horizon_straddling_fixture_no_garbage() -> None:
    # Straddles the horizon (w changes sign inside the polygon). Its in-front
    # remainder projects far outside the frame here, so nothing may survive —
    # and in particular no frame-spanning sliver may appear (the old clamping
    # produced exactly that).
    fixtures = [{"type": "shelf", "points": [[3000, 0], [5000, 0], [5000, 800], [3000, 800]]}]
    _h, _e, zones = fpw._compute_calibration(_horizon_pairs(), fixtures)
    assert zones == []


# === wall occlusion ===


def test_fixture_behind_wall_dropped() -> None:
    # A full-width wall stands between the camera and the shelf — the camera
    # can't see it, so it must not become a zone.
    fixtures = [{"type": "shelf", "points": [[400, 200], [600, 200], [600, 300], [400, 300]]}]
    walls = [{"points": [[0, 400], [1000, 400]]}]
    _h, _e, zones = fpw._compute_calibration(
        _SCALE_PAIRS, fixtures, walls=walls, cam_pos=(500.0, 700.0)
    )
    assert zones == []


def test_fixture_touching_wall_kept() -> None:
    # Shelves usually LINE walls: the wall coincides with the shelf's back edge.
    # The sight-line slack must keep the shelf a zone instead of treating its
    # own backing wall as an occluder.
    fixtures = [{"type": "shelf", "points": [[400, 200], [600, 200], [600, 300], [400, 300]]}]
    walls = [{"points": [[0, 200], [1000, 200]]}]  # collinear with the back edge
    _h, _e, zones = fpw._compute_calibration(
        _SCALE_PAIRS, fixtures, walls=walls, cam_pos=(500.0, 700.0)
    )
    assert len(zones) == 1


def test_fixture_partially_behind_wall_clipped() -> None:
    # A half-width partition hides the LEFT half of a wide shelf; only the
    # visible right part may survive, and nothing may leak far left.
    fixtures = [{"type": "shelf", "points": [[0, 200], [1000, 200], [1000, 300], [0, 300]]}]
    walls = [{"points": [[0, 400], [500, 400]]}]
    _h, _e, zones = fpw._compute_calibration(
        _SCALE_PAIRS, fixtures, walls=walls, cam_pos=(500.0, 700.0)
    )
    assert len(zones) == 1
    xs = [p[0] for p in zones[0]["points"]]
    assert min(xs) > 0.4  # hidden left half is gone
    assert max(xs) <= 1.0


def test_no_cam_pos_skips_occlusion() -> None:
    # Without a camera position (old plans / preview before placement) the wall
    # must not change behaviour — occlusion is simply skipped.
    fixtures = [{"type": "shelf", "points": [[400, 200], [600, 200], [600, 300], [400, 300]]}]
    walls = [{"points": [[0, 400], [1000, 400]]}]
    _h, _e, zones = fpw._compute_calibration(_SCALE_PAIRS, fixtures, walls=walls, cam_pos=None)
    assert len(zones) == 1


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


# ── v0.7.95: RANSAC / k1 lens distortion / 3D fixture heights ────────────────


def _grid_pairs(nx: int = 3, ny: int = 3) -> list[dict]:
    """A planar grid of plan(m)→image(0-1) pairs under a pure scale mapping
    (plan 10×8 m ↔ full frame)."""
    pairs = []
    for i in range(nx):
        for j in range(ny):
            px, py = 10.0 * i / (nx - 1), 8.0 * j / (ny - 1)
            pairs.append({"plan": [px, py], "image": [px / 10.0, py / 8.0]})
    return pairs


def test_ransac_survives_one_bad_click() -> None:
    # 9 good pairs + 1 wildly wrong click: RANSAC must ignore the outlier and
    # keep the fit tight; plain DLT would smear the error over every point.
    pairs = _grid_pairs()
    pairs.append({"plan": [5.0, 4.0], "image": [0.95, 0.05]})  # bad click
    fixtures = [{"type": "shelf", "points": [[2.5, 2.0], [7.5, 2.0], [7.5, 6.0], [2.5, 6.0]]}]
    _h, err, zones = fpw._compute_calibration(pairs, fixtures)
    assert err < 0.01  # outlier rejected (DLT here gives ~0.03+)
    assert len(zones) == 1
    z = zones[0]["points"]
    assert abs(z[0][0] - 0.25) < 0.02 and abs(z[0][1] - 0.25) < 0.02


def test_four_point_calibration_unchanged_by_upgrades() -> None:
    # The minimal 4-point flow must behave exactly as before: no RANSAC, no k1,
    # no 3D — byte-identical zone maths for the common case.
    pairs = [
        {"plan": [0, 0], "image": [0, 0]},
        {"plan": [1000, 0], "image": [1, 0]},
        {"plan": [1000, 800], "image": [1, 1]},
        {"plan": [0, 800], "image": [0, 1]},
    ]
    fixtures = [{"type": "shelf", "points": [[250, 200], [750, 200], [750, 600], [250, 600]]}]
    _h, err, zones = fpw._compute_calibration(pairs, fixtures)
    assert err < 1e-6
    assert abs(zones[0]["points"][0][0] - 0.25) < 1e-3


def test_k1_estimated_from_distorted_pairs() -> None:
    # Synthesize barrel distortion (k1=0.2) over a 4×4 grid: the fit must
    # recover most of it — reproj error far below the k1=0 fit.
    true_k1 = 0.2
    pairs = _grid_pairs(4, 4)
    for p in pairs:
        ((x, y),) = fpw._distort_pts([p["image"]], true_k1)
        p["image"] = [x, y]
    _h, err, _z = fpw._compute_calibration(pairs, [])
    assert err < 0.004  # without k1 the residual is ~0.01+


def test_height_zone_covers_more_than_footprint() -> None:
    # A tall shelf seen from a 3 m camera: the 3D zone (footprint + top faces)
    # must be a superset of the flat-footprint zone in area.
    import cv2
    import numpy as np

    # Ground truth: camera at (5, 12, 3) looking down-forward at the 10×8 m
    # floor (explicit right-handed look-at basis; rows = cam x/y/z in world).
    f, aspect = 0.9, 16 / 9
    k_mtx = np.array([[f, 0, 0.5], [0, f * aspect, 0.5], [0, 0, 1.0]])
    rot = np.array(
        [
            [-1.0, 0.0, 0.0],
            [0.0, 0.3511, -0.9363],
            [0.0, -0.9363, -0.3511],
        ]
    )
    rvec, _ = cv2.Rodrigues(rot)
    cam_world = np.array([5.0, 12.0, 3.0])
    tvec = (-rot @ cam_world).reshape(3, 1)

    def img_of(pt3):
        proj, _ = cv2.projectPoints(np.array([pt3], dtype=float), rvec, tvec, k_mtx, None)
        return [float(proj[0][0][0]), float(proj[0][0][1])]

    grid = [[x, y] for x in (1.0, 5.0, 9.0) for y in (1.0, 4.0, 7.0)]
    pairs = [{"plan": g, "image": img_of([g[0], g[1], 0.0])} for g in grid]
    foot = [[4.0, 3.0], [6.0, 3.0], [6.0, 3.6], [4.0, 3.6]]

    flat = [{"type": "shelf", "points": foot, "height_m": 0}]
    tall = [{"type": "shelf", "points": foot, "height_m": 1.8}]
    _h1, _e1, z_flat = fpw._compute_calibration(pairs, flat, img_aspect=aspect)
    _h2, _e2, z_tall = fpw._compute_calibration(pairs, tall, img_aspect=aspect)
    assert len(z_flat) == 1 and len(z_tall) == 1
    a_flat = fpw._poly_area(z_flat[0]["points"])
    a_tall = fpw._poly_area(z_tall[0]["points"])
    assert a_tall > a_flat * 1.3  # the visible solid is clearly bigger


def test_high_camera_sees_over_low_wall() -> None:
    # 2D: any wall blocks. 3D (cam_h known): a 3 m camera sees a fixture 6 m
    # past a 1.2 m gondola — the ray clears the top.
    segs = fpw._wall_segments([{"points": [[0, 5], [10, 5]], "height_m": 1.2}])
    poly = [[4.0, 8.0], [6.0, 8.0], [6.0, 9.0], [4.0, 9.0]]
    cam = (5.0, 1.0)
    hidden_2d = fpw._visible_part(poly, cam, segs)
    seen_3d = fpw._visible_part(poly, cam, segs, cam_h=3.0)
    assert hidden_2d == []
    assert len(seen_3d) >= 3  # fully visible over the low wall


def test_full_wall_still_blocks_in_3d() -> None:
    segs = fpw._wall_segments([{"points": [[0, 5], [10, 5]]}])  # default 2.8 m
    poly = [[4.0, 6.0], [6.0, 6.0], [6.0, 7.0], [4.0, 7.0]]  # right behind it
    assert fpw._visible_part(poly, (5.0, 1.0), segs, cam_h=3.0) == []
