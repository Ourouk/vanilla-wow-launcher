"""Launcher configuration — the distribution's server, endpoints and mirrors.

Every endpoint the app talks to (client updates, news, mod/addon catalogs,
realm, downloads) comes from a single JSON file instead of hardcoded values,
so a distribution only needs to ship one file to point the launcher at its
own server.

The file is `vanilla_wow_launcher.json`, discovered next to the executable
(frozen) or in the repo root (running from source), or passed explicitly via
``--launcher-config``. A configuration chosen through the first-launch wizard
is persisted into the per-user config directory and takes precedence over
auto-discovery on later runs. Only ``server.base_url`` is required; every
other URL is derived from it unless overridden:

    {
      "server": {
        "name": "My Server",
        "base_url": "https://server.example",
        "realm": "server.example",
        "manifest_url": "https://server.example/api/file/latest/manifest.json",
        "client_url": "https://server.example/client/latest",
        "torrent_url": "https://server.example/client/latest/client.torrent",
        "news_url": "https://server.example/news",
        "featured_news_url": "https://server.example/news/featured",
        "mods_registry_url": "https://server.example/api/mods.json",
        "addons_registry_url": "https://server.example/api/addons.json",
        "addons_registry_urls": [
          "https://server.example/api/addons.json",
          "https://server.example/addons-overrides.json"
        ]
      },
      "discord_url": "https://discord.gg/example",
      "theme": {
        "C_GOLD": "#d4a02f",
        "logo": "https://server.example/logo.png"
      },
      "mirrors": [
        {
          "name": "Backup",
          "base_url": "https://mirror.example",
          "manifest_url": "https://mirror.example/api/file/latest/manifest.json",
          "client_url": "https://dl.mirror.example/client/latest",
          "torrent_url": "https://dl.mirror.example/client/latest/client.torrent"
        }
      ]
    }

The manifest and client files are fetched from the configured endpoints; a
mirror's ``client_url`` may point at a separate CDN host, and mirrors are
optional (the server is the fallback).

The optional ``torrent_url`` (on the server or a mirror) advertises a
BitTorrent snapshot of the client files for the update backend: the launcher
fetches the ``.torrent`` over HTTPS and bulk-downloads the stale files via
libtorrent when it is available, falling back to per-file HTTP downloads
otherwise. A mirror's ``torrent_url`` takes precedence over the server's.

The optional ``theme`` object overrides the app's color theme per server:
color slots named like ``C_GOLD`` (each a ``#rrggbb`` hex value) plus an
optional ``logo`` URL shown as the header wordmark (see `core/themes`). It
is cosmetic and never validated strictly — a malformed theme falls back to
the default octowow palette instead of failing startup.

A missing or invalid configuration is a hard startup error: the app has
nothing to point at. This module is network-free; `core/security_http` builds
its download allowlist from the configured hosts.
"""

import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .log_sink import log
from .platform_support import config_dir, is_macos

LAUNCHER_FILE = "vanilla_wow_launcher.json"

_LOCK = threading.Lock()
_config: "LauncherConfig | None" = None
_path: str = ""
_error: str = ""


@dataclass
class Mirror:
    """One configured download mirror."""

    name: str
    base_url: str
    manifest_url: str
    client_url: str
    torrent_url: str | None = None


@dataclass
class LauncherConfig:
    """Validated launcher configuration with every endpoint resolved."""

    server_name: str
    server_url: str
    manifest_url: str
    client_url: str
    news_url: str
    featured_news_url: str
    mods_registry_url: str
    addons_registry_url: str
    realm: str
    addons_registry_urls: list[str] = field(default_factory=list)
    mirrors: list["Mirror"] = field(default_factory=list)
    discord_url: str | None = None
    theme: dict | None = None
    torrent_url: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.server_url)

    def download_hosts(self) -> set[str]:
        """Every host the configured server and mirrors may serve from —
        the base URLs plus any custom manifest/client endpoints (e.g. a
        separate CDN host)."""
        hosts: set[str] = set()
        for url in self._all_urls():
            host = urlsplit(url).hostname
            if host:
                hosts.add(host)
        return hosts

    def has_torrent(self) -> bool:
        """Whether any configured source (server or mirror) advertises a
        ``torrent_url``. Static — no network probing."""
        if self.torrent_url:
            return True
        return any(m.torrent_url for m in self.mirrors)

    def all_bases(self) -> list[str]:
        """The server followed by every mirror's base URL."""
        return [self.server_url] + [m.base_url for m in self.mirrors]

    def _all_urls(self) -> list[str]:
        """Every endpoint URL the app may contact: base URLs plus the
        resolved manifest/client endpoints of the server and mirrors."""
        urls: list[str] = list(self.all_bases())
        urls += [self.manifest_url, self.client_url]
        if self.torrent_url:
            urls.append(self.torrent_url)
        for m in self.mirrors:
            urls += [m.manifest_url, m.client_url]
            if m.torrent_url:
                urls.append(m.torrent_url)
        return urls


