"""Floor-plan editor (docs/30) — a pywebview window hosting the Konva web editor.

Like the live view (gui/live_view.py), pywebview must own the main thread and can
only run once per process, so the main GUI spawns this as a SEPARATE process
(`ChipmoSentryAgent.exe --floor-plan` when frozen, or
`python -m sentry_agent_pc.gui_main --floor-plan` in dev). The child loads the
bundled local web app and exposes `FloorPlanApi` to JS via pywebview's `js_api`
bridge — so the agent JWT (backend calls) stays in Python, never in the page.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.gui.floor_plan_web")

_FLAG = "--floor-plan"

# Bound for the JS↔Python bridge. The plan is a small vector document (a handful
# of polygons); >1 MB means a runaway shape list, not a real store.
_MAX_PLAN_BYTES = 1_000_000

# Phase B calibration: a fixture is turned into a zone for THIS camera only when
# at least one projected vertex lands within this margin of the [0,1] frame —
# i.e. the camera can actually see it. (Slightly outside is kept so a fixture half
# off-frame still becomes a clipped zone.)
_VISIBLE_MARGIN = 0.25


def _compute_calibration(
    pairs: list[dict[str, Any]], fixtures: list[dict[str, Any]]
) -> tuple[list[list[float]], float, list[dict[str, Any]]]:
    """Fit a plan→image homography from ≥4 point pairs and project the plan
    fixtures into this camera's normalized (0-1) image space → Camera.zones.

    Returns (homography 3x3 as nested lists, mean reprojection error in normalized
    units, zones). Raises ValueError on degenerate input. Pure (no I/O) so the
    geometry is unit-testable.
    """
    import cv2
    import numpy as np

    if len(pairs) < 4:
        raise ValueError("Дор хаяж 4 цэг хослол хэрэгтэй")
    plan = np.array([p["plan"] for p in pairs], dtype=np.float64)
    img = np.array([p["image"] for p in pairs], dtype=np.float64)
    homography, _ = cv2.findHomography(plan, img, 0)  # plan → image(0-1), DLT
    if homography is None:
        raise ValueError("Гомографи бодож чадсангүй — цэгүүд нэг шулуун дээр байж магадгүй")

    def project(points: Any) -> Any:
        return cv2.perspectiveTransform(
            np.asarray(points, dtype=np.float64).reshape(-1, 1, 2), homography
        ).reshape(-1, 2)

    reproj_err = float(np.mean(np.linalg.norm(project(plan) - img, axis=1)))

    zones: list[dict[str, Any]] = []
    for i, fix in enumerate(fixtures):
        pts = fix.get("points") or []
        if len(pts) < 3:
            continue
        tp = project(pts)
        inside = (
            (tp[:, 0] > -_VISIBLE_MARGIN)
            & (tp[:, 0] < 1 + _VISIBLE_MARGIN)
            & (tp[:, 1] > -_VISIBLE_MARGIN)
            & (tp[:, 1] < 1 + _VISIBLE_MARGIN)
        )
        if not bool(inside.any()):
            continue  # this fixture isn't in this camera's view
        clipped = [
            [round(float(min(max(x, 0.0), 1.0)), 4), round(float(min(max(y, 0.0), 1.0)), 4)]
            for x, y in tp
        ]
        zones.append(
            {
                "id": fix.get("id") or f"{fix.get('type', 'zone')}_{i}",
                "type": fix.get("type"),
                "points": clipped,
            }
        )
    return homography.tolist(), round(reproj_err, 5), zones


# The currently-running editor child, if any. Clicking «Plan зураг» again while a
# window is already open must NOT spawn a second WebView2 process (each holds a
# JWT-bearing backend session) — we reuse the live one instead.
_child: subprocess.Popen[bytes] | None = None


def open_floor_plan() -> None:
    """Spawn the floor-plan webview as a detached child process (never raises).

    If an editor child is already running, this is a no-op so repeated clicks
    can't pile up WebView2 processes."""
    global _child
    if _child is not None and _child.poll() is None:
        log.info("floor_plan.already_open")
        return
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, _FLAG]
    else:
        cmd = [sys.executable, "-m", "sentry_agent_pc.gui_main", _FLAG]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        _child = subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)
        log.info("floor_plan.spawned")
    except OSError as e:
        _child = None
        log.error("floor_plan.spawn_failed", error=str(e))


def maybe_run_floor_plan_from_argv(argv: list[str]) -> bool:
    """If argv requests the floor-plan editor, run it (blocking) and return True.
    Called at the top of the GUI entry point so the same exe serves both."""
    if _FLAG not in argv:
        return False
    _run_window()
    return True


def _run_window() -> None:
    """Create + start the webview window (blocks until closed)."""
    import webview

    from sentry_agent_pc.resources import floorplan_index

    index = floorplan_index()
    if not index.exists():
        log.error("floor_plan.assets_missing", path=str(index))
        return
    api = FloorPlanApi()
    log.info("floor_plan.window_open", index=str(index))
    window = webview.create_window(
        "Sentry — Plan зураг",
        url=str(index),
        width=1280,
        height=820,
        min_size=(960, 640),
        js_api=api,
    )
    api.bind(window)
    webview.start()


