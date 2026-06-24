"""Floor-plan editor (docs/30) — a pywebview window hosting the Konva web editor.

Like the live view (gui/live_view.py), pywebview must own the main thread and can
only run once per process, so the main GUI spawns this as a SEPARATE process
(`ChipmoSentryAgent.exe --floor-plan` when frozen, or
`python -m sentry_agent_pc.gui_main --floor-plan` in dev). The child loads the
bundled local web app and exposes `FloorPlanApi` to JS via pywebview's `js_api`
bridge — so the agent JWT (backend calls) stays in Python, never in the page.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.gui.floor_plan_web")

_FLAG = "--floor-plan"

# Bound for the JS↔Python bridge. The plan is a small vector document (a handful
# of polygons); >1 MB means a runaway shape list, not a real store.
_MAX_PLAN_BYTES = 1_000_000

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
        import json

        from sentry_agent_pc.backend_client import BackendClient

        if not isinstance(plan, dict):
            raise ValueError("plan нь объект байх ёстой")
        serialized = json.dumps(plan, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > _MAX_PLAN_BYTES:
            raise ValueError("План хэт том байна — элемент тоог багасгана уу")
        return BackendClient().agent_update_floor_plan(plan)
