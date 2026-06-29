"""autostart command building + resources path resolution (no registry writes)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from sentry_agent_pc import autostart, resources
from sentry_agent_pc.gui.app import AgentApp


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


def test_windows_icon_layers_have_transparent_background() -> None:
    expected_sizes = {
        (16, 16),
        (20, 20),
        (24, 24),
        (32, 32),
        (40, 40),
        (48, 48),
        (64, 64),
        (128, 128),
        (256, 256),
    }
    with Image.open(resources.icon_ico()) as icon:
        assert icon.info["sizes"] == expected_sizes
        for size in expected_sizes:
            layer = icon.ico.getimage(size).convert("RGBA")
            assert layer.getpixel((0, 0))[3] == 0

    with Image.open(resources.icon_png()) as tray:
        rgba = tray.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((rgba.width // 2, rgba.height // 10))[3] <= 2


def test_main_window_icon_is_set_directly(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    icon = tmp_path / "icon.ico"
    icon.touch()
    received: list[str] = []

    class FakeWindow:
        def iconbitmap(self, bitmap: str) -> None:
            received.append(bitmap)

    monkeypatch.setattr(resources, "icon_ico", lambda: icon)
    AgentApp._set_window_icon(FakeWindow())  # type: ignore[arg-type]

    assert received == [str(icon)]
