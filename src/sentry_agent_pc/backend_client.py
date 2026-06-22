"""Thin httpx wrapper for sentry-backend calls.

Two auth modes:
  • Agent (default): long-lived agent JWT obtained via the pairing flow and
    stored in the encrypted state file. Used for `/api/v1/agent/*` endpoints.
  • Dev/CLI: super-admin JWT in settings.dev_token, for the legacy user-scoped
    `/api/v1/cameras` endpoints (power users only).

`pair()` needs no token; everything else sends the agent JWT (or dev token).
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx
from pydantic import BaseModel

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.redact import scrub_credentials
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.backend_client")

# Bounded retry for idempotent calls (GET + heartbeat) so one transient network
# blip on a flaky store link doesn't drop a heartbeat/sync. Non-idempotent calls
# (pair/register/PATCH/DELETE) are never retried — a half-applied write must not
# be replayed.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SEC = 0.5
_BACKOFF_MAX_SEC = 4.0
# Only these transport-level failures are retried; HTTP status errors are not
# (the request reached the server and got a real answer).
_RETRIABLE_EXC = (httpx.TransportError, httpx.ConnectError, httpx.ReadTimeout)


class CameraRegistration(BaseModel):
    """Payload posted to the legacy user-scoped `/api/v1/cameras` (CLI)."""

    store_id: str
    name: str
    rtsp_url: str
    mediamtx_path: str | None = None
    risk_threshold: float = 11.0  # yellow band — wide net, VLM filters (see backend)


class BackendError(RuntimeError):
    """A backend call failed. ``status`` is the HTTP status code for a non-OK
    response, or ``None`` for a transport/network failure (no response arrived).
    Callers (e.g. the edge clip uploader) use it to retry 429/5xx but not 4xx."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _agent_jwt_from_state() -> str | None:
    """Read the stored agent JWT without importing state at module load."""
    from sentry_agent_pc.state import load_state

    return load_state().agent_jwt


class BackendClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_sec: int = 15,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url or s.backend_url).rstrip("/")
        # Prefer the paired agent JWT; fall back to the dev token for the CLI.
        self.token = token or _agent_jwt_from_state() or s.dev_token
        self.timeout = timeout_sec
        # Split timeout: short connect/pool so a dead link fails fast (and gets
        # retried), generous read for slow-but-alive backends.
        self._httpx_timeout = httpx.Timeout(
            connect=5.0, read=float(timeout_sec), write=10.0, pool=5.0
        )

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        ok_codes: tuple[int, ...] = (200, 201, 204),
        retriable: bool | None = None,
    ) -> httpx.Response:
        """Send one request, retrying transport blips only when idempotent.

        ``retriable`` defaults to ``method == "GET"``; callers pass ``True`` for
        the heartbeat POST (idempotent). Never set it for pair/register/PATCH/
        DELETE — replaying those could double-apply a write.
        """
        if retriable is None:
            retriable = method.upper() == "GET"
        attempts = _MAX_ATTEMPTS if retriable else 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(timeout=self._httpx_timeout) as client:
                    r = client.request(
                        method,
                        f"{self.base_url}{path}",
                        headers=self._headers(auth=auth),
                        json=json_body,
                        data=data,
                        files=files,
                    )
                break
            except _RETRIABLE_EXC as e:
                last_exc = e
                if attempt >= attempts:
                    # Scrub: the path may carry a camera UUID, the exception
                    # repr can echo a URL with embedded creds.
                    raise BackendError(
                        scrub_credentials(f"{method} {path} → network: {e}")
                    ) from e
                # Exponential backoff with jitter before the next attempt.
                delay = min(_BACKOFF_BASE_SEC * 2 ** (attempt - 1), _BACKOFF_MAX_SEC)
                delay += random.uniform(0, _BACKOFF_BASE_SEC)  # noqa: S311 — jitter, not crypto
                log.warning(
                    "backend.retry",
                    method=method,
                    path=scrub_credentials(path),
                    attempt=attempt,
                    error=scrub_credentials(str(e)),
                )
                time.sleep(delay)
        else:  # pragma: no cover — loop always breaks or raises
            raise BackendError(scrub_credentials(f"{method} {path} → {last_exc}"))
        if r.status_code not in ok_codes:
            raise BackendError(
                scrub_credentials(f"{method} {path} → {r.status_code} {r.text[:300]}"),
                status=r.status_code,
            )
        return r

    # ── Pairing (no auth) ───────────────────────────────────────────────
    def pair(self, code: str, name: str | None = None) -> dict[str, Any]:
        """POST /api/v1/agents/pair → {agent_token, store_id, store_name, ...}."""
        r = self._request(
            "POST",
            "/api/v1/agents/pair",
            auth=False,
            json_body={"code": code, "name": name},
            ok_codes=(200,),
        )
        return r.json()  # type: ignore[no-any-return]

    # ── Agent-scoped (agent JWT) ────────────────────────────────────────
    def heartbeat(self) -> None:
        # Heartbeat is idempotent (just liveness) → retry transport blips.
        self._request(
            "POST", "/api/v1/agent/heartbeat", ok_codes=(204,), retriable=True
        )

    def agent_list_cameras(self) -> list[dict[str, Any]]:
        r = self._request("GET", "/api/v1/agent/cameras", ok_codes=(200,))
        return r.json()  # type: ignore[no-any-return]

    def agent_stream_config(self) -> dict[str, Any]:
        """GET /api/v1/agent/stream-config → where/whether to push streams."""
        r = self._request("GET", "/api/v1/agent/stream-config", ok_codes=(200,))
        return r.json()  # type: ignore[no-any-return]

    def agent_register_camera(
        self,
        *,
        name: str,
        rtsp_url: str,
        mediamtx_path: str | None = None,
        risk_threshold: float = 11.0,
    ) -> dict[str, Any]:
        """POST /api/v1/agent/cameras — store comes from the agent token."""
        r = self._request(
            "POST",
            "/api/v1/agent/cameras",
            json_body={
                "name": name,
                "rtsp_url": rtsp_url,
                "mediamtx_path": mediamtx_path,
                "risk_threshold": risk_threshold,
            },
            ok_codes=(200, 201),
        )
        return r.json()  # type: ignore[no-any-return]

    def agent_update_camera(
        self,
        camera_uuid: str,
        *,
        name: str | None = None,
        rtsp_url: str | None = None,
        risk_threshold: float | None = None,
        zones: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/v1/agent/cameras/{id} — edit name / connection / threshold / zones.

        Only non-None fields are sent (partial update). The backend re-points
        the live worker at the new rtsp_url when it changes. For ``zones``,
        ``None`` = leave unchanged (omitted), ``[]`` = clear all zones (docs/29)."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if rtsp_url is not None:
            body["rtsp_url"] = rtsp_url
        if risk_threshold is not None:
            body["risk_threshold"] = risk_threshold
        if zones is not None:
            body["zones"] = zones
        r = self._request(
            "PATCH",
            f"/api/v1/agent/cameras/{camera_uuid}",
            json_body=body,
            ok_codes=(200,),
        )
        return r.json()  # type: ignore[no-any-return]

    def agent_delete_camera(self, camera_uuid: str) -> None:
        self._request(
            "DELETE",
            f"/api/v1/agent/cameras/{camera_uuid}",
            ok_codes=(200, 204, 404),
        )

    # ── Edge Stage-1 (config-poller + suspicious-clip handoff) ───────────
    def agent_edge_config(self) -> dict[str, Any]:
        """GET /api/v1/agent/edge-config → tunables to hot-apply (no release)."""
        r = self._request("GET", "/api/v1/agent/edge-config", ok_codes=(200,))
        # A proxy/tunnel can return 200 with a non-JSON body (HTML error page);
        # r.json() would then raise a raw ValueError that bubbles up unscrubbed
        # and crashes the poller loop. Convert to a scrubbed BackendError.
        try:
            return r.json()  # type: ignore[no-any-return]
        except ValueError as e:
            raise BackendError(
                scrub_credentials(f"GET /api/v1/agent/edge-config → bad JSON: {e}")
            ) from e

    def agent_upload_clip(
        self,
        clip_path: str,
        *,
        camera_uuid: str,
        risk_pct: float,
        behaviors: list[str],
        started_at: float,
        ended_at: float,
    ) -> dict[str, Any]:
        """POST a suspicious clip (multipart) to the cloud VLM host for a verdict.

        The edge already decided it's *worth looking at*; the server re-scores +
        runs the VLM and creates the alert. Non-retriable (a real upload write)."""
        with open(clip_path, "rb") as fh:  # noqa: PTH123 — httpx wants a file object
            r = self._request(
                "POST",
                "/api/v1/agent/edge/clips",
                data={
                    "camera_uuid": camera_uuid,
                    "risk_pct": str(risk_pct),
                    "behaviors": ",".join(behaviors),
                    "started_at": str(started_at),
                    "ended_at": str(ended_at),
                },
                files={"clip": ("clip.mp4", fh, "video/mp4")},
                ok_codes=(200, 201, 202),
            )
        return r.json()  # type: ignore[no-any-return]

    # ── Legacy user-scoped (dev token, CLI only) ────────────────────────
    def me(self) -> dict[str, Any]:
        r = self._request("GET", "/api/v1/auth/me", ok_codes=(200,))
        return r.json()  # type: ignore[no-any-return]

    def list_stores(self) -> list[dict[str, Any]]:
        r = self._request("GET", "/api/v1/stores", ok_codes=(200,))
        return r.json()  # type: ignore[no-any-return]

    def list_cameras(self) -> list[dict[str, Any]]:
        r = self._request("GET", "/api/v1/cameras", ok_codes=(200,))
        return r.json()  # type: ignore[no-any-return]

    def register_camera(self, reg: CameraRegistration) -> dict[str, Any]:
        r = self._request(
            "POST", "/api/v1/cameras", json_body=reg.model_dump(), ok_codes=(200, 201)
        )
        return r.json()  # type: ignore[no-any-return]

    def delete_camera(self, camera_uuid: str) -> None:
        self._request(
            "DELETE", f"/api/v1/cameras/{camera_uuid}", ok_codes=(200, 204, 404)
        )
