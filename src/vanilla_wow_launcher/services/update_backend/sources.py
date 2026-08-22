"""Download-source resolution for the client update backends.

Resolves the active `DownloadSource` (mirror failover probed via the
manifest/client endpoints, server as fallback). Kept separate from the
worker engines so both `VerifyWorker` and `UpdateWorker` share one
definition; `http_update` re-exports these names for compatibility.
"""

import urllib.request
from typing import NamedTuple
from urllib.error import HTTPError

from ...core.constants import UA
from ...core.log_sink import debug_emit
from ...core.security_http import allowed_download_hosts, secure_urlopen


class DownloadSource(NamedTuple):
    """The resolved endpoints of the active download source."""

    manifest_url: str
    client_url: str
    torrent_url: str | None = None


def _source_reachable(url: str) -> bool:
    """Whether a download source answers at `url`. Any HTTP response — even an
    error status (4xx/5xx) — proves the host is reachable; only transport
    failures (DNS, refused, timeout) count as down."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with secure_urlopen(
            req,
            timeout=5,
            allowed_hosts=allowed_download_hosts(),
        ) as r:
            r.read(1)
        return True
    except HTTPError:
        return True
    except Exception:
        return False


def _download_source() -> "DownloadSource | None":
    """Resolve the active download source: mirrors are tried in order
    (automatic failover, probed via their client-files endpoint) and the
    server is the fallback. Returns None when the launcher configuration is
    missing."""
    from ...core import launcher

    cfg = launcher.config()
    server = cfg.server_url if cfg else ""
    if not server:
        return None
    for mirror in cfg.mirrors if cfg else []:
        if not mirror.manifest_url or not mirror.client_url:
            continue
        debug_emit(
            f"[torrent] probing mirror {mirror.name} "
            f"(manifest={'yes' if mirror.manifest_url else 'no'}, "
            f"client={'yes' if mirror.client_url else 'no'}, "
            f"torrent={'yes' if mirror.torrent_url else 'no'})"
        )
        if _source_reachable(mirror.manifest_url) and _source_reachable(
            mirror.client_url
        ):
            debug_emit(f"[torrent] selected mirror {mirror.name}")
            # Fall back to the server's torrent snapshot when the chosen mirror
            # doesn't advertise one, so the resolved source still exposes a
            # torrent even though the recovery UI keys off the whole config.
            return DownloadSource(
                mirror.manifest_url,
                mirror.client_url,
                mirror.torrent_url or cfg.torrent_url,
            )
    debug_emit(
        f"[torrent] selected server {cfg.server_name} "
        f"(torrent={'yes' if cfg.torrent_url else 'no'})"
    )
    return DownloadSource(cfg.manifest_url, cfg.client_url, cfg.torrent_url)
