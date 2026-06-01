"""Small reusable CustomTkinter widgets."""

from __future__ import annotations

import customtkinter as ctk

CHIPMO_ORANGE = "#FF8A1F"


class Spinner(ctk.CTkLabel):
    """Tiny text-based spinner (no image deps — PyInstaller-friendly)."""

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, master: ctk.CTkBaseClass) -> None:
        super().__init__(
            master, text="", font=ctk.CTkFont(size=22), text_color=CHIPMO_ORANGE,
        )
        self._i = 0
        self._running = False
        self._after_id: str | None = None

    def start(self) -> None:
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        self.configure(text="")

    def _tick(self) -> None:
        if not self._running:
            return
        self.configure(text=self._FRAMES[self._i % len(self._FRAMES)])
        self._i += 1
        self._after_id = self.after(80, self._tick)
