"""Edge suspicious-clip → cloud upload with bounded retry (ADR-0029 §12 / B3).

The ``EdgeClipRecorder`` saves a clip locally (the "Сэжигтэй" gallery) and fires
``on_clip``; THIS is where the clip is forwarded to the backend
(``POST /agent/edge/clips`` → sentry-ai VLM → alert). The clip is already
persisted in the local ``ClipStore``, so a failed upload is not lost — we retry
**429 / 5xx / transport** with exponential backoff, then give up (the clip stays
local for the gallery and a future re-upload sweep).

This runs on the recorder's dedicated clip-worker thread, so a blocking backoff
here only applies backpressure to clip extraction (bounded, drop-oldest queue) —
it never stalls decode or the live view.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.edge.recorder import ClipRecord
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.uploader")

# Retry these — the server is reachable but momentarily overloaded/unavailable.
# 429 = the backend's edge_clip_rate_limit tripped (slowapi); 5xx = transient.
# 4xx (400/404/413) are permanent (bad camera, clip too large) → never retried.
_RETRY_STATUSES = frozenset({429, 502, 503, 504})


def upload_clip(
    client: BackendClient,
    rec: ClipRecord,
    camera_uuid: str,
    *,
    max_attempts: int = 4,
    backoff_base: float = 1.0,
    backoff_max: float = 30.0,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    """Upload one suspicious clip, retrying 429/5xx/transport with backoff.

    Returns True on a 2xx, False once retries are exhausted or the error is
    permanent (4xx). Never raises — a failed handoff must not break recording.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            client.agent_upload_clip(
                rec.path,
                camera_uuid=camera_uuid,
                risk_pct=rec.risk_pct,
                behaviors=rec.behaviors,
                started_at=rec.started_at,
                ended_at=rec.ended_at,
                behavior_detail=rec.behavior_detail,
                clip_id=rec.clip_id,
            )
        except BackendError as e:
            # status is None for a transport failure (no response) → retriable.
            retriable = e.status is None or e.status in _RETRY_STATUSES
            if not retriable or attempt >= max_attempts:
                log.warning(
                    "edge.upload_failed",
                    clip_id=rec.clip_id,
                    camera_uuid=camera_uuid,
                    status=e.status,
                    attempt=attempt,
                )
                return False
            delay = min(backoff_base * 2 ** (attempt - 1), backoff_max)
            delay += random.uniform(0, backoff_base)  # noqa: S311 — jitter, not crypto
            log.info(
                "edge.upload_retry",
                clip_id=rec.clip_id,
                status=e.status,
                attempt=attempt,
                delay=round(delay, 1),
            )
            sleep(delay)
        else:
            log.info(
                "edge.upload_ok",
                clip_id=rec.clip_id,
                camera_uuid=camera_uuid,
                attempt=attempt,
            )
            return True
    return False  # pragma: no cover — loop returns on success/permanent/exhaustion


def make_clip_uploader(
    camera_uuid: str,
    *,
    client_factory: Callable[[], BackendClient] = BackendClient,
) -> Callable[[ClipRecord], None]:
    """Build an ``on_clip`` callback that uploads each saved clip (best-effort).

    A fresh ``BackendClient`` per clip keeps the latest agent JWT + no shared
    httpx state across the recorder's lifetime.
    """

    def _on_clip(rec: ClipRecord) -> None:
        upload_clip(client_factory(), rec, camera_uuid)

    return _on_clip
