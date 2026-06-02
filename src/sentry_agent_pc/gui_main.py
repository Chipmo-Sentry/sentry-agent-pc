"""GUI-only entry point for the packaged .exe.

PyInstaller builds this as a windowed (no-console) executable so double-
clicking the .exe opens the desktop window directly. The CLI (`main.py`)
stays available for power users via `sentry-agent-pc <command>`.
"""

from __future__ import annotations

import sys

from sentry_agent_pc.gui.app import run
from sentry_agent_pc.gui.live_view import maybe_run_live_view_from_argv
from sentry_agent_pc.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    # The same .exe doubles as the live-view child process (see gui/live_view.py).
    if maybe_run_live_view_from_argv(sys.argv):
        return
    run()


if __name__ == "__main__":
    main()
