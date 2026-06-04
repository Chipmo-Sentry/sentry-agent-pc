"""The self-update .bat must avoid console-only `timeout`, copy the onedir
folder, and always relaunch."""

from __future__ import annotations

from pathlib import Path

from sentry_agent_pc.updater import _build_update_script


def _script() -> str:
    return _build_update_script(
        Path(r"C:\tmp\new"),
        Path(r"C:\app\Chipmo Sentry"),
        Path(r"C:\app\Chipmo Sentry\ChipmoSentryAgent.exe"),
        Path(r"C:\tmp\u.log"),
    )


def test_uses_ping_not_timeout() -> None:
    s = _script()
    # `timeout` fails without a console (the disappear-on-update bug); use ping.
    assert "timeout" not in s.lower()
    assert "ping -n" in s


def test_robocopies_folder_and_relaunches() -> None:
    s = _script()
    assert "robocopy" in s
    assert 'set "SRC=C:\\tmp\\new"' in s
    assert 'set "DST=C:\\app\\Chipmo Sentry"' in s
    assert 'start "" "%EXE%"' in s
    # robocopy success is exit code < 8.
    assert "lss 8" in s


def test_relaunches_even_if_copy_fails() -> None:
    s = _script()
    assert ":launch" in s
    assert "goto launch" in s


def test_kills_stray_instances_before_copy() -> None:
    # A webview child keeps the .exe locked; kill all instances first.
    s = _script()
    assert "taskkill /f /im ChipmoSentryAgent.exe" in s


def test_switches_cmd_to_utf8_before_using_paths() -> None:
    # cmd reads the .bat in the OEM code page by default, mangling non-ASCII
    # install paths. `chcp 65001` must run BEFORE the path vars are set/used.
    s = _script()
    assert "chcp 65001" in s
    assert s.index("chcp 65001") < s.index('set "SRC=')


def test_non_ascii_install_path_embeds_verbatim() -> None:
    # A Cyrillic/Mongolian install folder (or Cyrillic Windows username) must
    # land in the script unchanged — with chcp 65001 cmd then reads it right.
    s = _build_update_script(
        Path(r"C:\tmp\new"),
        Path(r"D:\ai camera\Sentry-v.3 тэмдэглэл\Chipmo Sentry"),
        Path(r"D:\ai camera\Sentry-v.3 тэмдэглэл\Chipmo Sentry\ChipmoSentryAgent.exe"),
        Path(r"C:\tmp\u.log"),
    )
    assert "Sentry-v.3 тэмдэглэл\\Chipmo Sentry" in s
