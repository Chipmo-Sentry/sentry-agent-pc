"""Update dialog — show available version, download with progress, apply.

Opened either from the header "Шинэчлэл" button (manual check) or automatically
on startup when a background check finds a newer release.
"""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc import __version__, updater
from sentry_agent_pc.gui import widgets
from sentry_agent_pc.gui.widgets import BRAND_ORANGE, BRAND_ORANGE_HOVER
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.gui.update")


class UpdateDialog(ctk.CTkToplevel):
    """Present an available update and drive download → apply → restart.

    Pass `info` when the caller already checked (startup auto-check). Pass
    None to make the dialog run the check itself (manual "Шинэчлэл" button).
    """

    def __init__(
        self,
        master: ctk.CTk,
        info: updater.UpdateInfo | None = None,
    ) -> None:
        super().__init__(master)
        self.title("Шинэчлэл")
        self.transient(master)
        self.grab_set()
        widgets.setup_dialog(self, 560, 480, min_width=480, min_height=400)

        self.info = info

        # Bottom-anchored controls FIRST (buttons → status → progress), so they
        # always stay visible; the notes box fills the remaining space above.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=20, pady=(4, 16))
        self.close_btn = ctk.CTkButton(
            btn_row,
            text="Хаах",
            fg_color="transparent",
            border_width=1,
            command=self.destroy,
        )
        self.close_btn.pack(side="right", padx=(8, 0))
        self.action_btn = ctk.CTkButton(
            btn_row,
            text="Шинэчлэх",
            fg_color=BRAND_ORANGE,
            hover_color=BRAND_ORANGE_HOVER,
            command=self._on_action,
        )
        self.action_btn.pack(side="right")

        self.status_lbl = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
            anchor="w",
            wraplength=470,
        )
        self.status_lbl.pack(side="bottom", fill="x", padx=20, pady=(0, 4))

        self.progress = ctk.CTkProgressBar(self)
        self.progress.set(0)
        self.progress.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        self.progress.pack_forget()  # hidden until download starts

        ctk.CTkLabel(
            self,
            text="Программын шинэчлэл",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(20, 2), padx=20, anchor="w")

        self.version_lbl = ctk.CTkLabel(
            self,
            text=f"Одоогийн хувилбар: v{__version__}",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
            anchor="w",
        )
        self.version_lbl.pack(fill="x", padx=20, pady=(0, 8))

        self.notes_box = ctk.CTkTextbox(self, wrap="word")
        self.notes_box.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self.notes_box.configure(state="disabled")

        if self.info is not None:
            self._show_available(self.info)
        else:
            self._start_check()

    # === Check ===

    def _start_check(self) -> None:
        self.action_btn.configure(state="disabled")
        self._set_notes("Шалгаж байна…")
        self.status_lbl.configure(text="GitHub-аас хамгийн сүүлийн хувилбарыг шалгаж байна…")

        def runner() -> None:
            info = updater.check_for_update()
            self.after(0, lambda: self._check_done(info))

        threading.Thread(target=runner, daemon=True).start()

    def _check_done(self, info: updater.UpdateInfo | None) -> None:
        if info is None:
            self.status_lbl.configure(
                text="✅ Та хамгийн сүүлийн хувилбарыг ашиглаж байна.",
                text_color="#4ADE80",
            )
            self._set_notes("Шинэчлэл алга.")
            self.action_btn.configure(text="Хаах", state="normal", command=self.destroy)
            return
        self.info = info
        self._show_available(info)

    def _show_available(self, info: updater.UpdateInfo) -> None:
        self.version_lbl.configure(
            text=f"Одоогийн: v{__version__}   →   Шинэ: v{info.version}",
            text_color="#FBBF24",
        )
        self._set_notes(info.notes or "(Тэмдэглэл байхгүй)")
        self.status_lbl.configure(text="Шинэ хувилбар бэлэн боллоо.", text_color="gray70")
        self.action_btn.configure(text="Шинэчлэх", state="normal", command=self._on_action)

    # === Download + apply ===

    def _on_action(self) -> None:
        if self.info is None:
            return
        if not updater.is_frozen():
            # Dev / non-frozen: can't self-replace — open the releases page.
            self.status_lbl.configure(
                text="Dev горим: GitHub releases хуудсыг нээж байна…",
                text_color="#FBBF24",
            )
            webbrowser.open(self.info.html_url)
            return

        self.action_btn.configure(state="disabled")
        self.close_btn.configure(state="disabled")
        self.progress.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        self.progress.set(0)
        self.status_lbl.configure(text="Татаж байна… 0%", text_color="gray70")

        def on_progress(done: int, total: int) -> None:
            frac = (done / total) if total else 0.0
            self.after(0, lambda: self._update_progress(frac))

        def runner() -> None:
            try:
                path = updater.download_asset(self.info, progress=on_progress)  # type: ignore[arg-type]
                self.after(0, lambda: self._download_done(path))
            except Exception as e:  # noqa: BLE001
                log.exception("update_download_failed")
                err = str(e)
                self.after(0, lambda: self._download_failed(err))

        threading.Thread(target=runner, daemon=True).start()

    def _update_progress(self, frac: float) -> None:
        self.progress.set(frac)
        self.status_lbl.configure(text=f"Татаж байна… {int(frac * 100)}%")

    def _download_done(self, path: Any) -> None:
        self.status_lbl.configure(
            text="Татаж дууслаа. Програм хаагдаж, шинэчлэгдээд дахин нээгдэнэ…",
            text_color="#4ADE80",
        )

        def _stop_tray() -> None:
            # Release the tray icon (and any handles) before the process exits.
            tray = getattr(self.master, "_tray", None)
            if tray is not None:
                tray.stop()

        # Give the label a beat to render, then swap + relaunch.
        self.after(
            800,
            lambda: updater.apply_update_and_restart(path, on_before_exit=_stop_tray),
        )

    def _download_failed(self, err: str) -> None:
        # Plain-language message + a one-click manual download. The raw error
        # (often a transient GitHub 504) is kept small for diagnostics only.
        self.progress.pack_forget()
        self.close_btn.configure(state="normal")
        self.status_lbl.configure(
            text=(
                "❌ Автомат шинэчлэл татаж чадсангүй (сервер түр завгүй байж "
                "магадгүй).\nДоорх товчоор хамгийн сүүлийн хувилбарыг гараар "
                "татаад суулгана уу."
            ),
            text_color="#FBBF24",
        )
        self._set_notes(
            "Гараар суулгах:\n"
            "1. '📥 Setup.exe татах' дарж файлыг татна\n"
            "2. Татсан ChipmoSentryAgent-Setup.exe-г ажиллуулж суулгана\n"
            "3. Хуучин тохиргоо (холболт, камер) хэвээр хадгалагдана\n\n"
            f"Шууд холбоос:\n{updater.SETUP_DOWNLOAD_URL}\n\n"
            f"(Алдааны дэлгэрэнгүй: {err[:140]})"
        )
        # Repurpose the primary button into a manual-download action.
        self.action_btn.configure(
            text="📥 Setup.exe татах",
            state="normal",
            command=lambda: webbrowser.open(updater.SETUP_DOWNLOAD_URL),
        )

    # === helpers ===

    def _set_notes(self, text: str) -> None:
        self.notes_box.configure(state="normal")
        self.notes_box.delete("1.0", "end")
        self.notes_box.insert("1.0", text)
        self.notes_box.configure(state="disabled")


