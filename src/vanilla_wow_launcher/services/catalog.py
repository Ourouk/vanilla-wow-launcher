"""Shared catalog plumbing for mods and addons.

Both registries follow the same model:

  * a remote HTTPS JSON catalog (fetched by the services in `services/mods.py`
    and `services/addons.py`, cached in the config file so startup works
    offline), and
  * an optional per-user custom JSON file in the config directory that
    extends/overrides the remote catalog — the "savvy user" escape hatch.

This module holds only the toolkit-agnostic, network-free pieces: catalog-URL
storage, custom-file resolution, entry validation and merge precedence.
Nothing from a JSON file is ever executed — the only special behaviours a mod
catalog may name are the allowlisted source kinds / post-install hooks below,
and download hosts are still vetted by `security_http` at fetch time.
"""

import json
import os
from urllib.parse import urlsplit

from ..core import config_store
from ..core.log_sink import log
from ..core.platform_support import config_dir

# Allowlisted mod source kinds / post-install hooks. A remote or custom mod
# entry can only reference these — it cannot name arbitrary code.
MOD_SOURCE_KINDS = {
    "github_release",
    "codeberg_release",
    "direct_file",
    "direct_tar",
}
MOD_POST_INSTALL_HOOKS = {"write_dxvk_conf"}

CUSTOM_FILE_TEMPLATE = "[\n]\n"

# Catalogs auto-refresh at most once a week: startup and panel loads serve
# the persisted cache instantly, and only a cache older than this TTL (or an
# explicit Settings → Reload / ⟳ refresh) hits the network again.
CATALOG_TTL = 7 * 86400


# ── catalog URL storage ──────────────────────────────────────────────────────


def get_registry_url(kind: str) -> str:
    """The per-user catalog URL override, or '' when the launcher-configured
    URL should be used instead."""
    return config_store.load_config().get(f"{kind}_registry_url") or ""


def set_registry_url(kind: str, url: str) -> str | None:
    """Validate and persist a per-user catalog URL override (HTTPS, no
    credentials). An empty value clears the override so the launcher URL is
    used again. Returns an error message, or None on success."""
    url = (url or "").strip().rstrip("/")
    if not url:
        config_store.update_config(
            lambda c: c.pop(f"{kind}_registry_url", None)
        )
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Invalid URL."
    if parts.scheme != "https":
        return "Catalog URL must use https."
    if not parts.hostname:
        return "Catalog URL is missing a host."
    if parts.username or parts.password:
        return "Catalog URL must not embed credentials."
    config_store.update_config(
        lambda c: c.__setitem__(f"{kind}_registry_url", url)
    )
    return None


def reset_registry_url(kind: str):
    """Drop the per-user override so the launcher-configured URL is used."""
    config_store.update_config(lambda c: c.pop(f"{kind}_registry_url", None))


# ── custom-file helpers ──────────────────────────────────────────────────────


def custom_file(kind: str) -> str:
    """Path of the per-user custom JSON file for a catalog kind."""
    return os.path.join(
        config_dir(), f"vanilla_wow_launcher_{kind}_custom.json"
    )


def load_custom(kind: str, validator) -> list:
    """Load and validate the per-user custom file.

    Returns the validated entries (empty on a missing file or malformed
    JSON). Invalid entries are skipped with a logged warning rather than
    failing the whole load.
    """
    path = custom_file(kind)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log(f"  {kind} custom file unreadable: {e}", "err")
        return []
    if not isinstance(raw, list):
        log(f"  {kind} custom file must contain a JSON list.", "err")
        return []
    out = []
    for entry in raw:
        cleaned = validator(entry) if isinstance(entry, dict) else None
        if cleaned is None:
            log(
                f"  {kind} custom file: skipping invalid entry {entry!r}",
                "err",
            )
            continue
        out.append(cleaned)
    return out


def write_custom_template(kind: str, template: str) -> bool:
    """Create the custom file from ``template`` when it doesn't exist yet."""
    path = custom_file(kind)
    if os.path.exists(path):
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(template)
    except OSError as e:
        log(f"  Could not create {kind} custom file: {e}", "err")
        return False
    return True


