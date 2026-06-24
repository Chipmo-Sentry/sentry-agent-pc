# Release signing (Ed25519) — activation runbook

The in-app updater downloads a release zip and checks its SHA-256. But that hash
is fetched from the **same** GitHub release, so it only proves the download
wasn't corrupted in transit — not that the release is **authentic**. Anyone who
could publish/replace a release (a leaked `GITHUB_TOKEN`, a maintainer-account
takeover) could ship arbitrary code to every store PC.

To close that, each release is signed with an **Ed25519** key. The private key
lives only as a GitHub Actions secret; the public key is pinned into the agent.
The updater refuses any release whose signature doesn't verify.

The code ships **inactive** (`updater._RELEASE_PUBLIC_KEY_B64` is empty → SHA-256
checks only, unchanged behavior). Activate it with the steps below. **Order
matters** — pin the public key only *after* a signed release exists, or the
updater will refuse every update.

## Activate

1. **Generate the keypair** (once, on a trusted machine):

   ```
   uv run python scripts/gen_signing_key.py
   ```

   It prints a PRIVATE and a PUBLIC base64 value. Treat PRIVATE like a password.

2. **Add the private key as a secret.** GitHub → repo → Settings → Secrets and
   variables → Actions → New repository secret:
   - Name: `RELEASE_SIGNING_KEY`
   - Value: the PRIVATE base64 from step 1

3. **Ship one signed release.** Bump the version + tag as usual:

   ```
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   With the secret present, `release.yml` now signs the zip and uploads
   `ChipmoSentryAgent-windows.zip.sig`. Confirm that asset is on the release.
   (The updater still won't *enforce* it yet — the public key isn't pinned.)

4. **Pin the public key.** Paste the PUBLIC base64 from step 1 into
   `src/sentry_agent_pc/updater.py`:

   ```python
   _RELEASE_PUBLIC_KEY_B64 = "<PUBLIC base64>"
   ```

   Bump the version, tag, and release. From this release on, the updater
   **requires** a valid signature to apply any update.

## Rotate / revoke

Re-run step 1, replace the `RELEASE_SIGNING_KEY` secret, ship a release signed
with the new key, then update the pinned public key (same order as above). Old
clients pinned to the previous key keep updating until they reach the release
that re-pins — so don't drop the old key from a release until clients have moved
past it.

## Notes

- Signing is over the zip's SHA-256 hex, not the raw bytes — equivalent security
  (SHA-256 is collision-resistant) and cheap to sign in CI. See
  `scripts/sign_release.py` and `updater._verify_signature`.
- This is independent of, and complementary to, Windows Authenticode. If a code-
  signing certificate is obtained later, sign the exe too; the Ed25519 gate stays
  as defense-in-depth for the self-update channel.
