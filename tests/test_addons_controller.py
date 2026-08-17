"""Unit tests for the addons controller (addons_controller).

No Tk involved: the controller is driven directly and its effects are read
from the shared EventDispatcher and its AddonsState. Backends (catalog fetch,
remote/cached sha lookup, install helpers, .toc parsing, config store, folder
deletion) are swapped for fakes via monkeypatch so nothing touches the
network or the real filesystem.
"""

import os
import threading
import time
from unittest.mock import Mock

import pytest

import vanilla_wow_launcher.controllers.addons as ac
from vanilla_wow_launcher.controllers.addons import (
    C_OK,
    C_TEXT_DIM,
    AddonsController,
)
from vanilla_wow_launcher.state.events import (
    AddonsLoaded,
    EventDispatcher,
    LogMessage,
    OperationFinished,
    StatusChanged,
)
from vanilla_wow_launcher.state.models import AddonError, AddonState


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    state = {"out_dir": str(tmp_path), "addons": {}}
    monkeypatch.setattr(ac.config_store, "load_config", lambda: state)
    monkeypatch.setattr(
        ac.config_store,
        "update_config",
        lambda mutator: (mutator(state), state)[1],
    )
    return state


@pytest.fixture
def backends(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ac.addons, "fetch_addons_catalog", lambda force=False: []
    )
    # Isolate the per-user custom file so a real user's config can't leak in.
    monkeypatch.setattr(
        ac.addons.catalog,
        "custom_file",
        lambda kind: str(tmp_path / f"{kind}_custom.json"),
    )
    monkeypatch.setattr(
        ac.addons,
        "addon_remote_sha",
        lambda git, branch=None, ref=None, force=False, raise_errors=False: (
            "REMOTE"
        ),
    )
    monkeypatch.setattr(
        ac.addons,
        "addon_cached_sha",
        lambda git, branch=None, ref=None: "REMOTE",
    )
    monkeypatch.setattr(
        ac.addons, "install_addon_files", lambda client, folder, git, sha: None
    )
    monkeypatch.setattr(
        ac.addons, "patch_pfui_default_profile", lambda client: None
    )
    monkeypatch.setattr(
        ac.addons, "read_toc_file", lambda path: {"Title": "X"}
    )
    monkeypatch.setattr(
        ac.addons,
        "addons_path",
        lambda client: os.path.join(client, "Interface", "AddOns"),
    )
    monkeypatch.setattr(ac.addons, "is_allowed_git_url", lambda url: True)
    monkeypatch.setattr(ac.shutil, "rmtree", lambda path: None)


@pytest.fixture
def controller(cfg, backends):
    return AddonsController(EventDispatcher())


def _drain_for(dispatcher, predicate, timeout=2.0):
    """Drain until an event matching `predicate` arrives; return everything
    drained along the way (assertion failure on timeout)."""
    deadline = time.monotonic() + timeout
    collected = []
    while True:
        collected.extend(dispatcher.drain())
        if any(predicate(e) for e in collected):
            return collected
        if time.monotonic() > deadline:
            raise AssertionError("expected event never arrived")
        time.sleep(0.005)


def _install_folder(client, name):
    """Create a real installed-addon folder with a .toc on disk."""
    addon_dir = os.path.join(client, "Interface", "AddOns", name)
    os.makedirs(addon_dir)
    with open(
        os.path.join(addon_dir, f"{name}.toc"), "w", encoding="utf-8"
    ) as f:
        f.write(f"## Title: {name}\n")
    return addon_dir


# ── verify ──────────────────────────────────────────────────────────────


def test_verify_scans_and_posts_addons_loaded(controller, cfg, tmp_path):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"]["Foo"] = {
        "git": "https://github.com/x/y",
        "branch": None,
        "ref": None,
        "sha": "REMOTE",
    }

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    assert controller.state.state == "done"

    assert controller.state.busy is False
    assert controller.state.verified_ts > 0
    assert controller.state.addons["Foo"].status == "upToDate"
    assert controller.state.addons["Foo"].git == "https://github.com/x/y"
    # The empty catalog still yields the curated recommendations (and Foo is
    # installed, so it must not also show as available).
    assert {a.folder for a in controller.state.available} == set(
        ac.addons.RECOMMENDED_ADDONS
    )
    assert controller.updates_count == 0


def test_ensure_catalog_loaded_stores_addon_state_objects(
    controller, monkeypatch
):
    monkeypatch.setattr(
        ac.addons,
        "addons_catalog",
        lambda force=False: [
            {
                "name": "Example",
                "git": "https://github.com/example/addon",
                "recommended": True,
            }
        ],
    )

    controller._ensure_catalog_loaded()

    assert all(
        isinstance(record, AddonState) for record in controller.state.available
    )
    assert any(
        record.folder == "Example" for record in controller.state.available
    )


