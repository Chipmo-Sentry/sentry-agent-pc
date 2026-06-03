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


def get_settings() -> Settings:
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
