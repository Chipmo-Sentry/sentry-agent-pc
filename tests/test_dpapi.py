"""Windows DPAPI sealing (H1) — round-trip + tamper rejection.

The crypto round-trip can only run on Windows (where CryptProtectData exists);
elsewhere we assert the module reports itself unavailable and refuses to run.
"""

from __future__ import annotations

import os

import pytest

from sentry_agent_pc import dpapi


def test_is_available_matches_platform() -> None:
    assert dpapi.is_available() == (os.name == "nt")


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_protect_unprotect_round_trip() -> None:
    secret = b"agent-jwt + rtsp://admin:pass@cam"
    sealed = dpapi.protect(secret)
    assert sealed != secret  # actually sealed, not echoed
    assert dpapi.unprotect(sealed) == secret


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_unprotect_rejects_tampered_blob() -> None:
    sealed = bytearray(dpapi.protect(b"sensitive"))
    sealed[-1] ^= 0xFF  # flip a bit → DPAPI integrity check must fail
    with pytest.raises(OSError):
        dpapi.unprotect(bytes(sealed))


@pytest.mark.skipif(os.name == "nt", reason="non-Windows refuses to run DPAPI")
def test_protect_raises_on_non_windows() -> None:
    with pytest.raises(RuntimeError):
        dpapi.protect(b"x")
