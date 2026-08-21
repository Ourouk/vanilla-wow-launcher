"""Atomic JSON persistence for the updater's config and hash cache.

All config changes go through `update_config()` so the read-modify-write
cycle stays under a lock (concurrent worker threads can't clobber each
other) and writes are atomic (temp file + rename, so a crash can't leave a
truncated file).
"""

import json
import os
import shutil
import sys
import threading

_CONFIG_LOCK = threading.RLock()

# Set by configure() at import time.
config_file: str = ""
cache_file: str = ""


def configure(
    cfg_file: str,
    cache: str,
    legacy_config=(),
    legacy_cache=(),
    legacy_pairs=(),
):
    """Point the store at the on-disk config and hash-cache files.

    `legacy_config`/`legacy_cache` accept a single path string or an
    iterable of paths. When a legacy path is given and the new location is
    empty but the legacy file exists, the legacy file is copied over
    (first-use migration after the data moved to OS-appropriate directories).
    `legacy_pairs` accepts an iterable of (legacy, new) path pairs for extra
    files that moved directories too (e.g. the custom catalog files).
    """
    global config_file, cache_file
    config_file = cfg_file
    cache_file = cache
    for legacy in _as_paths(legacy_config):
        _migrate(legacy, config_file)
    for legacy in _as_paths(legacy_cache):
        _migrate(legacy, cache_file)
    for legacy, new in _as_pairs(legacy_pairs):
        _migrate(legacy, new)


def _as_paths(value) -> tuple:
    """Normalize a legacy path argument (string or iterable) to a tuple."""
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(value)


def _as_pairs(value) -> tuple:
    """Normalize a (legacy, new) pairs argument to a tuple of tuples."""
    return tuple(tuple(pair) for pair in value)


def _migrate(legacy: str, new: str):
    """Copy a legacy file to the new location when the new one is missing."""
    if os.path.exists(new) or not os.path.exists(legacy):
        return
    try:
        os.makedirs(os.path.dirname(new) or ".", exist_ok=True)
        shutil.copyfile(legacy, new)
    except OSError as e:
        sys.stderr.write(f"[config] migration of {legacy} failed: {e}\n")


def _atomic_write(path: str, text: str):
    """Write via a temp file + atomic rename so a crash mid-write can never
    leave a truncated/corrupt file at `path`. Creates the parent directory so
    the per-user data dirs work on first write."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config() -> dict:
    try:
        with open(config_file) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        sys.stderr.write(f"[config] failed to read {config_file}: {e}\n")
        return {}


def save_config(data: dict):
    with _CONFIG_LOCK:
        if not config_file:
            return
        try:
            _atomic_write(config_file, json.dumps(data, indent=2))
        except Exception as e:
            sys.stderr.write(f"[config] failed to write {config_file}: {e}\n")


def update_config(mutator):
    """Load the current on-disk config under the lock, apply `mutator(cfg)`,
    save atomically, and return the result. Every config change — main thread
    or worker — should go through this so no stale in-memory snapshot can
    overwrite keys another thread just persisted."""
    with _CONFIG_LOCK:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return cfg


def load_cache() -> dict:
    try:
        with open(cache_file) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        sys.stderr.write(f"[cache] failed to read {cache_file}: {e}\n")
        return {}


def save_cache(cache: dict):
    with _CONFIG_LOCK:
        if not cache_file:
            return
        try:
            _atomic_write(cache_file, json.dumps(cache))
        except Exception as e:
            sys.stderr.write(f"[cache] failed to write {cache_file}: {e}\n")