def clear_custom(kind: str) -> bool:
    """Delete the custom file. Returns True when something was removed."""
    path = custom_file(kind)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError as e:
        log(f"  Could not clear {kind} custom file: {e}", "err")
    return False


# ── shared validation helpers ────────────────────────────────────────────────


def safe_folder(name) -> bool:
    """A directory name we are willing to install into (no separators, no
    traversal, no NUL)."""
    if not isinstance(name, str):
        return False
    name = name.strip()
    return (
        bool(name)
        and name not in (".", "..")
        and not any(ch in name for ch in "/\\")
        and "\x00" not in name
    )


def safe_relpath(p) -> bool:
    """A relative destination path: not absolute, no traversal, no NUL."""
    if not isinstance(p, str) or not p:
        return False
    if p.startswith(("/", "\\")) or p[1:2] == ":":
        return False
    parts = p.replace("\\", "/").split("/")
    return (
        all(part and part not in (".", "..") for part in parts)
        and "\x00" not in p
    )


def safe_ref(v) -> str | None:
    """A branch/tag/ref string (whitespace-free, no traversal), else None."""
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or any(ch.isspace() for ch in v) or ".." in v:
        return None
    return v


def _https_url(u) -> str | None:
    if not isinstance(u, str):
        return None
    u = u.strip()
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    return u