def _https_url(value: str) -> str | None:
    url = (value or "").strip().rstrip("/")
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    return url


def _default_manifest(base: str) -> str:
    """The derived manifest endpoint for a base URL (\"latest\" version)."""
    return base + "/api/file/latest/manifest.json"


def _default_client(base: str) -> str:
    """The derived client-files root for a base URL (\"latest\" version)."""
    return base + "/client/latest"


def _derive(data: dict) -> LauncherConfig:
    if not isinstance(data, dict):
        raise ValueError("launcher config must be a JSON object")
    server = data.get("server")
    if not isinstance(server, dict):
        raise ValueError("launcher config is missing the 'server' object")
    base = _https_url(server.get("base_url"))
    if base is None:
        raise ValueError(
            "launcher config 'server.base_url' must be an "
            "https URL and is required"
        )

    host = urlsplit(base).hostname or ""

    raw_discord_url = data.get("discord_url")
    if raw_discord_url is None:
        discord_url = None
    elif isinstance(raw_discord_url, str) and raw_discord_url.strip():
        discord_url = _https_url(raw_discord_url)
        if discord_url is None:
            raise ValueError(
                "launcher config 'discord_url' must be an https URL"
            )
    elif isinstance(raw_discord_url, str):
        discord_url = None
    else:
        raise ValueError("launcher config 'discord_url' must be an https URL")

    def _url(key, suffix):
        v = server.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        return base + suffix

    mirrors = []
    for m in data.get("mirrors") or []:
        if not isinstance(m, dict):
            continue
        mb = _https_url(m.get("base_url"))
        if mb is None:
            continue
        mhost = urlsplit(mb).hostname or ""
        mirrors.append(
            Mirror(
                name=(m.get("name") or mhost).strip(),
                base_url=mb,
                manifest_url=_https_url(m.get("manifest_url"))
                or _default_manifest(mb),
                client_url=_https_url(m.get("client_url"))
                or _default_client(mb),
                torrent_url=_https_url(m.get("torrent_url")),
            )
        )

    manifest_url = _https_url(server.get("manifest_url")) or _default_manifest(
        base
    )
    client_url = _https_url(server.get("client_url")) or _default_client(base)

    addons_registry_url = _url("addons_registry_url", "/api/addons.json")
    addons_registry_urls: list[str] = []
    raw_urls = server.get("addons_registry_urls")
    if isinstance(raw_urls, list) and raw_urls:
        for u in raw_urls:
            https = _https_url(u)
            if https:
                addons_registry_urls.append(https)
    if not addons_registry_urls:
        addons_registry_urls = [addons_registry_url]

    raw_theme = data.get("theme")
    theme = raw_theme if isinstance(raw_theme, dict) else None

    return LauncherConfig(
        server_name=(server.get("name") or host).strip(),
        server_url=base,
        manifest_url=manifest_url,
        client_url=client_url,
        news_url=_url(
            "news_url", "/forum/octonews.php?mode=list&forum=2&limit=8"
        ),
        featured_news_url=_url(
            "featured_news_url", "/forum/octonews.php?forum=35&mode=full"
        ),
        mods_registry_url=_url("mods_registry_url", "/api/mods.json"),
        addons_registry_url=addons_registry_url,
        addons_registry_urls=addons_registry_urls,
        realm=(server.get("realm") or host).strip(),
        mirrors=mirrors,
        discord_url=discord_url,
        theme=theme,
        torrent_url=_https_url(server.get("torrent_url")),
    )


def discover_path() -> str:
    """Locate ``vanilla_wow_launcher.json``: next to the executable (frozen)
    or the repo root (source), then the current working directory. On macOS
    the frozen app also searches the folder *containing* the .app bundle, so
    the config can sit next to the bundle (e.g. in the DMG root) rather than
    buried in Contents/MacOS."""
    if getattr(sys, "frozen", False):
        roots = [os.path.dirname(os.path.abspath(sys.executable))]
        if is_macos():
            # <bundle>.app/Contents/MacOS/<exe> → <bundle>.app → parent dir
            roots.append(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(
                            os.path.dirname(os.path.abspath(sys.executable))
                        )
                    )
                )
            )
    else:
        roots = [
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        ]
    roots.append(os.getcwd())
    for root in roots:
        candidate = os.path.join(root, LAUNCHER_FILE)
        if os.path.exists(candidate):
            return candidate
    return ""