def test_verify_marks_unreachable_addon_unknown(
    controller, cfg, tmp_path, monkeypatch
):
    """A remote that can't be reached to compare SHAs → status 'unknown'
    (retryable), not 'invalid' — and it doesn't count as an update."""
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"]["Foo"] = {
        "git": "https://github.com/x/y",
        "branch": None,
        "ref": None,
        "sha": "REMOTE",
    }
    monkeypatch.setattr(ac.addons, "addon_remote_sha", lambda *a, **k: None)

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    rec = controller.state.addons["Foo"]
    assert rec.status == "unknown"
    assert rec.error == "Couldn't check for updates"
    assert controller.updates_count == 0
    assert [r.folder for r in controller.update_all()] == []


def test_verify_adopts_untracked_installed_catalog_addon(
    controller, cfg, tmp_path, monkeypatch
):
    """An installed addon the launcher has never recorded but that is in the
    catalog is adopted silently: recorded with its current remote sha and
    marked up to date — no re-download, no 'update' flag on every
    pre-existing addon (the /srv/games first-scan case)."""
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"] = {}  # nothing tracked yet
    cfg["addons_catalog_cache"] = {}
    monkeypatch.setattr(
        ac.addons,
        "addons_catalog",
        lambda force=False: [
            {
                "name": "Foo",
                "git": "https://github.com/catalog/Foo",
                "branch": "main",
                "ref": None,
            }
        ],
    )
    seen = {}
    monkeypatch.setattr(
        ac.addons,
        "addon_remote_sha",
        lambda git, branch=None, ref=None, force=False, raise_errors=False: (
            seen.update(git=git, branch=branch) or "LIVE"
        ),
    )

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    rec = controller.state.addons["Foo"]
    assert rec.status == "upToDate"
    assert rec.git == "https://github.com/catalog/Foo"
    assert seen == {"git": "https://github.com/catalog/Foo", "branch": "main"}
    # The adoption wrote a tracking record so later verifies track it.
    assert cfg["addons"]["Foo"] == {
        "git": "https://github.com/catalog/Foo",
        "branch": "main",
        "ref": None,
        "sha": "LIVE",
    }
    assert controller.updates_count == 0


def test_verify_adoption_unresolvable_is_unknown(
    controller, cfg, tmp_path, monkeypatch
):
    """When the remote can't be resolved during adoption the addon is
    'unknown' (retryable), never flagged for update."""
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"] = {}
    monkeypatch.setattr(
        ac.addons,
        "addons_catalog",
        lambda force=False: [
            {
                "name": "Foo",
                "git": "https://github.com/catalog/Foo",
                "branch": "main",
                "ref": None,
            }
        ],
    )
    monkeypatch.setattr(ac.addons, "addon_remote_sha", lambda *a, **k: None)

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    rec = controller.state.addons["Foo"]
    assert rec.status == "unknown"
    assert rec.error == "Couldn't check for updates"
    # Nothing recorded — a later retry can adopt it.
    assert "Foo" not in cfg.get("addons", {})
    assert controller.updates_count == 0


def test_verify_offline_falls_back_to_cached_catalog(
    controller, cfg, monkeypatch
):
    cfg["addons_catalog_cache"] = {
        "catalog": [{"name": "Foo", "git": "https://github.com/x/y"}]
    }
    monkeypatch.setattr(
        ac.addons,
        "fetch_addons_catalog",
        lambda force=False: (_ for _ in ()).throw(ConnectionError("offline")),
    )

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    assert any(a.folder == "Foo" for a in controller.state.available)


def test_verify_ttl_skips_second_unless_force(controller, cfg, tmp_path):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"]["Foo"] = {
        "git": "https://github.com/x/y",
        "branch": None,
        "ref": None,
        "sha": "REMOTE",
    }

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))
    # Within the TTL a plain verify is a no-op; force() bypasses it.
    assert controller.verify() is False
    assert controller.verify(force=True) is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))


def test_verify_skips_when_busy(controller, cfg, monkeypatch):
    controller.state.busy = True
    assert controller.verify() is False
    assert controller._dispatcher.drain() == []


