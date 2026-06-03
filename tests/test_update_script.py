"""The self-update .bat must avoid the console-only `timeout` and always relaunch."""

from __future__ import annotations

from pathlib import Path

from sentry_agent_pc.updater import _build_update_script


def _script() -> str:
    return _build_update_script(
        Path(r"C:\tmp\new.exe"), Path(r"C:\app\ChipmoSentryAgent.exe"), Path(r"C:\tmp\u.log")
    )


def test_uses_ping_not_timeout() -> None:
    s = _script()
    # `timeout` fails without a console (the disappear-on-update bug); use ping.
    assert "timeout" not in s.lower()
    assert "ping -n" in s


def test_moves_and_relaunches() -> None:
    s = _script()
    assert 'set "SRC=C:\\tmp\\new.exe"' in s
    assert 'set "DST=C:\\app\\ChipmoSentryAgent.exe"' in s
    assert 'move /y "%SRC%" "%DST%"' in s
    assert 'start "" "%DST%"' in s


def test_relaunches_even_if_swap_fails() -> None:
    # The retry cap must fall through to :launch (never leave the app gone).
    s = _script()
    assert ":launch" in s
    assert "goto launch" in s
