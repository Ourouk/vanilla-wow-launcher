"""Addons engine: catalog, Git commit resolution and archive installation.

Addons are installed directly from Git hosts (GitHub, GitLab, Gitea,
Codeberg, and the OctoWoW Gitea at octowow.st) by downloading the repo
archive pinned to a commit SHA — no git client needed. Also hosts the pfUI
"Default" profile patch.

There is no bundled addon list — the ADDONS tab comes entirely from the
addon catalog (launcher-configured or user-set URL) merged with the per-user
custom file, so a distribution decides what it ships.
"""

import io
import json
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from urllib.parse import quote, urlsplit

from ..core import config_store as _config_store
from ..core.config_store import load_config, update_config
from ..core.constants import GITHUB_API, UA
from ..core.errors import describe_net_error
from ..core.filesystem import rmtree_force
from ..core.log_sink import log
from ..core.security_http import secure_urlopen
from . import catalog

# Catalogs refresh at most weekly (shared catalog.CATALOG_TTL); the
# per-URL timestamp lives in the config file.
ADDONS_CATALOG_TTL = catalog.CATALOG_TTL
ADDON_SHA_CACHE_TTL = 3600
ADDONS_VERIFY_TTL = 300  # skip re-verify on tab switches within this

# The per-user custom addon file (a JSON list, one entry per addon). Written
# empty on first use via Settings → Catalog registries.
CUSTOM_FILE_TEMPLATE = "[\n]\n"

# Recommended addon folder names ({folder: git_url}) for the star badge and
# the one-shot auto-install. Empty by default — a distribution may flag
# addons as recommended via its catalog's "recommended" flag instead.
RECOMMENDED_ADDONS: dict = {}

# Never shown in the updater, even when the catalog carries them. Empty by
# default — a distribution may populate it via its catalog's "blocked" flag.
BLOCKED_ADDONS = set()


ADDON_GIT_HOSTS = (
    "github.com",
    "gitlab.com",
    "gitea.com",
    "codeberg.org",
    "octowow.st",
)

ADDON_ZIP_HOSTS = {
    "github.com",
    "codeload.github.com",
    "gitlab.com",
    "gitea.com",
    "codeberg.org",
    "octowow.st",
}


def addons_path(client_dir: str) -> str:
    return os.path.join(client_dir, "Interface", "AddOns")


def is_allowed_git_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ADDON_GIT_HOSTS)


def _custom_validator(entry: dict) -> dict | None:
    """Validate a custom addon entry and enforce the git-host allowlist."""
    cleaned = catalog.validate_addon(entry)
    if cleaned is None:
        return None
    if cleaned["git"] and not is_allowed_git_url(cleaned["git"]):
        return None
    return cleaned


def fetch_addons_catalog(force=False) -> list:
    """The ordered addon catalog: every configured registry URL is fetched
    (or served from its own cache entry) and merged in order — a later
    registry overrides an earlier one by addon folder name. Cached per URL
    for a day ({"addons_catalog_cache": {url: {"timestamp", "catalog"}}}).
    A failed URL falls back to its last cached copy; an unconfigured URL
    list returns an empty list."""
    urls = registry_urls()
    if not urls:
        log("Addon catalog URL is not configured.", "err")
        return []
    now = time.time()
    cache = load_config().get("addons_catalog_cache", {})
    if isinstance(cache, dict) and "catalog" in cache and urls[0] not in cache:
        # Legacy single-URL cache shape → re-key it under the first
        # configured URL so the per-URL lookup keeps working.
        update_config(
            lambda c, u=urls[0]: c.setdefault(
                "addons_catalog_cache", {}
            ).__setitem__(u, c["addons_catalog_cache"])
        )
    merged = []
    for url in urls:
        part = _fetch_url_catalog(url, force, now)
        merged = catalog.merge_addons(merged, part)
    return merged