def test_verify_remote_checks_false_never_calls_remote_sha(
    controller, cfg, tmp_path, monkeypatch
):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"]["Foo"] = {
        "git": "https://github.com/x/y",
        "branch": None,
        "ref": None,
        "sha": "CACHED",
    }
    calls = []
    monkeypatch.setattr(
        ac.addons,
        "addon_remote_sha",
        lambda *a, **k: calls.append(1) or "LIVE",
    )
    monkeypatch.setattr(
        ac.addons,
        "addon_cached_sha",
        lambda git, branch=None, ref=None: "CACHED",
    )

    assert controller.verify(remote_checks=False) is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    assert calls == []
    # The cached sha matches the saved one — current without any API call.
    assert controller.state.addons["Foo"].status == "upToDate"


def test_verify_uses_catalog_source_when_saved_differs(
    controller, cfg, tmp_path, monkeypatch
):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"]["Foo"] = {
        "git": "https://github.com/old/Foo",
        "branch": None,
        "ref": None,
        "sha": "OLD",
    }
    monkeypatch.setattr(
        ac.addons,
        "addons_catalog",
        lambda force=False: [
            {
                "name": "Foo",
                "git": "https://github.com/launcher/Foo",
                "branch": "main",
                "ref": None,
            }
        ],
    )
    calls = []
    monkeypatch.setattr(
        ac.addons,
        "addon_remote_sha",
        lambda *a, **k: calls.append(a) or "LIVE",
    )

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    # The launcher catalog wins: a source conflict surfaces as an update
    # that migrates to the catalog repo — no remote check against the old one.
    assert controller.state.addons["Foo"].status == "outOfDate"
    assert (
        controller.state.addons["Foo"].git == "https://github.com/launcher/Foo"
    )
    assert controller.state.addons["Foo"].branch == "main"
    assert calls == []


def test_verify_checks_catalog_branch_when_repos_match(
    controller, cfg, tmp_path, monkeypatch
):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["addons"]["Foo"] = {
        "git": "https://github.com/launcher/Foo",
        "branch": None,
        "ref": None,
        "sha": "CURRENT",
    }
    monkeypatch.setattr(
        ac.addons,
        "addons_catalog",
        lambda force=False: [
            {
                "name": "Foo",
                "git": "https://github.com/launcher/Foo",
                "branch": "main",
                "ref": None,
            }
        ],
    )
    seen = {}
    monkeypatch.setattr(
        ac.addons,
        "addon_remote_sha",
        lambda git, branch=None, ref=None, force=False, raise_errors=False: (
            seen.update(git=git, branch=branch) or "LIVE"
        ),
    )

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    # Same repo but a different configured branch: verify uses the catalog's
    # branch, so the new branch's sha surfaces as an update.
    assert seen == {"git": "https://github.com/launcher/Foo", "branch": "main"}
    assert controller.state.addons["Foo"].status == "outOfDate"
    assert controller.state.addons["Foo"].branch == "main"


def test_verify_honors_catalog_recommended_and_blocked(
    controller, cfg, monkeypatch
):
    monkeypatch.setattr(
        ac.addons,
        "addons_catalog",
        lambda force=False: [
            {
                "name": "Star",
                "git": "https://github.com/x/Star",
                "recommended": True,
                "blocked": False,
            },
            {
                "name": "Hidden",
                "git": "https://github.com/x/Hidden",
                "recommended": False,
                "blocked": True,
            },
        ],
    )

    assert controller.verify() is True
    _drain_for(controller._dispatcher, lambda e: isinstance(e, AddonsLoaded))

    folders = {a.folder for a in controller.state.available}
    assert "Star" in folders
    assert "Hidden" not in folders
    # Flagged recommendations join the curated ones for the star/sort order.
    assert "Star" in controller.recommended
    assert set(ac.addons.RECOMMENDED_ADDONS) <= controller.recommended


# ── apply ───────────────────────────────────────────────────────────────


def test_apply_success_records_and_posts_finished(controller, cfg, tmp_path):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["out_dir"] = client
    cfg["addons"] = {}
    rec = {
        "folder": "Foo",
        "status": "available",
        "git": "https://github.com/a/b",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }

    assert controller.apply([rec]) is True

    collected = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert OperationFinished("addons", True, "") in collected
    assert any(
        isinstance(e, StatusChanged) and e.text == "Downloading addons…"
        for e in collected
    )
    assert any(
        isinstance(e, LogMessage) and "✓ Addon Foo installed." in e.text
        for e in collected
    )
    assert cfg["addons"]["Foo"] == {
        "git": "https://github.com/a/b",
        "branch": None,
        "ref": None,
        "sha": "REMOTE",
    }
    assert controller.state.errors == {}

    # The worker resolved/cached the sha, marked the addon up to date and
    # posted the snapshot before finishing — on full success we skip the
    # follow-up verify, so the AddonsLoaded is already in the drain.
    assert any(isinstance(e, AddonsLoaded) for e in collected)
    assert controller.state.addons["Foo"].status == "upToDate"
    assert controller.state.installing is False
    assert controller.state.busy is False


