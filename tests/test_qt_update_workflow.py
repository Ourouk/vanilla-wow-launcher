"""Headless Qt tests for the connected update/PLAY footer workflow.

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The QApplication is created once and shared through
the create_qt_app() singleton; each test builds a fresh ControllerHub +
MainWindow and shows it so child-widget visibility can be asserted. Real
controller/network entry points (verify, update, launch, news/mod/addon
fetches) are monkeypatched so nothing touches disk or the network.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from vanilla_wow_launcher.controllers.update import Readiness
from vanilla_wow_launcher.core.constants import UPDATER_VERSION
from vanilla_wow_launcher.state.events import (
    OperationFailed,
    OperationFinished,
    ProgressChanged,
    StatusChanged,
    UpdateFilesList,
)
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.main_window import MainWindow
from vanilla_wow_launcher.ui.qt.theme import Palette

_PLAY = Readiness("play", "PLAY", "Everything up to date!")
_UPDATE = Readiness("update", "UPDATE", "Update available!")
_CHECKING = Readiness("busy", "Checking…", "Verifying…")
_INSTALLING = Readiness("busy", "Installing…", "Downloading addons…")
_DISABLED = Readiness("disabled", "UPDATE", "Manifest unavailable")
_TERMINATE = Readiness(
    "terminate", "TERMINATE", "Running WoW.exe — click TERMINATE to quit"
)


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def window(qapp, monkeypatch):
    hub = ControllerHub()
    hub.settings.state.path = "/tmp/game-folder"
    hub.settings.state.first_run = False
    hub.settings.state.first_run_av_pending = False
    hub.settings.state.first_run_verify_pending = False
    # A previously verified setup: a manifest has been fetched, so the
    # real compute_readiness baseline is the gold UPDATE button.
    hub.updater.state.manifest_available = True
    monkeypatch.setattr(hub.updater, "start_verify", Mock())
    monkeypatch.setattr(hub.updater, "start_update", Mock())
    monkeypatch.setattr(
        hub.updater, "launch_game", Mock(return_value=(True, False))
    )
    monkeypatch.setattr(hub.updater, "check_updater_update", Mock())
    monkeypatch.setattr(hub.news, "load", Mock())
    monkeypatch.setattr(hub.mods, "load_latest_versions", Mock())
    monkeypatch.setattr(hub.addons, "verify", Mock())
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


# ── button readiness ──────────────────────────────────────────────────────


def test_initial_button_shows_update_until_ready(qapp, window, monkeypatch):
    hub = window._hub
    palette = Palette()
    assert window._updateButton.objectName() == "updateButton"
    assert window._updateButton.text() == "UPDATE"
    assert window._updateButton.isEnabled()

    monkeypatch.setattr(
        hub.updater, "compute_readiness", lambda addons_installing=False: _PLAY
    )
    window._refresh_ready_state()
    assert window._updateButton.text() == "PLAY"
    assert window._updateButton.isEnabled()
    assert palette.green_btn.name() in window._updateButton.styleSheet()
    assert window._statusLabel.text() == _PLAY.status

    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _UPDATE,
    )
    window._refresh_ready_state()
    assert window._updateButton.text() == "UPDATE"
    assert palette.green_btn.name() not in window._updateButton.styleSheet()
    assert palette.gold.name() in window._updateButton.styleSheet()


def test_busy_readiness_disables_button(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _CHECKING,
    )
    window._refresh_ready_state()
    assert window._updateButton.text() == "Checking…"
    assert not window._updateButton.isEnabled()

    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _INSTALLING,
    )
    window._refresh_ready_state()
    assert window._updateButton.text() == "Installing…"
    assert not window._updateButton.isEnabled()


def test_disabled_readiness_grays_update_button(qapp, window, monkeypatch):
    """No manifest available → the button keeps the UPDATE label but is
    grayed out and unclickable."""
    hub = window._hub
    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _DISABLED,
    )
    window._refresh_ready_state()
    assert window._updateButton.text() == "UPDATE"
    assert not window._updateButton.isEnabled()
    assert window._statusLabel.text() == "Manifest unavailable"

    window._updateButton.click()
    hub.updater.start_update.assert_not_called()
    hub.updater.launch_game.assert_not_called()


# ── button clicks ─────────────────────────────────────────────────────────


def test_click_play_launches_game(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater, "compute_readiness", lambda addons_installing=False: _PLAY
    )
    window._refresh_ready_state()

    window._updateButton.click()

    hub.updater.launch_game.assert_called_once_with()
    hub.updater.start_update.assert_not_called()
    assert window._updateButton.text() == "PLAY"
    assert not window._updateButton.isEnabled()
    assert window._statusLabel.text() == "Launching..."


def test_click_play_shows_dxvk_notice(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater, "compute_readiness", lambda addons_installing=False: _PLAY
    )
    hub.updater.launch_game.return_value = (True, True)
    informed = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda *a, **k: informed.append(True)),
    )

    window._updateButton.click()

    assert informed == [True]
    hub.updater.launch_game.assert_called_once_with()


def test_click_update_starts_update(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _UPDATE,
    )
    window._refresh_ready_state()

    window._updateButton.click()

    hub.updater.start_update.assert_called_once_with()
    hub.updater.launch_game.assert_not_called()
    assert window._stack.currentIndex() == window._pages["UPDATE"]


def test_click_terminate_terminates_game(qapp, window, monkeypatch):
    hub = window._hub
    terminate = Mock(return_value=True)
    monkeypatch.setattr(hub.updater, "terminate_game", terminate)
    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _TERMINATE,
    )
    window._refresh_ready_state()

    assert window._updateButton.text() == "TERMINATE"
    assert window._updateButton.isEnabled()

    window._updateButton.click()

    terminate.assert_called_once_with()
    hub.updater.launch_game.assert_not_called()
    assert window._statusLabel.text() == "Terminating…"


def test_click_ignored_while_running(qapp, window, monkeypatch):
    hub = window._hub
    hub.updater.state.running = True
    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _CHECKING,
    )
    window._refresh_ready_state()
    assert not window._updateButton.isEnabled()

    window._updateButton.click()

    hub.updater.launch_game.assert_not_called()
    hub.updater.start_update.assert_not_called()


# ── progress ──────────────────────────────────────────────────────────────


def test_progress_changed_drives_bar(qapp, window):
    window._onProgressChanged(0.5, "Downloading…")
    assert window._progressBar.value() == 50
    assert window._progressBar.isVisible()
    assert window._progressLabel.text() == "Downloading…"

    window._onProgressChanged(0.0, "")
    assert not window._progressBar.isVisible()
    assert window._progressBar.value() == 0


# ── operation finished / failed ───────────────────────────────────────────


def test_update_finished_updates_version_and_readiness(
    qapp, window, monkeypatch
):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater, "compute_readiness", lambda addons_installing=False: _PLAY
    )
    hub.updater.state.client_version = "1.14.3"

    hub.dispatcher.post(OperationFinished("update", True, ""))
    QTest.qWait(200)

    assert window._versionLabel.text() == "1.14.3"
    assert window._updateButton.text() == "PLAY"
    assert window._statusLabel.text() == _PLAY.status


def test_mods_finished_rerenders_mods_panel(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _UPDATE,
    )
    panel = window._stack.widget(window._pages["MODS"])
    panel._running = True

    hub.dispatcher.post(OperationFinished("mods", True, ""))
    QTest.qWait(200)

    assert panel._running is False
    assert window._versionLabel.text() == f"v{UPDATER_VERSION}"
    assert window._updateButton.text() == "UPDATE"


def test_operation_failed_refreshes_readiness(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.updater, "compute_readiness", lambda addons_installing=False: _PLAY
    )
    window._refresh_ready_state()
    assert window._updateButton.text() == "PLAY"

    monkeypatch.setattr(
        hub.updater,
        "compute_readiness",
        lambda addons_installing=False: _UPDATE,
    )
    hub.dispatcher.post(OperationFailed("update", "boom"))
    QTest.qWait(200)
    assert window._updateButton.text() == "UPDATE"


# ── worker polling (event-loop driven) ────────────────────────────────────


def test_poll_timer_processes_worker_completion(qapp, window):
    """The event-loop poll must drain the update controller's queues without
    a manual poll() call — a worker's __DONE__ marker has to unstick the
    busy state and finish the operation."""
    hub = window._hub
    spy = []
    hub.bridge.operationFinished.connect(lambda *a: spy.append(a))

    hub.updater.state.running = True
    hub.updater._log_q.put(("__DONE__", ""))

    QTest.qWait(250)

    assert hub.updater.state.running is False
    assert hub.updater.state.client_ready is True
    assert ("update", True, "") in spy


def test_poll_timer_renders_update_available(qapp, window):
    """check_updater_update() sets the flag in a worker thread; the poll
    timer must surface it as the header 'Update available!' label."""
    hub = window._hub
    assert not window._updateAvailableLabel.isVisible()

    hub.updater.updater_update_available = True
    QTest.qWait(250)
    assert window._updateAvailableLabel.isVisible()

    hub.updater.updater_update_available = False
    QTest.qWait(250)
    assert not window._updateAvailableLabel.isVisible()


def test_close_cancels_active_workers(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(hub.updater, "cancel", Mock())
    window.close()
    hub.updater.cancel.assert_called_once_with()


# ── first-run verify deferral ─────────────────────────────────────────────


def test_first_run_defers_verify_until_settings_close(qapp, window):
    hub = window._hub
    hub.settings.state.first_run_verify_pending = True
    hub.updater.start_verify.reset_mock()

    window.schedule_startup_tasks()
    hub.updater.start_verify.assert_not_called()
    hub.news.load.assert_not_called()

    window._on_settings_finished()
    assert hub.settings.state.first_run_verify_pending is False
    hub.updater.start_verify.assert_not_called()

    QTest.qWait(250)
    hub.updater.start_verify.assert_called_once_with(True)
    hub.news.load.assert_not_called()


def test_startup_auto_verifies_when_not_first_run(qapp, window):
    hub = window._hub
    hub.updater.start_verify.reset_mock()

    window.schedule_startup_tasks()

    QTest.qWait(700)
    hub.updater.start_verify.assert_called_once_with(False)
    hub.news.load.assert_called_once_with()


def test_startup_skips_client_verify_when_disabled(qapp, window):
    hub = window._hub
    hub.settings.state.config["client_update_enabled"] = False
    hub.updater.start_verify.reset_mock()
    window.schedule_startup_tasks()
    QTest.qWait(700)
    hub.updater.start_verify.assert_not_called()


# ── UPDATE tab placement ────────────────────────────────────────────────────


def test_update_tab_is_next_to_news():
    assert MainWindow.TABS.index("UPDATE") == MainWindow.TABS.index("NEWS") + 1


def test_update_panel_has_no_pieces_field(qapp, window):
    panel = window._stack.widget(window._pages["UPDATE"])
    assert (
        panel.findChild(type(panel), "updatePieces") is None
        or panel.findChild(object, "updatePieces") is None
    )


# ── updated-files list ──────────────────────────────────────────────────────


def test_update_files_list_populates_and_progress_marks_done(qapp, window):
    hub = window._hub
    panel = window._stack.widget(window._pages["UPDATE"])

    hub.dispatcher.post(UpdateFilesList(["Data/foo.mpq", "WoW.exe"]))
    QTest.qWait(150)
    texts = [
        panel._file_list.item(i).text()
        for i in range(panel._file_list.count())
    ]
    assert texts == ["Data/foo.mpq", "WoW.exe"]

    # HTTP progress marks the matching file done.
    hub.dispatcher.post(
        ProgressChanged(
            0.5,
            "Data/foo.mpq",
            phase="Downloading",
            transport="HTTP",
            current_file="Data/foo.mpq",
            downloaded=10240,
            total=20480,
            speed=5120.0,
        )
    )
    QTest.qWait(150)
    assert panel._transport.text() == "HTTP"
    assert "/" in panel._amount.text()
    assert "10 KB" in panel._amount.text()
    assert panel._speed.text() != "-"
    done = [
        panel._file_list.item(i).data(
            256  # Qt.UserRole
        )
        for i in range(panel._file_list.count())
    ]
    assert done[0] is True

    # An unknown streamed file is appended.
    hub.dispatcher.post(
        ProgressChanged(
            0.6,
            "Data/other.mpq",
            phase="Downloading",
            transport="HTTP",
            current_file="Data/other.mpq",
        )
    )
    QTest.qWait(150)
    texts = [
        panel._file_list.item(i).text()
        for i in range(panel._file_list.count())
    ]
    assert "Data/other.mpq" in texts


def test_update_files_list_persists_through_update(qapp, window):
    hub = window._hub
    panel = window._stack.widget(window._pages["UPDATE"])

    hub.dispatcher.post(UpdateFilesList(["Data/foo.mpq"]))
    QTest.qWait(150)
    assert panel._file_list.count() == 1

    # The needed-files list must stay visible while the actual update runs (a
    # torrent download reports no per-file paths to re-add), so it is not
    # cleared when the operation moves from verifying to updating.
    hub.dispatcher.post(StatusChanged("Updating…"))
    QTest.qWait(150)
    assert panel._file_list.count() == 1

    # A fresh list of needed files replaces it.
    hub.dispatcher.post(UpdateFilesList(["Data/bar.mpq"]))
    QTest.qWait(150)
    texts = [
        panel._file_list.item(i).text()
        for i in range(panel._file_list.count())
    ]
    assert texts == ["Data/bar.mpq"]


# ── informative fields (Method / Progress / Speed / Peers) ──────────────────


def test_informative_fields_render_http_and_bit_torrent(qapp, window):
    hub = window._hub
    panel = window._stack.widget(window._pages["UPDATE"])

    hub.dispatcher.post(
        ProgressChanged(
            0.3,
            "HTTP download",
            phase="Downloading",
            transport="HTTP",
            current_file="Data/a.mpq",
            downloaded=30,
            total=100,
            speed=1024.0,
            peers=0,
        )
    )
    QTest.qWait(150)
    assert panel._transport.text() == "HTTP"
    assert panel._peers.text() == "-"

    hub.dispatcher.post(
        ProgressChanged(
            0.4,
            "wow-client",
            phase="Downloading",
            transport="BitTorrent",
            current_file="wow-client",
            downloaded=40,
            total=100,
            speed=2048.0,
            peers=7,
        )
    )
    QTest.qWait(150)
    assert panel._transport.text() == "BitTorrent"
    assert panel._peers.text() == "7"
    assert panel._speed.text() != "-"
    # Torrent name has no "/", so it must NOT be appended to the file list.
    texts = [
        panel._file_list.item(i).text()
        for i in range(panel._file_list.count())
    ]
    assert "wow-client" not in texts
