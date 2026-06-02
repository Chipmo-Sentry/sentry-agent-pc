"""Updater version comparison + release-asset selection."""

from __future__ import annotations

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


def test_pick_exe_asset_prefers_exact_name() -> None:
    assets = [
        {"name": "other.exe", "browser_download_url": "u1"},
        {"name": updater.ASSET_NAME, "browser_download_url": "u2"},
    ]
    picked = updater._pick_exe_asset(assets)
    assert picked is not None
    assert picked["name"] == updater.ASSET_NAME


def test_pick_exe_asset_falls_back_to_any_exe() -> None:
    assets = [
        {"name": "readme.txt", "browser_download_url": "u1"},
        {"name": "Setup-x64.exe", "browser_download_url": "u2"},
    ]
    picked = updater._pick_exe_asset(assets)
    assert picked is not None
    assert picked["name"] == "Setup-x64.exe"


def test_pick_exe_asset_none_when_no_exe() -> None:
    assert updater._pick_exe_asset([{"name": "notes.md"}]) is None
    assert updater._pick_exe_asset([]) is None