def test_apply_success_does_not_run_post_install_verify(
    controller, cfg, tmp_path, monkeypatch
):
    """On full success the worker has already marked the just-installed
    addon upToDate; running verify() afterward can flip it back to
    outOfDate (fork migration, repo authoritative mismatch)."""
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["out_dir"] = client
    cfg["addons"] = {}
    rec = {
        "folder": "Foo",
        "status": "available",
        "git": "https://github.com/a/b",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }
    verify_mock = Mock(return_value=False)
    monkeypatch.setattr(controller, "verify", verify_mock)

    assert controller.apply([rec]) is True
    _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )

    verify_mock.assert_not_called()


def test_apply_failure_still_runs_post_install_verify(
    controller, cfg, monkeypatch
):
    """Failures need the verify so the error is overlaid on the
    AVAILABLE row."""
    monkeypatch.setattr(
        ac.addons,
        "install_addon_files",
        lambda client, folder, git, sha: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    rec = {
        "folder": "Foo",
        "status": "available",
        "git": "https://github.com/a/b",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }
    verify_mock = Mock(return_value=False)
    monkeypatch.setattr(controller, "verify", verify_mock)

    assert controller.apply([rec]) is True
    _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )

    verify_mock.assert_called_once_with(remote_checks=False)


def test_apply_marks_existing_addon_downloading(
    controller, cfg, tmp_path, monkeypatch
):
    """An update (the addon is already tracked) must flip its status to
    'downloading' synchronously — the panel re-renders immediately after
    apply() and relies on the new status to show progress instead of the
    stale outOfDate button."""
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["out_dir"] = client
    cfg["addons"] = {}
    rec = AddonState(
        folder="Foo", status="outOfDate", git="https://github.com/a/b", toc={}
    )
    controller.state.addons["Foo"] = rec

    release = threading.Event()
    monkeypatch.setattr(
        ac.addons,
        "install_addon_files",
        lambda client, folder, git, sha: release.wait(),
    )

    assert controller.apply([rec.to_dict()]) is True

    # Synchronous mutation: status is "downloading" right after apply().
    assert controller.state.addons["Foo"].status == "downloading"

    # Let the install finish — the worker marks it up to date and posts the
    # snapshot without waiting for the post-install verify.
    release.set()
    _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert controller.state.addons["Foo"].status == "upToDate"
    assert controller.state.installing is False
    assert controller.state.busy is False


def test_update_all_flips_records_to_downloading(
    controller, cfg, tmp_path, monkeypatch
):
    """The footer's 'Update all' path goes through apply() too — every
    out-of-date record must show 'downloading' synchronously."""
    client = str(tmp_path)
    _install_folder(client, "Foo")
    _install_folder(client, "Bar")
    cfg["out_dir"] = client
    cfg["addons"] = {}
    for name in ("Foo", "Bar"):
        controller.state.addons[name] = AddonState(
            folder=name,
            status="outOfDate",
            git=f"https://github.com/a/{name}",
            toc={},
        )

    release = threading.Event()
    monkeypatch.setattr(
        ac.addons,
        "install_addon_files",
        lambda client, folder, git, sha: release.wait(),
    )

    assert controller.apply(controller.update_all()) is True

    for name in ("Foo", "Bar"):
        assert controller.state.addons[name].status == "downloading"

    release.set()
    _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    for name in ("Foo", "Bar"):
        assert controller.state.addons[name].status == "upToDate"


def test_apply_failure_records_error_and_posts_finished(
    controller, cfg, monkeypatch
):
    def boom(client, folder, git, sha):
        raise RuntimeError("download blocked")

    monkeypatch.setattr(ac.addons, "install_addon_files", boom)
    rec = {
        "folder": "Foo",
        "status": "available",
        "git": "https://github.com/a/b",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }

    assert controller.apply([rec]) is True
    collected = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert (
        OperationFinished("addons", False, "Failed addons: Foo") in collected
    )
    assert any(
        isinstance(e, LogMessage) and "✗ Addon Foo: download blocked" in e.text
        for e in collected
    )
    assert controller.state.errors["Foo"].error == "download blocked"
    assert controller.state.errors["Foo"].git == "https://github.com/a/b"
    # The failed install never touched the config.
    assert cfg["addons"] == {}

    # The follow-up re-verify synthesizes an available row carrying the error.
    collected = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, AddonsLoaded)
    )
    avail = {a.folder: a for a in controller.state.available}
    assert "Foo" in avail
    assert avail["Foo"].error == "download blocked"


