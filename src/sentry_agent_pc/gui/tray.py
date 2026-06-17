"""System tray icon — keeps the agent in the Windows tray, minimize-to-tray.

The tray icon runs on its own thread (pystray `run_detached`) alongside the
CustomTkinter main loop. Menu actions marshal back to the Tk thread via
`app.after(0, ...)`. Closing the window hides it to the tray instead of
quitting; the user reopens or exits from the tray menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sentry_agent_pc import autostart, resources
from sentry_agent_pc.logging_setup import get_logger

if TYPE_CHECKING:
    from sentry_agent_pc.gui.app import AgentApp

log = get_logger("sentry_agent_pc.gui.tray")


class TrayController:
    """Owns the pystray Icon and bridges its menu to the Tk app."""

    def __init__(self, app: AgentApp) -> None:
        self.app = app
        self._icon: Any = None  # pystray.Icon — no type stubs

    def start(self) -> None:
        try:
            import pystray
            from PIL import Image
        except ImportError as e:
            log.warning("tray.unavailable", error=str(e))
            return

        try:
            image = Image.open(resources.icon_png())
        except Exception as e:  # noqa: BLE001 — fall back to no tray if icon missing
            log.warning("tray.icon_load_failed", error=str(e))
            return

        menu = pystray.Menu(
            pystray.MenuItem("Нээх", self._on_open, default=True),
            pystray.MenuItem(
                "Компьютер асахад эхлүүлэх",
                self._on_toggle_autostart,
                checked=lambda _item: autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Гарах", self._on_quit),
        )
        self._icon = pystray.Icon("chipmo_sentry", image, "Sentry", menu)
        try:
            self._icon.run_detached()  # icon loop on its own thread
            log.info("tray.started")
        except Exception as e:  # noqa: BLE001
            log.warning("tray.start_failed", error=str(e))
            self._icon = None

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("tray.stop_failed", error=str(e))
            self._icon = None

    @property
    def active(self) -> bool:
        return self._icon is not None

    # === menu handlers (run on the pystray thread → marshal to Tk) ===

    def _on_open(self, _icon: object = None, _item: object = None) -> None:
        self.app.after(0, self.app.show_window)

    def _on_quit(self, _icon: object = None, _item: object = None) -> None:
        self.app.after(0, self.app.quit_app)

    def _on_toggle_autostart(self, _icon: object = None, _item: object = None) -> None:
        autostart.set_enabled(not autostart.is_enabled())
