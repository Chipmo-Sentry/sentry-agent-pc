"""autostart command building + resources path resolution (no registry writes)."""

from __future__ import annotations

from sentry_agent_pc import autostart, resources


def test_launch_command_requests_minimized() -> None:
    cmd = autostart._launch_command()
    assert "--minimized" in cmd
    # Executable path is quoted so spaces (e.g. "Program Files") survive.
    assert cmd.startswith('"')


def test_resources_icons_exist() -> None:
    # Bundled with the package; must be present for tray + window + installer.
    assert resources.icon_ico().name == "icon.ico"
    assert resources.icon_png().name == "icon.png"
    assert resources.icon_ico().exists()
    assert resources.icon_png().exists()
