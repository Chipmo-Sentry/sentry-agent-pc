"""Updater version comparison + release-asset selection + checksum gate."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sentry_agent_pc import updater


def test_parse_version_strips_v_prefix() -> None:
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_handles_prerelease_and_build() -> None:
    assert updater.parse_version("v2.0.0-rc1") == (2, 0, 0)
    assert updater.parse_version("1.4.0+build7") == (1, 4, 0)


def test_parse_version_tolerates_garbage() -> None:
    # Non-numeric chunks degrade to 0 rather than raising.
    assert updater.parse_version("vX.Y") == (0, 0)


def test_version_ordering() -> None:
    assert updater.parse_version("v0.2.0") > updater.parse_version("v0.1.0")
    assert updater.parse_version("v0.1.10") > updater.parse_version("v0.1.2")
    assert updater.parse_version("v1.0.0") > updater.parse_version("v0.9.9")


def test_pick_asset_prefers_exact_zip_name() -> None:
    assets = [
        {"name": "ChipmoSentryAgent-Setup.exe", "browser_download_url": "u1"},
        {"name": updater.ASSET_NAME, "browser_download_url": "u2"},
    ]
    picked = updater._pick_asset(assets)
    assert picked is not None
    assert picked["name"] == updater.ASSET_NAME


def test_pick_asset_requires_exact_name_no_loose_zip_fallback() -> None:
    # A stray .zip must NOT be auto-selected — only the exact published name.
    assets = [
        {"name": "ChipmoSentryAgent-Setup.exe", "browser_download_url": "u1"},
        {"name": "build-x64.zip", "browser_download_url": "u2"},
    ]
    assert updater._pick_asset(assets) is None


def test_pick_asset_none_when_no_zip() -> None:
    assert updater._pick_asset([{"name": "ChipmoSentryAgent-Setup.exe"}]) is None
    assert updater._pick_asset([]) is None


def test_find_sidecar_sha256() -> None:
    assets = [
        {"name": updater.ASSET_NAME, "browser_download_url": "u1"},
        {"name": updater.SHA256_ASSET_NAME, "browser_download_url": "u2"},
    ]
    sidecar = updater._find_sidecar_sha256(assets)
    assert sidecar is not None and sidecar["browser_download_url"] == "u2"
    assert updater._find_sidecar_sha256([{"name": updater.ASSET_NAME}]) is None


def _info(**kw: object) -> updater.UpdateInfo:
    base: dict[str, object] = {
        "version": "9.9.9",
        "tag": "v9.9.9",
        "download_url": "u",
        "notes": "",
        "html_url": "h",
    }
    base.update(kw)
    return updater.UpdateInfo(**base)  # type: ignore[arg-type]


def test_verify_checksum_refuses_when_no_expected(tmp_path) -> None:
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    # No digest and no sidecar URL → refuse (and delete the file).
    with pytest.raises(RuntimeError, match="checksum"):
        updater._verify_checksum(_info(), hashlib.sha256(b"data").hexdigest(), f)
    assert not f.exists()


def test_verify_checksum_rejects_mismatch(tmp_path) -> None:
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    info = _info(expected_sha256="0" * 64)
    with pytest.raises(RuntimeError):
        updater._verify_checksum(info, hashlib.sha256(b"data").hexdigest(), f)
    assert not f.exists()


def test_verify_checksum_accepts_match(tmp_path) -> None:
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    good = hashlib.sha256(b"data").hexdigest()
    updater._verify_checksum(_info(expected_sha256=good.upper()), good, f)  # no raise
    assert f.exists()  # left in place on success


# --- release signing (M1, Ed25519) ------------------------------------------


def test_find_asset_named() -> None:
    assets = [{"name": "a"}, {"name": updater.SIG_ASSET_NAME, "browser_download_url": "u"}]
    found = updater._find_asset_named(assets, updater.SIG_ASSET_NAME)
    assert found is not None and found["browser_download_url"] == "u"
    assert updater._find_asset_named(assets, "missing") is None


def test_verify_signature_skips_when_no_pinned_key(tmp_path, monkeypatch) -> None:
    # Default (empty pin): signing not activated → no-op even with no .sig, and
    # the download is kept (behavior is unchanged from the SHA-256-only era).
    monkeypatch.setattr(updater, "_release_public_key", lambda: None)
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    updater._verify_signature(_info(), "deadbeef", f)  # no raise
    assert f.exists()


def test_verify_signature_accepts_valid(tmp_path, monkeypatch) -> None:
    priv = Ed25519PrivateKey.generate()
    sha = hashlib.sha256(b"data").hexdigest()
    sig = priv.sign(sha.encode("ascii"))
    monkeypatch.setattr(updater, "_RELEASE_PUBLIC_KEY_B64", "x")  # signing active
    monkeypatch.setattr(updater, "_release_public_key", lambda: priv.public_key())
    monkeypatch.setattr(updater, "_fetch_signature", lambda info: sig)
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    updater._verify_signature(_info(sig_url="s"), sha, f)  # no raise
    assert f.exists()


def test_verify_signature_rejects_tampered(tmp_path, monkeypatch) -> None:
    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    sha = hashlib.sha256(b"data").hexdigest()
    bad_sig = other.sign(sha.encode("ascii"))  # signed by the WRONG key
    monkeypatch.setattr(updater, "_RELEASE_PUBLIC_KEY_B64", "x")  # signing active
    monkeypatch.setattr(updater, "_release_public_key", lambda: priv.public_key())
    monkeypatch.setattr(updater, "_fetch_signature", lambda info: bad_sig)
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    with pytest.raises(RuntimeError):
        updater._verify_signature(_info(sig_url="s"), sha, f)
    assert not f.exists()  # refused + deleted


def test_verify_signature_fails_closed_on_bad_pinned_key(tmp_path, monkeypatch) -> None:
    # Review fix: a NON-empty but unparseable pinned key must REFUSE the update
    # (fail closed), not silently downgrade to SHA-256-only like the empty pin.
    monkeypatch.setattr(updater, "_RELEASE_PUBLIC_KEY_B64", "not-valid-base64-!!!")
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    with pytest.raises(RuntimeError):
        updater._verify_signature(_info(sig_url="s"), "deadbeef", f)
    assert not f.exists()


def test_fetch_signature_none_when_absent() -> None:
    # No sig asset on the release (sig_url None) → return immediately, no retries.
    assert updater._fetch_signature(_info()) is None


def test_verify_signature_refuses_when_sig_missing(tmp_path, monkeypatch) -> None:
    # Signing is on (key pinned) but the release carries no signature → refuse.
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(updater, "_RELEASE_PUBLIC_KEY_B64", "x")  # signing active
    monkeypatch.setattr(updater, "_release_public_key", lambda: priv.public_key())
    monkeypatch.setattr(updater, "_fetch_signature", lambda info: None)
    f = tmp_path / "a.zip"
    f.write_bytes(b"data")
    with pytest.raises(RuntimeError):
        updater._verify_signature(_info(), "deadbeef", f)
    assert not f.exists()


# --- robocopy /MIR mirror + safety guard (#14) ------------------------------


def test_is_safe_mirror_dest_accepts_real_install_dir() -> None:
    # A normal nested install dir (drive + ≥2 components) is safe to mirror.
    assert updater._is_safe_mirror_dest(Path(r"C:\Users\Acer\AppData\Local\ChipmoSentryAgent"))


def test_is_safe_mirror_dest_rejects_drive_root() -> None:
    # A drive root would be catastrophic to purge with /MIR.
    assert not updater._is_safe_mirror_dest(Path("C:\\"))


def test_is_safe_mirror_dest_rejects_shallow_path() -> None:
    # Drive + a single component is too shallow — refuse to mirror.
    assert not updater._is_safe_mirror_dest(Path(r"C:\Foo"))


def test_is_safe_mirror_dest_rejects_user_profile_root(monkeypatch) -> None:
    profile = r"C:\Users\Acer"
    monkeypatch.setenv("USERPROFILE", profile)
    # The profile root itself and its parent (\Users) must never be mirrored.
    assert not updater._is_safe_mirror_dest(Path(profile))
    assert not updater._is_safe_mirror_dest(Path(profile).parent)


def test_robocopy_line_uses_mir_for_safe_dest() -> None:
    line = updater._robocopy_line(Path(r"C:\Users\Acer\AppData\Local\ChipmoSentryAgent"))
    assert "/MIR" in line
    assert "/E " not in line  # not the non-purging fallback
    assert '"%SRC%" "%DST%"' in line


def test_robocopy_line_falls_back_to_e_for_unsafe_dest() -> None:
    # An unsafe DST must NOT mirror/purge — degrade to /E (copy, no purge).
    line = updater._robocopy_line(Path("C:\\"))
    assert "/MIR" not in line
    assert "/E" in line


def test_build_update_script_embeds_mir_for_real_install() -> None:
    script = updater._build_update_script(
        extract_dir=Path(r"C:\Temp\extract"),
        install_dir=Path(r"C:\Users\Acer\AppData\Local\ChipmoSentryAgent"),
        exe=Path(r"C:\Users\Acer\AppData\Local\ChipmoSentryAgent\ChipmoSentryAgent.exe"),
        log_path=Path(r"C:\Temp\chipmo_update.log"),
    )
    assert "robocopy" in script
    assert "/MIR" in script
