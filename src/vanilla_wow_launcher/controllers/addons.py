"""Addons panel controller.

Owns the ADDONS-panel business logic: the catalog fetch with its offline
fallback, the Interface/AddOns scan with .toc parsing, the cached remote-sha
verification (TTL-gated, cache-only on demand), the sequential
install/update/remove worker, and the one-shot recommended-addons
auto-install for a fresh game folder. Publishes snapshots as AddonsLoaded and
worker outcomes as OperationFinished on the shared EventDispatcher; the Qt
Addons panel renders them. No GUI toolkit.
"""

import os
import shutil
import threading
import time

from ..core import config_store
from ..core.constants import DEFAULT_OUT_DIR
from ..core.errors import describe_install_error
from ..core.helpers import same_git_repo
from ..services import addons
from ..state.events import (
    AddonsLoaded,
    EventDispatcher,
    LogMessage,
    OperationFinished,
    StatusChanged,
)
from ..state.models import AddonError, AddonsState, AddonState

# Footer-label colours — mirror qt_theme's ok / text-dim so the
# toolkit-agnostic footer_state() can render without importing Qt.
C_OK = "#6abf69"
C_TEXT_DIM = "#7a7670"

# Status message when the remote could not be reached to compare SHAs — a
# transient state (rate limit, outage), distinct from a broken addon folder.
C_COULD_NOT_CHECK = "Couldn't check for updates"


