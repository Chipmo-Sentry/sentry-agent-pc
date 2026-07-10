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
import math
import subprocess
import sys
from typing import Any

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.gui.floor_plan_web")

_FLAG = "--floor-plan"

# Bound for the JS↔Python bridge. The plan is a small vector document (a handful
# of polygons); >1 MB means a runaway shape list, not a real store.
_MAX_PLAN_BYTES = 1_000_000

# Phase B calibration: plan points with homogeneous w <= this lie at/behind the
# camera's principal plane — perspectiveTransform divides by w regardless and
# returns wrapped garbage, so the polygon is clipped against w >= _W_EPS in PLAN
# space before projecting.
_W_EPS = 1e-6
# A zone whose clipped normalized-image area is below this (0.2% of the frame)
# is an unusable sliver (typically the residue of a barely-visible fixture) —
# dropped rather than sent to the engine.
_MIN_ZONE_AREA = 0.002


def _clip_halfplane(pts: list[list[float]], a: float, b: float, c: float) -> list[list[float]]:
    """Sutherland–Hodgman single-edge clip: the part of polygon `pts` where
    a·x + b·y + c >= 0. Handles concave polygons; [] when fully outside."""
    out: list[list[float]] = []
    n = len(pts)
    for i in range(n):
        px, py = pts[i]
        qx, qy = pts[(i + 1) % n]
        dp = a * px + b * py + c
        dq = a * qx + b * qy + c
        if dp >= 0:
            out.append([px, py])
        if (dp >= 0) != (dq >= 0):
            t = dp / (dp - dq)
            out.append([px + (qx - px) * t, py + (qy - py) * t])
    return out


def _clip_unit_square(pts: list[list[float]]) -> list[list[float]]:
    """Clip a polygon to the normalized-image frame [0,1]²."""
    for a, b, c in ((1.0, 0.0, 0.0), (-1.0, 0.0, 1.0), (0.0, 1.0, 0.0), (0.0, -1.0, 1.0)):
        pts = _clip_halfplane(pts, a, b, c)
        if len(pts) < 3:
            return []
    return pts


def _poly_area(pts: list[list[float]]) -> float:
    """Polygon area via the shoelace formula (absolute value)."""
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# ── wall occlusion (plan space) ──────────────────────────────────────────────
# Walls block sight: a fixture the camera can't see past a wall must not become
# a zone — its projection would land ON the wall in the image, so a person
# walking in front of the wall would falsely trigger the zone.

# Boundary sampling step (m) and per-edge cap for the visibility test.
_VIS_STEP_M = 0.25
_VIS_MAX_SAMPLES = 200
# The sight line stops this far (m) short of the sample, so a wall the fixture
# leans against (shelves usually line walls) doesn't occlude the fixture itself.
_VIS_SLACK_M = 0.05


