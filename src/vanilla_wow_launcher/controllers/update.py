"""Update/verify orchestration controller.

Owns the verify/update lifecycle: starting VerifyWorker/UpdateWorker,
polling their queues, computing the footer READY/PLAY/UPDATE button state
and publishing everything as events on the shared EventDispatcher. No GUI
toolkit: the controller never touches widgets, it only posts events and
mutates its own UpdateState.
"""

import os
import queue
import threading
from dataclasses import dataclass

from ..core.config_store import load_config, update_config
from ..core.constants import DEFAULT_OUT_DIR
from ..core.filesystem import (
    get_client_version,
    pick_game_executable,
    remove_wdb,
)
from ..core.log_sink import debug_emit
from ..core.platform_support import can_launch_client, is_linux
from ..services.self_update import (
    fetch_updater_latest_tag,
    updater_update_available,
)
from ..services.update_backend import markers
from ..services.update_backend.http_update import (
    UpdateWorker,
    VerifyWorker,
    torrent_recovery_available,
)
from ..state.events import (
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
from ..state.models import UpdateState


@dataclass
class Readiness:
    """Footer button decision produced by UpdateController.compute_readiness.

    mode is "play", "update", "busy", "disabled" or "terminate"; label is the
    button text used in busy mode; status is the footer status line the UI
    should show.
    """

    mode: str
    label: str
    status: str


def _flatten_diff_tree(nodes, prefix=()) -> list[str]:
    """Flatten a diff tree (as returned by __DIFF_TREE__) into a list of
    relative file paths (e.g. "Data/foo.mpq", "WoW.exe").

    Node ``name`` values are basenames, so the recursion carries the parent
    directory chain to rebuild the full path (matching the relative paths the
    HTTP downloader streams via ``current_file``)."""
    paths: list[str] = []
    for node in nodes:
        t = node.get("type", "")
        name = node.get("name", "")
        cur = prefix + (name,)
        if t == "file" or t == "del":
            paths.append("/".join(cur))
        elif t == "mpq":
            paths.append("/".join(cur) + ".mpq")
        elif t == "dir":
            paths.extend(_flatten_diff_tree(node.get("files", []), cur))
    return paths


class UpdateController:
    """Owns the verify/update flow; speaks to the UI only through events.

    `get_out_dir` is an optional zero-arg callable returning the current game
    folder (the Qt UI supplies its path field's getter). When omitted the
    controller reads ``out_dir`` from the on-disk config, mirroring the
    UI's default.
    """

    def __init__(self, dispatcher: EventDispatcher, get_out_dir=None):
        self._dispatcher = dispatcher
        self.state = UpdateState()
        self._log_q: queue.Queue = queue.Queue()
        self._prog_q: queue.Queue = queue.Queue()
        self._worker: UpdateWorker | None = None
        self._verify_worker: VerifyWorker | None = None
        self._op: str | None = None
        # Set by check_updater_update(): a newer updater release exists and
        # the header "Update available!" label should be shown.
        self.updater_update_available = False
        if get_out_dir is None:

            def get_out_dir():
                return load_config().get("out_dir", DEFAULT_OUT_DIR)

        self._get_out_dir = get_out_dir

    # ── public API ──────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self.state.running

    @property
    def client_ready(self) -> bool:
        return self.state.client_ready

    @property
    def client_update_enabled(self) -> bool:
        return self._client_updates_enabled()

    @property
    def diff_nodes(self):
        return self.state.diff_nodes

    def start_verify(self, overwrite_config: bool = False):
        if not self._client_updates_enabled():
            return
        out = (self._get_out_dir() or "").strip()
        if not out:
            return
        # Cancel any verify already in flight before swapping the queues, so
        # a stale worker can't keep writing to a queue we no longer poll.
        if self._verify_worker is not None:
            self._verify_worker.cancel()
        # Clear torrent state from previous attempts
        self.state.torrent_reachable = None
        self.state.torrent_error = None
        self.state.torrent_stale = None
        self.state.running = True
        self._op = "verify"
        self.state.status = "Verifying…"
        self.state.manifest_available = False  # a fresh verify re-fetches it
        self._log_q = queue.Queue()
        self._prog_q = queue.Queue()
        worker = VerifyWorker(
            out,
            self._log_q,
            self._prog_q,
            overwrite_config=overwrite_config,
        )
        self._verify_worker = worker
        threading.Thread(target=worker.run, daemon=True).start()
        self._dispatcher.post(StatusChanged("Verifying…"))
        self._dispatcher.post(ProgressChanged(0.0, ""))

    def start_update(self):
        if self.state.running:
            self._dispatcher.post(
                LogMessage("An update is already in progress.\n", "dim")
            )
            return False
        if not self._client_updates_enabled():
            return False
        out = (self._get_out_dir() or "").strip()
        if not out:
            self._dispatcher.post(
                LogMessage("✗  Please set the game folder first.\n", "err")
            )
            return
        update_config(lambda c: c.__setitem__("out_dir", out))
        self._dispatcher.post(LogMessage(f"\nGame folder: {out}\n", "dim"))
        torrent_wanted = (
            set(self.state.torrent_stale)
            if self.state.torrent_stale is not None
            else None
        )
        if torrent_wanted == set():
            self.state.torrent_stale = None
            self.state.client_ready = True
            self.state.status = "Up to date"
            self._dispatcher.post(
                LogMessage("[torrent] No stale files; update skipped.\n", "ok")
            )
            self._dispatcher.post(ProgressChanged(1.0, ""))
            self._dispatcher.post(OperationFinished("update", True))
            return True
        # Clear torrent state from previous attempts
        self.state.torrent_reachable = None
        self.state.torrent_error = None
        # Note: torrent_stale is cleared AFTER capturing it for the worker
        self.state.running = True
        self._op = "update"
        self.state.status = "Updating…"
        self._log_q = queue.Queue()
        self._prog_q = queue.Queue()
        worker = UpdateWorker(
            out,
            self._log_q,
            self._prog_q,
        )
        self._worker = worker
        diff = self.state.diff_nodes
        self.state.diff_nodes = None
        self.state.torrent_stale = None
        threading.Thread(
            target=worker.run,
            args=(diff, torrent_wanted),
            daemon=True,
        ).start()
        self._dispatcher.post(StatusChanged("Updating…"))
        self._dispatcher.post(ProgressChanged(0.0, ""))
        return True

    def cancel(self):
        """Ask every live worker to stop; the queues are drained as normal."""
        for worker in (self._verify_worker, self._worker):
            if worker is not None:
                worker.cancel()

    def invalidate(self):
        """Drop readiness and the cached diff tree (game folder changed or a
        verify-game-files recheck)."""
        self.cancel()
        # Workers may still enqueue one final marker after cooperative cancel;
        # stop polling their queues before clearing the state they could alter.
        self._log_q = queue.Queue()
        self._prog_q = queue.Queue()
        self._worker = None
        self._verify_worker = None
        self._op = None
        self.state.running = False
        self.state.client_ready = False
        self.state.manifest_available = False
        self.state.diff_nodes = None
        # Also clear torrent state
        self.state.torrent_reachable = None
        self.state.torrent_error = None
        self.state.torrent_stale = None

    def read_client_version(self) -> str:
        """The client version straight from disk, cached on state (footer
        label at startup, before any worker has run)."""
        self.state.client_version = get_client_version(
            (self._get_out_dir() or "").strip()
        )
        return self.state.client_version

    def check_updater_update(self):
        """Background daily check of the updater's own GitHub releases; sets
        `updater_update_available` when a newer version exists. The UI polls
        that flag from its event loop and draws the header label."""

        def worker():
            try:
                tag = fetch_updater_latest_tag()
            except Exception:
                tag = None
            self.updater_update_available = bool(updater_update_available(tag))

        threading.Thread(target=worker, daemon=True).start()

    def launch_game(self) -> tuple:
        """Launch the client detached.

        Returns ``(ok, dxvk_notice)``: ``ok`` is False when the client can't
        be launched (a LogMessage explains why) and ``dxvk_notice`` is True
        when the one-time DXVK first-launch notice should be shown. Consumes
        the notice flag, the clear-wdb and the launch itself. Windows runs
        the binary directly; Linux runs it through umu-launcher (Proton/Wine)
        when umu-run is available. Only one game process is allowed at a
        time — a second launch is refused while one is running.
        """
        if not can_launch_client():
            self._dispatcher.post(
                LogMessage(
                    "Game launch is not available on this platform — on Linux, "
                    "umu-run must be installed (the client is a Windows "
                    "binary).\n",
                    "err",
                )
            )
            return False, False
        if self.state.game_running:
            self._dispatcher.post(
                LogMessage(
                    "A game is already running — use TERMINATE to end it "
                    "first.\n",
                    "err",
                )
            )
            return False, False
        client_dir = (self._get_out_dir() or "").strip()
        cfg = load_config()
        if is_linux():
            return self._launch_game_via_umu(client_dir, cfg)
        return self._launch_game_windows(client_dir, cfg)

    def _launch_game_windows(self, client_dir: str, cfg: dict) -> tuple:
        """Windows direct launch: prefer the loader mod's executable, then
        WoW.exe, spawned detached from the caller's job object."""
        import subprocess

        # Prefer the loader mod's executable when it's present on disk
        # (whatever the catalog called it).
        exe, exe_lbl = pick_game_executable(client_dir)
        if not os.path.exists(exe):
            self._dispatcher.post(
                LogMessage(f"{exe_lbl} not found at: {exe}\n", "err")
            )
            return False, False

        dxvk_notice = False
        if cfg.get("dxvk_notice_pending"):
            update_config(lambda c: c.pop("dxvk_notice_pending", None))
            dxvk_notice = True
        if cfg.get("clear_wdb_on_launch", False):
            remove_wdb(client_dir)

        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0
            )
            try:
                subprocess.Popen(
                    [exe], cwd=client_dir, creationflags=flags, close_fds=True
                )
            except OSError:
                # The job object doesn't permit breakaway — retry without it.
                flags &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
                subprocess.Popen(
                    [exe], cwd=client_dir, creationflags=flags, close_fds=True
                )
            self._dispatcher.post(LogMessage(f"Launched {exe_lbl}!\n", "ok"))
            return True, dxvk_notice
        except Exception as e:
            self._dispatcher.post(
                LogMessage(f"Failed to launch {exe_lbl}: {e}\n", "err")
            )
            return False, dxvk_notice

    def _launch_game_via_umu(self, client_dir: str, cfg: dict) -> tuple:
        """Linux launch through umu-launcher: the client run under Proton in
        a launcher-wide WINEPREFIX. Prefers the VanillaFixes loader like
        Windows. No DXVK notice (shader-cache stutter is a Windows/DXVK-mod
        concern). Records the running game and watches its exit."""
        from ..services import umu

        exe, exe_lbl = pick_game_executable(client_dir)
        if not os.path.exists(exe):
            self._dispatcher.post(
                LogMessage(f"{exe_lbl} not found at: {exe}\n", "err")
            )
            return False, False
        if cfg.get("clear_wdb_on_launch", False):
            remove_wdb(client_dir)

        launch_cfg = cfg.get("launch") or {}
        try:
            pid, pgid, proc = umu.launch(
                client_dir,
                exe,
                proton=launch_cfg.get("umu_proton") or umu.default_proton(),
                game_id=launch_cfg.get("umu_game_id", "umu-vanilla-wow"),
                umu_binary=launch_cfg.get("umu_binary_path", ""),
                renderer=launch_cfg.get("umu_renderer", "auto"),
                gamemode=launch_cfg.get("umu_gamemode", True),
                wayland=launch_cfg.get("umu_wayland", True),
            )
            self.state.game_running = True
            self.state.game_pid = pid
            self.state.game_pgid = pgid
            self._dispatcher.post(GameLaunched(pid, pgid))
            threading.Thread(
                target=self._watch_game, args=(proc, pid), daemon=True
            ).start()
            prefix = umu.compute_wine_prefix()
            self._dispatcher.post(
                LogMessage(
                    f"Launched {exe_lbl} via umu (PID {pid}, WINEPREFIX "
                    f"{prefix}).\n",
                    "ok",
                )
            )
            self._dispatcher.post(
                StatusChanged("Running WoW.exe — click TERMINATE to quit")
            )
            return True, False
        except Exception as e:
            self.state.game_running = False
            self.state.game_pid = None
            self.state.game_pgid = None
            self._dispatcher.post(
                LogMessage(f"Failed to launch {exe_lbl} via umu: {e}\n", "err")
            )
            return False, False

    def _watch_game(self, proc, pid: int):
        """Background watcher: blocks until the umu process exits, then
        clears the running state and publishes GameExited."""
        try:
            code = proc.wait()
        except Exception:
            code = None
        self.state.game_running = False
        self.state.game_pid = None
        self.state.game_pgid = None
        self._dispatcher.post(GameExited(pid, code))
        if code in (0, None):
            self._dispatcher.post(StatusChanged("Game exited."))
        else:
            self._dispatcher.post(StatusChanged(f"Game exited (code {code})."))

    def terminate_game(self) -> bool:
        """Request termination of the running game (umu + WoW.exe process
        group). Returns True when a game was running; the actual exit is
        reported asynchronously via GameExited from the watcher."""
        if not self.state.game_running:
            return False
        pid = self.state.game_pid
        pgid = self.state.game_pgid
        self._dispatcher.post(
            LogMessage(f"Terminating game (PID {pid})…\n", "acct")
        )
        from ..services import umu

        try:
            umu.kill_game(pid, pgid)
        except Exception as e:
            self._dispatcher.post(
                LogMessage(f"Failed to terminate game: {e}\n", "err")
            )
        return True

    def poll(self):
        """Drain the worker queues once and post the resulting events."""
        try:
            while True:
                msg, tag = self._log_q.get_nowait()
                self._handle_log(msg, tag)
        except queue.Empty:
            pass

        latest = None
        try:
            while True:
                latest = self._prog_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            val, lbl = latest[:2]
            details = latest[2] if len(latest) > 2 else {}
            self.state.progress = max(0.0, min(1.0, val))
            self.state.progress_label = lbl
            phase = details.get("phase", "")
            transport = details.get("transport", "")
            self.state.progress_phase = phase
            self.state.progress_transport = transport
            self.state.progress_file = details.get("current_file", "")
            self.state.progress_downloaded = details.get("downloaded", 0)
            self.state.progress_total = details.get("total", 0)
            self.state.progress_speed = details.get("speed", 0.0)
            self.state.progress_peers = details.get("peers", 0)
            self.state.progress_verified_pieces = details.get(
                "verified_pieces", 0
            )
            self.state.progress_total_pieces = details.get("total_pieces", 0)
            self._dispatcher.post(
                ProgressChanged(
                    val,
                    lbl,
                    phase=phase,
                    transport=transport,
                    current_file=self.state.progress_file,
                    downloaded=self.state.progress_downloaded,
                    total=self.state.progress_total,
                    speed=self.state.progress_speed,
                    peers=self.state.progress_peers,
                    verified_pieces=self.state.progress_verified_pieces,
                    total_pieces=self.state.progress_total_pieces,
                )
            )

    def compute_readiness(self, addons_installing: bool = False) -> Readiness:
        """Footer button/status decision for the current state.

        `addons_installing` is owned by the addons controller; the UI passes
        its own flag so the button stays disabled while addons download,
        exactly like the mods flow.
        """
        if self.state.game_running:
            return Readiness(
                "terminate",
                "TERMINATE",
                "Running WoW.exe — click TERMINATE to quit",
            )
        if addons_installing:
            return Readiness("busy", "Installing…", "Downloading addons…")
        if self.state.running:
            label = "Updating…" if self._op == "update" else "Checking…"
            return Readiness("busy", label, self.state.status)
        if not self._client_updates_enabled():
            if self._mods_have_errors():
                return Readiness("busy", "PLAY", "Mod errors — check MODS tab")
            if not can_launch_client():
                return Readiness("busy", "READY", "Client updates disabled")
            return Readiness("play", "PLAY", "Client updates disabled")
        if not self.state.manifest_available:
            # No manifest → can't verify via SHA-1, but a torrent-only verify
            # may have established readiness or a stale-file list.
            if self.state.torrent_stale:
                n = len(self.state.torrent_stale)
                error_suffix = ""
                if self.state.torrent_error:
                    error_suffix = f" ({self.state.torrent_error})"
                return Readiness(
                    "update",
                    "UPDATE",
                    f"{n} file(s) to update via BitTorrent{error_suffix}",
                )
            if self.state.torrent_reachable is False:
                # Neither the manifest nor the BitTorrent snapshot is
                # reachable — there is no update to offer. Play if we can,
                # otherwise gray the button.
                error_detail = (
                    f": {self.state.torrent_error}"
                    if self.state.torrent_error
                    else ""
                )
                if not can_launch_client():
                    return Readiness(
                        "disabled",
                        "UPDATE",
                        f"Torrent unavailable{error_detail}",
                    )
                return Readiness(
                    "play", "PLAY", f"Torrent unavailable{error_detail}"
                )
            if self.state.torrent_error and not self.state.torrent_stale:
                # Torrent reachable but had an error (stalled, session, disk,
                # verify failed). Offer recovery so the user can retry.
                return Readiness(
                    "update",
                    "UPDATE",
                    f"Download via BitTorrent ({self.state.torrent_error})",
                )
            if not self.state.client_ready and torrent_recovery_available():
                return Readiness("update", "UPDATE", "Download via BitTorrent")
            if not can_launch_client():
                return Readiness("disabled", "UPDATE", "Manifest unavailable")
            if not self.state.client_ready:
                return Readiness("play", "PLAY", "Manifest unavailable")
            return Readiness("play", "PLAY", "Everything up to date!")
        if not self.state.client_ready:
            return Readiness("update", "UPDATE", "Update available!")
        if self._mods_have_errors():
            return Readiness("busy", "PLAY", "Mod errors — check MODS tab")
        if not can_launch_client():
            return Readiness("busy", "READY", "Everything up to date!")
        return Readiness("play", "PLAY", "Everything up to date!")

    # ── internals ───────────────────────────────────────────────────────────

    def _client_updates_enabled(self) -> bool:
        return bool(load_config().get("client_update_enabled", True))

    def _handle_log(self, msg: str, tag: str):
        if markers.is_version(msg):
            self.state.client_version = markers.version_of(msg)
            return
        handler = self._MARKER_HANDLERS.get(msg)
        if handler is not None:
            handler(self, tag)
        else:
            debug_emit(msg)
            self._dispatcher.post(LogMessage(msg, tag))

    def _on_done(self, tag: str):
        self.state.running = False
        self.state.client_ready = True
        self.state.manifest_available = True
        self._op = None
        self._dispatcher.post(ProgressChanged(1.0, ""))
        self._dispatcher.post(OperationFinished("update", True))

    def _on_error(self, tag: str):
        op = self._op or "update"
        self.state.running = False
        self.state.client_ready = False
        self._op = None
        self._dispatcher.post(ProgressChanged(0.0, ""))
        self._dispatcher.post(OperationFailed(op, ""))

    def _on_manifest_available(self, tag: str):
        # A valid manifest was fetched and parsed — the update button may
        # offer its real verdict again.
        self.state.manifest_available = True

    def _on_manifest_unavailable(self, tag: str):
        # The manifest couldn't be fetched/parsed — treat the client as
        # never verified and gray out the update button.
        self.state.manifest_available = False
        self.state.client_ready = False
        self.state.running = False
        self._op = None
        self._dispatcher.post(ProgressChanged(0.0, ""))
        self._dispatcher.post(OperationFinished("verify", False))

    def _on_up_to_date(self, tag: str):
        self.state.running = False
        self.state.client_ready = True
        self.state.manifest_available = True
        self._op = None
        self._dispatcher.post(ProgressChanged(1.0, ""))
        self._dispatcher.post(OperationFinished("verify", True))

    def _on_update_needed(self, tag: str):
        self.state.running = False
        self.state.client_ready = False
        self.state.manifest_available = True
        self._op = None
        self._dispatcher.post(ProgressChanged(0.0, ""))
        self._dispatcher.post(OperationFinished("verify", False))

    def _on_diff_tree(self, tag: str):
        self.state.diff_nodes = tag
        if tag:
            self._dispatcher.post(UpdateFilesList(_flatten_diff_tree(tag)))

    def _on_torrent_reachable(self, tag: str):
        self.state.torrent_reachable = True

    def _on_torrent_unreachable(self, tag: str):
        # No manifest and the BitTorrent snapshot can't be fetched — don't
        # offer a dead recovery download; the UI falls back to PLAY (or a
        # disabled UPDATE) instead.
        self._torrent_failure(tag, reachable=False)

    def _on_torrent_corrupt(self, tag: str):
        # Torrent file downloaded but malformed — cannot use for recovery.
        self._torrent_failure(tag, reachable=False)

    def _on_torrent_stalled(self, tag: str):
        # Verification stalled (low/no peers) — torrent reachable, offer
        # recovery download on next launch.
        self._torrent_failure(tag, reachable=True)

    def _on_torrent_session_error(self, tag: str):
        # Session creation failed — torrent reachable, offer retry.
        self._torrent_failure(tag, reachable=True)

    def _on_torrent_disk_error(self, tag: str):
        # Disk full / permission denied — torrent reachable, show error.
        self._torrent_failure(tag, reachable=True)

    def _on_torrent_verify_failed(self, tag: str):
        # The snapshot was fetched but libtorrent recheck failed — the
        # torrent IS reachable, so offer recovery download.
        self._torrent_failure(tag, reachable=True)

    def _torrent_failure(self, tag: str, *, reachable: bool):
        """Common landing for failed torrent verifications: end the
        operation, remember the error, and gate the recovery offer on
        whether the snapshot itself was reachable."""
        self.state.running = False
        self.state.client_ready = False
        self.state.manifest_available = False
        self.state.torrent_reachable = reachable
        self.state.torrent_error = tag or None
        self.state.torrent_stale = None
        op = self._op or "verify"
        self._op = None
        self._dispatcher.post(ProgressChanged(0.0, ""))
        self._dispatcher.post(OperationFinished(op, False))

    def _on_torrent_diff(self, tag: str):
        # A manifest-less verify against the BitTorrent snapshot found
        # stale files: remember which ones so the update only fetches
        # them. The manifest stays unavailable so a later verify still
        # re-checks, but the client is not ready yet.
        self.state.running = False
        self.state.client_ready = False
        self.state.manifest_available = False
        self.state.torrent_stale = list(tag) if tag else []
        self._op = None
        self._dispatcher.post(ProgressChanged(0.0, ""))
        self._dispatcher.post(
            UpdateFilesList(sorted(self.state.torrent_stale))
        )
        self._dispatcher.post(OperationFinished("verify", False))

    def _on_torrent_up_to_date(self, tag: str):
        # The client already matches the BitTorrent snapshot even though
        # no manifest was ever fetched.
        self.state.running = False
        self.state.client_ready = True
        self.state.manifest_available = False
        self.state.torrent_stale = None
        self._op = None
        self._dispatcher.post(ProgressChanged(1.0, ""))
        self._dispatcher.post(OperationFinished("verify", True))

    def _on_torrent_recovery_done(self, tag: str):
        # A manifest-less BitTorrent recovery install finished: the client
        # files are present and piece-hash verified, but no manifest was
        # ever fetched — keep manifest_available False so the next verify
        # still re-checks everything.
        self.state.running = False
        self.state.client_ready = True
        self.state.manifest_available = False
        self._op = None
        self._dispatcher.post(ProgressChanged(1.0, ""))
        self._dispatcher.post(OperationFinished("update", True))

    _MARKER_HANDLERS = {
        markers.DONE: _on_done,
        markers.ERROR: _on_error,
        markers.MANIFEST_AVAILABLE: _on_manifest_available,
        markers.MANIFEST_UNAVAILABLE: _on_manifest_unavailable,
        markers.UP_TO_DATE: _on_up_to_date,
        markers.UPDATE_NEEDED: _on_update_needed,
        markers.DIFF_TREE: _on_diff_tree,
        markers.TORRENT_REACHABLE: _on_torrent_reachable,
        markers.TORRENT_UNREACHABLE: _on_torrent_unreachable,
        markers.TORRENT_CORRUPT: _on_torrent_corrupt,
        markers.TORRENT_STALLED: _on_torrent_stalled,
        markers.TORRENT_SESSION_ERROR: _on_torrent_session_error,
        markers.TORRENT_DISK_ERROR: _on_torrent_disk_error,
        markers.TORRENT_VERIFY_FAILED: _on_torrent_verify_failed,
        markers.TORRENT_DIFF: _on_torrent_diff,
        markers.TORRENT_UP_TO_DATE: _on_torrent_up_to_date,
        markers.TORRENT_RECOVERY_DONE: _on_torrent_recovery_done,
    }

    def _mods_have_errors(self) -> bool:
        return any(
            bool(s.get("error"))
            for s in load_config().get("mods", {}).values()
        )
