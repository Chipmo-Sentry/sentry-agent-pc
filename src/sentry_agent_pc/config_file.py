"""Read/write the agent .env file from the GUI settings panel.

The .env lives next to the encrypted state file in the per-user config dir.
GUI writes BACKEND_URL + DEV_TOKEN here; settings.get_settings() reads them.
"""

from __future__ import annotations

from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR

ENV_PATH = DEFAULT_CONFIG_DIR / ".env"


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


def write_config(backend_url: str, dev_token: str) -> None:
    """Write BACKEND_URL + DEV_TOKEN to the .env (overwrites whole file)."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"BACKEND_URL={backend_url.strip()}",
        f"DEV_TOKEN={dev_token.strip()}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
