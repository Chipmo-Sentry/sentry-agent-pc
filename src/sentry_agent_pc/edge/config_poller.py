"""Edge config-poller (ADR-0029 §12 / I7): periodically pull the per-store edge
tunables from the backend and HOT-APPLY them to the running Stage-1 pipelines
without a release — mirroring the AI node's config_poller.

Inert until the backend serves a per-store config and bumps ``version`` (I3):
today ``GET /agent/edge-config`` returns the defaults at ``version`` 1, so this
applies once (a no-op, pipelines already default) then idles on the unchanged
version. When I3 lands, an operator change bumps the version and the next poll
re-applies it to every live pipeline.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any, Protocol

from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.config_poller")


class _Configurable(Protocol):
    def apply_config(self, config: EdgeConfig) -> None: ...


class _EdgeConfigClient(Protocol):
    def agent_edge_config(self) -> dict[str, Any]: ...


def poll_and_apply(
    client: _EdgeConfigClient,
    pipes: Iterable[_Configurable | None],
    last_version: int,
) -> int:
    """Fetch edge-config; if its ``version`` changed, build an EdgeConfig and
    apply it to each pipe. Returns the version to remember next time — unchanged
    on a no-op or any error, so a transient blip never re-applies or crashes the
    poll loop.
    """
    try:
        data = client.agent_edge_config()
    except Exception as e:  # noqa: BLE001 — a poll blip must not break the loop
        log.info("edge_config.poll_skipped", error=str(e))
        return last_version
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    if version == last_version:
        return last_version
    cfg = EdgeConfig.from_dict(data)
    applied = 0
    for pipe in pipes:
        if pipe is None:
            continue
        with contextlib.suppress(Exception):
            pipe.apply_config(cfg)
            applied += 1
    log.info("edge_config.applied", version=version, pipes=applied)
    return version
