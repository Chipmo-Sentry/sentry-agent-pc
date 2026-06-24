"""Windows DPAPI (Data Protection API) — OS-backed sealing for secrets at rest.

``CryptProtectData`` encrypts a blob bound to the CURRENT WINDOWS USER: only the
same user on the same machine can ``CryptUnprotectData`` it, and the key is held
by the OS (never derivable from anything on disk). This is the real at-rest
protection the state file's old machine-derived Fernet key only approximated —
there, anyone running code as this user could re-derive the key and read the
agent JWT + camera passwords.

Pure ``ctypes`` (no pywin32 dependency). Windows-only — ``is_available()`` is
False elsewhere, and callers fall back to the legacy scheme on non-Windows / dev.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# A fixed, non-secret entropy salt mixed into the seal so a blob protected by this
# app can't be unprotected by an unrelated CryptUnprotectData caller (and vice
# versa). It is NOT a key — it only needs to match between protect/unprotect.
_ENTROPY = b"chipmo-sentry-agent-state-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


def is_available() -> bool:
    """True only where DPAPI exists (Windows)."""
    return os.name == "nt"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    """Build a DATA_BLOB plus the backing buffer (which the caller MUST keep alive
    until the Crypt* call returns, or the blob would point at freed memory)."""
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(data: bytes) -> bytes:
    """Seal ``data`` to the current Windows user. Raises on non-Windows / failure."""
    if not is_available():
        raise RuntimeError("DPAPI is Windows-only")
    blob_in, _in_buf = _blob(data)
    blob_ent, _ent_buf = _blob(_ENTROPY)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, int(blob_out.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def unprotect(blob: bytes) -> bytes:
    """Unseal a blob produced by :func:`protect`. Raises if it wasn't sealed by
    THIS user on THIS machine (which is exactly the protection we want)."""
    if not is_available():
        raise RuntimeError("DPAPI is Windows-only")
    blob_in, _in_buf = _blob(blob)
    blob_ent, _ent_buf = _blob(_ENTROPY)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, int(blob_out.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
