"""Edge clip uploader (ADR-0029 §12 / B3): forwards a recorded suspicious clip
to the backend, retrying 429/5xx/transport with backoff but never 4xx, and never
raising (a failed handoff must not break recording)."""

from __future__ import annotations

from typing import Any

from sentry_agent_pc.backend_client import BackendError
from sentry_agent_pc.edge import uploader
from sentry_agent_pc.edge.recorder import ClipRecord


def _rec() -> ClipRecord:
    return ClipRecord(
        clip_id="c1",
        camera_id="cam",
        path="/tmp/x.mp4",
        started_at=1.0,
        ended_at=4.0,
        risk_pct=72.0,
        behaviors=["pocket"],
        created_at=10.0,
    )


class _FakeClient:
    """agent_upload_clip raises BackendError(status) per the scripted outcomes
    ("ok" = success), recording each call's camera_uuid."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []
        self.clip_ids: list[object] = []

    def agent_upload_clip(self, clip_path: str, *, camera_uuid: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(camera_uuid)
        self.clip_ids.append(kw.get("clip_id"))
        outcome = self.outcomes.pop(0)
        if outcome != "ok":
            raise BackendError("boom", status=outcome)  # type: ignore[arg-type]
        return {"id": "alert"}


def test_success_first_try() -> None:
    client = _FakeClient(["ok"])
    delays: list[float] = []
    ok = uploader.upload_clip(client, _rec(), "uuid-1", sleep=delays.append)
    assert ok is True
    assert client.calls == ["uuid-1"]
    # Edge clip id forwarded → backend stores it as the alert's edge_clip_id so
    # the agent «Сэжигтэй» row matches the frontend alert.
    assert client.clip_ids == ["c1"]
    assert delays == []


def test_retries_429_then_succeeds() -> None:
    client = _FakeClient([429, "ok"])
    delays: list[float] = []
    ok = uploader.upload_clip(client, _rec(), "u", sleep=delays.append)
    assert ok is True
    assert len(client.calls) == 2
    assert len(delays) == 1  # one backoff before the retry


def test_retries_transport_none_status() -> None:
    client = _FakeClient([None, "ok"])  # status None = network/transport blip
    delays: list[float] = []
    ok = uploader.upload_clip(client, _rec(), "u", sleep=delays.append)
    assert ok is True
    assert len(client.calls) == 2


def test_permanent_4xx_not_retried() -> None:
    client = _FakeClient([400])
    delays: list[float] = []
    ok = uploader.upload_clip(client, _rec(), "u", sleep=delays.append)
    assert ok is False
    assert len(client.calls) == 1  # one shot, no retry on a permanent error
    assert delays == []


def test_exhausts_max_attempts_on_persistent_429() -> None:
    client = _FakeClient([429, 429, 429, 429])
    delays: list[float] = []
    ok = uploader.upload_clip(client, _rec(), "u", max_attempts=4, sleep=delays.append)
    assert ok is False
    assert len(client.calls) == 4
    assert len(delays) == 3  # backoff between attempts, none after the last


def test_make_clip_uploader_invokes_upload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_upload(client: object, rec: ClipRecord, camera_uuid: str, **kw: object) -> bool:
        captured["uuid"] = camera_uuid
        captured["rec"] = rec
        return True

    monkeypatch.setattr(uploader, "upload_clip", fake_upload)
    made: list[int] = []
    cb = uploader.make_clip_uploader("uuid-9", client_factory=lambda: made.append(1) or "client")
    cb(_rec())
    assert captured["uuid"] == "uuid-9"
    assert made == [1]
