"""Thin httpx wrapper for sentry-backend calls.

M1.5 mode: use `--dev-token` (super-admin JWT) supplied via settings.
M2 will swap this for paired agent JWT after the pairing flow lands.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.backend_client")


class CameraRegistration(BaseModel):
    """Payload posted to backend `/api/v1/cameras`."""

    store_id: str
    name: str
    rtsp_url: str
    mediamtx_path: str | None = None
    risk_threshold: float = 70.0


class BackendError(RuntimeError):
    pass


class BackendClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_sec: int = 15,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url or s.backend_url).rstrip("/")
        self.token = token or s.dev_token
        self.timeout = timeout_sec

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def me(self) -> dict[str, Any]:
        """GET /api/v1/auth/me — validates the token + returns user info."""
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/api/v1/auth/me", headers=self._headers())
        if r.status_code != 200:
            raise BackendError(f"auth/me failed: {r.status_code} {r.text[:200]}")
        return r.json()  # type: ignore[no-any-return]

    def list_stores(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/api/v1/stores", headers=self._headers())
        if r.status_code != 200:
            raise BackendError(f"stores list failed: {r.status_code} {r.text[:200]}")
        return r.json()  # type: ignore[no-any-return]

    def list_cameras(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/api/v1/cameras", headers=self._headers())
        if r.status_code != 200:
            raise BackendError(f"cameras list failed: {r.status_code} {r.text[:200]}")
        return r.json()  # type: ignore[no-any-return]

    def register_camera(self, reg: CameraRegistration) -> dict[str, Any]:
        """POST /api/v1/cameras → returns CameraPublic dict including uuid."""
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/v1/cameras",
                headers=self._headers(),
                json=reg.model_dump(),
            )
        if r.status_code not in (200, 201):
            raise BackendError(
                f"camera register failed: {r.status_code} {r.text[:300]}",
            )
        return r.json()  # type: ignore[no-any-return]