def user_config_path() -> str:
    """The per-user launcher-config file, where a first-launch selection is
    persisted so future launches reuse it (lives in the OS config dir)."""
    return os.path.join(config_dir(), LAUNCHER_FILE)


def _auto_path() -> str:
    """The configuration to use when no explicit path was given: the
    previously imported per-user file, else the auto-discovered one."""
    user = user_config_path()
    if os.path.exists(user):
        return user
    return discover_path()


def persist(path: str) -> tuple[str, str]:
    """Copy a validated launcher config into the per-user directory, writing
    atomically (temp file + rename). Only a semantically valid launcher
    config (parseable JSON that passes `_derive`) is persisted. Returns
    (destination, error); exactly one is set."""
    dest = user_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        _derive(json.loads(raw))  # don't persist truncated/invalid configs
        directory = os.path.dirname(dest) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            os.close(fd)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:
        return "", f"Could not save the launcher configuration: {e}"
    return dest, ""


def persist_text(text: str) -> tuple[str, str]:
    """Persist already-fetched, validated launcher config *text* into the
    per-user directory, writing atomically (temp file + rename). Returns
    (destination, error); exactly one is set. Used when the config was
    obtained over the network rather than from a local file."""
    dest = user_config_path()
    try:
        _derive(json.loads(text))  # don't persist truncated/invalid configs
        directory = os.path.dirname(dest) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            os.close(fd)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:
        return "", f"Could not save the launcher configuration: {e}"
    return dest, ""


def validate_path(path: str) -> tuple["LauncherConfig | None", str]:
    """Validate a launcher config file WITHOUT storing it as the active config.
    Returns (config, error); exactly one is set. Never touches module globals."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return _derive(raw), ""
    except Exception as e:
        return None, str(e)


def validate_dict(data) -> tuple["LauncherConfig | None", str]:
    """Validate a launcher config *dict* WITHOUT storing it as the active
    config. Returns (config, error); exactly one is set. Never touches module
    globals. Used by the first-launch wizard to validate a config fetched
    over the network before accepting it."""
    try:
        return _derive(data), ""
    except Exception as e:
        return None, str(e)


def configure(path: str | None = None) -> tuple["LauncherConfig | None", str]:
    """Load and validate the launcher configuration from ``path`` (or an
    auto-discovered file). Returns (config, error); exactly one is set."""
    global _config, _path, _error
    with _LOCK:
        path = path or _auto_path()
        if not path:
            _config, _path, _error = (
                None,
                "",
                (
                    f"No {LAUNCHER_FILE} found. A launcher configuration is "
                    "required — create one or pass --launcher-config."
                ),
            )
            return None, _error
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            config = _derive(raw)
        except Exception as e:
            _config, _path, _error = (
                None,
                path,
                (f"Invalid launcher configuration ({path}): {e}"),
            )
            return None, _error
        _config, _path, _error = config, path, ""
        log(f"Launcher configuration loaded: {config.server_url}")
        return config, ""


def configure_from_dict(data: dict) -> "LauncherConfig | None":
    """Load from a dict (tests and programmatic callers). Returns None when
    invalid; the error is recorded for config_error()."""
    global _config, _path, _error
    with _LOCK:
        try:
            config = _derive(data)
        except Exception as e:
            _config, _path, _error = None, "", str(e)
            return None
        _config, _path, _error = config, "", ""
        return config


def reset():
    """Drop the loaded configuration (test teardown)."""
    global _config, _path, _error
    with _LOCK:
        _config, _path, _error = None, "", ""


def config() -> "LauncherConfig | None":
    with _LOCK:
        return _config


def config_error() -> str:
    with _LOCK:
        return _error


def is_configured() -> bool:
    c = config()
    return bool(c and c.configured)


def server_url() -> str:
    c = config()
    return c.server_url if c else ""


def server_name() -> str:
    c = config()
    return c.server_name if c else ""


def discord_url() -> str | None:
    c = config()
    return c.discord_url if c else None


def news_url() -> str:
    c = config()
    return c.news_url if c else ""


def featured_news_url() -> str:
    c = config()
    return c.featured_news_url if c else ""


def mods_registry_url() -> str:
    c = config()
    return c.mods_registry_url if c else ""


def addons_registry_url() -> str:
    c = config()
    return c.addons_registry_url if c else ""


def addons_registry_urls() -> list[str]:
    """The ordered launcher-configured addon catalog URLs — later entries
    override earlier ones by addon folder name."""
    c = config()
    return list(c.addons_registry_urls) if c else []


def realm() -> str:
    c = config()
    return c.realm if c else ""


def mirrors() -> list["Mirror"]:
    c = config()
    return list(c.mirrors) if c else []
