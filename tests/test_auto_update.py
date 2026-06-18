"""Automatic (no-click) self-update flow: download → tray toast → apply+restart.

Locks in the safety invariant — `apply_update_and_restart` is reached ONLY after a
verified download succeeds; a failed download notifies + reports so the caller can
retry, and NEVER swaps the install in.
"""

from __future__ import annotations

from typing import Any

from sentry_agent_pc import updater
from sentry_agent_pc.gui import update_dialog as ud


class _InlineThread:
    """Drop-in for threading.Thread that runs the target synchronously, so the
    download runner completes deterministically inside the call under test."""

    def __init__(self, target: Any = None, daemon: Any = None, **_: Any) -> None:
        self._target = target

    def start(self) -> None:
        if self._target is not None:
            self._target()


class _FakeTray:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []
        self.stopped = False

    def notify(self, title: str, message: str) -> None:
        self.notes.append((title, message))

    def stop(self) -> None:
        self.stopped = True


class _FakeApp:
    """Stand-in for the Tk app: `after` queues callbacks the test drains."""

    def __init__(self) -> None:
        self._tray = _FakeTray()
        self._queue: list[Any] = []

    def after(self, _delay: int, fn: Any) -> None:
        self._queue.append(fn)

    def run_pending(self) -> None:
        # FIFO; callbacks may enqueue more (e.g. _ok → the deferred apply).
        while self._queue:
            self._queue.pop(0)()


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


def test_auto_update_applies_only_after_successful_download(monkeypatch) -> None:
    monkeypatch.setattr(ud.threading, "Thread", _InlineThread)
    fake_path = object()
    monkeypatch.setattr(ud.updater, "download_asset", lambda info: fake_path)
    applied: dict[str, Any] = {}
    monkeypatch.setattr(
        ud.updater,
        "apply_update_and_restart",
        lambda path, on_before_exit=None: applied.update(path=path, cb=on_before_exit),
    )

    app = _FakeApp()
    done: list[bool] = []
    ud.auto_update_in_background(app, _info(), on_done=done.append)
    app.run_pending()

    assert done == [True]
    assert applied["path"] is fake_path
    # A tray toast was shown before the swap.
    assert app._tray.notes and "9.9.9" in app._tray.notes[-1][1]
    # The apply hook releases the tray so the .exe lock clears for the swap.
    applied["cb"]()
    assert app._tray.stopped


def test_auto_update_failed_download_never_applies(monkeypatch) -> None:
    monkeypatch.setattr(ud.threading, "Thread", _InlineThread)

    def boom(_info: Any) -> Any:
        raise RuntimeError("GitHub 504")

    monkeypatch.setattr(ud.updater, "download_asset", boom)
    applied: list[bool] = []
    monkeypatch.setattr(
        ud.updater, "apply_update_and_restart", lambda *a, **k: applied.append(True)
    )

    app = _FakeApp()
    done: list[bool] = []
    ud.auto_update_in_background(app, _info(), on_done=done.append)
    app.run_pending()

    assert applied == []  # NEVER swap the install on a failed/unverified download
    assert done == [False]  # caller told to clear its flag + retry next check
    assert any("чадсангүй" in msg for _, msg in app._tray.notes)
