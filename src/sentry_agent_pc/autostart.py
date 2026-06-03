"""Run-at-boot management via the Windows HKCU Run key.

Writes `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\ChipmoSentry`
= "<exe>" --minimized, so the agent launches into the system tray on login.
Per-user (HKCU) needs no admin rights. No-ops cleanly on non-Windows (dev).
"""

from __future__ import annotations

import sys

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.autostart")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "ChipmoSentry"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _launch_command() -> str:
    """Command written to the Run key. Frozen → the .exe; dev → python -m."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{sys.executable}" -m sentry_agent_pc.gui_main --minimized'


def is_enabled() -> bool:
    if not _is_windows():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.debug("autostart.query_failed", error=str(e))
        return False


def enable() -> bool:
    """Register the agent to start on login. Returns True on success."""
    if not _is_windows():
        log.info("autostart.skip_non_windows")
        return False
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
        log.info("autostart.enabled", cmd=_launch_command())
        return True
    except OSError as e:
        log.warning("autostart.enable_failed", error=str(e))
        return False


def disable() -> bool:
    """Remove the auto-start entry. Returns True if removed or already absent."""
    if not _is_windows():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        log.info("autostart.disabled")
        return True
    except FileNotFoundError:
        return True  # already gone
    except OSError as e:
        log.warning("autostart.disable_failed", error=str(e))
        return False


def set_enabled(enabled: bool) -> bool:
    return enable() if enabled else disable()
