"""Encrypted state file — camera list, JWT, pairing data.

At rest the file is sealed with Windows **DPAPI** (``CryptProtectData``, bound to
the current user; the key is held by the OS and isn't derivable from anything on
disk) — see ``dpapi.py``. This is real protection of the agent JWT + camera
passwords against another local user. On non-Windows (dev/test), or for a legacy
file written before DPAPI, it falls back to a Fernet key derived from a stable
per-machine secret (Windows ``MachineGuid``, else a persisted random key); such a
legacy file is read for migration and re-sealed as DPAPI on the next save.

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
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from sentry_agent_pc import dpapi
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.state")

SCHEMA_VERSION = 2

# Marks a state file sealed with Windows DPAPI (CryptProtectData, user-bound) —
# the real at-rest protection. A file WITHOUT this prefix is the legacy
# machine-key Fernet format; it's read for migration, then rewritten as DPAPI on
# the next save. The marker is binary so it can't collide with a Fernet token
# (which is urlsafe-base64).
_DPAPI_MAGIC = b"DPAPIv1\n"

# Serialises the encrypt+write+replace in save_state so the GUI heartbeat thread
# and a CLI/camera-add can't race on the state file (corruption / lost updates).
_save_lock = threading.Lock()

# Set when an EXISTING state file failed to decrypt on the last load. DPAPI can
# fail TRANSIENTLY (a locked/roaming/not-yet-loaded profile), not only because the
# file belongs to another user — so a failed load must NOT let an empty state be
# persisted OVER the still-present file, or a transient failure would permanently
# wipe a valid pairing + camera passwords. _write_state_locked refuses to clobber
# the file with an UNPAIRED state while this is set; a genuine (re)pair (paired
# state) is allowed through and clears it.
_existing_file_unreadable = False


class CameraRecord(BaseModel):
    uuid: str | None = None  # set after backend register
    name: str
    ip: str
    rtsp_url: str
    mediamtx_path: str | None = None
    codec: str | None = None
    resolution: tuple[int, int] | None = None
    last_probe_ok_at: str | None = None
    # Backend compute_tier (ADR-0029): "cloud" (central Stage-1) | "edge_pc"
    # (this PC runs Stage-1 + uploads suspicious clips) | "edge_device". Synced
    # from the backend by reconcile_with_backend; gates the edge clip upload.
    compute_tier: str = "cloud"
    # docs/29 — per-camera detection zones, drawn in the zone editor: a list of
    # {"id","type","points":[[x,y],...]} with NORMALIZED 0-1 coords. None = none
    # drawn yet. The editor loads these to edit; Save PATCHes them to the backend
    # and persists here. Synced down by reconcile_with_backend (cross-PC).
    zones: list[dict[str, Any]] | None = None

    def matches(self, other: CameraRecord) -> bool:
        """Identity match resilient to a not-yet-registered camera (uuid is None).

        Prefer the backend uuid; fall back to the (per-camera unique) RTSP URL.
        Without this, matching by ``uuid == other.uuid`` when both are None hits
        EVERY unregistered camera — a delete would wipe them all, an edit/
        reconnect would act on the wrong one.
        """
        if self.uuid is not None and other.uuid is not None:
            return self.uuid == other.uuid
        return self.rtsp_url == other.rtsp_url


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

    def clear_pairing(self) -> None:
        """Null EVERY pairing field at once. One place so unpair can't drift from
        pair and leave a stale store id from a previous store (cross-tenant risk)."""
        self.agent_jwt = None
        self.paired_org_id = None
        self.default_store_id = None
        self.store_name = None


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

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            if guid:
                return f"{platform.node()}|{guid}"
        except OSError as e:
            log.debug("state.machineguid_unavailable", error=str(e))
    return _persisted_random_secret()


def _restrict_permissions(path: Path) -> None:
    """Best-effort: lock a file down to the current user only.

    On Windows this runs ``icacls`` to drop inheritance and grant only the
    current user. Failure is non-fatal — it's defense-in-depth on top of the
    DPAPI sealing, not the primary protection.
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


def _decrypt_state_bytes(raw: bytes) -> bytes:
    """Decrypt on-disk state bytes → plaintext JSON.

    DPAPI-sealed files (the ``_DPAPI_MAGIC`` prefix) unseal via the OS; a file
    without the prefix is the legacy machine-key Fernet format (still read so an
    upgrade doesn't lose the existing pairing — it's re-sealed on the next save).
    """
    if raw.startswith(_DPAPI_MAGIC):
        return dpapi.unprotect(raw[len(_DPAPI_MAGIC) :])
    return Fernet(_machine_key()).decrypt(raw)


def load_state() -> AgentState:
    global _existing_file_unreadable
    settings = get_settings()
    path = settings.state_path
    if not path.exists():
        _existing_file_unreadable = False
        return AgentState()
    try:
        decoded = _decrypt_state_bytes(path.read_bytes())
        data = json.loads(decoded.decode("utf-8"))
        if data.get("schema_version", 1) > SCHEMA_VERSION:
            log.warning("state.schema_too_new", got=data.get("schema_version"))
        result = AgentState.model_validate(data)
        _existing_file_unreadable = False  # decrypted cleanly
        return result
    except (InvalidToken, json.JSONDecodeError, ValueError, OSError, RuntimeError) as e:
        # An existing file we couldn't decrypt. This is NOT necessarily another
        # user's data — DPAPI also fails transiently (locked/roaming/not-yet-loaded
        # profile), and a DPAPI file on a non-Windows host raises RuntimeError. A
        # blank state is fine for THIS read, but flag it so the next save won't
        # clobber the still-present file with an empty/unpaired state.
        _existing_file_unreadable = True
        log.warning("state.load_failed", error=str(e), path=str(path))
        return AgentState()


def _encrypt_state_bytes(plaintext: bytes) -> bytes:
    """Seal plaintext JSON for disk. Prefer Windows DPAPI (user-bound, OS-held
    key); fall back to the legacy machine-key Fernet on non-Windows / dev, or if
    DPAPI itself fails — so a save never loses state."""
    if dpapi.is_available():
        try:
            return _DPAPI_MAGIC + dpapi.protect(plaintext)
        except OSError as e:
            log.warning("state.dpapi_protect_failed", error=str(e))
    return Fernet(_machine_key()).encrypt(plaintext)


def _write_state_locked(state: AgentState, path: Path) -> None:
    """Encrypt + atomically write `state` to `path`. Caller MUST hold _save_lock."""
    global _existing_file_unreadable
    if _existing_file_unreadable and not state.is_paired and path.exists():
        # Last load couldn't decrypt the on-disk file (possibly a TRANSIENT DPAPI
        # failure). Refuse to overwrite it with an unpaired/empty state — that
        # would make a recoverable transient permanent. A genuine (re)pair carries
        # a JWT (is_paired) and is allowed through below.
        log.warning("state.skip_overwrite_unreadable", path=str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _encrypt_state_bytes(state.model_dump_json().encode("utf-8"))
    # Write into a UNIQUE temp file in the SAME directory, then os.replace
    # (atomic on the same volume). A unique name — not a shared ".tmp" — means
    # two concurrent writers never share a scratch file.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encrypted)
        Path(tmp_name).replace(path)
        _existing_file_unreadable = False  # file is now freshly written + readable
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
