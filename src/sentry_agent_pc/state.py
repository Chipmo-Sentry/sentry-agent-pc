"""Encrypted state file — camera list, JWT, pairing data.

M1.5: uses Fernet with a key derived from a stable per-machine secret
(Windows ``MachineGuid``, else a persisted random key). This MACHINE-BINDS /
OBFUSCATES the state file so it isn't trivially readable and doesn't decrypt on
a different install. It is NOT protection against a local attacker: anyone who
can read this user's registry + config dir (i.e. already runs code as this user)
can re-derive the key. Real OS-backed protection (Windows DPAPI, which seals to
the user/machine credential) is deferred to M2.
# DPAPI deferred to M2 (see audit)

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
import functools
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.state")

SCHEMA_VERSION = 2

# Serialises the encrypt+write+replace in save_state so the GUI heartbeat thread
# and a CLI/camera-add can't race on the state file (corruption / lost updates).
_save_lock = threading.Lock()


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


@functools.lru_cache(maxsize=1)
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


def _restrict_permissions(path: Path) -> None:
    """Best-effort: lock a file down to the current user only.

    On Windows this runs ``icacls`` to drop inheritance and grant only the
    current user. Failure is non-fatal — it's a hardening nicety, not a gate
    (and it never replaces real OS-backed protection; # DPAPI deferred to M2).
    """
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        log.debug("state.icacls_failed", error=str(e), path=str(path))


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
        # Atomic create: O_CREAT|O_EXCL fails if a concurrent run already made the
        # file, killing the TOCTOU double-write race. On FileExistsError we lose
        # the race → just re-read the winner's value.
        try:
            fd = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = key_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
            raise
        try:
            os.write(fd, val.encode("utf-8"))
        finally:
            os.close(fd)
        _restrict_permissions(key_path)
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


def _write_state_locked(state: AgentState, path: Path) -> None:
    """Encrypt + atomically write `state` to `path`. Caller MUST hold _save_lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = Fernet(_machine_key()).encrypt(state.model_dump_json().encode("utf-8"))
    # Write into a UNIQUE temp file in the SAME directory, then os.replace
    # (atomic on the same volume). A unique name — not a shared ".tmp" — means
    # two concurrent writers never share a scratch file.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encrypted)
        Path(tmp_name).replace(path)
    except BaseException:
        # Don't leak the scratch file if write/replace failed.
        Path(tmp_name).unlink(missing_ok=True)
        raise


def save_state(state: AgentState) -> None:
    path = get_settings().state_path
    # Hold the lock around encrypt+write+replace so concurrent writers (GUI
    # heartbeat thread vs. a CLI/camera-add) can't clobber each other.
    with _save_lock:
        _write_state_locked(state, path)
    _restrict_permissions(path)


def mutate_state(fn: Callable[[AgentState], None]) -> AgentState:
    """load → fn(state) → save, all under the save lock.

    Convenience for read-modify-write callers that want the mutation to be
    serialised against other savers. Returns the saved state. ``fn`` mutates the
    state in place. (Persist is inlined rather than calling save_state because
    the Lock is non-reentrant.)
    """
    path = get_settings().state_path
    with _save_lock:
        state = load_state()
        fn(state)
        _write_state_locked(state, path)
    _restrict_permissions(path)
    return state
