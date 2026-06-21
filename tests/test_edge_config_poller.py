"""Edge config-poller (ADR-0029 §12 / I7): hot-applies backend edge tunables to
running pipelines only when the payload `version` changes; a poll error or an
unchanged version is a no-op, and None pipes are skipped."""

from __future__ import annotations

from typing import Any

from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.config_poller import poll_and_apply


class _FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)

    def agent_edge_config(self) -> dict[str, Any]:
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _FakePipe:
    def __init__(self) -> None:
        self.applied: list[EdgeConfig] = []

    def apply_config(self, config: EdgeConfig) -> None:
        self.applied.append(config)


def test_applies_on_version_change() -> None:
    client = _FakeClient([{"version": 2, "person_conf": 0.5}])
    pipe = _FakePipe()
    v = poll_and_apply(client, [pipe], last_version=-1)
    assert v == 2
    assert len(pipe.applied) == 1
    assert pipe.applied[0].person_conf == 0.5  # payload knob threaded through


def test_skips_when_version_unchanged() -> None:
    client = _FakeClient([{"version": 1, "person_conf": 0.9}])
    pipe = _FakePipe()
    v = poll_and_apply(client, [pipe], last_version=1)
    assert v == 1
    assert pipe.applied == []  # same version → no re-apply


def test_offline_keeps_version() -> None:
    client = _FakeClient([RuntimeError("network down")])
    pipe = _FakePipe()
    v = poll_and_apply(client, [pipe], last_version=5)
    assert v == 5  # error → remembered version unchanged
    assert pipe.applied == []


def test_none_pipes_skipped_but_version_advances() -> None:
    client = _FakeClient([{"version": 3}])
    v = poll_and_apply(client, [None], last_version=0)
    assert v == 3  # advanced even though there was nothing to apply to