class AddonsController:
    """Owns the addons lifecycle; speaks to the UI only through events.

    `get_out_dir` is an optional zero-arg callable returning the current game
    folder (the Qt UI supplies its path field's getter). When omitted the
    controller reads ``out_dir`` from the on-disk config, mirroring the
    UI's default.
    """

    def __init__(self, dispatcher: EventDispatcher, get_out_dir=None):
        self._dispatcher = dispatcher
        self.state = AddonsState()
        self._recommended = set(addons.RECOMMENDED_ADDONS)
        if get_out_dir is None:

            def get_out_dir():
                return config_store.load_config().get(
                    "out_dir", DEFAULT_OUT_DIR
                )

        self._get_out_dir = get_out_dir

    # ── public API ──────────────────────────────────────────────────────────

    @property
    def recommended(self) -> set:
        """Set of recommended addon folder names (★ badge / sort order).
        Grows as the catalog flags addons as recommended during a verify."""
        return self._recommended

    @property
    def git_hosts(self) -> tuple:
        """The allowed git hosts shown in the custom-addon dialog hint."""
        return tuple(addons.ADDON_GIT_HOSTS)

    def is_allowed_git_url(self, url: str) -> bool:
        return addons.is_allowed_git_url(url)

    @property
    def updates_count(self) -> int:
        return self.state.updates_count

    @property
    def installing(self) -> bool:
        return self.state.installing

    def verify(self, force=False, remote_checks=True) -> bool:
        """Scan Interface/AddOns, match against the catalog and check every
        tracked addon's remote commit sha (config-cached). With
        remote_checks=False the scan is guaranteed network-free: shas come
        from the cache only (used for post-install/update refreshes).
        Returns True when a background scan actually started."""
        if self.state.busy:
            return False
        # A recent verify result is already rendered — plain tab switches
        # within the TTL don't need a rescan or a rebuild at all.
        if (
            not force
            and self.state.state == "done"
            and (time.time() - self.state.verified_ts)
            < addons.ADDONS_VERIFY_TTL
        ):
            return False
        client = (self._get_out_dir() or "").strip()
        self.state.busy = True
        self.state.state = "verifying"

        def worker():
            try:
                catalog = addons.addons_catalog(force=force)
            except Exception:
                # offline — fall back to whatever the config still holds
                catalog = addons.catalog_from_cache()

            # Blocked / recommended come from the curated constants plus any
            # flags the catalog (remote or custom) carries.
            blocked = set(addons.BLOCKED_ADDONS)
            recommended = set(addons.RECOMMENDED_ADDONS)
            available = []
            by_name = {}
            for a in catalog:
                name = a.get("name")
                if not name:
                    continue
                if a.get("blocked"):
                    blocked.add(name)
                if a.get("recommended"):
                    recommended.add(name)
                if name in blocked:
                    continue
                rec = {
                    "folder": name,
                    "status": "available",
                    "git": a.get("git"),
                    "branch": a.get("branch"),
                    "ref": a.get("ref"),
                    "toc": a.get("toc") or {},
                    "description": a.get("description"),
                    "error": None,
                }
                available.append(rec)
                by_name[name] = rec

            # Curated recommendations: apply git-URL overrides on top of the
            # catalog, and synthesize entries for recommended addons the
            # catalog doesn't carry (or has renamed). Overridden forks may
            # use a different default branch, so branch/ref are reset.
            for name, override in addons.RECOMMENDED_ADDONS.items():
                rec = by_name.get(name)
                if rec is None:
                    available.append(
                        {
                            "folder": name,
                            "status": "available",
                            "git": override,
                            "branch": None,
                            "ref": None,
                            "toc": {},
                            "description": None,
                            "error": None,
                        }
                    )
                elif not same_git_repo(rec.get("git"), override):
                    rec.update(git=override, branch=None, ref=None)
            self._recommended = recommended

            installed = {}
            records = config_store.load_config().get("addons", {})
            ap = addons.addons_path(client) if client else ""
            if ap and os.path.isdir(ap):
                for name in sorted(os.listdir(ap)):
                    if name.startswith(("Blizzard_", "Turtle_")):
                        continue
                    dirp = os.path.join(ap, name)
                    if not os.path.isdir(dirp):
                        continue
                    rec = {
                        "folder": name,
                        "status": "unknown",
                        "git": None,
                        "branch": None,
                        "ref": None,
                        "toc": {},
                        "description": None,
                        "error": None,
                    }
                    toc_path = os.path.join(dirp, f"{name}.toc")
                    if not os.path.exists(toc_path):
                        rec.update(status="invalid", error="Missing .toc file")
                        installed[name] = rec
                        continue
                    rec["toc"] = addons.read_toc_file(toc_path)
                    avail = next(
                        (a for a in available if a["folder"] == name), None
                    )
                    if avail:
                        rec["description"] = avail["description"]
                    saved = records.get(name)
                    override = addons.RECOMMENDED_ADDONS.get(name)
                    if (
                        saved
                        and saved.get("git")
                        and override
                        and not same_git_repo(saved["git"], override)
                    ):
                        # Installed from a different repo than the curated
                        # fork — offer an update that migrates to the fork.
                        rec.update(
                            git=override,
                            branch=None,
                            ref=None,
                            status="outOfDate",
                        )
                    elif saved and saved.get("git") and avail:
                        # The launcher catalog is authoritative for the addon's
                        # source. A saved repo that differs means the addon was
                        # installed from elsewhere — offer an update that
                        # migrates to the catalog's repo. Even when the repos
                        # match, verify against the catalog's branch/ref (the
                        # saved record may predate a catalog branch change).
                        if not same_git_repo(saved["git"], avail["git"]):
                            rec.update(
                                git=avail["git"],
                                branch=avail["branch"],
                                ref=avail["ref"],
                                status="outOfDate",
                            )
                        else:
                            rec.update(
                                git=avail["git"],
                                branch=avail["branch"],
                                ref=avail["ref"],
                            )
                            if remote_checks:
                                remote = addons.addon_remote_sha(
                                    rec["git"],
                                    rec["branch"],
                                    rec["ref"],
                                    force=force,
                                )
                            else:
                                remote = addons.addon_cached_sha(
                                    rec["git"], rec["branch"], rec["ref"]
                                )
                                if remote is None:
                                    # no cached answer — assume current rather
                                    # than hitting the network
                                    remote = saved.get("sha")
                            if remote is None:
                                rec.update(
                                    status="unknown", error=C_COULD_NOT_CHECK
                                )
                            elif remote == saved.get("sha"):
                                rec["status"] = "upToDate"
                            else:
                                rec["status"] = "outOfDate"
                    elif saved and saved.get("git"):
                        # Installed addon not in the catalog (a custom entry) —
                        # keep tracking the saved source.
                        rec.update(
                            git=saved.get("git"),
                            branch=saved.get("branch"),
                            ref=saved.get("ref"),
                        )
                        if remote_checks:
                            remote = addons.addon_remote_sha(
                                rec["git"],
                                rec["branch"],
                                rec["ref"],
                                force=force,
                            )
                        else:
                            remote = addons.addon_cached_sha(
                                rec["git"], rec["branch"], rec["ref"]
                            )
                            if remote is None:
                                # no cached answer — assume current rather
                                # than hitting the network
                                remote = saved.get("sha")
                        if remote is None:
                            rec.update(
                                status="unknown", error=C_COULD_NOT_CHECK
                            )
                        elif remote == saved.get("sha"):
                            rec["status"] = "upToDate"
                        else:
                            rec["status"] = "outOfDate"
                    elif avail:
                        # Installed addon the launcher has never recorded (a
                        # folder set up elsewhere, or addons pre-dating the
                        # launcher). Adopt it silently: record its catalog
                        # source and resolve its current remote sha as the
                        # baseline, so it's tracked like any launcher-installed
                        # addon from now on — no re-download and no "update"
                        # flag on every pre-existing addon. Offline/rate-limited
                        # resolution leaves it retryable, never "out of date".
                        if remote_checks:
                            remote = addons.addon_remote_sha(
                                avail["git"],
                                avail["branch"],
                                avail["ref"],
                                force=force,
                            )
                        else:
                            remote = addons.addon_cached_sha(
                                avail["git"], avail["branch"], avail["ref"]
                            )
                        rec.update(
                            git=avail["git"],
                            branch=avail["branch"],
                            ref=avail["ref"],
                        )
                        if remote:
                            rec["status"] = "upToDate"
                            config_store.update_config(
                                lambda c, f=name, g=avail["git"], b=avail["branch"], r=avail["ref"], s=remote: (
                                    c.setdefault("addons", {}).__setitem__(
                                        f,
                                        {
                                            "git": g,
                                            "branch": b,
                                            "ref": r,
                                            "sha": s,
                                        },
                                    )
                                )
                            )
                        else:
                            rec.update(
                                status="unknown", error=C_COULD_NOT_CHECK
                            )
                    installed[name] = rec

            # Overlay install failures from this session: the rescan drops
            # them (a failed install leaves no folder on disk), so re-attach
            # errors to the matching available row — or synthesize one for a
            # failed custom addon. Errors for now-installed folders are stale
            # and dropped.
            for folder in [f for f in self.state.errors if f in installed]:
                self.state.errors.pop(folder, None)
            by_name = {a["folder"]: a for a in available}
            for folder, info in self.state.errors.items():
                rec = by_name.get(folder)
                if rec is None:
                    rec = {
                        "folder": folder,
                        "status": "available",
                        "git": info.git,
                        "branch": None,
                        "ref": None,
                        "toc": {},
                        "description": None,
                        "error": None,
                    }
                    available.append(rec)
                rec["error"] = info.error

            self._finish_verify(installed, available)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def apply(self, recs) -> bool:
        """Install/update the given addon records sequentially. Returns True
        when the worker actually started."""
        client = (self._get_out_dir() or "").strip()
        if not client or self.state.busy or not recs:
            return False
        self.state.busy = True
        self.state.installing = True
        for rec in recs:
            rec["status"] = "downloading"
            rec_state = self.state.addons.get(rec["folder"])
            if rec_state is None:
                rec_state = AddonState.from_dict(rec)
                self.state.addons[rec["folder"]] = rec_state
            else:
                # Existing record (this is an update, not a first install) —
                # flip its status in place so the panel's immediate _render()
                # shows "downloading…" instead of the stale outOfDate.
                rec_state.status = "downloading"
                rec_state.error = None
        self._dispatcher.post(StatusChanged("Downloading addons…"))
        threading.Thread(
            target=self._apply_worker, args=(client, list(recs)), daemon=True
        ).start()
        return True

    def update_all(self) -> list:
        """The out-of-date installed addons as record dicts, for apply()."""
        return [
            rec.to_dict()
            for rec in self.state.addons.values()
            if rec.status == "outOfDate"
        ]

    def remove(self, folder: str):
        """Delete an installed addon folder and drop its config record (the
        confirmation dialog lives in the UI layer)."""
        client = (self._get_out_dir() or "").strip()
        if not client or self.state.busy:
            return
        try:
            dirp = os.path.join(addons.addons_path(client), folder)
            if os.path.isdir(dirp):
                shutil.rmtree(dirp)
            config_store.update_config(
                lambda c: c.get("addons", {}).pop(folder, None)
            )
            self._dispatcher.post(
                LogMessage(f"Removed addon {folder}\n", "dim")
            )
        except Exception as e:
            self._dispatcher.post(
                LogMessage(f"Failed to remove addon {folder}: {e}\n", "err")
            )
        self.state.addons.pop(folder, None)
        self.state.errors.pop(folder, None)
        self.state.updates_count = sum(
            1
            for rec in self.state.addons.values()
            if rec.status == "outOfDate"
        )
        self._dispatcher.post(AddonsLoaded(self.state))
        self._dispatcher.post(OperationFinished("addons_verify", True, ""))

    def apply_recommended_addons(self) -> bool:
        """Install every recommended addon (from catalog + constant) not yet
        present. Returns True when an install actually started."""
        if self.state.busy:
            return False
        client = (self._get_out_dir() or "").strip()
        if not client or not os.path.exists(os.path.join(client, "WoW.exe")):
            return False
        # If the recommended set only has the constant (empty by default) and
        # the available list is empty, force a catalog fetch so the recommended
        # set gets populated from the server's catalog.
        if not self.state.available and self._recommended == set(
            addons.RECOMMENDED_ADDONS
        ):
            self._ensure_catalog_loaded()
        ap = addons.addons_path(client)
        recs = []
        for name in sorted(self._recommended):
            if os.path.isdir(os.path.join(ap, name)):
                continue
            rec = next(
                (r for r in self.state.available if r.folder == name), None
            )
            if rec is not None:
                recs.append(dict(rec))
            elif name in addons.RECOMMENDED_ADDONS:
                recs.append(
                    {
                        "folder": name,
                        "status": "available",
                        "git": addons.RECOMMENDED_ADDONS[name],
                        "branch": None,
                        "ref": None,
                        "toc": {},
                        "description": None,
                        "error": None,
                    }
                )
        if not recs:
            return False
        self._dispatcher.post(
            LogMessage("\nInstalling recommended addons...\n", "acct")
        )
        self.apply(recs)
        return True

    def reset(self):
        """Drop the session verify TTL/content (called when the game folder
        changes). The section open/closed state is intentionally preserved."""
        self.state.verified_ts = 0.0
        self.state.state = "idle"
        self.state.addons = {}
        self.state.available = []
        self.state.errors = {}
        self.state.updates_count = 0
        self._recommended = set(addons.RECOMMENDED_ADDONS)

    def invalidate(self):
        """Drop the verify TTL so the next verify() rescans and rebuilds the
        list (used after a catalog reload)."""
        self.state.verified_ts = 0.0
        self.state.state = "idle"

    def footer_state(self) -> tuple[str, str, str]:
        """The ADDONS footer label as (text, fg, cursor)."""
        if self.state.state == "verifying" or self.state.busy:
            return "Checking…", C_TEXT_DIM, "arrow"
        if any(
            rec.status == "outOfDate" for rec in self.state.addons.values()
        ):
            return "Update all", C_OK, "hand2"
        return "Everything up to date", C_TEXT_DIM, "arrow"

    def _ensure_catalog_loaded(self):
        """Force a catalog fetch and populate _recommended and state.available.
        Used by apply_recommended_addons on first run when the catalog hasn't
        been fetched yet."""
        try:
            catalog = addons.addons_catalog(force=True)
        except Exception:
            catalog = addons.catalog_from_cache()

        blocked = set(addons.BLOCKED_ADDONS)
        recommended = set(addons.RECOMMENDED_ADDONS)
        available = []
        by_name = {}
        for a in catalog:
            name = a.get("name")
            if not name:
                continue
            if a.get("blocked"):
                blocked.add(name)
            if a.get("recommended"):
                recommended.add(name)
            if name in blocked:
                continue
            rec = {
                "folder": name,
                "status": "available",
                "git": a.get("git"),
                "branch": a.get("branch"),
                "ref": a.get("ref"),
                "toc": a.get("toc") or {},
                "description": a.get("description"),
                "error": None,
            }
            available.append(rec)
            by_name[name] = rec

        for name, override in addons.RECOMMENDED_ADDONS.items():
            rec = by_name.get(name)
            if rec is None:
                available.append(
                    {
                        "folder": name,
                        "status": "available",
                        "git": override,
                        "branch": None,
                        "ref": None,
                        "toc": {},
                        "description": None,
                        "error": None,
                    }
                )
            elif not same_git_repo(rec.get("git"), override):
                rec.update(git=override, branch=None, ref=None)

        self._recommended = recommended
        self.state.available = [AddonState.from_dict(rec) for rec in available]

    # ── internals ───────────────────────────────────────────────────────────

    def _finish_verify(self, installed: dict, available: list):
        self.state.state = "done"
        self.state.addons = {
            folder: AddonState.from_dict(rec)
            for folder, rec in installed.items()
        }
        self.state.available = [AddonState.from_dict(rec) for rec in available]
        self.state.verified_ts = time.time()
        self.state.busy = False
        self.state.updates_count = sum(
            1 for rec in installed.values() if rec["status"] == "outOfDate"
        )
        self._dispatcher.post(AddonsLoaded(self.state))

    def _apply_worker(self, client: str, recs: list):
        failed = []
        for rec in recs:
            self._dispatcher.post(
                StatusChanged(f"Installing {rec['folder']}…")
            )
            try:
                if not rec.get("git") or not addons.is_allowed_git_url(
                    rec["git"]
                ):
                    raise RuntimeError(
                        "Addon URL is not from an allowed git host"
                    )
                sha = addons.addon_remote_sha(
                    rec["git"],
                    rec.get("branch"),
                    rec.get("ref"),
                    force=True,
                    raise_errors=True,
                )
                if not sha:
                    raise RuntimeError("Could not resolve remote commit")
                addons.install_addon_files(
                    client, rec["folder"], rec["git"], sha
                )
                if rec["folder"] == "pfUI":
                    addons.patch_pfui_default_profile(client)
                record = {
                    "git": rec["git"],
                    "branch": rec.get("branch"),
                    "ref": rec.get("ref"),
                    "sha": sha,
                }
                config_store.update_config(
                    lambda c, f=rec["folder"], r=record: c.setdefault(
                        "addons", {}
                    ).__setitem__(f, r)
                )
                self.state.errors.pop(rec["folder"], None)
                st_rec = self.state.addons.get(rec["folder"])
                if st_rec is not None:
                    # Instant feedback: the row flips to "Up to date" now
                    # instead of staying on the stale status until the
                    # post-install verify reconciles it.
                    st_rec.status = "upToDate"
                    st_rec.error = None
                self._dispatcher.post(AddonsLoaded(self.state))
                self._dispatcher.post(
                    LogMessage(f"  ✓ Addon {rec['folder']} installed.")
                )
            except Exception as e:
                err = describe_install_error(e)
                self._dispatcher.post(
                    LogMessage(f"  ✗ Addon {rec['folder']}: {err}")
                )
                st_rec = self.state.addons.get(rec["folder"])
                if st_rec is not None:
                    st_rec.status = "invalid"
                    st_rec.error = err
                self.state.errors[rec["folder"]] = AddonError(
                    err, rec.get("git")
                )
                failed.append(rec["folder"])

        self.state.busy = False
        self.state.installing = False
        self.state.verified_ts = 0.0  # make the re-verify run
        self._dispatcher.post(
            OperationFinished(
                "addons",
                not failed,
                "" if not failed else f"Failed addons: {', '.join(failed)}",
            )
        )
        if failed:
            # Only rescan when something failed — the overlay attaches the
            # install error to the matching AVAILABLE row. On full success
            # the worker has already marked every just-installed addon
            # upToDate and published the snapshot; running verify() here
            # would re-scan the disk and could flip those rows back to
            # outOfDate (e.g. when RECOMMENDED_ADDONS defines a curated
            # fork with a different git URL than the catalog entry the
            # user just installed).
            self.verify(remote_checks=False)
