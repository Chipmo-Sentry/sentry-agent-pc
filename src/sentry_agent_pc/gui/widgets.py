"""Small reusable CustomTkinter widgets."""

from __future__ import annotations

import contextlib

import customtkinter as ctk

from sentry_agent_pc import resources

# Canonical brand-orange accent, defined ONCE here and imported everywhere else
# (dialogs, app header, spinner). Previously each gui file hardcoded its own
# literal — #FF8A1F in most dialogs, #E68425 in the app header, #E57A12 hover —
# which drifted independently. Unify on the majority value.
BRAND_ORANGE = "#FF8A1F"
BRAND_ORANGE_HOVER = "#E57A12"
CHIPMO_ORANGE = BRAND_ORANGE  # alias kept for the existing call sites


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

    apply_brand_icon(win)


def apply_brand_icon(win: ctk.CTkToplevel) -> None:
    """Put the app icon on a dialog's title bar.

    CTkToplevel re-applies its OWN default icon ~200ms after creation (a known
    CustomTkinter quirk), clobbering the parent's `iconbitmap(default=...)`. So
    every dialog showed the generic Tk/CTk icon instead of the Sentry mark. We
    set the brand .ico AFTER that delay so it sticks.
    """

    def _apply() -> None:
        with contextlib.suppress(Exception):  # icon is cosmetic
            ico = resources.icon_ico()
            if ico.exists():
                win.iconbitmap(str(ico))

    with contextlib.suppress(Exception):
        win.after(300, _apply)


def password_field(
    parent: ctk.CTkBaseClass, label: str, default: str = ""
) -> ctk.CTkEntry:
    """A labelled password entry with a 👁 show/hide toggle.

    So the user can confirm a fiddly RTSP password (special chars like '*')
    was typed correctly instead of guessing behind dots."""
    ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", padx=20, pady=(8, 0))
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", padx=20)
    entry = ctk.CTkEntry(row, show="•")
    entry.pack(side="left", fill="x", expand=True)
    if default:
        entry.insert(0, default)

    def _toggle() -> None:
        hidden = entry.cget("show") != ""
        entry.configure(show="" if hidden else "•")
        btn.configure(text="🙈" if hidden else "👁")

    btn = ctk.CTkButton(
        row, text="👁", width=40, fg_color="transparent", border_width=1,
        text_color="gray70", command=_toggle,
    )
    btn.pack(side="left", padx=(6, 0))
    return entry


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
