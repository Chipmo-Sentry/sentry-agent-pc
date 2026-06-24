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

    # Self-check: once a public key is PINNED in the updater, verify the signature
    # we just produced actually validates against it. This turns "wrong/rotated
    # private key in the secret" into a RED CI build instead of a fleet-wide
    # update outage discovered only in the field. Skipped before activation
    # (empty pin), since there's nothing to check against yet.
    from sentry_agent_pc import updater  # noqa: PLC0415 — CI-only, avoid import cost

    pin = updater._RELEASE_PUBLIC_KEY_B64
    if pin:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(pin)).verify(
                signature, sha256_hex.encode("ascii")
            )
        except (InvalidSignature, ValueError) as e:
            print(
                f"ERROR: signature does not verify against the pinned key ({e}). "
                "The RELEASE_SIGNING_KEY secret likely doesn't match "
                "updater._RELEASE_PUBLIC_KEY_B64.",
                file=sys.stderr,
            )
            return 1
        print("self-check: signature verifies against the pinned public key")

    sig_path = zip_path.with_name(zip_path.name + ".sig")
    sig_path.write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")
    print(f"signed {zip_path.name} (sha256={sha256_hex}) -> {sig_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
