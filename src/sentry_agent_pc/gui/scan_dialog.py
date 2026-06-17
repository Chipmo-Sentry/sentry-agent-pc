"""Scan Camera dialog — ONVIF/RTSP discovery → approval → per-camera register.

Flow:
  1. Open → background scan (ONVIF WS-Discovery + LAN port-554 sweep)
  2. Show found-but-NOT-yet-registered cameras as a table; already-registered
     IPs are hidden (re-adding a live camera is never wanted — delete it first
     to re-add).
  3. User ticks cameras, supplies username/password per row (eye toggle to
     reveal the password)
  4. "Сонгосныг бүртгэх" → per-row: resolve a stream (ONVIF → RTSP paths) →
     register. Status shows inline; on success the row turns green.
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc.gui import widgets
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.gui.scan")

CHIPMO_ORANGE = "#FF8A1F"

# Table columns. Index drives the .grid(column=...) of every cell + header.
_COL_CHECK = 0
_COL_IP = 1
_COL_USER = 2
_COL_PASS = 3
_COL_STATUS = 4


class _DeviceRow:
    """One discovered (unregistered) camera, laid out as a row in the table grid."""

    def __init__(
        self,
        grid: ctk.CTkScrollableFrame,
        row: int,
        candidate: svc.DiscoveredCandidate,
    ) -> None:
        self.candidate = candidate
        self._pass_shown = False

        self.checkbox = ctk.CTkCheckBox(grid, text="", width=24)
        self.checkbox.grid(row=row, column=_COL_CHECK, padx=(10, 4), pady=6)
        self.checkbox.select()

        ctk.CTkLabel(
            grid, text=candidate.ip, font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", justify="left",
        ).grid(row=row, column=_COL_IP, sticky="w", padx=6, pady=6)

        self.user_entry = ctk.CTkEntry(grid, width=110, placeholder_text="admin")
        self.user_entry.insert(0, get_settings().onvif_default_user)
        self.user_entry.grid(row=row, column=_COL_USER, padx=4, pady=6)

        # Password cell: entry + eye toggle, packed in a sub-frame so they sit
        # together in the one grid cell.
        pass_cell = ctk.CTkFrame(grid, fg_color="transparent")
        pass_cell.grid(row=row, column=_COL_PASS, padx=4, pady=6)
        self.pass_entry = ctk.CTkEntry(pass_cell, width=140, show="•")
        self.pass_entry.pack(side="left")
        self.eye_btn = ctk.CTkButton(
            pass_cell, text="👁", width=30, fg_color="transparent", border_width=1,
            command=self._toggle_password,
        )
        self.eye_btn.pack(side="left", padx=(4, 0))

        self.status_lbl = ctk.CTkLabel(
            grid, text="", font=ctk.CTkFont(size=11), text_color="gray60",
            anchor="w", wraplength=220, justify="left",
        )
        self.status_lbl.grid(row=row, column=_COL_STATUS, sticky="w", padx=6, pady=6)

    def _toggle_password(self) -> None:
        self._pass_shown = not self._pass_shown
        self.pass_entry.configure(show="" if self._pass_shown else "•")
        self.eye_btn.configure(text="🙈" if self._pass_shown else "👁")

    def is_selected(self) -> bool:
        return bool(self.checkbox.get())

    def credentials(self) -> tuple[str, str]:
        return self.user_entry.get(), self.pass_entry.get()

    def set_status(self, text: str, color: str = "gray60") -> None:
        self.status_lbl.configure(text=text, text_color=color)


class ScanDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, on_done: Callable[[], None]) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.title("Камер хайх (ONVIF)")
        self.transient(master)
        self.grab_set()
        widgets.setup_dialog(self, 760, 600, min_width=620, min_height=460)

        self.rows: list[_DeviceRow] = []

        # Bottom button bar FIRST so it stays visible when results fill up.
        self.btn_row = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_row.pack(side="bottom", fill="x", padx=20, pady=14)
        self.close_btn = ctk.CTkButton(
            self.btn_row, text="Хаах", fg_color="transparent", border_width=1,
            command=self.destroy,
        )
        self.close_btn.pack(side="right", padx=(8, 0))
        self.register_btn = ctk.CTkButton(
            self.btn_row, text="Сонгосныг бүртгэх", fg_color=CHIPMO_ORANGE,
            hover_color="#E57A12", command=self._register_selected, state="disabled",
        )
        self.register_btn.pack(side="right")
        self.rescan_btn = ctk.CTkButton(
            self.btn_row, text="↻ Дахин хайх", fg_color="transparent", border_width=1,
            command=self._start_scan, state="disabled",
        )
        self.rescan_btn.pack(side="left")

        ctk.CTkLabel(
            self, text="Сүлжээнд холбогдсон камеруудыг хайж байна…",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(18, 4), padx=20, anchor="w")

        self.info_lbl = ctk.CTkLabel(
            self, text="ONVIF WS-Discovery — 5 секунд хүлээнэ үү.",
            font=ctk.CTkFont(size=12), text_color="gray60", anchor="w",
            wraplength=700, justify="left",
        )
        self.info_lbl.pack(fill="x", padx=20, anchor="w")

        self.spinner = widgets.Spinner(self)
        self.spinner.pack(pady=10)
        self.spinner.start()

        self.results = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.results.pack(fill="both", expand=True, padx=16, pady=8)
        # IP + Status columns take the slack; inputs stay their natural width.
        self.results.grid_columnconfigure(_COL_IP, weight=1)
        self.results.grid_columnconfigure(_COL_STATUS, weight=1)

        self._start_scan()

    def _add_table_header(self) -> None:
        hdr = {
            _COL_CHECK: "",
            _COL_IP: "Камер (IP)",
            _COL_USER: "Нэвтрэх",
            _COL_PASS: "Нууц үг",
            _COL_STATUS: "Төлөв",
        }
        for col, text in hdr.items():
            ctk.CTkLabel(
                self.results, text=text, font=ctk.CTkFont(size=11, weight="bold"),
                text_color="gray50", anchor="w",
            ).grid(row=0, column=col, sticky="w", padx=6, pady=(0, 2))

    def _start_scan(self) -> None:
        for w in self.results.winfo_children():
            w.destroy()
        self.rows.clear()
        self._header_added = False
        self.spinner.start()
        self.register_btn.configure(state="disabled")
        self.rescan_btn.configure(state="disabled")
        self.info_lbl.configure(text="Сканердаж байна…", text_color="gray70")

        def _ui(fn: Callable[[], None]) -> None:
            # Callbacks fire on the scan worker thread → marshal onto the UI.
            try:
                if self.winfo_exists():
                    self.after(0, fn)
            except tk.TclError:
                pass

        def on_found(cand: svc.DiscoveredCandidate) -> None:
            _ui(lambda: self._add_row_live(cand))

        def on_phase(text: str) -> None:
            _ui(lambda: self.info_lbl.configure(text=text, text_color="gray70"))

        def work() -> list[svc.DiscoveredCandidate]:
            return svc.scan(timeout_sec=5.0, on_found=on_found, on_phase=on_phase)

        def done(candidates: Any) -> None:
            self.spinner.stop()
            self.rescan_btn.configure(state="normal")
            if isinstance(candidates, dict) and not candidates.get("ok", True):
                self.info_lbl.configure(
                    text=f"Алдаа: {candidates.get('error')}", text_color="#FF6B6B",
                )
                return

            registered = sum(1 for c in candidates if c.already_registered)
            # Cameras streamed into self.rows live as they were found. If none
            # are fresh, explain (already-registered ones are hidden — re-adding a
            # live camera is never the goal; delete it first to re-add).
            if not self.rows:
                base = (
                    "Шинээр нэмэх камер олдсонгүй."
                    if registered
                    else "Камер олдсонгүй (ONVIF болон RTSP/554 аль нь ч хариулсангүй)."
                )
                hint = (
                    f"\n\n{registered} камер аль хэдийн бүртгэлтэй (нуусан). "
                    "Дахин холбохыг хүсвэл эхлээд «Камер» жагсаалтаас устгана уу."
                    if registered
                    else (
                        "\n\n• Камер асаалттай, ижил сүлжээнд холбогдсон эсэхийг шалгана уу\n"
                        "• Зарим камер P2P/cloud-only (RTSP байхгүй) — 'Камер нэмэх'-ээр "
                        "IP + нэр/нууцаар гараар оруулна уу."
                    )
                )
                self.info_lbl.configure(text=base + hint, text_color="#FBBF24")
                return

            note = f"{len(self.rows)} шинэ камер олдлоо."
            if registered:
                note += f" ({registered} бүртгэлтэйг нуусан.)"
            note += (
                " Камер бүрийн нэр/нууц үгийг оруулаад 'Сонгосныг бүртгэх' дарна уу "
                "(👁 товчоор нууц үгээ шалгаж болно)."
            )
            self.info_lbl.configure(text=note, text_color="gray70")

        self._run_bg(work, done)

    def _add_row_live(self, cand: svc.DiscoveredCandidate) -> None:
        """Append one freshly-discovered camera to the table as the scan runs."""
        if not self._header_added:
            self._add_table_header()
            self._header_added = True
        self.rows.append(_DeviceRow(self.results, len(self.rows) + 1, cand))
        self.register_btn.configure(state="normal")
        self.info_lbl.configure(
            text=f"{len(self.rows)} камер олдлоо… (хайсаар байна)", text_color="gray70"
        )

    def _register_selected(self) -> None:
        selected = [r for r in self.rows if r.is_selected()]
        if not selected:
            self.info_lbl.configure(text="Камер сонгогдоогүй байна.", text_color="#FBBF24")
            return
        self.register_btn.configure(state="disabled")
        self.rescan_btn.configure(state="disabled")
        self._register_next(selected, 0)

    def _register_next(self, rows: list[_DeviceRow], idx: int) -> None:
        if idx >= len(rows):
            self.info_lbl.configure(text="Бүртгэл дууслаа.", text_color="#4ADE80")
            self.on_done()
            self.register_btn.configure(state="normal")
            self.rescan_btn.configure(state="normal")
            return

        row = rows[idx]
        user, pwd = row.credentials()
        row.set_status("Холбож байна…", "gray60")

        def work() -> dict[str, Any]:
            ip = row.candidate.ip
            xaddr = row.candidate.xaddr or None
            # Multi-protocol resolve: ONVIF → RTSP path brute-force, any codec.
            stream = svc.resolve_stream(ip, user, pwd, onvif_xaddr=xaddr)
            if not stream.ok or not stream.rtsp_url:
                return {"ok": False, "error": stream.error or "Стрим олдсонгүй"}
            name = f"Камер — {ip}"
            # Hand the just-verified stream to register so it doesn't re-pull
            # the same URL (faster, one fewer RTSP session on the camera).
            result = svc.register_camera(
                name=name, ip=ip, rtsp_url=stream.rtsp_url, resolved=stream,
            )
            return {
                "ok": result.ok,
                "error": result.error,
                "codec": result.codec,
                "resolution": result.resolution,
            }

        def done(r: dict[str, Any]) -> None:
            if r.get("ok"):
                res = r.get("resolution")
                res_txt = f"{res[0]}×{res[1]}" if res else ""
                row.set_status(f"✅ {r.get('codec', '').upper()} {res_txt}", "#4ADE80")
            else:
                row.set_status(f"❌ {r.get('error', 'алдаа')[:60]}", "#FF6B6B")
            self._register_next(rows, idx + 1)

        self._run_bg(work, done)

    def _run_bg(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("scan_bg_failed")
                result = {"ok": False, "error": str(e)}
            # The dialog may have been closed while this ran on a daemon thread;
            # scheduling onto a destroyed widget raises TclError. Guard it.
            try:
                if self.winfo_exists():
                    self.after(0, lambda: on_done(result))
            except tk.TclError:
                pass

        threading.Thread(target=runner, daemon=True).start()
