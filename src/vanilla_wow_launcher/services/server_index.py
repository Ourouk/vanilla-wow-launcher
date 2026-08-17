"""First-launch server index: discover the private servers whose launcher
configs are hosted in this repo.

The wizard fetches ``servers.json`` (a plain list of ``{id, name,
config_url}`` entries) from the repo's raw GitHub contents, then fetches the
chosen server's ``vanilla_wow_launcher.json`` on accept. Both are ordinary
HTTPS JSON fetches through ``security_http`` (GitHub raw is in the download
allowlist). Failures degrade gracefully: an unreadable index yields an empty
list (the wizard then offers only a local-file browse), and an unreadable
server config surfaces an error in the wizard rather than crashing.
"""

import json
import urllib.request

from ..core.constants import LAUNCHER_SERVERS_INDEX_URL
from ..core.security_http import secure_urlopen

SERVER_INDEX_TIMEOUT = 10
_FETCH_UA = "VanillaWoWLauncher"


def _https_get(url: str, timeout: int = SERVER_INDEX_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _FETCH_UA})
    with secure_urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_servers_index(
    url: str = LAUNCHER_SERVERS_INDEX_URL,
) -> list[dict]:
    """Fetch the list of available private-server launcher configs.

    Returns a list of ``{id, name, config_url, description}`` dicts with
    invalid entries dropped. Returns an empty list when the index can't be
    fetched or parsed, so the wizard can fall back to browsing for a local
    file.
    """
    try:
        data = json.loads(_https_get(url))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    servers = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        name = entry.get("name")
        config_url = entry.get("config_url")
        if not sid or not name or not isinstance(config_url, str):
            continue
        if not config_url.lower().startswith("https://"):
            continue
        servers.append(
            {
                "id": sid,
                "name": name,
                "config_url": config_url,
                "description": entry.get("description", ""),
            }
        )
    return servers


def fetch_server_config(
    config_url: str,
) -> tuple[dict | None, str | None, str]:
    """Fetch and parse a single server's launcher config.

    Returns ``(data, raw_text, error)``; exactly one of ``data`` / ``error``
    is set. ``raw_text`` is the exact JSON text, suitable for persisting to
    disk once validated.
    """
    try:
        raw = _https_get(config_url)
        data = json.loads(raw)
    except Exception as e:
        return None, None, f"Could not fetch the server configuration: {e}"
    if not isinstance(data, dict):
        return None, None, "The server configuration is not a JSON object."
    return data, raw, ""
