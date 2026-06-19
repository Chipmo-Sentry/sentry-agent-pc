"""EdgeConfig tests — defaults + partial/coerced from_dict (config-poller payload)."""

from __future__ import annotations

from sentry_agent_pc.edge.config import EdgeConfig


def test_defaults() -> None:
    c = EdgeConfig()
    assert c.open_risk == 60.0
    assert c.close_risk == 30.0
    assert c.pre_sec == 3.0 and c.post_sec == 3.0
    assert c.frame_skip == 3
    assert c.upload_clips is True


def test_from_dict_partial_overrides_and_coerces() -> None:
    c = EdgeConfig.from_dict(
        {"open_risk": 75, "frame_skip": "5", "upload_clips": 0, "unknown_key": 99}
    )
    assert c.open_risk == 75.0 and isinstance(c.open_risk, float)
    assert c.frame_skip == 5 and isinstance(c.frame_skip, int)
    assert c.upload_clips is False
    assert c.close_risk == EdgeConfig().close_risk  # untouched fields keep defaults


def test_from_dict_none_and_empty_give_defaults() -> None:
    assert EdgeConfig.from_dict(None) == EdgeConfig()
    assert EdgeConfig.from_dict({}) == EdgeConfig()


def test_from_dict_ignores_bad_values() -> None:
    c = EdgeConfig.from_dict({"frame_skip": "not-a-number", "open_risk": None})
    assert c == EdgeConfig()  # both rejected → all defaults
