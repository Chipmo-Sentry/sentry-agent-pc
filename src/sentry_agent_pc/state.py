"""Encrypted state file — camera list, JWT, pairing data.

M1.5: uses Fernet (key derived from machine-id + agent install path) so the
state file isn't trivially readable. Windows-only DPAPI integration is M2;
Fernet works cross-OS for dev.

Schema:
    {
      "schema_version": 1,
      "agent_jwt": null,
      "paired_org_id": null,
      "default_store_id": null,
      "cameras": [
        {
          "uuid": "019e...",
          "name": "Камер 1 — Hikvision",
          "ip": "192.168.1.64",
          "rtsp_url": "rtsp://admin:pass@.../101",
          "mediamtx_path": "cam1_hik",
          "codec": "h264",
          "resolution": [1920, 1080],
          "last_probe_ok_at": "2026-06-01T12:34:56Z"
        }
      ],
      "ignored_devices": ["192.168.1.99"]
    }
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.state")

SCHEMA_VERSION = 1


class CameraRecord(BaseModel):
    uuid: str | None = None  # set after backend register
    name: str
    ip: str
    rtsp_url: str
    mediamtx_path: str | None = None
    codec: str | None = None
    resolution: tuple[int, int] | None = None
    last_probe_ok_at: str | None = None


class AgentState(BaseModel):
    schema_version: int = SCHEMA_VERSION
    agent_jwt: str | None = None
    paired_org_id: str | None = None
    default_store_id: str | None = None
    cameras: list[CameraRecord] = Field(default_factory=list)
    ignored_devices: list[str] = Field(default_factory=list)


def _machine_key() -> bytes:
    """Derive a stable per-machine Fernet key.

    Combines OS hostname + MAC-ish identifier so the key survives reboots
    but moving the state file to another machine renders it unreadable.
    This is a pragmatic "scramble at rest" — NOT a hardware secure enclave.
    """
    parts = [
        platform.node(),
        platform.machine(),
        platform.system(),
        # uuid.getnode() is the MAC of an interface (or random if unavailable);
        # imported lazily to keep import time fast.
    ]
    import uuid

    parts.append(str(uuid.getnode()))
    raw = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


def load_state() -> AgentState:
    settings = get_settings()
    path = settings.state_path
    if not path.exists():
        return AgentState()
    try:
        encrypted = path.read_bytes()
        f = Fernet(_machine_key())
        decoded = f.decrypt(encrypted)
        data = json.loads(decoded.decode("utf-8"))
        if data.get("schema_version", 1) > SCHEMA_VERSION:
            log.warning("state.schema_too_new", got=data.get("schema_version"))
        return AgentState.model_validate(data)
    except (InvalidToken, json.JSONDecodeError, ValueError) as e:
        log.warning("state.load_failed", error=str(e), path=str(path))
        return AgentState()


def save_state(state: AgentState) -> None:
    settings = get_settings()
    path = settings.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    f = Fernet(_machine_key())
    plain = state.model_dump_json().encode("utf-8")
    encrypted = f.encrypt(plain)
    # Write atomically: tmp + replace
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(encrypted)
    tmp.replace(path)
