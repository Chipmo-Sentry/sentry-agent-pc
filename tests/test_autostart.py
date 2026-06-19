"""autostart command building + resources path resolution (no registry writes)."""

from __future__ import annotations

from sentry_agent_pc import autostart, resources


def test_launch_command_requests_minimized() -> None:
    cmd = autostart._launch_command()
    assert "--minimized" in cmd
    # Executable path is quoted so spaces (e.g. "Program Files") survive.
    assert cmd.startswith('"')


def test_enable_disable_are_noop_success_on_non_windows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # On dev (non-Windows) auto-start isn't applicable; "not applicable /
    # already off" is SUCCESS, so callers must see True, not False.
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    assert autostart.enable() is True
    assert autostart.disable() is True
    assert autostart.set_enabled(True) is True
    assert autostart.set_enabled(False) is True


def test_resources_icons_exist() -> None:
    # Bundled with the package; must be present for tray + window + installer.
    assert resources.icon_ico().name == "icon.ico"
    assert resources.icon_png().name == "icon.png"
    assert resources.icon_ico().exists()
    assert resources.icon_png().exists()
