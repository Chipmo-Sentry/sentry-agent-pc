"""Embedded live view — a pywebview window loading the web /live page.

pywebview must own the main thread and can only run once per process, which
conflicts with the CustomTkinter mainloop. So the main GUI launches the live
view as a SEPARATE process (`ChipmoSentryAgent.exe --live-view <url>` when
frozen, or `python -m sentry_agent_pc.gui_main --live-view <url>` in dev) —
see `open_live_view()`. This module is the child-process entry point.

On Windows pywebview renders with the system WebView2 (EdgeChromium) runtime,
so the desktop view is the exact same page — WebRTC video + AI overlay — that
the browser shows.
"""

from __future__ import annotations

import subprocess
import sys

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.gui.live_view")

_LIVE_VIEW_FLAG = "--live-view"


def live_url() -> str:
    """The /live URL to load, from configured frontend_url."""
    return f"{get_settings().frontend_url.rstrip('/')}/live"


def open_live_view() -> None:
    """Spawn a child process that opens the webview window.

    Runs detached so the main GUI stays responsive and a webview crash can't
    take down the agent. No-op-safe: errors are logged, not raised.
    """
    url = live_url()
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, _LIVE_VIEW_FLAG, url]
    else:
        cmd = [sys.executable, "-m", "sentry_agent_pc.gui_main", _LIVE_VIEW_FLAG, url]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)
        log.info("live_view.spawned", url=url)
    except OSError as e:
        log.error("live_view.spawn_failed", error=str(e))


def maybe_run_live_view_from_argv(argv: list[str]) -> bool:
    """If argv requests the live view, run it (blocking) and return True.

    Called at the top of the GUI entry point so the same executable serves
    both the main window and the live-view child process.
    """
    if _LIVE_VIEW_FLAG not in argv:
        return False
    idx = argv.index(_LIVE_VIEW_FLAG)
    url = argv[idx + 1] if idx + 1 < len(argv) else live_url()
    _run_window(url)
    return True


def _run_window(url: str) -> None:
    """Create and start the webview window (blocks until the window closes)."""
    import webview  # local import — only the child process needs it

    log.info("live_view.window_open", url=url)
    webview.create_window(
        "Chipmo Sentry — Шууд харах",
        url=url,
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    # private_mode=False persists cookies so the user's web login survives
    # across live-view launches.
    webview.start(private_mode=False)
