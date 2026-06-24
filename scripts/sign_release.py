"""Sign a release zip's SHA-256 with the Ed25519 private key (CI only).

    SIGNING_KEY=<base64 priv> uv run python scripts/sign_release.py <zip>

Writes ``<zip>.sig`` — the base64 Ed25519 signature over the zip's SHA-256 hex
(ascii). The updater fetches this sidecar and verifies it against the pinned
public key before applying the update. Mirrors updater._verify_signature.

The private key comes from the RELEASE_SIGNING_KEY secret via the SIGNING_KEY
env var; it is never written to disk.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sign_release.py <zip>", file=sys.stderr)
        return 2
    key_b64 = os.environ.get("SIGNING_KEY", "").strip()
    if not key_b64:
        print("SIGNING_KEY env is empty — nothing to sign", file=sys.stderr)
        return 1
    zip_path = Path(sys.argv[1])
    if not zip_path.is_file():
        print(f"no such file: {zip_path}", file=sys.stderr)
        return 1

    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64))
    sha256_hex = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
    signature = priv.sign(sha256_hex.encode("ascii"))

    sig_path = zip_path.with_name(zip_path.name + ".sig")
    sig_path.write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")
    print(f"signed {zip_path.name} (sha256={sha256_hex}) -> {sig_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
