"""A dark, sortable table widget (ttk.Treeview) styled to match the CustomTkinter
dark theme — the agent's lightweight "datagrid": real columns, click-to-sort
headers, a scrollbar, zebra rows. Used by the «Сэжигтэй» clip detail + «Зан үйл».

ttk.Treeview (not CTk frames) so we get column alignment + header sorting for
free; the "clam" ttk theme is the only built-in one that honours custom bg/fg.
"""

from __future__ import annotations

import contextlib
import tkinter as tk
from collections.abc import Sequence
from functools import partial
from tkinter import ttk
from typing import Any, cast

# Palette = the sentry-ui-kit dark tokens (shared with the rest of the app).
from sentry_agent_pc.gui.widgets import (
    BRAND_PRIMARY,
    UI_BORDER,
    UI_FG,
    UI_MUTED,
    UI_MUTED_FG,
    UI_SURFACE,
)

_ROW_BG = UI_SURFACE  # #161616 elevated panel
_ROW_ALT = UI_MUTED  # #1f1f1f zebra stripe
_HEAD_BG = UI_BORDER  # #262626 header strip
_FG = UI_FG  # near-white
_HEAD_FG = UI_MUTED_FG  # zinc-400
_SEL = BRAND_PRIMARY  # royal-blue selection


class DataTable(ttk.Frame):
    """A sortable, scrollable table. Build with column specs, then ``set_rows``.

    Each column spec is ``(key, heading, width, anchor)``; width 0 = stretch.
    Rows are tuples aligned to the columns (str values)."""

    def __init__(
        self,
        master: tk.Misc,
        columns: Sequence[tuple[str, str, int, str]],
        *,
        height: int = 14,
    ) -> None:
        super().__init__(master)
        self._cols = [c[0] for c in columns]
        self._numeric_hint = {c[0] for c in columns}  # sort tries numeric first

        style = ttk.Style(self)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")  # honours custom bg/fg (default themes don't)
        style.configure(
            "Sentry.Treeview",
            background=_ROW_BG,
            fieldbackground=_ROW_BG,
            foreground=_FG,
            rowheight=30,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Sentry.Treeview.Heading",
            background=_HEAD_BG,
            foreground=_HEAD_FG,
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padding=(8, 6),
        )
        style.map(
            "Sentry.Treeview", background=[("selected", _SEL)], foreground=[("selected", "white")]
        )
        style.map("Sentry.Treeview.Heading", background=[("active", "#383838")])

        self.tree = ttk.Treeview(
            self, columns=self._cols, show="headings", style="Sentry.Treeview", height=height
        )
        for key, heading, width, anchor in columns:
            self.tree.heading(key, text=heading, command=partial(self._sort, key, reverse=False))
            self.tree.column(key, width=width, anchor=cast("Any", anchor), stretch=(width == 0))
        self.tree.tag_configure("odd", background=_ROW_ALT)
        self.tree.tag_configure("even", background=_ROW_BG)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

    def set_rows(self, rows: Sequence[Sequence[str]]) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for i, row in enumerate(rows):
            self.tree.insert("", "end", values=tuple(row), tags=("odd" if i % 2 else "even",))

    def _sort(self, col: str, *, reverse: bool) -> None:
        rows = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        rows.sort(key=lambda r: _sort_key(r[0]), reverse=reverse)
        for i, (_, k) in enumerate(rows):
            self.tree.move(k, "", i)
            self.tree.item(k, tags=("odd" if i % 2 else "even",))
        self.tree.heading(col, command=lambda: self._sort(col, reverse=not reverse))


def _sort_key(value: str) -> tuple[int, float | str]:
    """Numeric-aware sort: strip +, %, с/мс/units and sort numbers before text."""
    s = value.strip()
    cleaned = (
        s.replace("+", "")
        .replace("%", "")
        .replace("мс", "")
        .replace("с", "")
        .replace("~", "")
        .strip()
    )
    try:
        return (0, float(cleaned))
    except ValueError:
        return (1, s.lower())