def check_in_background(
    master: ctk.CTk, on_available: Callable[[updater.UpdateInfo], None]
) -> None:
    """Silently check for an update; call `on_available(info)` on the UI thread
    only if a newer release exists. Used for the startup auto-check."""

    def runner() -> None:
        info = updater.check_for_update()
        if info is not None:
            master.after(0, lambda: on_available(info))

    threading.Thread(target=runner, daemon=True).start()


def auto_update_in_background(
    app: Any,
    info: updater.UpdateInfo,
    *,
    on_done: Callable[[bool], None] | None = None,
) -> None:
    """Silently download `info` and restart into it with only a tray toast — no
    dialog, no click. The download (verified by SHA-256 inside `download_asset`)
    runs on a daemon thread; on success a short toast shows and the app swaps +
    relaunches itself. On failure the next periodic check simply retries.

    Only meaningful in the frozen build (the caller gates on `is_frozen()`).
    `on_done(success)` runs on the UI thread so the caller can clear its
    'update in progress' flag — the success path restarts the process, so it
    typically only fires on failure.
    """
    log.info("auto_update.starting", version=info.version)

    def _notify(message: str) -> None:
        tray = getattr(app, "_tray", None)
        if tray is not None:
            tray.notify("Sentry шинэчлэл", message)

    def _apply(path: Any) -> None:
        _notify(f"v{info.version} суулгаж байна. Програм дахин нээгдэнэ…")

        def _stop_tray() -> None:
            tray = getattr(app, "_tray", None)
            if tray is not None:
                tray.stop()

        # Brief beat so the toast renders before the in-place swap + relaunch.
        app.after(
            2500,
            lambda: updater.apply_update_and_restart(path, on_before_exit=_stop_tray),
        )

    def runner() -> None:
        try:
            path = updater.download_asset(info)
        except Exception as e:  # noqa: BLE001 — transient (e.g. GitHub 504); retry next check
            log.exception("auto_update.download_failed")
            err = str(e)

            def _fail() -> None:
                log.info("auto_update.will_retry", error=err[:120])
                _notify("Шинэчлэл татаж чадсангүй — дараа дахин оролдоно.")
                if on_done:
                    on_done(False)

            app.after(0, _fail)
            return

        def _ok() -> None:
            if on_done:
                on_done(True)
            _apply(path)

        app.after(0, _ok)

    threading.Thread(target=runner, daemon=True).start()
