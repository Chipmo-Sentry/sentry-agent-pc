"""Main desktop window — camera list + Scan/Add + Settings.

CustomTkinter native window. Long-running ops (ONVIF scan, RTSP probe,
backend calls) run on background threads and post results back to the UI
thread via `self.after(...)` to avoid freezing the window.
"""

from __future__ import annotations

import platform
import threading
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc import __version__, updater
from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.config_file import DEFAULT_BACKEND_URL, read_config, write_config
from sentry_agent_pc.gui.add_dialog import AddCameraDialog
from sentry_agent_pc.gui.scan_dialog import ScanDialog
from sentry_agent_pc.gui.update_dialog import UpdateDialog, check_in_background
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.state import CameraRecord, load_state, save_state
from sentry_agent_pc.streaming.controller import get_stream_controller

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
        # First run / unpaired → guide the user straight to the pairing screen.
        if not load_state().is_paired:
            self.after(400, self.open_pairing)
        # Silent update check shortly after launch; prompt only if newer exists.
        self.after(2500, self._auto_check_update)

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
            text="🔗 Холболт",
            width=110,
            command=self.open_pairing,
        ).pack(side="right", padx=(8, 16))

        ctk.CTkButton(
            header,
            text="⬆ Шинэчлэл",
            width=110,
            fg_color="transparent",
            border_width=1,
            command=self.open_update,
        ).pack(side="right", padx=4)

        ctk.CTkLabel(
            header,
            text=f"v{__version__}",
            font=ctk.CTkFont(size=11),
            text_color="gray50",
        ).pack(side="right", padx=4)

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

        ctk.CTkButton(
            bar,
            text="📺  Шууд харах",
            width=160,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="transparent",
            border_width=1,
            command=self.open_live_view,
        ).pack(side="left", padx=(10, 0))

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
        self._refresh_streaming()

    def _refresh_streaming(self) -> None:
        """Reconcile cloud stream-push relays with the current camera list.

        Runs on a background thread (network + ffmpeg supervision). No-op when
        unpaired or when the backend reports pull/on-LAN topology."""
        if not load_state().is_paired:
            return
        threading.Thread(
            target=get_stream_controller().refresh,
            name="stream-refresh",
            daemon=True,
        ).start()

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
            # Backend delete FIRST — if it fails we keep the local record so the
            # list stays consistent with the server (no orphaned local rows).
            if cam.uuid:
                BackendClient().delete_camera(cam.uuid)  # raises on real failure
            # Backend ok (or no uuid) → drop from local state
            from sentry_agent_pc.state import save_state

            state = load_state()
            state.cameras = [x for x in state.cameras if x.uuid != cam.uuid]
            save_state(state)
            return {"ok": True}

        def done(result: Any) -> None:
            if isinstance(result, dict) and not result.get("ok", True):
                self.set_status(f"⚠ Устгаж чадсангүй: {result.get('error', '')[:60]}")
            else:
                self.set_status("Камер устгагдлаа")
            self.refresh_cameras()

        self._run_bg(work, done, status="Устгаж байна…")

    # === Dialogs ===

    def open_scan(self) -> None:
        if not self._require_paired():
            return
        ScanDialog(self, on_done=self.refresh_cameras)

    def open_add(self) -> None:
        if not self._require_paired():
            return
        AddCameraDialog(self, on_done=self.refresh_cameras)

    def open_pairing(self) -> None:
        PairingDialog(self, on_saved=self._on_pairing_saved)

    def open_update(self) -> None:
        """Manual update check from the header button (dialog runs the check)."""
        UpdateDialog(self, info=None)

    def open_live_view(self) -> None:
        """Open the embedded live view (same WebRTC + AI overlay as the web app)."""
        if not self._require_paired():
            return
        from sentry_agent_pc.gui.live_view import open_live_view

        open_live_view()
        self.set_status("Шууд харах цонх нээгдэж байна…")

    def _auto_check_update(self) -> None:
        """Silent startup check — only opens the dialog if a newer release exists."""
        def on_available(info: updater.UpdateInfo) -> None:
            self.set_status(f"Шинэ хувилбар бэлэн: v{info.version}")
            UpdateDialog(self, info=info)

        check_in_background(self, on_available)

    def _on_pairing_saved(self) -> None:
        self.set_status("Холболт шинэчлэгдсэн")
        self._check_backend_async()
        self.refresh_cameras()

    # === Backend status ===

    def _require_paired(self) -> bool:
        if not load_state().is_paired:
            self.set_status("⚠ Эхлээд дэлгүүртэйгээ холбоно уу ('🔗 Холболт')")
            self.open_pairing()
            return False
        return True

    def _check_backend_async(self) -> None:
        state = load_state()
        if not state.is_paired:
            self.backend_label.configure(
                text="Холбогдоогүй — '🔗 Холболт' дарна уу", text_color="#FBBF24",
            )
            return
        store = state.store_name or "дэлгүүр"

        def work() -> dict[str, Any]:
            try:
                BackendClient().heartbeat()
                return {"ok": True}
            except BackendError as e:
                return {"ok": False, "error": str(e)}

        def done(result: dict[str, Any]) -> None:
            if result.get("ok"):
                self.backend_label.configure(
                    text=f"✅ {store}", text_color="#4ADE80",
                )
            else:
                self.backend_label.configure(
                    text=f"⚠ {store} — холбогдсонгүй", text_color="#FF6B6B",
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


class PairingDialog(ctk.CTkToplevel):
    """Connect this PC to a store using a 6-digit code from the web app.

    The admin opens app.sentry.chipmo.mn → Дэлгүүр → 'Компьютер холбох',
    generates a code, and types it here. On success we store the returned
    agent JWT (+ store name) in the encrypted state file.
    """

    def __init__(self, master: AgentApp, on_saved: Callable[[], None]) -> None:
        super().__init__(master)
        self.on_saved = on_saved
        self.title("Дэлгүүртэй холбох")
        self.geometry("520x420")
        self.transient(master)
        self.grab_set()

        state = load_state()
        cfg = read_config()

        ctk.CTkLabel(
            self, text="Дэлгүүртэй холбох",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(20, 2), padx=20, anchor="w")

        if state.is_paired:
            ctk.CTkLabel(
                self,
                text=f"✅ Одоо холбогдсон дэлгүүр: {state.store_name or '—'}",
                font=ctk.CTkFont(size=13), text_color="#4ADE80", anchor="w",
            ).pack(fill="x", padx=20, pady=(2, 8))

        ctk.CTkLabel(
            self,
            text="Веб апп → Дэлгүүр → 'Компьютер холбох' дарж 6 оронтой код аваад "
            "доор оруулна уу.",
            font=ctk.CTkFont(size=12), text_color="gray70", anchor="w", wraplength=470,
            justify="left",
        ).pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkLabel(self, text="6 оронтой код:", anchor="w").pack(fill="x", padx=20)
        self.code_entry = ctk.CTkEntry(
            self, placeholder_text="123456",
            font=ctk.CTkFont(size=22, weight="bold"), justify="center",
        )
        self.code_entry.pack(fill="x", padx=20, pady=(2, 12))

        ctk.CTkLabel(self, text="Backend URL (default-ыг хэвээр үлдээж болно):",
                     anchor="w", font=ctk.CTkFont(size=11), text_color="gray60").pack(
            fill="x", padx=20,
        )
        self.url_entry = ctk.CTkEntry(self, placeholder_text=DEFAULT_BACKEND_URL)
        self.url_entry.pack(fill="x", padx=20, pady=(2, 4))
        self.url_entry.insert(0, cfg.get("BACKEND_URL") or DEFAULT_BACKEND_URL)

        self.status_lbl = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color="gray60",
            wraplength=470, anchor="w",
        )
        self.status_lbl.pack(fill="x", padx=20, pady=(6, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=18, side="bottom")
        ctk.CTkButton(btn_row, text="Хаах", fg_color="transparent", border_width=1,
                      command=self.destroy).pack(side="right", padx=(8, 0))
        self.connect_btn = ctk.CTkButton(
            btn_row, text="Холбох", fg_color=CHIPMO_ORANGE,
            hover_color="#E57A12", command=self._pair,
        )
        self.connect_btn.pack(side="right")
        if state.is_paired:
            ctk.CTkButton(
                btn_row, text="Салгах", fg_color="transparent", border_width=1,
                text_color="#FF6B6B", border_color="#FF6B6B", command=self._unpair,
            ).pack(side="left")

    def _pair(self) -> None:
        code = self.code_entry.get().strip()
        url = self.url_entry.get().strip() or DEFAULT_BACKEND_URL
        if not code.isdigit() or len(code) != 6:
            self.status_lbl.configure(
                text="Код 6 оронтой тоо байх ёстой.", text_color="#FF6B6B",
            )
            return
        self.connect_btn.configure(state="disabled")
        self.status_lbl.configure(text="Холбож байна…", text_color="gray60")
        write_config(url)

        def runner() -> None:
            try:
                result = BackendClient(base_url=url).pair(code, name=platform.node())
                state = load_state()
                state.agent_jwt = result["agent_token"]
                state.paired_org_id = result.get("organization_id")
                state.default_store_id = result.get("store_id")
                state.store_name = result.get("store_name")
                save_state(state)
                out: dict[str, Any] = {"ok": True, "store": result.get("store_name")}
            except (BackendError, KeyError) as e:
                out = {"ok": False, "error": str(e)}
            self.after(0, lambda: self._pair_done(out))

        threading.Thread(target=runner, daemon=True).start()

    def _pair_done(self, result: dict[str, Any]) -> None:
        self.connect_btn.configure(state="normal")
        if result.get("ok"):
            self.status_lbl.configure(
                text=f"✅ '{result.get('store')}' дэлгүүртэй холбогдлоо!",
                text_color="#4ADE80",
            )
            self.on_saved()
            self.after(1200, self.destroy)
        else:
            self.status_lbl.configure(
                text=f"❌ {result.get('error', 'алдаа')[:120]}", text_color="#FF6B6B",
            )

    def _unpair(self) -> None:
        state = load_state()
        state.agent_jwt = None
        state.paired_org_id = None
        state.store_name = None
        save_state(state)
        self.on_saved()
        self.destroy()


def run() -> None:
    """GUI entry point."""
    app = AgentApp()
    app.mainloop()