def _cache_entry(url: str) -> dict:
    """The cached catalog record for a URL, handling the legacy single-URL
    shape (a bare {"timestamp", "catalog"} object). Read through
    `_config_store` so the controller's offline fallback honors test
    monkeypatches of `config_store.load_config`."""
    cache = _config_store.load_config().get("addons_catalog_cache", {}) or {}
    if isinstance(cache, dict) and url in cache:
        return cache[url]
    if isinstance(cache, dict) and "catalog" in cache:
        return cache
    return {}


def _fetch_url_catalog(url: str, force: bool, now: float) -> list:
    """Fetch and cache one catalog URL; on failure serve its cached copy."""
    entry = _cache_entry(url)
    if (
        not force
        and entry.get("catalog") is not None
        and (now - entry.get("timestamp", 0)) < ADDONS_CATALOG_TTL
    ):
        return entry["catalog"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with secure_urlopen(req, timeout=10) as r:
            raw = json.load(r)
    except Exception:
        # offline — serve the last good cached copy for this URL
        return entry.get("catalog") or []
    catalog_list = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        cleaned = _custom_validator(e)
        if cleaned is None:
            continue
        catalog_list.append(cleaned)
    update_config(
        lambda c, u=url, o=catalog_list, t=now: c.setdefault(
            "addons_catalog_cache", {}
        ).__setitem__(u, {"timestamp": t, "catalog": o})
    )
    return catalog_list


def addons_catalog(force=False) -> list:
    """The effective addon catalog: the remote/cached catalogs merged in
    registry order (later wins) and then merged with the per-user custom
    file (custom entries override by folder name)."""
    remote = fetch_addons_catalog(force=force)
    return catalog.merge_addons(
        remote, catalog.load_custom("addons", _custom_validator)
    )


def catalog_from_cache() -> list:
    """The cached catalogs merged with the custom file, without any network —
    used as the offline fallback when a fresh fetch fails."""
    cache = _config_store.load_config().get("addons_catalog_cache", {}) or {}
    urls = registry_urls()
    parts = []
    if urls:
        for url in urls:
            entry = _cache_entry(url)
            if entry.get("catalog"):
                parts.append(entry["catalog"])
    elif isinstance(cache, dict) and cache.get("catalog"):
        parts.append(cache["catalog"])
    merged = []
    for part in parts:
        merged = catalog.merge_addons(merged, part)
    return catalog.merge_addons(
        merged, catalog.load_custom("addons", _custom_validator)
    )


def catalog_last_updated() -> float | None:
    """The newest per-URL catalog fetch timestamp (epoch), or None when no
    catalog was ever fetched. Network-free."""
    cache = _config_store.load_config().get("addons_catalog_cache", {}) or {}
    stamps = [
        e.get("timestamp")
        for e in cache.values()
        if isinstance(e, dict) and isinstance(e.get("timestamp"), (int, float))
    ]
    return max(stamps) if stamps else None


def registry_url() -> str:
    """The addon catalog URL shown in Settings: a per-user override, else the
    first launcher-configured URL, else ''."""
    override = catalog.get_registry_url("addons")
    if override:
        return override
    urls = addons_registry_default_urls()
    return urls[0] if urls else ""


def registry_urls() -> list[str]:
    """The ordered list of addon catalog URLs in effect: a per-user override
    (Settings) replaces the whole list with itself; otherwise the
    launcher-configured list is used."""
    override = catalog.get_registry_url("addons")
    if override:
        return [override]
    return addons_registry_default_urls()


def addons_registry_default_url() -> str:
    """The launcher-configured addon catalog URL ('' when not configured)."""
    urls = addons_registry_default_urls()
    return urls[0] if urls else ""


def addons_registry_default_urls() -> list[str]:
    """The launcher-configured addon catalog URLs, in override order ('' list
    when not configured)."""
    from ..core import launcher

    return launcher.addons_registry_urls()


def set_registry_url(url: str) -> str | None:
    """Validate and store a per-user catalog URL override (empty clears it);
    returns an error string or None on success."""
    return catalog.set_registry_url("addons", url)


def reset_registry_url():
    """Drop the per-user override so the launcher-configured URL is used."""
    catalog.reset_registry_url("addons")


def custom_file() -> str:
    """Path of the per-user custom addon JSON file."""
    return catalog.custom_file("addons")


def open_custom_file() -> bool:
    """Create the custom addon file (with the template) when missing."""
    return catalog.write_custom_template("addons", CUSTOM_FILE_TEMPLATE)


def clear_custom_file() -> bool:
    """Delete the custom addon file. True when something was removed."""
    return catalog.clear_custom("addons")


def read_toc_file(path: str) -> dict:
    """Parse '## Key: Value' metadata lines from a WoW addon .toc file."""
    toc = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return toc
    if content.startswith("\ufeff"):  # strip UTF-8 BOM
        content = content[1:]
    for line in content.splitlines():
        if not line.startswith("## "):
            continue
        key, sep, value = line[3:].partition(":")
        if sep:
            toc[key.strip()] = value.strip()
    return toc


def _git_parts(git_url: str):
    """→ (kind, repo_url, owner, repo, api_base); kind ∈ github/gitlab/gitea.
    Handles path prefixes like <host>/git/<owner>/<repo>."""
    parts = urlsplit(git_url)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        raise ValueError(f"Unsupported git URL: {git_url}")
    owner, repo = segs[-2], segs[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    prefix = "/".join(segs[:-2])
    origin = f"https://{parts.netloc}"
    repo_url = origin + (f"/{prefix}" if prefix else "") + f"/{owner}/{repo}"
    if host == "github.com" or host.endswith(".github.com"):
        return "github", repo_url, owner, repo, GITHUB_API
    if host == "gitlab.com" or host.endswith(".gitlab.com"):
        return "gitlab", repo_url, owner, repo, f"{origin}/api/v4"
    api = origin + (f"/{prefix}" if prefix else "") + "/api/v1"
    return "gitea", repo_url, owner, repo, api


def _api_json(url: str, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with secure_urlopen(req, timeout=timeout) as r:
        return json.load(r)


def addon_remote_sha(
    git_url: str, branch=None, ref=None, force=False, raise_errors=False
) -> str | None:
    """Latest commit sha of a repo's branch (or pinned ref), cached in the
    config file so repeated verifies don't burn API quota. Returns None on
    failure — or raises with a readable cause when raise_errors is set.

    The git hosts' REST APIs are rate-limited from shared IPs (GitHub in
    particular), so when the API path fails the call falls back to the
    smart-HTTP endpoint via ``git ls-remote`` — same result, no API quota.
    Git itself remains optional: without it (or without a reachable remote)
    the cached sha / None path still applies, so the packaged launcher never
    hard-depends on a Git executable.
    """
    # Allowlist gate: never open an API connection nor spawn `git` for a
    # host outside ADDON_GIT_HOSTS, whatever a catalog entry carries.
    if not is_allowed_git_url(git_url):
        return None
    key = f"{git_url}#{ref or branch or ''}"
    now = time.time()
    if not force:
        entry = load_config().get("addon_sha_cache", {}).get(key)
        if entry and (now - entry.get("timestamp", 0)) < ADDON_SHA_CACHE_TTL:
            return entry.get("sha")

    kind, _repo_url, owner, repo, api = _git_parts(git_url)
    pin = ref or branch  # explicit branch/ref when the caller has one
    sha = None
    api_error = None
    try:
        if kind == "github":
            if pin:
                sha = _api_json(
                    f"{api}/repos/{owner}/{repo}/commits/{pin}"
                ).get("sha")
            else:
                lst = _api_json(
                    f"{api}/repos/{owner}/{repo}/commits?per_page=1"
                )
                sha = lst[0].get("sha") if lst else None
        elif kind == "gitlab":
            proj = quote(f"{owner}/{repo}", safe="")
            if pin:
                sha = _api_json(
                    f"{api}/projects/{proj}/repository/commits/"
                    f"{quote(pin, safe='')}"
                ).get("id")
            else:
                lst = _api_json(
                    f"{api}/projects/{proj}/repository/commits?per_page=1"
                )
                sha = lst[0].get("id") if lst else None
        else:  # gitea / codeberg
            q = f"?sha={pin}&limit=1" if pin else "?limit=1"
            lst = _api_json(f"{api}/repos/{owner}/{repo}/commits{q}")
            sha = lst[0].get("sha") if lst else None
    except Exception as e:
        api_error = e
        sha = None

    if not sha:
        # API failed (rate limit, outage) or returned nothing — fall back to
        # `git ls-remote` against the repo's smart-HTTP endpoint.
        sha = _git_ls_remote_sha(git_url, pin)

    if sha is None and raise_errors:
        cause = api_error or RuntimeError(
            f"could not resolve remote commit for {git_url}"
        )
        raise RuntimeError(describe_net_error(cause)) from cause

    if sha is None and not raise_errors:
        # Not an error for the caller (returns None), but worth a diagnostic
        # line so a wall of "Failed to verify" isn't a silent mystery.
        api_cause = (
            describe_net_error(api_error)
            if api_error
            else "API returned no commits"
        )
        log(
            f"  Could not resolve remote commit for {git_url} — {api_cause}; "
            f"git ls-remote fallback also failed.",
            "dim",
        )

    if sha:
        update_config(
            lambda c: c.setdefault("addon_sha_cache", {}).__setitem__(
                key, {"timestamp": now, "sha": sha}
            )
        )
    return sha


def _git_ls_remote_sha(git_url: str, pin: str | None) -> str | None:
    """Latest commit sha via ``git ls-remote`` — the smart-HTTP fallback that
    sidesteps the git hosts' REST API quota. No clone, no worktree mutation.

    Returns None when git is missing, the command fails/times out, or the
    requested ref can't be resolved — never raises (a broken git must not
    take down the addon scan).
    """
    args = ["git", "ls-remote", git_url]
    args.append(pin if pin else "HEAD")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=15, env=env
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    lines = [line.partition("\t") for line in proc.stdout.splitlines()]
    if pin:
        exact = [
            sha
            for sha, _, ref in lines
            if ref in (f"refs/heads/{pin}", f"refs/tags/{pin}")
        ]
        if exact:
            return exact[0]
        # Loose match (e.g. a short branch/ref name resolving via remotes).
        loose = [sha for sha, _, ref in lines if ref.endswith("/" + pin)]
        return loose[0] if loose else None
    for sha, _, ref in lines:
        if ref == "HEAD":
            return sha
    return None


def addon_cached_sha(git_url: str, branch=None, ref=None):
    """Cached remote sha regardless of age — never touches the network."""
    key = f"{git_url}#{ref or branch or ''}"
    entry = load_config().get("addon_sha_cache", {}).get(key)
    return entry.get("sha") if entry else None


def addon_zip_url(git_url: str, sha: str) -> str:
    kind, repo_url, _owner, repo, _api = _git_parts(git_url)
    if kind == "gitlab":
        return f"{repo_url}/-/archive/{sha}/{repo}-{sha}.zip"
    return f"{repo_url}/archive/{sha}.zip"


def install_addon_files(client_dir: str, folder: str, git_url: str, sha: str):
    """Download the repo archive at `sha` and unpack it into
    Interface/AddOns/<folder>, atomically replacing any existing copy."""
    url = addon_zip_url(git_url, sha)
    log(f"  Downloading {folder} @ {sha[:10]}…")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with secure_urlopen(req, timeout=120, allowed_hosts=ADDON_ZIP_HOSTS) as r:
        data = r.read()

    dest_root = os.path.join(addons_path(client_dir), folder)
    tmp_root = dest_root + ".tmp_install"
    tmp_abs = os.path.abspath(tmp_root)
    if os.path.isdir(tmp_root):
        rmtree_force(tmp_root)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Strip the archive's top-level "<repo>-<sha>/" directory and
                # normalise separators (a zip entry may use "/" or "\").
                parts = [
                    p
                    for p in info.filename.replace("\\", "/").split("/")[1:]
                    if p not in ("", ".")
                ]
                if not parts or ".." in parts:
                    continue
                target = os.path.join(tmp_root, *parts)
                # Defence in depth: never write outside the target folder even
                # if the guards above are somehow bypassed.
                if not os.path.abspath(target).startswith(tmp_abs + os.sep):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if os.path.isdir(dest_root):
            rmtree_force(dest_root)
        os.replace(tmp_root, dest_root)
    except BaseException:
        # Never leave a half-written ".tmp_install" behind on failure
        if os.path.isdir(tmp_root):
            try:
                rmtree_force(tmp_root)
            except Exception:
                pass
        raise
    log(f"  Installed addon {folder}")


# ── pfUI "Default" profile patch ─────────────────────────────────────────────
# pfUI ships a set of built-in design profiles. After every pfUI install/update
# we add a curated "Default" profile and make it the firstrun default. Because
# an update overwrites pfUI's files, the patch is re-applied each time and is
# idempotent (marked blocks are replaced, not duplicated).

# The curated profile (JSON captured from a configured pfUI, profile renamed to
# "Default"). Loaded as a Python dict and emitted as a Lua table at patch time.
PFUI_DEFAULT_PROFILE = json.loads(r"""
{"appearance":{"border":{"default":"-1"},"castbar":{"castbarcolor":"1,0.796,0.251,0.8"},"cd":{"debuffs":"1","font":"Interface\\AddOns\\pfUI\\fonts\\Myriad-Pro.ttf","milliseconds":"0"},"infight":{"health":"0"},"minimap":{"arrowscale":"2"}},"buffs":{"hidelist":"","showoverflow":"1","showspillover":"1"},"castbar":{"focus":{"showicon":"1","showtimer":"0"},"player":{"hide_blizz":"0","hide_pfui":"1","showtimer":"0"},"target":{"showicon":"1","showtimer":"0"}},"character":{"inventory":{"durability":"0"},"reputation":{"repRequired":"0"}},"disabled":{"actionbar":"1","addonbuttons":"0","addoncompat":"0","addons":"0","afkcam":"0","autoshift":"0","autovendor":"0","bags":"1","bgscore":"0","bubbles":"1","buff":"1","buffwatch":"0","castbar":"0","chat":"1","chatcopy":"0","combopoints":"0","cooldown":"0","custom":"0","easteregg":"0","energytick":"0","eqcompare":"0","equipmentmanager":"0","farmmode":"0","feigndeath":"0","firstrun":"0","focus":"0","gm":"0","group":"0","gryphons":"0","hdgraphic":"0","hoverbind":"0","hunterbar":"0","infight":"0","innervatecall":"0","itemclick":"0","itemcount":"1","loot":"1","macrotweak":"0","map":"0","mapcolors":"0","mapreveal":"0","marktracking":"0","minimap":"0","mirrortimers":"0","mouseover":"0","nameplates":"0","nampower":"0","panel":"0","pet":"0","pettarget":"0","pixelperfect":"0","player":"0","questitem":"0","raid":"0","roll":"1","screenshot":"0","sellvalue":"0","share":"0","skin":"0","skin_Auctionhouse":"0","skin_Barbershop":"0","skin_Battlefield":"0","skin_Battlefield Minimap":"0","skin_Battlefield Score":"0","skin_Books":"0","skin_Character":"0","skin_Coin Pickup":"0","skin_Color Picker":"0","skin_Dress Up Frame":"0","skin_Everlook Broadcasting":"0","skin_Flightmaster":"1","skin_Friends":"1","skin_GM Survey":"0","skin_Game Menu":"0","skin_Gossip and Quest":"1","skin_Guild Registrar":"0","skin_Guild Tabard":"0","skin_Help":"0","skin_Inspect":"0","skin_KeyBindings":"0","skin_Macro":"0","skin_Mailbox":"1","skin_Merchant":"1","skin_Opacity":"0","skin_Outline":"0","skin_Player":"1","skin_Quest":"1","skin_Quest Tracker":"0","skin_Reputation":"1","skin_Social":"0","skin_TradeSkill":"1","skin_Trainer":"1","skin_Tutorials":"0","skin_Unitframe":"1","timerbar":"0","tooltip":"0","tracker":"0","unitframes":"0"},"equipment":{"durability":"0"},"nameplates":{"clickthrough":"0","hidelist":"","showonlyname":"0"},"panels":{"fpsloc":"Right","hidelist":"","lootannounce":"0","mouseover":"0"},"reputation":{"repRequired":"0"},"skins":{"dark":"1","font":"Interface\\AddOns\\pfUI\\fonts\\Myriad-Pro.ttf","fontscale":"1"},"tooltips":{"hideincombat":"0","hidelist":"","mousefollow":"0"},"unitframes":{"clickthrough":"0","hidelist":"","petbars":"1","showstagger":"0"}}
""")


def _lua_value(v, indent: int = 0) -> str:
    """Serialize a JSON-derived value to a pfUI-style Lua literal."""
    if isinstance(v, dict):
        pad, cpad = " " * (indent + 2), " " * indent
        items = "".join(
            f'{pad}["{k}"] = {_lua_value(val, indent + 2)},\n'
            for k, val in v.items()
        )
        return "{\n" + items + cpad + "}"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


_PFUI_MARK_BEGIN = "-- OCTO_UPDATER_DEFAULT_PROFILE_BEGIN"
_PFUI_MARK_END = "-- OCTO_UPDATER_DEFAULT_PROFILE_END"
_PFUI_CHAT_BEGIN = "-- OCTO_UPDATER_CHAT_SKIP_BEGIN"
_PFUI_CHAT_END = "-- OCTO_UPDATER_CHAT_SKIP_END"

# Strips any Vanilla WoW Launcher injected block, regardless of which marker pair.
_PFUI_STRIP_RE = (
    r"[ \t]*-- OCTO_UPDATER_[A-Z_]+?_BEGIN.*?-- OCTO_UPDATER_[A-Z_]+?_END\n?"
)


def patch_pfui_default_profile(client_dir: str):
    """Add the curated 'Default' profile to a freshly installed/updated pfUI
    and make it the firstrun default. Idempotent; degrades gracefully if
    pfUI's file layout has changed."""
    import re

    base = os.path.join(addons_path(client_dir), "pfUI")
    profiles_lua = os.path.join(base, "env", "profiles.lua")
    firstrun_lua = os.path.join(base, "modules", "firstrun.lua")
    if not os.path.exists(profiles_lua):
        return

    # 1) profiles.lua — append (or replace) a marked block defining Default.
    block = (
        f"{_PFUI_MARK_BEGIN}\n"
        f"local octo_default = {_lua_value(PFUI_DEFAULT_PROFILE)}\n"
        f'pfUI_profiles["Default"] = octo_default\n'
        f"{_PFUI_MARK_END}\n"
    )
    try:
        with open(profiles_lua, encoding="utf-8", errors="replace") as f:
            txt = f.read()
        txt = re.sub(
            re.escape(_PFUI_MARK_BEGIN)
            + r".*?"
            + re.escape(_PFUI_MARK_END)
            + r"\n?",
            "",
            txt,
            flags=re.S,
        )
        with open(profiles_lua, "w", encoding="utf-8") as f:
            f.write(txt.rstrip() + "\n\n" + block)
        log("  pfUI: 'Default' profile installed.")
    except OSError as e:
        log(f"  pfUI: could not patch profiles.lua ({e})")
        return

    # 2) pfUI.lua — use 'Default' (not 'Modern') as the fresh-install config,
    #    so the very first login already lands in the Default profile.
    pfui_lua = os.path.join(base, "pfUI.lua")
    if os.path.exists(pfui_lua):
        try:
            with open(pfui_lua, encoding="utf-8", errors="replace") as f:
                pf = f.read()
            old = 'CopyTable(pfUI_profiles["Modern"]) or {}'
            if old in pf:
                pf = pf.replace(
                    old, 'CopyTable(pfUI_profiles["Default"]) or {}', 1
                )
                with open(pfui_lua, "w", encoding="utf-8") as f:
                    f.write(pf)
                log("  pfUI: 'Default' set as the fresh-install profile.")
        except OSError as e:
            log(f"  pfUI: could not patch pfUI.lua ({e})")

    # 3) firstrun.lua — add a 'Default' button, make it the fallback profile,
    #    and skip the chat wizard steps whenever the chat module is disabled.
    if not os.path.exists(firstrun_lua):
        return
    try:
        with open(firstrun_lua, encoding="utf-8", errors="replace") as f:
            fr = f.read()

        # Remove any previous injections (idempotent re-apply after updates).
        fr = re.sub(_PFUI_STRIP_RE, "", fr, flags=re.S)

        # When the chat module is disabled (e.g. the "Default" profile), the
        # chat firstrun steps can't apply anything, so pre-mark them done to
        # keep them from showing. Injected right after the step table is made.
        chat_skip = (
            f"  {_PFUI_CHAT_BEGIN}\n"
            "  if pfUI_config and pfUI_config.disabled"
            ' and pfUI_config.disabled.chat == "1" then\n'
            "    pfUI_init = pfUI_init or {}\n"
            '    pfUI_init["chat_right"] = true\n'
            '    pfUI_init["chat_position"] = true\n'
            '    pfUI_init["chat_channels"] = true\n'
            "  end\n"
            f"  {_PFUI_CHAT_END}\n"
        )
        chat_anchor = "  pfUI.firstrun.steps = {}\n"
        if chat_anchor in fr:
            fr = fr.replace(chat_anchor, chat_anchor + chat_skip, 1)

        # Insert a Default button just before the built-in "Modern" button.
        button = (
            f"    {_PFUI_MARK_BEGIN}\n"
            '    f.Default = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")\n'
            "    f.Default:SetWidth(250)\n"
            "    f.Default:SetHeight(20)\n"
            '    f.Default:SetPoint("BOTTOM", 0, 125)\n'
            "    f.Default:SetTextColor(1,1,1)\n"
            '    f.Default:SetText("Default (recommended)")\n'
            '    f.Default:SetScript("OnClick", function()\n'
            '      _G["pfUI_config"] = CopyTable(pfUI_profiles["Default"])\n'
            '      pfUI_init.selected_profile = "Default"\n'
            "      pfUI:LoadConfig()\n"
            "      ReloadUI()\n"
            "    end)\n"
            "    SkinButton(f.Default)\n"
            f"    {_PFUI_MARK_END}\n\n"
        )
        anchor = '    f.Modern = CreateFrame("Button"'
        if anchor in fr:
            fr = fr.replace(anchor, button + anchor, 1)

        # Make Default the profile used when the user doesn't pick one.
        fr = fr.replace(
            'pfUI_init.selected_profile or "Modern"',
            'pfUI_init.selected_profile or "Default"',
        )

        with open(firstrun_lua, "w", encoding="utf-8") as f:
            f.write(fr)
        log("  pfUI: 'Default' added to the firstrun profile picker.")
    except OSError as e:
        log(f"  pfUI: could not patch firstrun.lua ({e})")