class FloorPlanApi:
    """The Python ↔ JS bridge exposed as `window.pywebview.api.*` in the editor.

    Runs in the child process, which holds the agent JWT (via the state file), so
    backend calls happen here and the web page never sees credentials."""

    def __init__(self) -> None:
        self._window: Any = None

    def bind(self, window: Any) -> None:
        self._window = window

    def list_cameras(self) -> list[dict[str, str]]:
        """Registered cameras (name + mediamtx_path id) for the placement picker."""
        from sentry_agent_pc.state import load_state

        return [
            {"camera_id": c.mediamtx_path or "", "name": c.name}
            for c in load_state().cameras
            if c.mediamtx_path
        ]

    def load_plan(self) -> dict[str, Any]:
        """The store's saved floor plan (empty dict on any error → JS starts blank)."""
        from sentry_agent_pc.backend_client import BackendClient

        try:
            return BackendClient().agent_get_floor_plan()
        except Exception as e:  # noqa: BLE001 — never crash the editor on a load error
            log.warning("floor_plan.load_failed", error=str(e)[:200])
            return {}

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """PATCH the plan to the backend. Raises on failure → the JS Promise
        rejects and the editor shows the error (so a bad save is never silent).

        This bridge is the only Python gate before a JWT-authenticated PATCH, so
        it validates shape + bounds the payload before sending — a runaway shape
        list can't be forwarded to the backend verbatim."""
        from sentry_agent_pc.backend_client import BackendClient

        if not isinstance(plan, dict):
            raise ValueError("plan нь объект байх ёстой")
        serialized = json.dumps(plan, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > _MAX_PLAN_BYTES:
            raise ValueError("План хэт том байна — элемент тоог багасгана уу")
        return BackendClient().agent_update_floor_plan(plan)

    def get_camera_frame(self, camera_id: str) -> dict[str, Any]:
        """Grab a still from the camera (matched by mediamtx_path) for Phase B
        calibration → {ok, image: data-URL, width, height} or {ok: False, error}.

        Runs in this child process, which is on the camera LAN, so it pulls the
        frame directly (RTSP/snapshot) — the page never sees the camera URL."""
        import base64
        import io

        from sentry_agent_pc.discovery.frame_grab import grab_still
        from sentry_agent_pc.state import load_state

        cam = next((c for c in load_state().cameras if c.mediamtx_path == camera_id), None)
        if cam is None:
            return {"ok": False, "error": "Камер олдсонгүй"}
        res = grab_still(cam)
        if not res.ok or res.image is None:
            return {"ok": False, "error": res.error or "Зураг авч чадсангүй"}
        buf = io.BytesIO()
        res.image.convert("RGB").save(buf, format="JPEG", quality=85)
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "ok": True,
            "image": f"data:image/jpeg;base64,{data}",
            "width": res.width,
            "height": res.height,
        }

    def save_calibration(
        self, camera_id: str, pairs: list[dict[str, Any]], plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase B: fit a plan→image homography from the clicked point pairs,
        derive this camera's zones from the plan fixtures, and persist both —
        the floor-plan (camera homography + calib points) AND Camera.zones — so
        the behaviour engine starts using the zones. Raises on failure → the JS
        Promise rejects and the editor shows the error."""
        from sentry_agent_pc.backend_client import BackendClient
        from sentry_agent_pc.state import load_state

        if not isinstance(plan, dict):
            raise ValueError("plan нь объект байх ёстой")
        cam = next((c for c in load_state().cameras if c.mediamtx_path == camera_id), None)
        if cam is None or not cam.uuid:
            raise ValueError("Камер олдсонгүй (эхлээд камераа бүртгэнэ үү)")

        homography, reproj_err, zones = _compute_calibration(pairs, plan.get("fixtures") or [])

        # Fold the calibration into THIS plan object (passed from the editor, so
        # it matches what's drawn) and save it + the derived zones together.
        cams = plan.setdefault("cameras", [])
        entry = next((c for c in cams if c.get("camera_id") == camera_id), None)
        if entry is None:
            entry = {"camera_id": camera_id, "name": cam.name}
            cams.append(entry)
        entry["homography"] = homography
        entry["reproj_err"] = reproj_err
        entry["calib_points"] = pairs

        if len(json.dumps(plan, separators=(",", ":")).encode("utf-8")) > _MAX_PLAN_BYTES:
            raise ValueError("План хэт том байна")

        client = BackendClient()
        client.agent_update_floor_plan(plan)
        client.agent_update_camera(cam.uuid, zones=zones)
        log.info(
            "floor_plan.calibrated", camera_id=camera_id, reproj_err=reproj_err, zones=len(zones)
        )
        return {"ok": True, "reproj_err": reproj_err, "zone_count": len(zones)}
