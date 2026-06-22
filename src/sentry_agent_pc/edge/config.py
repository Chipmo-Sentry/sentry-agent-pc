"""Edge Stage-1 tunables — one place for every knob, so the central config-poller
can hot-apply operator changes WITHOUT a release (mirrors the node's pattern).

EdgeConfig is immutable; ``from_dict`` builds one from a partial backend payload
(unknown keys ignored, missing keys keep defaults). The runtime swaps the whole
config atomically to hot-apply.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.config")

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class EdgeConfig:
    # Detection
    person_conf: float = 0.35
    item_conf: float = 0.40
    frame_skip: int = 3  # run YOLO every Nth decoded frame
    # Behaviour signal weights + geometry
    w_holding: float = 5.0
    w_conceal: float = 14.0
    w_wrist_torso: float = 3.0
    reach_frac: float = 0.35  # wrist→item proximity as a fraction of person height
    near_frac: float = 0.18  # wrist→hip (concealment) proximity fraction
    min_kp_conf: float = 0.30
    # docs/29 P1c (edge parity) — zone-aware signals. exit_after_concealment is
    # strong (a concealed person entering an exit zone should push the gate open
    # so the clip is recorded + sent to the cloud VLM); repeated_shelf_visit is a
    # mild dwell hint. No-op on a camera with no zones drawn.
    w_exit_after_conceal: float = 40.0
    w_repeated_shelf: float = 3.0
    repeated_shelf_threshold: int = 3  # distinct shelf entries before it banks
    # Risk → episode FSM
    decay: float = 0.90
    open_risk: float = 60.0
    close_risk: float = 30.0
    post_quiet_sec: float = 2.0
    drop_after_sec: float = 1.5
    iou_match: float = 0.3
    band_yellow: float = 40.0
    band_red: float = 70.0
    # Clip recorder (the −3s … +3s requirement)
    pre_sec: float = 3.0
    post_sec: float = 3.0
    segment_sec: float = 1.0
    keep_sec: float = 45.0
    max_clips: int = 50
    max_age_sec: float = 7 * 24 * 3600
    # Server handoff
    upload_clips: bool = True  # push suspicious clips to the cloud VLM host

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EdgeConfig:
        """Build from a partial dict (e.g. backend payload); ignore unknown keys."""
        if not data:
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known or value is None:
                continue
            try:
                kwargs[key] = _coerce(known[key], value)
            except (TypeError, ValueError):
                log.warning("edge_config.reject_value", key=key, value=repr(value))
                continue
        return cls(**kwargs)


def _coerce(type_str: Any, value: Any) -> Any:
    """Best-effort coerce a JSON value to the field's annotated scalar type.

    Strings are parsed by content — ``"false"``/``"off"`` become ``False`` (a
    plain ``bool("false")`` is ``True``, which would silently flip a knob).
    Invalid values raise so ``from_dict`` rejects + logs them rather than
    coercing to a wrong default.
    """
    t = str(type_str)
    if "bool" in t:
        if isinstance(value, str):
            s = value.strip().lower()
            if s in _TRUE_STRINGS:
                return True
            if s in _FALSE_STRINGS:
                return False
            raise ValueError(f"not a bool: {value!r}")
        return bool(value)
    if "int" in t:
        return int(float(value))  # tolerate "5", "5.0", 5.0 → 5
    if "float" in t:
        return float(value)
    return value
