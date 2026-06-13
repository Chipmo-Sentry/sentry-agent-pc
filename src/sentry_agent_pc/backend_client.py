"""Thin httpx wrapper for sentry-backend calls.

Two auth modes:
  • Agent (default): long-lived agent JWT obtained via the pairing flow and
    stored in the encrypted state file. Used for `/api/v1/agent/*` endpoints.
  • Dev/CLI: super-admin JWT in settings.dev_token, for the legacy user-scoped
    `/api/v1/cameras` endpoints (power users only).

`pair()` needs no token; everything else sends the agent JWT (or dev token).
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.backend_client")


class CameraRegistration(BaseModel):
    """Payload posted to the legacy user-scoped `/api/v1/cameras` (CLI)."""

    store_id: str
    name: str
    rtsp_url: str
    mediamtx_path: str | None = None
    risk_threshold: float = 70.0


class BackendError(RuntimeError):
    pass


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
        ok_codes: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(auth=auth),
                json=json_body,
            )
        if r.status_code not in ok_codes:
            raise BackendError(f"{method} {path} → {r.status_code} {r.text[:300]}")
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
        self._request("POST", "/api/v1/agent/heartbeat", ok_codes=(204,))

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
        risk_threshold: float = 70.0,
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
    ) -> dict[str, Any]:
        """PATCH /api/v1/agent/cameras/{id} — edit name / connection / threshold.

        Only non-None fields are sent (partial update). The backend re-points
        the live worker at the new rtsp_url when it changes."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if rtsp_url is not None:
            body["rtsp_url"] = rtsp_url
        if risk_threshold is not None:
            body["risk_threshold"] = risk_threshold
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
