"""GUI-only entry point for the packaged .exe.

PyInstaller builds this as a windowed (no-console) executable so double-
clicking the .exe opens the desktop window directly. The CLI (`main.py`)
stays available for power users via `sentry-agent-pc <command>`.
"""

from __future__ import annotations

import sys

from sentry_agent_pc.gui.app import run, set_app_user_model_id
from sentry_agent_pc.gui.floor_plan_web import maybe_run_floor_plan_from_argv
from sentry_agent_pc.gui.live_view import maybe_run_live_view_from_argv
from sentry_agent_pc.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    # Bind the taskbar identity before ANY Tk window is created — including the
    # webview child paths below, which never reach run().
    set_app_user_model_id()
    # The same .exe doubles as the live-view + floor-plan webview child processes.
    if maybe_run_live_view_from_argv(sys.argv):
        return
    if maybe_run_floor_plan_from_argv(sys.argv):
        return
    # --minimized: launched by auto-start at login → start hidden in the tray.
    run(minimized="--minimized" in sys.argv)


if __name__ == "__main__":
    main()