def test_apply_without_folder_is_noop(controller, cfg):
    cfg["out_dir"] = ""
    rec = {
        "folder": "Foo",
        "status": "available",
        "git": "url",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }
    assert controller.apply([rec]) is False
    assert controller._dispatcher.drain() == []


def test_apply_marks_pfui_for_profile_patch(
    controller, cfg, tmp_path, monkeypatch
):
    client = str(tmp_path)
    _install_folder(client, "pfUI")
    cfg["out_dir"] = client
    cfg["addons"] = {}
    patched = []
    monkeypatch.setattr(
        ac.addons,
        "patch_pfui_default_profile",
        lambda client: patched.append(client),
    )
    rec = {
        "folder": "pfUI",
        "status": "available",
        "git": "https://github.com/a/pfui",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }

    assert controller.apply([rec]) is True
    _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert patched == [client]


# ── update_all / remove ─────────────────────────────────────────────────


def test_update_all_only_collects_out_of_date(controller):
    controller.state.addons["A"] = AddonState(folder="A", status="outOfDate")
    controller.state.addons["B"] = AddonState(folder="B", status="upToDate")
    controller.state.addons["C"] = AddonState(folder="C", status="outOfDate")

    recs = controller.update_all()
    assert [r["folder"] for r in recs] == ["A", "C"]


def test_remove_deletes_folder_and_cleans(
    controller, cfg, tmp_path, monkeypatch
):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["out_dir"] = client
    cfg["addons"]["Foo"] = {"git": "https://github.com/x/y", "sha": "S"}
    controller.state.addons["Foo"] = AddonState(
        folder="Foo", status="outOfDate"
    )
    controller.state.errors["Foo"] = AddonError(
        "oops", "https://github.com/x/y"
    )
    deleted = []
    monkeypatch.setattr(ac.shutil, "rmtree", lambda path: deleted.append(path))

    controller.remove("Foo")

    assert deleted
    assert "Foo" not in cfg["addons"]
    assert "Foo" not in controller.state.addons
    assert "Foo" not in controller.state.errors
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "Removed addon Foo" in e.text
        for e in events
    )
    assert any(isinstance(e, AddonsLoaded) for e in events)


def test_remove_logs_failure(controller, cfg, tmp_path, monkeypatch):
    client = str(tmp_path)
    _install_folder(client, "Foo")
    cfg["out_dir"] = client
    cfg["addons"]["Foo"] = {"git": "https://github.com/x/y", "sha": "S"}

    def boom(path):
        raise OSError("locked")

    monkeypatch.setattr(ac.shutil, "rmtree", boom)

    controller.remove("Foo")

    assert any(
        isinstance(e, LogMessage)
        and "Failed to remove addon Foo: locked" in e.text
        for e in controller._dispatcher.drain()
    )


# ── footer / badge data ─────────────────────────────────────────────────


def test_footer_state_and_updates_count(controller):
    assert controller.footer_state() == (
        "Everything up to date",
        C_TEXT_DIM,
        "arrow",
    )
    assert controller.updates_count == 0

    controller.state.state = "verifying"
    assert controller.footer_state() == ("Checking…", C_TEXT_DIM, "arrow")
    controller.state.state = "done"

    controller.state.busy = True
    assert controller.footer_state()[0] == "Checking…"
    controller.state.busy = False

    controller.state.addons["A"] = AddonState(folder="A", status="outOfDate")
    controller.state.updates_count = 1
    assert controller.footer_state() == ("Update all", C_OK, "hand2")
    assert controller.updates_count == 1


# ── reset ───────────────────────────────────────────────────────────────


def test_reset_clears_state(controller, cfg):
    controller.state.verified_ts = 123.0
    controller.state.state = "done"
    controller.state.addons["A"] = AddonState(folder="A", status="outOfDate")
    controller.state.available = [AddonState(folder="B")]
    controller.state.errors["A"] = AddonError("oops")
    controller.state.updates_count = 3
    controller.state.sections_open["INSTALLED"] = False

    controller.reset()

    assert controller.state.verified_ts == 0.0
    assert controller.state.state == "idle"
    assert controller.state.addons == {}
    assert controller.state.available == []
    assert controller.state.errors == {}
    assert controller.state.updates_count == 0
    # Section open/closed state survives a folder change.
    assert controller.state.sections_open["INSTALLED"] is False
