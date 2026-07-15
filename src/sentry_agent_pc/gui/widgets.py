"""Small reusable CustomTkinter widgets."""

from __future__ import annotations

import contextlib
import tkinter as tk

import customtkinter as ctk

from sentry_agent_pc import resources

# Brand palette — mirrors sentry-ui-kit's dark theme (src/styles.css) so the
# desktop agent reads as the same product as the web apps: a royal-blue accent
# on a тас хар (near-black) Linear/Vercel-style surface. Defined ONCE here and
# imported everywhere else (dialogs, app shell, spinner). The brand pivoted off
# the old orange accent (2026-06-16) — these are the canonical tokens now.
BRAND_PRIMARY = "#2563EB"  # blue-600 — primary actions, selection, accent
BRAND_PRIMARY_HOVER = "#1D4ED8"  # blue-700 — hover/pressed
UI_BG = "#0A0A0A"  # near-black page / window base (--color-background)
UI_SURFACE = "#161616"  # elevated panel / card (--color-surface)
UI_SURFACE_2 = "#1C1C1C"  # a shade above surface — panel headers, selected rows
UI_MUTED = "#1F1F1F"  # input / subtle fill (--color-muted)
UI_MUTED_HOVER = "#272727"  # row / ghost-button hover
UI_BORDER = "#262626"  # subtle border (--color-border)
UI_LINE_SOFT = "#202020"  # faint inner divider (table row separators)
UI_FG = "#FAFAFA"  # near-white primary text (--color-foreground)
UI_MUTED_FG = "#A1A1AA"  # zinc-400 secondary text (--color-muted-foreground)
UI_SUCCESS = "#4ADE80"  # online / ready
UI_WARNING = "#FBBF24"  # caution
UI_DANGER = "#F87171"  # error / offline
UI_INFO = "#60A5FA"  # informational blue text (pills, links)

# Status-pill palette: (foreground text/dot colour, soft fill). Kept solid (no
# alpha) because CTk frames don't composite RGBA — these are hand-mixed so a pill
# reads as a faint tinted chip on the тас хар base, matching the console mockup.
PILL_VARIANTS: dict[str, tuple[str, str]] = {
    "neutral": (UI_MUTED_FG, UI_MUTED),
    "good": (UI_SUCCESS, "#122019"),
    "warn": (UI_WARNING, "#221B0E"),
    "danger": (UI_DANGER, "#221314"),
    "blue": (UI_INFO, "#101B31"),
}


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


def password_field(parent: ctk.CTkBaseClass, label: str, default: str = "") -> ctk.CTkEntry:
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
        row,
        text="👁",
        width=40,
        fg_color="transparent",
        border_width=1,
        text_color="gray70",
        command=_toggle,
    )
    btn.pack(side="left", padx=(6, 0))
    return entry


class StatusPill(ctk.CTkFrame):
    """A compact rounded status chip: an optional coloured dot + a short label,
    tinted by `variant` (neutral/good/warn/danger/blue). The console UI uses
    these everywhere a state is shown (online/offline, AI ready, risk band) so a
    status reads as a glanceable chip instead of coloured raw text.

    Call ``set(text, variant)`` to update it live (e.g. push-status ticks)."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        text: str = "",
        variant: str = "neutral",
        *,
        dot: bool = True,
    ) -> None:
        super().__init__(master, corner_radius=11, fg_color=PILL_VARIANTS["neutral"][1])
        self._show_dot = dot
        self._dot = ctk.CTkLabel(self, text="●", font=ctk.CTkFont(size=9)) if dot else None
        if self._dot is not None:
            self._dot.pack(side="left", padx=(9, 0))
        self._lbl = ctk.CTkLabel(self, text=text, font=ctk.CTkFont(size=11, weight="bold"))
        self._lbl.pack(side="left", padx=(5 if dot else 10, 10), pady=2)
        self.set(text, variant)

    def set(self, text: str | None = None, variant: str | None = None) -> None:
        if variant is not None:
            fg, bg = PILL_VARIANTS.get(variant, PILL_VARIANTS["neutral"])
            self.configure(fg_color=bg)
            self._lbl.configure(text_color=fg)
            if self._dot is not None:
                self._dot.configure(text_color=fg)
        if text is not None:
            self._lbl.configure(text=text)


class Panel(ctk.CTkFrame):
    """A titled console card: a header strip (title + optional right-slot) over a
    thin divider, then a content ``body`` frame. Callers pack their content into
    ``panel.body`` and add right-aligned pills/buttons to ``panel.head``.

    This is the one surface primitive the console pages sit on so every card
    reads the same (bordered elevated surface, 40px header, hairline divider)."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        title: str = "",
        *,
        pad: int = 12,
    ) -> None:
        super().__init__(
            master,
            corner_radius=12,
            fg_color=UI_SURFACE,
            border_width=1,
            border_color=UI_BORDER,
        )
        self.head = ctk.CTkFrame(self, fg_color="transparent", height=40)
        self.head.pack(fill="x", padx=13)
        self.head.pack_propagate(False)
        self.title_label = ctk.CTkLabel(
            self.head,
            text=title,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=UI_FG,
            anchor="w",
        )
        self.title_label.pack(side="left")
        ctk.CTkFrame(self, height=1, fg_color=UI_BORDER, corner_radius=0).pack(fill="x")
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=pad, pady=pad)


def metric_chip(
    parent: ctk.CTkBaseClass,
    value: str,
    label: str,
    *,
    hint: str = "",
    value_color: str = UI_FG,
) -> tuple[ctk.CTkFrame, ctk.CTkLabel, ctk.CTkLabel]:
    """A small KPI tile (label · big value · faint hint) for a metric strip.

    Returns (box, value_label, hint_label) so the caller can update the value and
    hint live. Packs nothing — the caller places `box`."""
    box = ctk.CTkFrame(
        parent, fg_color=UI_SURFACE, corner_radius=11, border_width=1, border_color=UI_BORDER
    )
    ctk.CTkLabel(
        box, text=label, font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG, anchor="w"
    ).pack(anchor="w", padx=12, pady=(9, 0))
    vlbl = ctk.CTkLabel(
        box,
        text=value,
        font=ctk.CTkFont(size=18, weight="bold"),
        text_color=value_color,
        anchor="w",
    )
    vlbl.pack(anchor="w", padx=12, pady=(2, 0))
    hlbl = ctk.CTkLabel(
        box, text=hint, font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG, anchor="w"
    )
    hlbl.pack(anchor="w", padx=12, pady=(1, 9))
    return box, vlbl, hlbl


def dark_menu(parent: ctk.CTkBaseClass) -> tk.Menu:
    """A tkinter popup Menu styled to match the dark console (CTk has no native
    context menu). Used for the camera row `⋯` action menu."""
    return tk.Menu(
        parent,
        tearoff=0,
        bg=UI_SURFACE_2,
        fg=UI_FG,
        activebackground=BRAND_PRIMARY,
        activeforeground="white",
        bd=0,
        relief="flat",
        font=("Segoe UI", 10),
    )


class Spinner(ctk.CTkLabel):
    """Tiny text-based spinner (no image deps — PyInstaller-friendly)."""

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, master: ctk.CTkBaseClass) -> None:
        super().__init__(
            master,
            text="",
            font=ctk.CTkFont(size=22),
            text_color=BRAND_PRIMARY,
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
