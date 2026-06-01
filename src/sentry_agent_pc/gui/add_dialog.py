"""Add Camera dialog — manual brand-template entry.

Flow:
  1. Pick brand from dropdown (shows notes_mn hint)
  2. Enter IP, port, username, password, optional custom path
  3. "Шалгах ба нэмэх" → build candidate URLs → probe first H.264 → register
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc.discovery import manual as manual_mod
from sentry_agent_pc.gui import widgets
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.gui.add")

CHIPMO_ORANGE = "#FF8A1F"


class AddCameraDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, on_done: Callable[[], None]) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.title("Камер нэмэх (гараар)")
        self.geometry("560x600")
        self.transient(master)
        self.grab_set()

        self.brands = manual_mod.list_brands()
        self.brand_by_label = {b.label: b for b in self.brands}

        ctk.CTkLabel(
            self, text="Камер гараар нэмэх",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(18, 8), padx=20, anchor="w")

        # Brand picker
        ctk.CTkLabel(self, text="Камерын брэнд:", anchor="w").pack(fill="x", padx=20)
        self.brand_var = ctk.StringVar(value=self.brands[0].label)
        self.brand_menu = ctk.CTkOptionMenu(
            self, values=list(self.brand_by_label.keys()),
            variable=self.brand_var, command=self._on_brand_change,
        )
        self.brand_menu.pack(fill="x", padx=20, pady=(2, 4))

        self.notes_lbl = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11), text_color="#FBBF24",
            anchor="w", wraplength=500, justify="left",
        )
        self.notes_lbl.pack(fill="x", padx=20)

        # Form fields
        self.ip_entry = self._field("IP хаяг:", "192.168.1.64")
        self.port_entry = self._field("Port:", "554")
        self.user_entry = self._field("Нэвтрэх нэр:", get_settings().onvif_default_user)
        self.pass_entry = self._field("Нууц үг:", "", show="•")
        self.path_entry = self._field(
            "RTSP path (заавал биш — хоосон бол брэндийн default):", "",
        )

        # Status + spinner
        self.spinner = widgets.Spinner(self)
        self.spinner.pack(pady=(8, 0))
        self.status_lbl = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color="gray60",
            wraplength=500,
        )
        self.status_lbl.pack(pady=4, padx=20)

        # Buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=16, side="bottom")
        ctk.CTkButton(btn_row, text="Болих", fg_color="transparent", border_width=1,
                      command=self.destroy).pack(side="right", padx=(8, 0))
        self.add_btn = ctk.CTkButton(
            btn_row, text="Шалгах ба нэмэх", fg_color=CHIPMO_ORANGE,
            hover_color="#E57A12", command=self._submit,
        )
        self.add_btn.pack(side="right")

        self._on_brand_change(self.brand_var.get())

    def _field(self, label: str, default: str, show: str | None = None) -> ctk.CTkEntry:
        ctk.CTkLabel(self, text=label, anchor="w").pack(fill="x", padx=20, pady=(8, 0))
        entry = ctk.CTkEntry(self, show=show)
        entry.pack(fill="x", padx=20)
        if default:
            entry.insert(0, default)
        return entry

    def _on_brand_change(self, label: str) -> None:
        brand = self.brand_by_label.get(label)
        if brand and brand.notes_mn:
            self.notes_lbl.configure(text=f"💡 {brand.notes_mn}")
        else:
            self.notes_lbl.configure(text="")

    def _submit(self) -> None:
        brand = self.brand_by_label[self.brand_var.get()]
        host = self.ip_entry.get().strip()
        if not host:
            self._status("IP хаяг оруулна уу.", "#FF6B6B")
            return
        try:
            port = int(self.port_entry.get().strip() or str(brand.default_port))
        except ValueError:
            self._status("Port бүхэл тоо байх ёстой.", "#FF6B6B")
            return
        user = self.user_entry.get().strip()
        pwd = self.pass_entry.get()
        custom_path = self.path_entry.get().strip() or None

        self.add_btn.configure(state="disabled")
        self.spinner.start()
        self._status("RTSP шалгаж байна…", "gray60")

        def work() -> dict[str, Any]:
            _main, candidates = svc.build_manual_url(
                brand.key, host=host, username=user, password=pwd,
                port=port, custom_path=custom_path,
            )
            probe = svc.probe_first_h264(candidates)
            if not probe.ok:
                return {"ok": False, "error": f"RTSP холбогдсонгүй: {probe.error}"}
            if not probe.is_h264:
                return {
                    "ok": False,
                    "error": f"Камер {probe.codec} буцаалаа — H.264 шаардлагатай. "
                    "Web UI-аас codec солих.",
                }
            name = f"{brand.label} — {host}"
            result = svc.register_camera(name=name, ip=host, rtsp_url=probe.url)
            return {
                "ok": result.ok,
                "error": result.error,
                "codec": result.codec,
                "resolution": result.resolution,
                "path": result.mediamtx_path,
            }

        def done(r: dict[str, Any]) -> None:
            self.spinner.stop()
            self.add_btn.configure(state="normal")
            if r.get("ok"):
                res = r.get("resolution")
                res_txt = f"{res[0]}×{res[1]}" if res else ""
                self._status(
                    f"✅ Бүртгэгдлээ: {r.get('codec', '').upper()} {res_txt} "
                    f"(path={r.get('path')})",
                    "#4ADE80",
                )
                self.on_done()
                self.after(1200, self.destroy)
            else:
                self._status(f"❌ {r.get('error', 'алдаа')}", "#FF6B6B")

        self._run_bg(work, done)

    def _status(self, text: str, color: str = "gray60") -> None:
        self.status_lbl.configure(text=text, text_color=color)

    def _run_bg(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("add_bg_failed")
                result = {"ok": False, "error": str(e)}
            self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()
