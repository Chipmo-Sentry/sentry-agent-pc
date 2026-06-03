"""Read/write the agent .env file from the GUI.

The .env lives next to the encrypted state file in the per-user config dir and
holds BACKEND_URL + FRONTEND_URL. The agent JWT is NOT stored here — it lives
in the encrypted state file after pairing.
"""

from __future__ import annotations

from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR

ENV_PATH = DEFAULT_CONFIG_DIR / ".env"

# Defaults for the packaged agent (customers don't normally edit these).
DEFAULT_BACKEND_URL = "https://sentry-backend-production-4a8f.up.railway.app"
# Web app base — the "📺 Шууд харах" webview loads {frontend_url}/live.
DEFAULT_FRONTEND_URL = "https://sentry-frontend-production.up.railway.app"


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


def backend_url() -> str:
    return read_config().get("BACKEND_URL") or DEFAULT_BACKEND_URL


def frontend_url() -> str:
    return read_config().get("FRONTEND_URL") or DEFAULT_FRONTEND_URL


def write_config(backend_url: str, frontend_url: str | None = None) -> None:
    """Write BACKEND_URL (+ FRONTEND_URL) to the .env (overwrites whole file).

    If `frontend_url` is omitted, the existing/configured value is preserved.
    """
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fe = (frontend_url or read_config().get("FRONTEND_URL") or DEFAULT_FRONTEND_URL).strip()
    ENV_PATH.write_text(
        f"BACKEND_URL={backend_url.strip()}\nFRONTEND_URL={fe}\n",
        encoding="utf-8",
    )
