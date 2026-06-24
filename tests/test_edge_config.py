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


def test_from_dict_string_bools_parsed_by_content() -> None:
    # plain bool("false") is True — make sure we parse the string content
    assert EdgeConfig.from_dict({"upload_clips": "false"}).upload_clips is False
    assert EdgeConfig.from_dict({"upload_clips": "off"}).upload_clips is False
    assert EdgeConfig.from_dict({"upload_clips": "true"}).upload_clips is True
    assert EdgeConfig.from_dict({"upload_clips": "on"}).upload_clips is True
    # garbage bool string → rejected, keeps default (True)
    assert EdgeConfig.from_dict({"upload_clips": "maybe"}).upload_clips is True


def test_from_dict_int_tolerates_float_strings() -> None:
    assert EdgeConfig.from_dict({"frame_skip": "5.0"}).frame_skip == 5
    assert EdgeConfig.from_dict({"max_clips": 7.9}).max_clips == 7


def test_from_dict_rejects_non_finite_floats() -> None:
    # M3: NaN/inf must be dropped (a non-finite weight poisons `raw` → detection
    # silently dies); the field keeps its default instead.
    assert EdgeConfig.from_dict({"w_conceal": "nan"}).w_conceal == EdgeConfig().w_conceal
    assert EdgeConfig.from_dict({"w_conceal": "inf"}).w_conceal == EdgeConfig().w_conceal
    assert EdgeConfig.from_dict({"open_risk": float("nan")}).open_risk == EdgeConfig().open_risk


def test_from_dict_clamps_decay_into_unit_interval() -> None:
    # M3: decay must stay in [0, 1) so `raw` always cools off and the episode FSM
    # can close. A misconfigured >= 1 (or negative) is clamped, not honoured.
    assert EdgeConfig.from_dict({"decay": 1.5}).decay < 1.0
    assert EdgeConfig.from_dict({"decay": 5}).decay < 1.0
    assert EdgeConfig.from_dict({"decay": -0.3}).decay == 0.0
    # a valid in-range value is untouched
    assert EdgeConfig.from_dict({"decay": 0.8}).decay == 0.8


def test_behaviors_page_registry_keys_are_real_fields() -> None:
    # The agent's «Зан үйл» menu renders the FULL effective config from this
    # registry; every key must be a real EdgeConfig field or the row would show
    # "—" for a knob the engine actually runs with.
    from dataclasses import fields

    from sentry_agent_pc.gui.app import _EDGE_CONFIG_ROWS

    names = {f.name for f in fields(EdgeConfig)}
    assert _EDGE_CONFIG_ROWS  # non-empty
    for _group, key, _label, _unit in _EDGE_CONFIG_ROWS:
        assert key in names, f"{key} is not an EdgeConfig field"


def test_edge_config_rows_formats_values() -> None:
    # The read-only table formatter: weights get a + sign, bools become Тийм/Үгүй,
    # missing keys show "—", and floats render compactly.
    from sentry_agent_pc.gui.app import AgentApp

    cfg = {"w_conceal": 14.0, "upload_clips": False, "open_risk": 60.0}
    rows = AgentApp._edge_config_rows(cfg)
    by_key = {label: value for _g, label, value, _u in rows}
    assert by_key["Эд зүйл нуух"] == "+14"
    assert by_key["Cloud руу илгээх"] == "Үгүй"
    assert by_key["Эпизод нээх босго"] == "60"
    # a key absent from cfg → em-dash placeholder
    assert by_key["Барих радиус"] == "—"
