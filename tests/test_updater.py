"""Updater version comparison + release-asset selection + checksum gate."""

from __future__ import annotations

import hashlib

import pytest

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
