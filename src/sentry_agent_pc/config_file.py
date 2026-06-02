"""Read/write the agent .env file from the GUI.

The .env lives next to the encrypted state file in the per-user config dir and
holds only BACKEND_URL. The agent JWT is NOT stored here — it lives in the
encrypted state file after pairing.
"""

from __future__ import annotations

from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR

ENV_PATH = DEFAULT_CONFIG_DIR / ".env"

# Default backend for the packaged agent (customers don't edit this normally).
DEFAULT_BACKEND_URL = "https://sentry-backend-production-4a8f.up.railway.app"


def read_config() -> dict[str, str]:
    """Parse the .env into a dict. Missing file → empty dict."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip().upper()] = val.strip()
    return out


def write_config(backend_url: str) -> None:
    """Write BACKEND_URL to the .env (overwrites whole file)."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text(
        f"BACKEND_URL={backend_url.strip()}\n", encoding="utf-8"
    )
