"""Floor-plan editor (docs/30) — a pywebview window hosting the Konva web editor.

Like the live view (gui/live_view.py), pywebview must own the main thread and can
only run once per process, so the main GUI spawns this as a SEPARATE process
(`ChipmoSentryAgent.exe --floor-plan` when frozen, or
`python -m sentry_agent_pc.gui_main --floor-plan` in dev). The child loads the
bundled local web app and exposes `FloorPlanApi` to JS via pywebview's `js_api`
bridge — so the agent JWT (backend calls) stays in Python, never in the page.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from typing import Any

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.gui.floor_plan_web")

_FLAG = "--floor-plan"


def open_floor_plan() -> None:
    """Spawn the floor-plan webview as a detached child process (never raises)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, _FLAG]
    else:
        cmd = [sys.executable, "-m", "sentry_agent_pc.gui_main", _FLAG]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)
        log.info("floor_plan.spawned")
    except OSError as e:
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
        rejects and the editor shows the error (so a bad save is never silent)."""
        from sentry_agent_pc.backend_client import BackendClient

        return BackendClient().agent_update_floor_plan(plan)

    def pick_image(self) -> str | None:
        """Open a native file dialog, return the chosen image as a data: URL the
        editor draws as a traceable background. None if cancelled / unreadable."""
        import webview

        win = self._window or (webview.windows[0] if webview.windows else None)
        if win is None:
            return None
        result = win.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Зураг (*.png;*.jpg;*.jpeg;*.bmp;*.webp)", "Бүх файл (*.*)"),
        )
        if not result:
            return None
        sel = result[0] if isinstance(result, (list, tuple)) else result
        path = Path(str(sel))
        try:
            raw = path.read_bytes()
        except OSError as e:
            log.warning("floor_plan.image_read_failed", error=str(e))
            return None
        ext = path.suffix.lower().lstrip(".")
        mime = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
