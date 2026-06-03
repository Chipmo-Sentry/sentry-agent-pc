"""Small reusable CustomTkinter widgets."""

from __future__ import annotations

import customtkinter as ctk

CHIPMO_ORANGE = "#FF8A1F"


def setup_dialog(
    win: ctk.CTkToplevel,
    width: int,
    height: int,
    *,
    min_width: int | None = None,
    min_height: int | None = None,
) -> None:
    """Size a dialog responsively: set a default + minimum size and center it
    on the screen. minsize defaults to the requested size so content (and the
    bottom button bar) is never clipped on first open, regardless of DPI.

    Callers should also pack the bottom button bar BEFORE the scrollable/expanding
    body so the buttons reserve their space and stay visible when space is tight.
    """
    win.minsize(min_width or width, min_height or height)
    try:
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 3)  # bias slightly above center
        win.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:  # noqa: BLE001 — geometry is best-effort; fall back to size only
        win.geometry(f"{width}x{height}")


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
