"""Unit tests for the update controller (update_controller).

No Tk involved: the controller is driven directly and its effects are read
from the shared EventDispatcher and its UpdateState. VerifyWorker/UpdateWorker
are swapped for a scripted fake via monkeypatch.
"""

import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

import vanilla_wow_launcher.controllers.update as uc
from vanilla_wow_launcher.controllers.update import UpdateController
from vanilla_wow_launcher.state.events import (
    EventDispatcher,
    GameExited,
    GameLaunched,
    LogMessage,
    OperationFailed,
    OperationFinished,
    ProgressChanged,
    StatusChanged,
    UpdateFilesList,
)


class _FakeProc:
    """A Popen stand-in whose wait() blocks until released, so the game
    watcher thread completes only when the test says so."""

    def __init__(self):
        self.exit_event = threading.Event()

    def wait(self):
        self.exit_event.wait()
        return 0


def _wait_until_true(predicate, timeout=2.0):
    """Spin until `predicate` is true (assertion failure on timeout)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.005)


class ScriptedWorker:
    """Fake VerifyWorker/UpdateWorker with the real constructor signature.

    run() replays the class-level `script` (log messages) and `prog_script`
    (progress updates) into its queues, then signals `done`.
    """

    instances = []
    script = []
    prog_script = []
    done = threading.Event()

    def __init__(self, out_dir, log_q, prog_q, *args, **kwargs):
        self.out_dir = out_dir
        self.log_q = log_q
        self.prog_q = prog_q
        self.args = args
        self.kwargs = kwargs
        self.overwrite_config = kwargs.get("overwrite_config", False)
        self.cancelled = False
        self.run_args = None
        type(self).instances.append(self)

    def cancel(self):
        self.cancelled = True

    def run(self, *args):
        self.run_args = args
        for msg, tag in type(self).script:
            self.log_q.put((msg, tag))
        for item in type(self).prog_script:
            self.prog_q.put(item)
        type(self).done.set()


@pytest.fixture
def worker_cls(monkeypatch):
    ScriptedWorker.instances = []
    ScriptedWorker.script = []
    ScriptedWorker.prog_script = []
    ScriptedWorker.done.clear()
    monkeypatch.setattr(uc, "VerifyWorker", ScriptedWorker)
    monkeypatch.setattr(uc, "UpdateWorker", ScriptedWorker)
    yield ScriptedWorker
    ScriptedWorker.done.clear()


@pytest.fixture
def config(monkeypatch):
    cfg = {"out_dir": "/tmp/octo-game"}
    monkeypatch.setattr(uc, "load_config", lambda: cfg)
    monkeypatch.setattr(
        uc, "update_config", lambda mutator: (mutator(cfg), cfg)[1]
    )
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    return cfg


@pytest.fixture
def controller(config):
    return UpdateController(EventDispatcher())


def _wait_and_poll(controller, worker_cls, timeout=2.0):
    """Wait for the scripted worker's thread, then drain its queues once."""
    deadline = time.monotonic() + timeout
    while not worker_cls.done.is_set():
        if time.monotonic() > deadline:
            raise AssertionError("scripted worker never finished")
        time.sleep(0.005)
    controller.poll()


# ── verify flow ─────────────────────────────────────────────────────────


def test_verify_up_to_date_marks_client_ready(controller, worker_cls, config):
    worker_cls.script = [("__UP_TO_DATE__", "")]
    controller.start_verify()
    initial = controller._dispatcher.drain()
    assert StatusChanged("Verifying…") in initial
    assert ProgressChanged(0.0, "") in initial

    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert ProgressChanged(1.0, "") in events
    assert OperationFinished("verify", True) in events
    assert controller.state.client_ready is True
    assert controller.state.running is False


# ── debug stdout mirroring ───────────────────────────────────────────────


