"""Self-update checks against this repo's GitHub releases (cached daily)."""

import json
import time
import urllib.request

from ..core.config_store import load_config, update_config
from ..core.constants import GITHUB_API, MOD_UA, UPDATER_VERSION
from ..core.helpers import parse_version
from ..core.security_http import secure_urlopen

# Self-update: the updater checks its own GitHub releases once a day.
UPDATER_REPO = "Ourouk/vanilla-wow-launcher"
UPDATER_CHECK_TTL = 86400  # 1 day, cached in the config file


def fetch_updater_latest_tag(force: bool = False) -> str | None:
    """Latest release tag of the updater's own repo, cached for a day. Returns
    None when there are no releases yet (GitHub 404) or on any error."""
    now = time.time()
    if not force:
        entry = load_config().get("updater_release_cache", {})
        if (
            entry.get("tag") is not None
            and (now - entry.get("timestamp", 0)) < UPDATER_CHECK_TTL
        ):
            return entry["tag"]
    try:
        req = urllib.request.Request(
            f"{GITHUB_API}/repos/{UPDATER_REPO}/releases/latest",
            headers={"User-Agent": MOD_UA},
        )
        with secure_urlopen(req, timeout=10) as r:
            tag = json.load(r).get("tag_name")
    except Exception:
        return None
    if tag:
        update_config(
            lambda c: c.__setitem__(
                "updater_release_cache", {"timestamp": now, "tag": tag}
            )
        )
    return tag


def updater_update_available(latest_tag: str) -> bool:
    if not latest_tag:
        return False
    a, b = parse_version(latest_tag), parse_version(UPDATER_VERSION)
    n = max(len(a), len(b))  # zero-pad so 1.1 == 1.1.0
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b
