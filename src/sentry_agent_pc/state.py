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
import os
import platform

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.state")

SCHEMA_VERSION = 2


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
    store_name: str | None = None
    cameras: list[CameraRecord] = Field(default_factory=list)
    ignored_devices: list[str] = Field(default_factory=list)

    @property
    def is_paired(self) -> bool:
        return bool(self.agent_jwt)


def _machine_key() -> bytes:
    """Derive a STABLE per-machine Fernet key.

    ⚠️ History: this used to mix in ``uuid.getnode()`` (a NIC MAC). On a
    multi-homed PC — several NICs, plus VPN/ZeroTier virtual adapters that come
    and go — ``getnode()`` returns a DIFFERENT interface's MAC between runs (and
    a random value when none is readable). The key then changed across reboots,
    the state file failed to decrypt, and the agent silently lost its pairing +
    camera list on every restart. Never use a network identifier here.

    Now: Windows ``MachineGuid`` (stable across reboots, unique per OS install,
    independent of networking), else a random key persisted once on disk. Both
    survive reboots; the persisted-key fallback also survives moving the install.
    """
    secret = _stable_machine_secret()
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _stable_machine_secret() -> str:
    """A reboot-stable, network-independent secret to key state encryption."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as k:
                guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            if guid:
                return f"{platform.node()}|{guid}"
        except OSError as e:
            log.debug("state.machineguid_unavailable", error=str(e))
    return _persisted_random_secret()


def _persisted_random_secret() -> str:
    """A random secret generated once and stored next to the state file.

    Stable forever after first run. Weaker "bound to this machine" property
    than MachineGuid (copying both files together still decrypts), but never
    loses state — which is the whole point.
    """
    import secrets

    key_path = get_settings().state_path.with_name("machine.key")
    try:
        if key_path.exists():
            val = key_path.read_text(encoding="utf-8").strip()
            if val:
                return val
        key_path.parent.mkdir(parents=True, exist_ok=True)
        val = secrets.token_hex(32)
        key_path.write_text(val, encoding="utf-8")
        return val
    except OSError as e:
        log.warning("state.keyfile_failed", error=str(e))
        # Last resort: hostname (stable but weak) — still better than losing state.
        return platform.node() or "chipmo-sentry-agent"


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