def test_poll_echoes_worker_logs_to_stdout_in_debug(
    controller, worker_cls, config, monkeypatch, capsys
):
    monkeypatch.setenv("VANILLA_WOW_DEBUG", "1")
    worker_cls.script = [("Verifying files...", "acct"), ("__DONE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    out = capsys.readouterr().out
    assert "Verifying files..." in out
    assert "__DONE__" not in out


def test_poll_does_not_echo_to_stdout_by_default(
    controller, worker_cls, config, monkeypatch, capsys
):
    monkeypatch.delenv("VANILLA_WOW_DEBUG", raising=False)
    worker_cls.script = [("Verifying files...", "acct"), ("__DONE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    assert capsys.readouterr().out == ""


def test_verify_needs_update_sets_diff_and_not_ready(
    controller, worker_cls, config
):
    diff = [{"type": "file", "name": "a.bin"}]
    worker_cls.script = [("__DIFF_TREE__", diff), ("__UPDATE_NEEDED__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert OperationFinished("verify", False) in events
    assert ProgressChanged(0.0, "") in events
    assert UpdateFilesList(["a.bin"]) in events
    assert controller.state.diff_nodes == diff
    assert controller.state.client_ready is False
    assert controller.state.running is False


def test_diff_tree_flattens_full_relative_paths(
    controller, worker_cls, config
):
    """The UpdateFilesList event carries full relative paths (dir names
    included) so they match the paths the HTTP downloader streams."""
    diff = [
        {
            "type": "dir",
            "name": "Data",
            "files": [
                {"type": "file", "name": "foo.mpq"},
                {"type": "mpq", "name": "patch"},
            ],
        },
        {"type": "file", "name": "WoW.exe"},
    ]
    worker_cls.script = [("__DIFF_TREE__", diff), ("__UPDATE_NEEDED__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert (
        UpdateFilesList(["Data/foo.mpq", "Data/patch.mpq", "WoW.exe"])
        in events
    )


def test_verify_failure_records_null_diff(controller, worker_cls, config):
    worker_cls.script = [("__DIFF_TREE__", None), ("__UPDATE_NEEDED__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()
    assert controller.state.diff_nodes is None
    assert controller.state.client_ready is False
    assert controller.state.running is False


def test_manifest_available_marker_sets_flag(controller, worker_cls, config):
    worker_cls.script = [("__MANIFEST_AVAILABLE__", "")]
    controller.start_verify()
    assert controller.state.manifest_available is False
    _wait_and_poll(controller, worker_cls)
    assert controller.state.manifest_available is True


def test_manifest_unavailable_disables_and_posts_finished(
    controller, worker_cls, config
):
    worker_cls.script = [("__MANIFEST_UNAVAILABLE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert OperationFinished("verify", False) in events
    assert controller.state.manifest_available is False
    assert controller.state.client_ready is False
    assert controller.state.running is False


def test_torrent_recovery_done_marks_client_ready(
    controller, worker_cls, config
):
    """A successful manifest-less torrent recovery: client ready but the
    manifest flag stays False (nothing was ever fetched)."""
    worker_cls.script = [("__TORRENT_RECOVERY_DONE__", "")]
    controller.start_update()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert OperationFinished("update", True) in events
    assert controller.state.client_ready is True
    assert controller.state.manifest_available is False
    assert controller.state.running is False


def test_start_verify_and_invalidate_reset_manifest_available(
    controller, worker_cls, config
):
    controller.state.manifest_available = True
    controller.start_verify()
    assert controller.state.manifest_available is False
    controller.state.manifest_available = True
    controller.invalidate()
    assert controller.state.manifest_available is False


def test_verify_passes_overwrite(controller, worker_cls, config):
    controller.start_verify(overwrite_config=True)
    w = worker_cls.instances[0]
    assert w.overwrite_config is True
    assert w.args == ()
    assert w.out_dir == config["out_dir"]


def test_verify_passes_no_overwrite_by_default(controller, worker_cls, config):
    controller.start_verify()
    assert worker_cls.instances[0].overwrite_config is False


def test_start_verify_cancels_previous_worker(controller, worker_cls, config):
    controller.start_verify()
    first = worker_cls.instances[0]
    controller.start_verify()
    assert first.cancelled is True
    assert len(worker_cls.instances) == 2


def test_start_verify_without_folder_is_noop(worker_cls, config):
    ctrl = UpdateController(EventDispatcher(), get_out_dir=lambda: "")
    ctrl.start_verify()
    assert ctrl._dispatcher.drain() == []
    assert not worker_cls.instances


def test_disabled_client_updates_prevent_verify_and_update(
    controller, worker_cls, config
):
    config["client_update_enabled"] = False
    controller.start_verify()
    controller.start_update()
    assert not worker_cls.instances
    assert controller.state.running is False


# ── update flow ─────────────────────────────────────────────────────────


def test_update_done_reports_version(controller, worker_cls, config):
    worker_cls.script = [("__VERSION__1.12.2", ""), ("__DONE__", "")]
    controller.start_update()
    initial = controller._dispatcher.drain()
    assert StatusChanged("Updating…") in initial
    assert ProgressChanged(0.0, "") in initial
    assert LogMessage("\nGame folder: /tmp/octo-game\n", "dim") in initial

    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert OperationFinished("update", True) in events
    assert controller.state.client_version == "1.12.2"
    assert controller.state.client_ready is True
    assert controller.state.running is False


def test_update_error_posts_failure(controller, worker_cls, config):
    worker_cls.script = [("__ERROR__", "")]
    controller.start_update()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert OperationFailed("update", "") in events
    assert controller.state.client_ready is False
    assert controller.state.running is False


def test_verify_error_posts_verify_failure(controller, worker_cls, config):
    worker_cls.script = [("__ERROR__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)

    events = controller._dispatcher.drain()
    assert OperationFailed("verify", "") in events


def test_update_receives_diff_from_verify(controller, worker_cls, config):
    diff = [{"type": "file", "name": "a.bin"}]
    worker_cls.script = [("__DIFF_TREE__", diff), ("__UPDATE_NEEDED__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    worker_cls.script = [("__DONE__", "")]
    worker_cls.prog_script = []
    worker_cls.done.clear()
    controller.start_update()
    _wait_and_poll(controller, worker_cls)
    w = worker_cls.instances[1]
    assert w.run_args == (diff, None)
    assert controller.state.diff_nodes is None


def test_update_receives_stale_paths_from_torrent_verify(
    controller, worker_cls, config
):
    worker_cls.script = [("__DONE__", "")]
    controller.state.manifest_available = False
    controller.state.torrent_stale = ["Data/a.bin"]

    controller.start_update()
    _wait_and_poll(controller, worker_cls)

    worker = worker_cls.instances[0]
    assert worker.run_args == (None, {"Data/a.bin"})


def test_start_update_without_folder_logs_error(worker_cls, config):
    ctrl = UpdateController(EventDispatcher(), get_out_dir=lambda: "  ")
    ctrl.start_update()
    events = ctrl._dispatcher.drain()
    assert (
        LogMessage("✗  Please set the game folder first.\n", "err") in events
    )
    assert ctrl.state.running is False
    assert not worker_cls.instances


def test_start_update_when_busy_reports_and_returns_false(
    controller, worker_cls, config
):
    controller.state.running = True

    assert controller.start_update() is False

    events = controller._dispatcher.drain()
    assert any(
        isinstance(event, LogMessage) and "already in progress" in event.text
        for event in events
    )
    assert not worker_cls.instances


# ── queue draining / progress / hashes ──────────────────────────────────


def test_log_lines_become_log_events(controller, worker_cls, config):
    worker_cls.script = [("hello world", "acct")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    assert LogMessage("hello world", "acct") in controller._dispatcher.drain()


def test_progress_posts_latest(controller, worker_cls, config):
    worker_cls.prog_script = [(0.3, "a.bin"), (0.9, "b.mpq")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    assert ProgressChanged(0.9, "b.mpq") in events
    assert controller.state.progress == 0.9
    assert controller.state.progress_label == "b.mpq"


def test_cancel_stops_live_workers(controller, worker_cls, config):
    controller.start_verify()
    w = worker_cls.instances[0]
    controller.cancel()
    assert w.cancelled is True


def test_invalidate_resets_readiness(controller, worker_cls, config):
    controller.state.client_ready = True
    controller.state.manifest_available = True
    controller.state.diff_nodes = [{"type": "file", "name": "a.bin"}]
    controller.invalidate()
    assert controller.state.client_ready is False
    assert controller.state.manifest_available is False
    assert controller.state.diff_nodes is None


def test_empty_torrent_stale_set_skips_update_worker(
    controller, worker_cls, config
):
    controller.state.manifest_available = False
    controller.state.torrent_stale = []

    assert controller.start_update() is True

    assert controller.state.client_ready is True
    assert controller.state.running is False
    assert not worker_cls.instances
    assert OperationFinished("update", True) in controller._dispatcher.drain()


def test_invalidate_cancels_worker_and_drops_its_queues(
    controller, worker_cls, config
):
    controller.start_verify()
    worker = worker_cls.instances[0]

    controller.invalidate()

    assert worker.cancelled is True
    assert controller.state.running is False
    assert controller._op is None


def test_events_delivered_to_subscribers(controller, worker_cls, config):
    got = []
    controller._dispatcher.subscribe(got.append)
    worker_cls.script = [("__UP_TO_DATE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.dispatch_all()
    kinds = {type(e) for e in got}
    assert StatusChanged in kinds
    assert ProgressChanged in kinds
    assert OperationFinished in kinds


# ── compute_readiness ────────────────────────────────────────────────────


def test_readiness_disabled_without_manifest_when_cannot_launch(
    controller, worker_cls, config, monkeypatch
):
    """No manifest + no launch possibility → button grayed UPDATE."""
    monkeypatch.setattr(uc, "can_launch_client", lambda: False)
    r = controller.compute_readiness()
    assert r.mode == "disabled"
    assert r.label == "UPDATE"
    assert r.status == "Manifest unavailable"


def test_readiness_recovery_update_when_manifest_down(
    controller, worker_cls, config, monkeypatch
):
    """No manifest + client not ready + torrent recovery possible → enabled
    UPDATE offering a full BitTorrent re-download."""
    monkeypatch.setattr(uc, "torrent_recovery_available", lambda: True)
    monkeypatch.setattr(uc, "can_launch_client", lambda: False)
    r = controller.compute_readiness()
    assert r.mode == "update"
    assert r.label == "UPDATE"
    assert r.status == "Download via BitTorrent"


def test_readiness_no_recovery_without_torrent(
    controller, worker_cls, config, monkeypatch
):
    """No manifest + no torrent source → stays grayed UPDATE."""
    monkeypatch.setattr(uc, "torrent_recovery_available", lambda: False)
    monkeypatch.setattr(uc, "can_launch_client", lambda: False)
    r = controller.compute_readiness()
    assert r.mode == "disabled"


def test_readiness_play_without_manifest_when_can_launch(
    controller, worker_cls, config
):
    """No manifest + launchable → PLAY (the client may be on disk; the
    manifest just couldn't be verified)."""
    r = controller.compute_readiness()
    assert r.mode == "play"
    assert r.label == "PLAY"
    assert r.status == "Manifest unavailable"


def test_readiness_allows_play_without_manifest_when_updates_disabled(
    controller, worker_cls, config
):
    config["client_update_enabled"] = False
    r = controller.compute_readiness()
    assert r.mode == "play"
    assert r.status == "Client updates disabled"


def test_readiness_update_available_when_not_ready(
    controller, worker_cls, config
):
    controller.state.manifest_available = True
    r = controller.compute_readiness()
    assert r.mode == "update"
    assert r.label == "UPDATE"
    assert r.status == "Update available!"


def test_torrent_diff_stores_stale_and_not_ready(
    controller, worker_cls, config
):
    """A manifest-less torrent verify found stale files → the controller
    records them, keeps the client not-ready and the manifest unavailable."""
    worker_cls.script = [("__TORRENT_DIFF__", ["Data/a.bin", "Patch.mpq"])]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_stale == ["Data/a.bin", "Patch.mpq"]
    assert controller.state.client_ready is False
    assert controller.state.manifest_available is False


def test_torrent_up_to_date_marks_client_ready(controller, worker_cls, config):
    """A manifest-less torrent verify found nothing stale → the client is
    ready even though no manifest was ever fetched."""
    worker_cls.script = [("__TORRENT_UP_TO_DATE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.client_ready is True
    assert controller.state.manifest_available is False
    assert controller.state.torrent_stale is None


def test_readiness_torrent_diff_offers_update(controller, worker_cls, config):
    """Torrent-only verify with stale files → enabled UPDATE showing the
    count."""
    controller.state.manifest_available = False
    controller.state.torrent_stale = ["Data/a.bin", "Patch.mpq"]
    r = controller.compute_readiness()
    assert r.mode == "update"
    assert r.label == "UPDATE"
    assert r.status == "2 file(s) to update via BitTorrent"


def test_readiness_torrent_up_to_date_offers_play(
    controller, worker_cls, config
):
    """Torrent-only verify with no stale files → PLAY up-to-date even with no
    manifest."""
    controller.state.manifest_available = False
    controller.state.client_ready = True
    r = controller.compute_readiness()
    assert r.mode == "play"
    assert r.label == "PLAY"
    assert r.status == "Everything up to date!"


def test_update_passes_torrent_wanted_to_worker(
    controller, worker_cls, config
):
    """start_update hands the stale torrent files to the update worker so it
    only fetches those, and clears them from state."""
    controller.state.torrent_stale = ["Data/a.bin", "Patch.mpq"]
    worker_cls.script = [("__DONE__", "")]
    controller.start_update()
    _wait_and_poll(controller, worker_cls)
    w = worker_cls.instances[0]
    assert w.run_args == (None, {"Data/a.bin", "Patch.mpq"})
    assert controller.state.torrent_stale is None


def test_torrent_unreachable_sets_flag(controller, worker_cls, config):
    """No manifest + unreachable torrent → the controller records it so the
    UI stops offering a dead recovery download."""
    worker_cls.script = [("__TORRENT_UNREACHABLE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_reachable is False
    assert controller.state.client_ready is False
    assert controller.state.manifest_available is False
    assert controller.state.torrent_stale is None


@pytest.mark.parametrize(
    "marker",
    [
        "__TORRENT_UNREACHABLE__",
        "__TORRENT_CORRUPT__",
        "__TORRENT_STALLED__",
        "__TORRENT_SESSION_ERROR__",
        "__TORRENT_DISK_ERROR__",
        "__TORRENT_VERIFY_FAILED__",
    ],
)
def test_torrent_failure_preserves_update_operation_kind(controller, marker):
    controller._op = "update"

    controller._handle_log(marker, "failure")

    events = controller._dispatcher.drain()
    assert OperationFinished("update", False) in events


def test_torrent_verify_failed_sets_flag(controller, worker_cls, config):
    """No manifest + torrent fetched but recheck failed → the torrent IS
    reachable, so recovery download is offered."""
    worker_cls.script = [("__TORRENT_VERIFY_FAILED__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_reachable is True
    assert controller.state.client_ready is False
    assert controller.state.manifest_available is False
    assert controller.state.torrent_stale is None


def test_readiness_unreachable_torrent_falls_back_to_play(
    controller, worker_cls, config
):
    """No manifest + unreachable torrent + launchable → PLAY only, never a
    dead recovery UPDATE."""
    controller.state.manifest_available = False
    controller.state.torrent_reachable = False
    r = controller.compute_readiness()
    assert r.mode == "play"
    assert r.label == "PLAY"
    assert r.status == "Torrent unavailable"


def test_readiness_unreachable_torrent_disabled_when_not_launchable(
    controller, worker_cls, config, monkeypatch
):
    """No manifest + unreachable torrent + not launchable → grayed UPDATE."""
    monkeypatch.setattr(uc, "can_launch_client", lambda: False)
    controller.state.manifest_available = False
    controller.state.torrent_reachable = False
    r = controller.compute_readiness()
    assert r.mode == "disabled"
    assert r.label == "UPDATE"
    assert r.status == "Torrent unavailable"


def test_readiness_play_when_ready_and_launchable(
    controller, worker_cls, config
):
    controller.state.client_ready = True
    controller.state.manifest_available = True
    r = controller.compute_readiness()
    assert r.mode == "play"
    assert r.status == "Everything up to date!"


def test_readiness_ready_when_not_launchable(
    controller, worker_cls, config, monkeypatch
):
    monkeypatch.setattr(uc, "can_launch_client", lambda: False)
    controller.state.client_ready = True
    controller.state.manifest_available = True
    r = controller.compute_readiness()
    assert r.mode == "busy"
    assert r.label == "READY"
    assert r.status == "Everything up to date!"


def test_readiness_play_blocked_by_mod_errors(controller, worker_cls, config):
    config["mods"] = {"SomeMod": {"error": "download blocked"}}
    controller.state.client_ready = True
    controller.state.manifest_available = True
    r = controller.compute_readiness()
    assert r.mode == "busy"
    assert r.label == "PLAY"
    assert r.status == "Mod errors — check MODS tab"


def test_readiness_blocked_while_addons_install(
    controller, worker_cls, config
):
    controller.state.client_ready = True
    r = controller.compute_readiness(addons_installing=True)
    assert r.mode == "busy"
    assert r.label == "Installing…"
    assert r.status == "Downloading addons…"


def test_readiness_busy_while_verifying(controller, worker_cls, config):
    controller.start_verify()
    r = controller.compute_readiness()
    assert r.mode == "busy"
    assert r.label == "Checking…"
    assert r.status == "Verifying…"


def test_readiness_busy_while_updating(controller, worker_cls, config):
    controller.start_update()
    r = controller.compute_readiness()
    assert r.mode == "busy"
    assert r.label == "Updating…"
    assert r.status == "Updating…"


# ── launch_game ──────────────────────────────────────────────────────────


def test_launch_game_unavailable_logs_error(
    controller, worker_cls, config, monkeypatch
):
    monkeypatch.setattr(uc, "can_launch_client", lambda: False)
    ok, dxvk = controller.launch_game()
    assert ok is False
    assert dxvk is False
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "not available" in e.text for e in events
    )


def test_launch_game_linux_via_umu(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_text("")
    config["out_dir"] = str(game)
    config["launch"] = {
        "umu_proton": "GE-Proton9-4",
        "umu_game_id": "umu-test",
    }
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "remove_wdb", lambda *a: None)
    launched = {}
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.launch",
        lambda out_dir, exe, **kw: (
            launched.update(exe=exe, kw=kw) or (1234, 9999, _FakeProc())
        ),
    )
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.compute_wine_prefix",
        lambda: "/prefix",
    )

    ok, dxvk = controller.launch_game()

    assert ok is True
    assert dxvk is False
    assert launched["exe"] == str(game / "WoW.exe")
    assert launched["kw"]["proton"] == "GE-Proton9-4"
    assert launched["kw"]["game_id"] == "umu-test"
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "via umu" in e.text for e in events
    )
    assert GameLaunched(1234, 9999) in events
    assert controller.state.game_running is True
    assert controller.state.game_pid == 1234
    assert controller.state.game_pgid == 9999


def test_launch_game_linux_prefers_vanillafixes(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_text("")
    (game / "VanillaFixes.exe").write_text("")
    config["out_dir"] = str(game)
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "remove_wdb", lambda *a: None)
    launched = {}
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.launch",
        lambda out_dir, exe, **kw: (
            launched.update(exe=exe, kw=kw) or (1234, 9999, _FakeProc())
        ),
    )
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.compute_wine_prefix",
        lambda: "/prefix",
    )

    ok, _ = controller.launch_game()

    assert ok is True
    assert launched["exe"] == str(game / "VanillaFixes.exe")
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage)
        and "Launched VanillaFixes.exe via umu" in e.text
        for e in events
    )


def test_game_watcher_posts_exited_and_clears_state(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_text("")
    config["out_dir"] = str(game)
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "remove_wdb", lambda *a: None)
    proc = _FakeProc()
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.launch",
        lambda *a, **k: (1234, 9999, proc),
    )
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.compute_wine_prefix",
        lambda: "/prefix",
    )

    controller.launch_game()
    controller._dispatcher.drain()
    assert controller.state.game_running is True

    proc.exit_event.set()
    _wait_until_true(lambda: not controller.state.game_running)

    events = controller._dispatcher.drain()
    assert GameExited(1234, 0) in events
    assert any(
        isinstance(e, StatusChanged) and "Game exited" in e.text
        for e in events
    )
    assert controller.state.game_pid is None
    assert controller.state.game_pgid is None


def test_single_instance_refuses_second_launch(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_text("")
    config["out_dir"] = str(game)
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "remove_wdb", lambda *a: None)
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.launch",
        lambda *a, **k: (1234, 9999, _FakeProc()),
    )
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.compute_wine_prefix",
        lambda: "/prefix",
    )

    assert controller.launch_game()[0] is True
    controller._dispatcher.drain()

    ok, dxvk = controller.launch_game()

    assert ok is False
    assert dxvk is False
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "already running" in e.text
        for e in events
    )


def test_terminate_game_kills_running_process(
    controller, worker_cls, config, monkeypatch
):
    controller.state.game_running = True
    controller.state.game_pid = 1234
    controller.state.game_pgid = 9999
    killed = []
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.kill_game",
        lambda pid, pgid: killed.append((pid, pgid)),
    )

    assert controller.terminate_game() is True

    assert killed == [(1234, 9999)]
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "Terminating game" in e.text
        for e in events
    )


def test_terminate_game_noop_when_nothing_running(
    controller, worker_cls, config, monkeypatch
):
    killed = []
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.kill_game",
        lambda pid, pgid: killed.append((pid, pgid)),
    )
    assert controller.terminate_game() is False
    assert killed == []


def test_readiness_terminate_when_game_running(controller, worker_cls, config):
    controller.state.game_running = True
    r = controller.compute_readiness()
    assert r.mode == "terminate"
    assert r.label == "TERMINATE"
    assert r.status == "Running WoW.exe — click TERMINATE to quit"


def test_launch_game_linux_missing_exe(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    config["out_dir"] = str(tmp_path / "nope")
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    ok, dxvk = controller.launch_game()
    assert ok is False
    assert dxvk is False
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "WoW.exe not found" in e.text
        for e in events
    )


def test_launch_game_linux_umu_failure(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_text("")
    config["out_dir"] = str(game)
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "remove_wdb", lambda *a: None)
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.launch",
        Mock(side_effect=RuntimeError("umu-run missing")),
    )

    ok, dxvk = controller.launch_game()

    assert ok is False
    assert dxvk is False
    events = controller._dispatcher.drain()
    assert any(
        isinstance(e, LogMessage) and "via umu" in e.text for e in events
    )


def test_launch_game_windows_prefers_vanillafixes(
    controller, worker_cls, config, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_text("")
    (game / "VanillaFixes.exe").write_text("")
    config["out_dir"] = str(game)
    monkeypatch.setattr(uc, "can_launch_client", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: False)
    monkeypatch.setattr(uc, "remove_wdb", lambda *a: None)
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)

    ok, dxvk = controller.launch_game()

    assert ok is True
    assert dxvk is False
    args, kwargs = popen.call_args
    assert args[0] == [str(game / "VanillaFixes.exe")]
    assert kwargs["cwd"] == str(game)


# ── Typed torrent exception handler tests ────────────────────────────────────


def test_torrent_corrupt_sets_unreachable(controller, worker_cls, config):
    """Corrupt torrent → unreachable + error detail."""
    worker_cls.script = [
        ("__TORRENT_CORRUPT__", "Failed to parse torrent: bad data")
    ]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_reachable is False
    assert (
        controller.state.torrent_error == "Failed to parse torrent: bad data"
    )
    assert controller.state.manifest_available is False


def test_torrent_stalled_sets_error(controller, worker_cls, config):
    """Verification stalled → reachable + error with peer count."""
    worker_cls.script = [("__TORRENT_STALLED__", "Stalled (0 peers)")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_reachable is True
    assert controller.state.torrent_error == "Stalled (0 peers)"


def test_torrent_session_error_sets_error(controller, worker_cls, config):
    """Session error → reachable + error."""
    worker_cls.script = [
        (
            "__TORRENT_SESSION_ERROR__",
            "Failed to create session: address in use",
        )
    ]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_reachable is True
    assert "address in use" in controller.state.torrent_error


def test_torrent_disk_error_sets_error(controller, worker_cls, config):
    """Disk error → reachable + error."""
    worker_cls.script = [("__TORRENT_DISK_ERROR__", "No space left on device")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    controller._dispatcher.drain()

    assert controller.state.torrent_reachable is True
    assert controller.state.torrent_error == "No space left on device"


def test_readiness_torrent_unreachable_with_error(
    controller, worker_cls, config
):
    """Unreachable torrent with error → status includes error detail."""
    controller.state.manifest_available = False
    controller.state.torrent_reachable = False
    controller.state.torrent_error = "not a valid torrent"
    r = controller.compute_readiness()
    assert r.mode == "play"
    assert r.status == "Torrent unavailable: not a valid torrent"


def test_readiness_torrent_error_with_stale(controller, worker_cls, config):
    """Stale files + error → download with peer count."""
    controller.state.manifest_available = False
    controller.state.torrent_reachable = True
    controller.state.torrent_stale = ["a.bin", "b.bin"]
    controller.state.torrent_error = "Stalled (3 peers)"
    r = controller.compute_readiness()
    assert r.mode == "update"
    assert "2 file(s) to update" in r.status
    assert "(Stalled (3 peers))" in r.status


def test_readiness_torrent_error_no_stale(controller, worker_cls, config):
    """Error only, no stale → download with error detail."""
    controller.state.manifest_available = False
    controller.state.torrent_reachable = True
    controller.state.torrent_stale = None
    controller.state.torrent_error = "session failed"
    r = controller.compute_readiness()
    assert r.mode == "update"
    assert r.status == "Download via BitTorrent (session failed)"


# ── torrent snapshot lifecycle ──────────────────────────────────────────


def test_torrent_progress_posts_piece_counts(controller, worker_cls, config):
    """A 3-tuple progress item carrying verified_pieces/total_pieces reaches
    the state and the ProgressChanged event unchanged."""
    worker_cls.prog_script = [
        (
            0.0,
            "Verifying",
            {"phase": "Verifying", "verified_pieces": 1, "total_pieces": 4},
        ),
        (
            0.5,
            "Verifying",
            {"phase": "Verifying", "verified_pieces": 3, "total_pieces": 4},
        ),
    ]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    events = controller._dispatcher.drain()
    latest = [
        e
        for e in events
        if isinstance(e, ProgressChanged) and e.verified_pieces == 3
    ]
    assert latest
    assert controller.state.progress_verified_pieces == 3
    assert controller.state.progress_total_pieces == 4


def test_torrent_verify_diff_then_update_lifecycle(
    controller, worker_cls, config
):
    """verify → __TORRENT_DIFF__ → update → __DONE__ is a coherent lifecycle:
    stale paths captured into the worker args, then cleared on completion."""
    stale = ["Data/a.bin"]
    worker_cls.script = [
        ("__TORRENT_DIFF__", stale),
    ]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    assert controller.state.torrent_stale == stale
    assert controller.state.client_ready is False
    assert controller.state.manifest_available is False
    assert controller.compute_readiness().mode == "update"

    worker_cls.script = [("__DONE__", "")]
    worker_cls.prog_script = []
    worker_cls.done.clear()
    controller.start_update()
    _wait_and_poll(controller, worker_cls)
    w = worker_cls.instances[1]
    assert w.run_args == (None, {"Data/a.bin"})
    assert controller.state.torrent_stale is None
    assert controller.state.client_ready is True
    assert OperationFinished("update", True) in controller._dispatcher.drain()


def test_torrent_up_to_date_after_verify(controller, worker_cls, config):
    """__TORRENT_UP_TO_DATE__ → play readiness with no stale paths."""
    worker_cls.script = [("__TORRENT_UP_TO_DATE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    assert controller.state.client_ready is True
    assert controller.state.torrent_stale is None
    assert controller.compute_readiness().mode == "play"


def test_torrent_diff_empty_skips_update_and_marks_ready(
    controller, worker_cls, config
):
    """A verify that reports an empty stale set completes the update inline
    without ever spawning a worker."""
    worker_cls.script = [("__TORRENT_DIFF__", []), ("__DONE__", "")]
    controller.start_verify()
    _wait_and_poll(controller, worker_cls)
    assert controller.state.torrent_stale == []

    controller._dispatcher.drain()
    assert controller.start_update() is True
    assert controller.state.client_ready is True
    assert controller.state.running is False
    assert len(worker_cls.instances) == 1  # no update worker was created
    assert OperationFinished("update", True) in controller._dispatcher.drain()