def _wall_segments(
    walls: list[dict[str, Any]] | None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for w in walls or []:
        pts = w.get("points") or []
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            segs.append(((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
    return segs


def _segs_cross(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    """True iff segments p-q and a-b properly cross (shared endpoints/collinear
    touches don't count — the slack shrink handles those cases)."""

    def cross(o: tuple[float, float], u: tuple[float, float], v: tuple[float, float]) -> float:
        return (u[0] - o[0]) * (v[1] - o[1]) - (u[1] - o[1]) * (v[0] - o[0])

    d1, d2 = cross(a, b, p), cross(a, b, q)
    d3, d4 = cross(p, q, a), cross(p, q, b)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _visible_part(
    pts: list[list[float]],
    cam_pos: tuple[float, float],
    segs: list[tuple[tuple[float, float], tuple[float, float]]],
    *,
    step: float = _VIS_STEP_M,
) -> list[list[float]]:
    """The part of polygon `pts` the camera can see past the walls.

    Densifies the boundary, sight-tests each sample, and keeps the longest
    circular run of visible samples — an approximation of the visible region
    that handles the common cases exactly: fully visible → the original crisp
    polygon; fully hidden → []; half behind a partition → the visible half.
    """
    dense: list[list[float]] = []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        k = max(1, min(int(length / step), _VIS_MAX_SAMPLES))
        for j in range(k):
            t = j / k
            dense.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])

    def sees(p: list[float]) -> bool:
        dx, dy = p[0] - cam_pos[0], p[1] - cam_pos[1]
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            return True
        s = max(0.0, (dist - _VIS_SLACK_M) / dist)
        q = (cam_pos[0] + dx * s, cam_pos[1] + dy * s)
        return not any(_segs_cross(cam_pos, q, a, b) for a, b in segs)

    vis = [sees(p) for p in dense]
    if all(vis):
        return pts
    if not any(vis):
        return []
    # Longest circular run of visible samples (scan the doubled ring).
    m = len(dense)
    best_len = best_start = run = 0
    for idx in range(2 * m):
        if vis[idx % m]:
            if run == 0:
                start = idx
            run += 1
            if run > best_len:
                best_len, best_start = min(run, m), start
        else:
            run = 0
    return [dense[(best_start + k) % m] for k in range(best_len)]


def _compute_calibration(
    pairs: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    walls: list[dict[str, Any]] | None = None,
    cam_pos: tuple[float, float] | None = None,
) -> tuple[list[list[float]], float, list[dict[str, Any]]]:
    """Fit a plan→image homography from ≥4 point pairs and project the plan
    fixtures into this camera's normalized (0-1) image space → Camera.zones.

    When `walls` + `cam_pos` are given, a fixture only contributes the part the
    camera can actually SEE past the walls (see _visible_part) — a shelf behind
    a partition must not become a zone sitting on that partition's image.

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

    # H is defined only up to scale and findHomography may return it negated —
    # w would then be negative for every genuinely visible point. Normalize the
    # sign against the clicked pairs (they are in front of the camera by
    # construction: the operator clicked them on the image).
    if float(np.mean(plan @ homography[2, :2] + homography[2, 2])) < 0:
        homography = -homography
    h31, h32, h33 = (float(v) for v in homography[2])

    wall_segs = _wall_segments(walls)
    zones: list[dict[str, Any]] = []
    for i, fix in enumerate(fixtures):
        if fix.get("type") == "furniture":
            continue  # scenery (буйдан/сандал) — never a detection zone
        raw = fix.get("points") or []
        if len(raw) < 3:
            continue
        pts = [[float(x), float(y)] for x, y in raw]
        # Walls block sight: keep only the part the camera can see (plan space).
        if wall_segs and cam_pos is not None:
            pts = _visible_part(pts, cam_pos, wall_segs)
            if len(pts) < 3:
                continue  # fully hidden behind a wall
        # Clip away the part at/behind the camera's principal plane FIRST —
        # projecting it yields wrapped coordinates no image-space clip can repair.
        pts = _clip_halfplane(pts, h31, h32, h33 - _W_EPS)
        if len(pts) < 3:
            continue
        tp = project(pts)
        clipped = _clip_unit_square([[float(x), float(y)] for x, y in tp])
        if len(clipped) < 3 or _poly_area(clipped) < _MIN_ZONE_AREA:
            continue  # not (meaningfully) in this camera's view
        zones.append(
            {
                "id": fix.get("id") or f"{fix.get('type', 'zone')}_{i}",
                "type": fix.get("type"),
                "points": [[round(x, 4), round(y, 4)] for x, y in clipped],
            }
        )
    return homography.tolist(), round(reproj_err, 5), zones


def _plan_cam_pos(plan: dict[str, Any], camera_id: str | None) -> tuple[float, float] | None:
    """The plan-space position of the camera being calibrated (operator-placed),
    or None when unknown — occlusion is then skipped, as before."""
    if not camera_id:
        return None
    for c in plan.get("cameras") or []:
        if c.get("camera_id") == camera_id:
            pos = c.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                return (float(pos[0]), float(pos[1]))
    return None


def _rtsp_host_port(url_or_ip: str) -> tuple[str, int]:
    """Parse the host + port to probe for reachability from an rtsp:// URL (creds
    stripped) or a bare IP. Defaults to the standard RTSP port 554."""
    import re

    m = re.match(r"rtsp://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", url_or_ip, re.IGNORECASE)
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else 554
    return url_or_ip.split("/")[0], 554


def _tcp_reachable(url_or_ip: str, *, timeout: float = 1.5) -> bool:
    """True if a TCP connect to the camera's RTSP host:port answers in time — a
    fast (no ffmpeg) online/offline signal for the editor's status badge."""
    import socket

    if not url_or_ip:
        return False
    host, port = _rtsp_host_port(url_or_ip)
    if not host:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


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

    def _confirm_close() -> bool:
        # Unsaved edits must not vanish on a stray window close — the editor is a
        # detached process, so this dialog is the ONLY guard. Returning False
        # cancels the close (pywebview `closing` contract).
        if not api.dirty:
            return True
        return bool(
            window.create_confirmation_dialog(
                "Sentry — Plan зураг",
                "Хадгалаагүй өөрчлөлт байна. Хадгалахгүйгээр хаах уу?",
            )
        )

    window.events.closing += _confirm_close
    webview.start()


class FloorPlanApi:
    """The Python ↔ JS bridge exposed as `window.pywebview.api.*` in the editor.

    Runs in the child process, which holds the agent JWT (via the state file), so
    backend calls happen here and the web page never sees credentials."""

    def __init__(self) -> None:
        self._window: Any = None
        # Mirrors the editor's unsaved-changes flag (set_dirty) so the window's
        # closing handler can guard against losing work.
        self.dirty = False

    def bind(self, window: Any) -> None:
        self._window = window

    def set_dirty(self, dirty: bool) -> None:
        """The editor reports its unsaved-changes state after every mutation/save
        so the close guard (see _run_window) knows whether to prompt."""
        self.dirty = bool(dirty)

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
        result = BackendClient().agent_update_floor_plan(plan)
        self.dirty = False  # saved — the close guard can stand down
        return result

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

    def preview_calibration(
        self, pairs: list[dict[str, Any]], plan: dict[str, Any], camera_id: str | None = None
    ) -> dict[str, Any]:
        """Phase B dry-run: fit the homography + derive zones WITHOUT saving, so
        the editor can overlay the projected zones on the camera snapshot and the
        operator verifies the mapping BY EYE before committing. Pure compute —
        no backend calls, no state writes. {ok, reproj_err, zones} or {ok: False,
        error} (never raises: a degenerate point set is normal mid-calibration)."""
        try:
            if not isinstance(plan, dict):
                raise ValueError("plan нь объект байх ёстой")
            homography, reproj_err, zones = _compute_calibration(
                pairs,
                plan.get("fixtures") or [],
                walls=plan.get("walls"),
                cam_pos=_plan_cam_pos(plan, camera_id),
            )
        except Exception as e:  # noqa: BLE001 — preview must degrade, not crash
            return {"ok": False, "error": str(e)[:200]}
        del homography  # preview only surfaces quality + zones; H is refit on save
        return {"ok": True, "reproj_err": reproj_err, "zones": zones}

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

        homography, reproj_err, zones = _compute_calibration(
            pairs,
            plan.get("fixtures") or [],
            walls=plan.get("walls"),
            cam_pos=_plan_cam_pos(plan, camera_id),
        )

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
        self.dirty = False  # calibration persists the whole plan too
        log.info(
            "floor_plan.calibrated", camera_id=camera_id, reproj_err=reproj_err, zones=len(zones)
        )
        return {"ok": True, "reproj_err": reproj_err, "zone_count": len(zones)}

    def camera_status(self, camera_id: str) -> dict[str, Any]:
        """A fast online/offline check (TCP connect to the camera's RTSP port) for
        the editor's status badge — {ok, online} or {ok: False, error}."""
        from sentry_agent_pc.state import load_state

        cam = next((c for c in load_state().cameras if c.mediamtx_path == camera_id), None)
        if cam is None:
            return {"ok": False, "error": "Камер олдсонгүй"}
        return {"ok": True, "online": _tcp_reachable(cam.rtsp_url or cam.ip)}
