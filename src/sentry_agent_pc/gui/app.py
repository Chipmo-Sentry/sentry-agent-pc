"""Main desktop window — camera list + Scan/Add + Settings.

CustomTkinter native window. Long-running ops (ONVIF scan, RTSP probe,
backend calls) run on background threads and post results back to the UI
thread via `self.after(...)` to avoid freezing the window.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.config_file import read_config, write_config
from sentry_agent_pc.gui.add_dialog import AddCameraDialog
from sentry_agent_pc.gui.scan_dialog import ScanDialog
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.state import CameraRecord, load_state

log = get_logger("sentry_agent_pc.gui.app")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CHIPMO_ORANGE = "#FF8A1F"


class AgentApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Chipmo Sentry — Камерын агент")
        self.geometry("960x640")
        self.minsize(820, 520)

        self._build_header()
        self._build_toolbar()
        self._build_camera_list()
        self._build_statusbar()

        self.refresh_cameras()
        self._check_backend_async()

    # === Layout ===

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, height=56, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text="🛡  Chipmo Sentry",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=CHIPMO_ORANGE,
        ).pack(side="left", padx=16)

        self.backend_label = ctk.CTkLabel(
            header,
            text="Backend: шалгаж байна…",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
        )
        self.backend_label.pack(side="left", padx=8)

        ctk.CTkButton(
            header,
            text="⚙ Тохиргоо",
            width=110,
            command=self.open_settings,
        ).pack(side="right", padx=16)

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, height=56, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(12, 4))

        ctk.CTkButton(
            bar,
            text="🔍  Камер хайх (Scan)",
            width=180,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.open_scan,
        ).pack(side="left")

        ctk.CTkButton(
            bar,
            text="➕  Камер нэмэх (Add)",
            width=180,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=CHIPMO_ORANGE,
            hover_color="#E57A12",
            command=self.open_add,
        ).pack(side="left", padx=(10, 0))

        ctk.CTkButton(
            bar,
            text="↻ Сэргээх",
            width=110,
            height=40,
            fg_color="transparent",
            border_width=1,
            command=self.refresh_cameras,
        ).pack(side="right")

    def _build_camera_list(self) -> None:
        # Column headers
        head = ctk.CTkFrame(self, fg_color="gray20", height=34)
        head.pack(fill="x", padx=16, pady=(8, 0))
        head.pack_propagate(False)
        cols = [("Нэр", 260), ("IP", 130), ("Path", 120), ("Codec", 90), ("Чанар", 120), ("", 100)]
        for text, width in cols:
            ctk.CTkLabel(
                head, text=text, width=width, anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"), text_color="gray80",
            ).pack(side="left", padx=4)

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=28, corner_radius=0)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_label = ctk.CTkLabel(
            bar, text="Бэлэн", font=ctk.CTkFont(size=11), text_color="gray70",
        )
        self.status_label.pack(side="left", padx=16)

    # === Camera list rendering ===

    def refresh_cameras(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        state = load_state()
        if not state.cameras:
            ctk.CTkLabel(
                self.list_frame,
                text="Камер бүртгэгдээгүй байна.\n\n"
                "'Камер хайх' дарж автоматаар олох, эсвэл 'Камер нэмэх' дарж гараар нэмнэ үү.",
                font=ctk.CTkFont(size=13),
                text_color="gray60",
                justify="center",
            ).pack(pady=60)
            self.set_status(f"{len(state.cameras)} камер")
            return

        for cam in state.cameras:
            self._render_camera_row(cam)
        self.set_status(f"{len(state.cameras)} камер бүртгэлтэй")

    def _render_camera_row(self, cam: CameraRecord) -> None:
        row = ctk.CTkFrame(self.list_frame, fg_color="gray17", corner_radius=8)
        row.pack(fill="x", pady=3)

        res = f"{cam.resolution[0]}×{cam.resolution[1]}" if cam.resolution else "—"
        cells = [
            (cam.name, 260),
            (cam.ip, 130),
            (cam.mediamtx_path or "—", 120),
            ((cam.codec or "—").upper(), 90),
            (res, 120),
        ]
        for text, width in cells:
            ctk.CTkLabel(
                row, text=text, width=width, anchor="w",
                font=ctk.CTkFont(size=12),
            ).pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            row, text="Устгах", width=80, height=26,
            fg_color="transparent", border_width=1,
            text_color="#FF6B6B", border_color="#FF6B6B",
            hover_color="gray25",
            command=lambda c=cam: self._delete_camera(c),
        ).pack(side="right", padx=8)

    def _delete_camera(self, cam: CameraRecord) -> None:
        dlg = ctk.CTkInputDialog(
            text=f"'{cam.name}' камерыг устгахдаа итгэлтэй байна уу?\n"
            "Баталгаажуулахын тулд 'устга' гэж бичнэ үү:",
            title="Камер устгах",
        )
        if (dlg.get_input() or "").strip().lower() != "устга":
            return

        def work() -> dict[str, Any]:
            client = BackendClient()
            if cam.uuid:
                # Backend DELETE also removes the MediaMTX path (AG8)
                import httpx

                with httpx.Client(timeout=15) as c:
                    c.delete(
                        f"{client.base_url}/api/v1/cameras/{cam.uuid}",
                        headers=client._headers(),  # noqa: SLF001
                    )
            # Remove from local state
            state = load_state()
            state.cameras = [x for x in state.cameras if x.uuid != cam.uuid]
            from sentry_agent_pc.state import save_state

            save_state(state)
            return {"ok": True}

        self._run_bg(work, lambda _r: self.refresh_cameras(), status="Устгаж байна…")

    # === Dialogs ===

    def open_scan(self) -> None:
        if not self._require_backend():
            return
        ScanDialog(self, on_done=self.refresh_cameras)

    def open_add(self) -> None:
        if not self._require_backend():
            return
        AddCameraDialog(self, on_done=self.refresh_cameras)

    def open_settings(self) -> None:
        SettingsDialog(self, on_saved=self._on_settings_saved)

    def _on_settings_saved(self) -> None:
        self.set_status("Тохиргоо хадгалагдсан")
        self._check_backend_async()
        self.refresh_cameras()

    # === Backend status ===

    def _require_backend(self) -> bool:
        cfg = read_config()
        if not cfg.get("DEV_TOKEN"):
            self.set_status("⚠ Эхлээд Тохиргоо хэсэгт backend холболтоо оруулна уу")
            self.open_settings()
            return False
        return True

    def _check_backend_async(self) -> None:
        def work() -> dict[str, Any]:
            try:
                me = BackendClient().me()
                return {"ok": True, "email": me.get("email")}
            except BackendError as e:
                return {"ok": False, "error": str(e)}

        def done(result: dict[str, Any]) -> None:
            if result.get("ok"):
                self.backend_label.configure(
                    text=f"Backend ✅ {result.get('email')}", text_color="#4ADE80",
                )
            else:
                self.backend_label.configure(
                    text="Backend ❌ холбогдсонгүй", text_color="#FF6B6B",
                )

        self._run_bg(work, done)

    # === Threading helper ===

    def _run_bg(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        status: str | None = None,
    ) -> None:
        """Run `work` on a thread; call `on_done(result)` on the UI thread."""
        if status:
            self.set_status(status)

        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("bg_task_failed")
                result = {"ok": False, "error": str(e)}
            self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: AgentApp, on_saved: Callable[[], None]) -> None:
        super().__init__(master)
        self.on_saved = on_saved
        self.title("Тохиргоо")
        self.geometry("520x320")
        self.transient(master)
        self.grab_set()

        cfg = read_config()

        ctk.CTkLabel(
            self, text="Backend холболт",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(20, 4), padx=20, anchor="w")

        ctk.CTkLabel(self, text="Backend URL:", anchor="w").pack(
            fill="x", padx=20, pady=(8, 0),
        )
        self.url_entry = ctk.CTkEntry(self, placeholder_text="http://localhost:8000")
        self.url_entry.pack(fill="x", padx=20)
        self.url_entry.insert(0, cfg.get("BACKEND_URL", "http://localhost:8000"))

        ctk.CTkLabel(self, text="Нэвтрэх токен (JWT):", anchor="w").pack(
            fill="x", padx=20, pady=(12, 0),
        )
        self.token_entry = ctk.CTkEntry(self, show="•")
        self.token_entry.pack(fill="x", padx=20)
        self.token_entry.insert(0, cfg.get("DEV_TOKEN", ""))

        ctk.CTkLabel(
            self,
            text="Токен авах: app.sentry.chipmo.mn-д нэвтэрч 'Агент холбох' хэсгээс.",
            font=ctk.CTkFont(size=11), text_color="gray60", anchor="w", wraplength=460,
        ).pack(fill="x", padx=20, pady=(6, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=20, side="bottom")
        ctk.CTkButton(btn_row, text="Болих", fg_color="transparent", border_width=1,
                      command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_row, text="Хадгалах", fg_color=CHIPMO_ORANGE,
                      hover_color="#E57A12", command=self._save).pack(side="right")

    def _save(self) -> None:
        write_config(self.url_entry.get(), self.token_entry.get())
        self.on_saved()
        self.destroy()


def run() -> None:
    """GUI entry point."""
    app = AgentApp()
    app.mainloop()
