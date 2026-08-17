"""App-level constants and filesystem paths shared across modules.

Vanilla WoW Launcher: the executable dir (APP_DIR) is next to the .exe when
frozen (PyInstaller), otherwise next to this file — never the current working
directory. Persistent config/cache files live in OS-appropriate per-user
directories so the app works from read-only install locations: Linux config
in ~/.vanilla-wow-launcher, Windows config in %APPDATA%\\VanillaWoWLauncher,
macOS config in ~/Library/Application Support; the hash cache is kept
separate (Linux XDG cache dir, %LOCALAPPDATA%, ~/Library/Caches).

Files from earlier versions are migrated on first run: the old next-to-exe
``octo_updater_config.json`` / ``octo_updater_hash_cache.json`` (LEGACY_*), the
pre-rename per-user files under the old "octo-updater" directories
(LEGACY_USER_*), and the superseded current-version locations (next-to-exe and
XDG) after config/cache moved to the per-user dirs.

There is deliberately no hardcoded server here: every endpoint (client
updates, news, mod/addon catalogs, realm, mirrors) comes from the launcher
configuration (`core/launcher.py`).
"""

import os
import sys

from .platform_support import (
    cache_dir,
    config_dir,
    default_out_dir,
    is_macos,
    is_windows,
)

UPDATER_VERSION = "1.3.2"
UA = f"VanillaWoWLauncher/{UPDATER_VERSION}"
DOWNLOAD_RETRY = 5
DOWNLOAD_TIMEOUT = 10  # seconds without any data before a transfer aborts

GITHUB_API = "https://api.github.com"
MOD_UA = f"VanillaWoWLauncher/{UPDATER_VERSION}"

# First-run server picker: the index of private-server launcher configs hosted
# in this repo (raw GitHub contents). Lives at the repo root as servers.json.
LAUNCHER_SERVERS_INDEX_URL = (
    "https://raw.githubusercontent.com/Ourouk/vanilla-wow-launcher/main/"
    "servers.json"
)

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # Running from source: the repo root (three levels up from
    # src/vanilla_wow_launcher/core/constants.py).
    APP_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

CONFIG_FILE = os.path.join(config_dir(), "vanilla_wow_launcher_config.json")
CACHE_FILE = os.path.join(cache_dir(), "vanilla_wow_launcher_hash_cache.json")

# Legacy location: the config/cache used to live next to the executable
# under the old "octo_updater" names. Migrated on first use.
LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "octo_updater_config.json")
LEGACY_CACHE_FILE = os.path.join(APP_DIR, "octo_updater_hash_cache.json")


def _legacy_user_config_dir() -> str:
    """The pre-rename per-user config directory (old "octo-updater")."""
    if is_windows():
        return APP_DIR
    if is_macos():
        home = os.environ.get("HOME") or os.path.expanduser("~")
        return os.path.join(
            home, "Library", "Application Support", "OctoUpdater"
        )
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "octo-updater")


def _legacy_user_cache_dir() -> str:
    """The pre-rename per-user cache directory (old "octo-updater")."""
    if is_windows():
        return APP_DIR
    if is_macos():
        home = os.environ.get("HOME") or os.path.expanduser("~")
        return os.path.join(home, "Library", "Caches", "OctoUpdater")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "octo-updater")


LEGACY_USER_CONFIG_FILE = os.path.join(
    _legacy_user_config_dir(), "octo_updater_config.json"
)
LEGACY_USER_CACHE_FILE = os.path.join(
    _legacy_user_cache_dir(), "octo_updater_hash_cache.json"
)


# Superseded current-version locations: where config/cache lived before the
# move to the per-user dirs (Windows: next to the executable; Linux: XDG).
def _legacy_app_config_file() -> str:
    return os.path.join(APP_DIR, "vanilla_wow_launcher_config.json")


def _legacy_app_cache_file() -> str:
    return os.path.join(APP_DIR, "vanilla_wow_launcher_hash_cache.json")


def _legacy_xdg_config_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(
        base, "vanilla-wow-launcher", "vanilla_wow_launcher_config.json"
    )


def _legacy_xdg_cache_file() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(
        base, "vanilla-wow-launcher", "vanilla_wow_launcher_hash_cache.json"
    )


# Ordered migration candidates, most recent location first: the first file
# that exists on disk is copied to the new per-user location on first run.
LEGACY_CONFIG_FILES = (
    _legacy_app_config_file(),
    _legacy_xdg_config_file(),
    LEGACY_CONFIG_FILE,
    LEGACY_USER_CONFIG_FILE,
)
LEGACY_CACHE_FILES = (
    _legacy_app_cache_file(),
    _legacy_xdg_cache_file(),
    LEGACY_CACHE_FILE,
    LEGACY_USER_CACHE_FILE,
)


def legacy_config_dir() -> str:
    """The config directory used by previous versions, for migrating files
    that share it (e.g. the custom catalog files). macOS never moved, so it
    returns '' — there is nothing to migrate from."""
    if is_windows():
        return APP_DIR
    if is_macos():
        return ""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "vanilla-wow-launcher")


def legacy_custom_pairs() -> tuple:
    """(legacy, new) path pairs for the custom catalog files that moved with
    the config directory (empty when it didn't move)."""
    old = legacy_config_dir()
    if not old:
        return ()
    pairs = []
    for kind in ("mods", "addons"):
        legacy = os.path.join(old, f"vanilla_wow_launcher_{kind}_custom.json")
        new = os.path.join(
            config_dir(), f"vanilla_wow_launcher_{kind}_custom.json"
        )
        if legacy != new:
            pairs.append((legacy, new))
    return tuple(pairs)


# First-run default game folder — a user-writable location.
DEFAULT_OUT_DIR = default_out_dir()

# News feed timings (endpoints come from the launcher configuration).
NEWS_TIMEOUT = 8
NEWS_CACHE_TTL = 300
