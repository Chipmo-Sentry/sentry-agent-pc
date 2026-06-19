"""Edit Camera dialog — change a registered camera's connection or name.

Fills the form from the camera's current ``rtsp_url`` (host, port, user,
password, path). On save:
  * connection fields changed → rebuild the URL, re-probe, PATCH the backend
    (new rtsp_url re-points the live worker) + refresh local codec/resolution
  * only the name changed → PATCH the name, no probe

Closes the gap the "Дахин холбох" button can't: when the camera's IP itself
changed (DHCP), reconnect (which reuses the stored IP) fails — here the user
edits the IP and reconnects.
"""

from __future__ import annotations

import contextlib
import threading
import urllib.parse
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc.discovery import rtsp_probe
from sentry_agent_pc.gui import widgets
from sentry_agent_pc.gui.widgets import BRAND_ORANGE, BRAND_ORANGE_HOVER
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.settings import get_settings
from sentry_agent_pc.state import CameraRecord

log = get_logger("sentry_agent_pc.gui.edit")


def parse_rtsp(url: str) -> dict[str, str]:
    """Split an ``rtsp://user:pass@host:port/path`` URL into editable parts.

    Credentials are URL-decoded; path keeps no leading slash. Missing parts
    come back as empty strings (port defaults to "554")."""
    out = {"user": "", "password": "", "host": "", "port": "554", "path": ""}
    try:
        p = urllib.parse.urlsplit(url)
        if p.username is not None:
            out["user"] = urllib.parse.unquote(p.username)
        if p.password is not None:
            out["password"] = urllib.parse.unquote(p.password)
        if p.hostname:
            out["host"] = p.hostname
        if p.port:
            out["port"] = str(p.port)
        out["path"] = (p.path or "").lstrip("/")
        if p.query:
            out["path"] += f"?{p.query}"
    except ValueError:
        pass
    return out


def build_rtsp(*, user: str, password: str, host: str, port: int, path: str) -> str:
    """Assemble an rtsp URL from explicit fields (credentials URL-encoded)."""
    auth = ""
    if user:
        auth = urllib.parse.quote(user, safe="")
        if password:
            auth += ":" + urllib.parse.quote(password, safe="")
        auth += "@"
    path = path.lstrip("/")
    return f"rtsp://{auth}{host}:{port}/{path}"


