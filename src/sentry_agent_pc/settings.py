"""Agent runtime settings.

Reads from `%APPDATA%\\Chipmo\\sentry-agent\\.env` if present, falls back to
env vars. State file (encrypted) lives next to .env in the same directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_dir() -> Path:
    """Best-effort cross-OS config dir.

    Windows: %APPDATA%\\Chipmo\\sentry-agent
    POSIX (dev on macOS/Linux): ~/.config/chipmo/sentry-agent
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "Chipmo" / "sentry-agent"
    return Path.home() / ".config" / "chipmo" / "sentry-agent"


DEFAULT_CONFIG_DIR = _default_config_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_CONFIG_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    backend_url: str = "http://localhost:8000"
    # Web app base — the embedded live view loads `{frontend_url}/live`.
    # (config_file.frontend_url() is the editable source of truth for the GUI.)
    frontend_url: str = "https://sentry-frontend-production.up.railway.app"
    # Dev path: super-admin JWT (M1.5 mode). M2 replaces with paired agent JWT.
    dev_token: str | None = None
    # Backing store for camera list + JWT + pairing state. Encrypted at rest.
    state_path: Path = DEFAULT_CONFIG_DIR / "state.bin"
    # ONVIF discovery
    onvif_probe_timeout_sec: float = 5.0
    onvif_default_user: str = "admin"
    # ffmpeg path — absolute or just "ffmpeg" if on PATH
    ffmpeg_path: str = "ffmpeg"
    rtsp_probe_timeout_sec: int = 5
    log_level: str = "INFO"
    # Edge Stage-1 AI overlay in the offline live view: run YOLO + behaviour
    # locally and draw mask/trail/wrist/risk when the bundled OpenVINO model is
    # present. Off (or model missing) → plain decode, unchanged.
    edge_ai_enabled: bool = True
    # Record −3s…+3s clips for suspicious episodes into the "Сэжигтэй" gallery.
    # Only records off the local MediaMTX fan-out (loopback) so it NEVER opens a
    # second direct camera connection (Hikvision/connection-limit safe); when the
    # fan-out isn't serving the camera, no clip is recorded.
    edge_clips_enabled: bool = True
    # Forward each recorded suspicious clip to the cloud (POST /agent/edge/clips →
    # sentry-ai VLM → alert), in addition to keeping it in the local gallery. The
    # clip is already persisted locally, so a failed upload is never lost — the
    # uploader retries 429/5xx/transport with backoff (ADR-0029 §12 / B3). Off →
    # clips stay local only (no cloud alert from the edge path).
    edge_upload_enabled: bool = True

    # Self-update (GitHub Releases). When on, the app silently downloads a newer
    # release in the background and restarts itself into it — no click needed —
    # so unattended store PCs stay current. Off → the app still CHECKS and prompts
    # via the update dialog (the pre-auto behaviour). Only the frozen build can
    # self-replace; in dev this is a no-op. Tunable in %APPDATA%\...\.env.
    auto_update: bool = True
    # How often to re-check for a new release while running (hours), on top of the
    # startup check. Floored to 0.25h so a misconfig can't hammer the GitHub API.
    update_check_interval_hours: float = 1.0
    # Local MediaMTX fan-out: pull each camera ONCE and share it with the cloud
    # push relay + the offline grid (so cheap cameras aren't hit by 2 sessions).
    # Master switch — off → both consumers connect to the camera directly (the
    # pre-fan-out behaviour). Resolves like ffmpeg_path: absolute path, bundled
    # binary, or "mediamtx" on PATH.
    local_fanout_enabled: bool = True
    mediamtx_path: str = "mediamtx"
    # Loopback-only ports; non-standard to avoid clashing with a manually run
    # ingest/cloud MediaMTX (8554/9997) during testing on the same box.
    local_mediamtx_rtsp_port: int = 18554
    local_mediamtx_api_port: int = 19997


def get_settings() -> Settings:
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