def _safe_slug(s) -> str | None:
    """A repo owner/repo slug: printable ASCII letters, digits, . _ -."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or len(s) > 100 or any(ch.isspace() for ch in s):
        return None
    if not all(ch.isalnum() or ch in "._-" for ch in s):
        return None
    return s


def _valid_extract_map(emap) -> dict | None:
    """Sanitize an extract_map {zip/tar entry pattern: dest} into a dict of
    valid relative destinations. None when not a dict, or when every entry
    failed validation (a map the installer could never honour)."""
    if emap is None:
        return None
    if not isinstance(emap, dict):
        return None
    out = {}
    for pattern, dest in emap.items():
        if (
            isinstance(pattern, str)
            and pattern
            and isinstance(dest, str)
            and safe_relpath(dest)
        ):
            out[pattern] = dest
    return out or None


# ── addon entries ────────────────────────────────────────────────────────────


def validate_addon(entry: dict) -> dict | None:
    """Sanitize one addon catalog entry; None when unusable.

    The slim output carries the keys the ADDONS panel/installer consume,
    plus the optional ``recommended`` / ``blocked`` flags. Git hosts are
    vetted by the addons service (which owns the host allowlist), so they
    are only length-checked here.
    """
    name = ((entry.get("name") or entry.get("folder")) or "").strip()
    if not safe_folder(name):
        return None
    rec = {
        "name": name,
        "git": None,
        "branch": None,
        "ref": None,
        "description": None,
        "toc": {},
        "recommended": False,
        "blocked": False,
    }
    git = entry.get("git")
    if isinstance(git, str) and git.strip():
        rec["git"] = git.strip()
    rec["branch"] = safe_ref(entry.get("branch"))
    rec["ref"] = safe_ref(entry.get("ref"))
    desc = entry.get("description")
    rec["description"] = desc if isinstance(desc, str) else None
    toc = entry.get("toc")
    if isinstance(toc, dict):
        rec["toc"] = {
            k: toc[k] for k in ("Title", "Notes", "Interface") if k in toc
        }
    rec["recommended"] = bool(entry.get("recommended", False))
    rec["blocked"] = bool(entry.get("blocked", False))
    return rec


def merge_addons(remote: list, custom: list) -> list:
    """Custom addon entries override remote ones by folder name; new folders
    are appended. ``recommended`` / ``blocked`` flags OR together so a custom
    file can only add them."""
    by_folder = {a.get("name"): a for a in remote}
    for entry in custom:
        folder = entry.get("name")
        if not folder:
            continue
        base = by_folder.get(folder)
        if base is None:
            by_folder[folder] = dict(entry)
            continue
        for key in ("git", "branch", "ref", "description"):
            if entry.get(key) is not None:
                base[key] = entry[key]
        base["recommended"] = base.get("recommended") or entry.get(
            "recommended", False
        )
        base["blocked"] = base.get("blocked") or entry.get("blocked", False)
    return list(by_folder.values())


# ── mod entries ──────────────────────────────────────────────────────────────


def validate_mod(entry: dict) -> dict | None:
    """Sanitize one mod catalog entry into the shape the mod installer uses;
    None when unusable or when a field would break the installer."""
    if not isinstance(entry, dict):
        return None
    mid = (entry.get("id") or "").strip()
    if not safe_folder(mid):
        return None
    name = (entry.get("name") or mid).strip()
    if not name:
        return None
    source = entry.get("source")
    if not isinstance(source, dict):
        return None
    kind = source.get("kind")
    if kind not in MOD_SOURCE_KINDS:
        return None

    mod = {
        "id": mid,
        "name": name,
        "essential": bool(entry.get("essential", False)),
        "description": (
            entry.get("description")
            if isinstance(entry.get("description"), str)
            else ""
        ),
        "repo_url": _https_url(entry.get("repo_url")),
        "source": {},
    }
    hooks = source.get("post_install") or []
    if hooks:
        if not isinstance(hooks, list) or not all(
            h in MOD_POST_INSTALL_HOOKS for h in hooks
        ):
            return None
        mod["source"]["post_install"] = list(hooks)

    if kind in ("github_release", "codeberg_release"):
        owner = _safe_slug(source.get("owner"))
        repo = _safe_slug(source.get("repo"))
        pattern = source.get("asset_pattern")
        if (
            not owner
            or not repo
            or not isinstance(pattern, str)
            or not pattern
        ):
            return None
        raw_emap = source.get("extract_map")
        emap = _valid_extract_map(raw_emap)
        if raw_emap is not None and emap is None:
            return None  # a map was given but nothing in it is usable
        version_from = source.get("version_from")
        mod["source"].update(
            {
                "kind": kind,
                "owner": owner,
                "repo": repo,
                "asset_pattern": pattern,
                "prefer_no": source.get("prefer_no")
                if isinstance(source.get("prefer_no"), str)
                else None,
                "extract_map": emap,
                "version_from": version_from
                if version_from == "asset"
                else None,
            }
        )
    elif kind == "direct_file":
        url = _https_url(source.get("url"))
        dest = source.get("dest")
        emap = _valid_extract_map(source.get("extract_map"))
        if not url or (
            not (isinstance(dest, str) and safe_relpath(dest)) and not emap
        ):
            return None
        mod["source"].update({"kind": "direct_file", "url": url})
        if isinstance(dest, str) and safe_relpath(dest):
            mod["source"]["dest"] = dest
        if emap:
            mod["source"]["extract_map"] = emap
        if source.get("pinned_version") is not None:
            mod["source"]["pinned_version"] = str(source["pinned_version"])
    elif kind == "direct_tar":
        url = _https_url(source.get("url"))
        emap = _valid_extract_map(source.get("extract_map"))
        if not url or not emap:
            return None
        mod["source"].update(
            {"kind": "direct_tar", "url": url, "extract_map": emap}
        )
        if source.get("pinned_version") is not None:
            mod["source"]["pinned_version"] = str(source["pinned_version"])

    register = entry.get("register_dll")
    if register is not None:
        if not isinstance(register, str) or not safe_relpath(register):
            return None
        mod["register_dll"] = register
    files = entry.get("installed_files")
    if files is not None:
        if not isinstance(files, list) or not all(
            isinstance(f, str) and safe_relpath(f) for f in files
        ):
            return None
        mod["installed_files"] = list(files)
    return mod


def merge_mods(remote: list, custom: list) -> list:
    """Custom mod entries override remote ones by id; new ids are appended."""
    by_id = {m["id"]: m for m in remote}
    for entry in custom:
        mid = entry.get("id")
        if not mid:
            continue
        base = by_id.get(mid)
        if base is None:
            by_id[mid] = dict(entry)
            continue
        for key in (
            "name",
            "description",
            "repo_url",
            "essential",
            "source",
            "register_dll",
            "installed_files",
        ):
            if entry.get(key) is not None:
                base[key] = entry[key]
    return list(by_id.values())