class EditCameraDialog(ctk.CTkToplevel):
    def __init__(
        self, master: ctk.CTk, camera: CameraRecord, on_done: Callable[[], None]
    ) -> None:
        super().__init__(master)
        self.cam = camera
        self.on_done = on_done
        self._orig = parse_rtsp(camera.rtsp_url)
        self.title("Камер засах")
        self.transient(master)
        self.grab_set()
        widgets.setup_dialog(self, 560, 600, min_width=480, min_height=440)

        # Bottom button bar first so it never clips.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=20, pady=14)
        ctk.CTkButton(
            btn_row, text="Болих", fg_color="transparent", border_width=1,
            command=self.destroy,
        ).pack(side="right", padx=(8, 0))
        self.save_btn = ctk.CTkButton(
            btn_row, text="Хадгалах", fg_color=BRAND_ORANGE,
            hover_color=BRAND_ORANGE_HOVER, command=self._submit,
        )
        self.save_btn.pack(side="right")

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True)
        self._body = body

        ctk.CTkLabel(
            body, text="Камерын холболтыг засах",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(8, 2), padx=20, anchor="w")
        ctk.CTkLabel(
            body,
            text="IP эсвэл нэвтрэх мэдээлэл өөрчлөгдсөн бол энд засаад хадгалахад "
            "дахин шалгаж, шууд харах урсгалыг шинэ хаяг руу холбоно.",
            font=ctk.CTkFont(size=11), text_color="gray70",
            anchor="w", wraplength=480, justify="left",
        ).pack(fill="x", padx=20, pady=(0, 8))

        self.name_entry = self._field("Нэр:", camera.name)
        self.ip_entry = self._field("IP хаяг:", camera.ip or self._orig["host"])
        self.port_entry = self._field("Port:", self._orig["port"])
        self.user_entry = self._field(
            "Нэвтрэх нэр:", self._orig["user"] or get_settings().onvif_default_user
        )
        self.pass_entry = widgets.password_field(self._body, "Нууц үг:", self._orig["password"])
        self.path_entry = self._field(
            "RTSP path (хоосон бол автоматаар хайна):", self._orig["path"]
        )

        self.spinner = widgets.Spinner(body)
        self.spinner.pack(pady=(8, 0))
        self.status_lbl = ctk.CTkLabel(
            body, text="", font=ctk.CTkFont(size=12), text_color="gray60",
            wraplength=480,
        )
        self.status_lbl.pack(pady=4, padx=20)

    def _field(self, label: str, default: str, show: str | None = None) -> ctk.CTkEntry:
        ctk.CTkLabel(self._body, text=label, anchor="w").pack(fill="x", padx=20, pady=(8, 0))
        entry = ctk.CTkEntry(self._body, show=show)
        entry.pack(fill="x", padx=20)
        if default:
            entry.insert(0, default)
        return entry

    def _connection_changed(self, ip: str, port: str, user: str, pwd: str, path: str) -> bool:
        return (
            ip != (self.cam.ip or self._orig["host"])
            or port != self._orig["port"]
            or user != self._orig["user"]
            or pwd != self._orig["password"]
            or path != self._orig["path"]
        )

    def _submit(self) -> None:
        name = self.name_entry.get().strip()
        ip = self.ip_entry.get().strip()
        if not name:
            self._status("Нэр оруулна уу.", "#FF6B6B")
            return
        if not ip:
            self._status("IP хаяг оруулна уу.", "#FF6B6B")
            return
        try:
            port = int(self.port_entry.get().strip() or "554")
        except ValueError:
            self._status("Port бүхэл тоо байх ёстой.", "#FF6B6B")
            return
        user = self.user_entry.get().strip()
        pwd = self.pass_entry.get()
        path = self.path_entry.get().strip()

        conn_changed = self._connection_changed(ip, str(port), user, pwd, path)
        name_changed = name != self.cam.name

        if not conn_changed and not name_changed:
            self._status("Өөрчлөлт алга.", "gray60")
            return

        self.save_btn.configure(state="disabled")
        self.spinner.start()
        self._status("Хадгалж байна…", "gray60")

        def work() -> dict[str, Any]:
            rtsp_url: str | None = None
            resolved: svc.ResolvedStream | None = None
            if conn_changed:
                # An explicit path → build + probe directly; else fall back to the
                # full resolver (ONVIF + RTSP path brute-force).
                if path:
                    candidate = build_rtsp(
                        user=user, password=pwd, host=ip, port=port, path=path
                    )
                    pr = rtsp_probe.probe(candidate, timeout_sec=5)
                    if pr.ok and (pr.codec or "").lower() in ("h264", "hevc", "h265"):
                        rtsp_url = candidate
                        resolved = svc.ResolvedStream(
                            ok=True, rtsp_url=candidate, codec=pr.codec,
                            width=pr.width, height=pr.height,
                        )
                if rtsp_url is None:
                    rs = svc.resolve_stream(ip, user, pwd)
                    if not rs.ok or not rs.rtsp_url:
                        return {"ok": False, "error": rs.error or "RTSP холбогдсонгүй"}
                    rtsp_url = rs.rtsp_url
                    resolved = rs

            result = svc.update_camera_connection(
                camera_uuid=self.cam.uuid,
                name=name if name_changed else None,
                ip=ip if conn_changed else None,
                rtsp_url=rtsp_url,
                resolved=resolved,
            )
            return {
                "ok": result.ok,
                "error": result.error,
                "codec": result.codec,
                "resolution": result.resolution,
            }

        self._run_bg(work, self._done)

    def _done(self, r: dict[str, Any]) -> None:
        self.spinner.stop()
        self.save_btn.configure(state="normal")
        if r.get("ok"):
            res = r.get("resolution")
            res_txt = f" {res[0]}×{res[1]}" if res else ""
            codec = (r.get("codec") or "").upper()
            self._status(f"✅ Хадгалагдлаа{(' — ' + codec + res_txt) if codec else ''}", "#4ADE80")
            self.on_done()
            self.after(1100, self.destroy)
        else:
            self._status(f"❌ {r.get('error', 'алдаа')}", "#FF6B6B")

    def _status(self, text: str, color: str = "gray60") -> None:
        with contextlib.suppress(Exception):  # status label may be gone mid-close
            self.status_lbl.configure(text=text, text_color=color)

    def _run_bg(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("edit_bg_failed")
                result = {"ok": False, "error": str(e)}
            with contextlib.suppress(Exception):  # window closed mid-task
                self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()
