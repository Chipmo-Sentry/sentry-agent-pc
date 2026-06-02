"""Scan Camera dialog — ONVIF discovery → approval → per-camera register.

Flow:
  1. Open → background ONVIF scan (3-5 sec spinner)
  2. Show found devices as checkboxes; already-registered ones pre-disabled
  3. User ticks cameras, supplies username/password per device
  4. "Бүртгэх" → per-device: fetch profiles → pick H.264 → probe → register
  5. Progress shown inline; on success row turns green
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc.gui import widgets
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.gui.scan")

CHIPMO_ORANGE = "#FF8A1F"


class _DeviceRow:
    """Holds the widgets + entry refs for one discovered candidate."""

    def __init__(
        self,
        parent: ctk.CTkFrame,
        candidate: svc.DiscoveredCandidate,
    ) -> None:
        self.candidate = candidate
        self.frame = ctk.CTkFrame(parent, fg_color="gray17", corner_radius=8)
        self.frame.pack(fill="x", pady=3)

        top = ctk.CTkFrame(self.frame, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))

        self.checkbox = ctk.CTkCheckBox(top, text="", width=24)
        self.checkbox.pack(side="left")
        if candidate.already_registered:
            self.checkbox.configure(state="disabled")
        else:
            self.checkbox.select()

        label = f"{candidate.ip}"
        if candidate.already_registered:
            label += "  (бүртгэлтэй)"
        ctk.CTkLabel(
            top, text=label, font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        ).pack(side="left", padx=6)

        self.status_lbl = ctk.CTkLabel(
            top, text="", font=ctk.CTkFont(size=11), text_color="gray60",
        )
        self.status_lbl.pack(side="right", padx=6)

        # Credentials row (hidden for already-registered)
        self.user_entry: ctk.CTkEntry | None = None
        self.pass_entry: ctk.CTkEntry | None = None
        if not candidate.already_registered:
            creds = ctk.CTkFrame(self.frame, fg_color="transparent")
            creds.pack(fill="x", padx=40, pady=(0, 8))
            ctk.CTkLabel(creds, text="Нэвтрэх:", width=60, anchor="w").pack(side="left")
            self.user_entry = ctk.CTkEntry(creds, width=130, placeholder_text="admin")
            self.user_entry.pack(side="left", padx=4)
            self.user_entry.insert(0, get_settings().onvif_default_user)
            ctk.CTkLabel(creds, text="Нууц үг:", width=60, anchor="w").pack(side="left", padx=(10, 0))
            self.pass_entry = ctk.CTkEntry(creds, width=160, show="•")
            self.pass_entry.pack(side="left", padx=4)

    def is_selected(self) -> bool:
        return bool(self.checkbox.get()) and not self.candidate.already_registered

    def credentials(self) -> tuple[str, str]:
        user = self.user_entry.get() if self.user_entry else ""
        pwd = self.pass_entry.get() if self.pass_entry else ""
        return user, pwd

    def set_status(self, text: str, color: str = "gray60") -> None:
        self.status_lbl.configure(text=text, text_color=color)


class ScanDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, on_done: Callable[[], None]) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.title("Камер хайх (ONVIF)")
        self.geometry("640x560")
        self.transient(master)
        self.grab_set()

        self.rows: list[_DeviceRow] = []

        ctk.CTkLabel(
            self, text="Сүлжээнд холбогдсон камеруудыг хайж байна…",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(18, 4), padx=20, anchor="w")

        self.info_lbl = ctk.CTkLabel(
            self, text="ONVIF WS-Discovery — 5 секунд хүлээнэ үү.",
            font=ctk.CTkFont(size=12), text_color="gray60", anchor="w",
        )
        self.info_lbl.pack(padx=20, anchor="w")

        self.spinner = widgets.Spinner(self)
        self.spinner.pack(pady=10)
        self.spinner.start()

        self.results = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.results.pack(fill="both", expand=True, padx=16, pady=8)

        self.btn_row = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_row.pack(fill="x", padx=20, pady=14, side="bottom")
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

        self._start_scan()

    def _start_scan(self) -> None:
        for w in self.results.winfo_children():
            w.destroy()
        self.rows.clear()
        self.spinner.start()
        self.register_btn.configure(state="disabled")
        self.rescan_btn.configure(state="disabled")
        self.info_lbl.configure(text="ONVIF WS-Discovery — 5 секунд хүлээнэ үү.")

        def work() -> list[svc.DiscoveredCandidate]:
            return svc.scan(timeout_sec=5.0)

        def done(candidates: Any) -> None:
            self.spinner.stop()
            self.rescan_btn.configure(state="normal")
            if isinstance(candidates, dict) and not candidates.get("ok", True):
                self.info_lbl.configure(
                    text=f"Алдаа: {candidates.get('error')}", text_color="#FF6B6B",
                )
                return
            if not candidates:
                self.info_lbl.configure(
                    text=(
                        "Нэг ч ONVIF камер олдсонгүй.\n\n"
                        "Боломжит шалтгаан:\n"
                        "• Камер ONVIF-г дэмждэггүй эсвэл идэвхгүй (тохиргооноос асаана уу)\n"
                        "• P2P/cloud-only хэрэглээний камер (ж: Skyworth ZHCSDB6, Tuya/iCSee) — "
                        "RTSP байхгүй тул дэмжигдэхгүй\n"
                        "• Камер өөр дэд сүлжээнд эсвэл firewall хаасан\n\n"
                        "Дэмжигддэг: Hikvision, Dahua, UNV (Uniview), Imou болон бусад "
                        "стандарт ONVIF/RTSP камер. 'Камер нэмэх'-ээр IP+нууцаар гараар оруулж "
                        "болно."
                    ),
                    text_color="#FBBF24",
                )
                return
            self.info_lbl.configure(
                text=f"{len(candidates)} төхөөрөмж олдлоо. Бүртгэх камераа сонгоно уу:",
                text_color="gray70",
            )
            for cand in candidates:
                self.rows.append(_DeviceRow(self.results, cand))
            self.register_btn.configure(state="normal")

        self._run_bg(work, done)

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
            cand = svc.authenticate_and_fetch(row.candidate, user, pwd)
            if cand.error:
                return {"ok": False, "error": cand.error}
            profile = svc.pick_h264_profile(cand)
            if profile is None or not profile.rtsp_uri:
                return {"ok": False, "error": "H.264 profile олдсонгүй"}
            rtsp_url = svc.embed_credentials(profile.rtsp_uri, user, pwd)
            name = f"{cand.manufacturer or 'Камер'} — {cand.ip}"
            result = svc.register_camera(
                name=name, ip=cand.ip, rtsp_url=rtsp_url,
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
                row.set_status(f"❌ {r.get('error', 'алдаа')[:40]}", "#FF6B6B")
            self._register_next(rows, idx + 1)

        self._run_bg(work, done)

    def _run_bg(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("scan_bg_failed")
                result = {"ok": False, "error": str(e)}
            self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()
