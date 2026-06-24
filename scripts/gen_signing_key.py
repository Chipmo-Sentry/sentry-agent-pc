"""Generate an Ed25519 release-signing keypair (run ONCE, locally).

    uv run python scripts/gen_signing_key.py

Prints two base64 values:
  • PRIVATE — add as the GitHub Actions secret RELEASE_SIGNING_KEY (Settings →
    Secrets and variables → Actions). Keep it secret; never commit it.
  • PUBLIC  — paste into updater._RELEASE_PUBLIC_KEY_B64 to activate verification.

See docs/RELEASE_SIGNING.md for the full activation order (the public key must be
pinned only AFTER a signed release exists, or the updater refuses every update).
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print("PRIVATE (-> GitHub secret RELEASE_SIGNING_KEY):")
    print(base64.b64encode(priv_raw).decode("ascii"))
    print()
    print("PUBLIC (-> updater._RELEASE_PUBLIC_KEY_B64):")
    print(base64.b64encode(pub_raw).decode("ascii"))


if __name__ == "__main__":
    main()
