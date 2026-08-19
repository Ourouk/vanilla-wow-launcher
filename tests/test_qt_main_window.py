"""Headless Qt tests for the main window shell.

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The QApplication is created once and shared through
the create_qt_app() singleton; each test builds a fresh ControllerHub +
MainWindow and shows it so child-widget visibility (e.g. the progress bar)
can be asserted. QTest.qWait drives the event loop so the bridge's QTimer
fires.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from vanilla_wow_launcher.core import launcher
from vanilla_wow_launcher.state.events import (
    OperationFailed,
    OperationFinished,
    ProgressChanged,
    StatusChanged,
)
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.main_window import MainWindow


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def window(qapp):
    hub = ControllerHub()
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def test_construction_sets_title_and_default_tab(qapp, window):
    assert window.windowTitle() == "Vanilla WoW Launcher"
    assert window._stack.count() == 5
    assert window._pages == {
        name: idx for idx, name in enumerate(MainWindow.TABS)
    }
    assert window._stack.currentIndex() == 0
    assert window._navButtons["NEWS"].isChecked()
    assert window._discordButton is None


def test_discord_button_opens_configured_url(qapp, monkeypatch):
    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://launcher.test"},
            "discord_url": "https://discord.gg/example",
        }
    )
    hub = ControllerHub()
    win = MainWindow(hub)
    win.show()
    try:
        opened = []
        monkeypatch.setattr(
            "vanilla_wow_launcher.ui.qt.main_window.webbrowser.open",
            opened.append,
        )
        assert win._discordButton is not None
        assert win._discordButton.text() == "DISCORD"
        win._discordButton.click()
        assert opened == ["https://discord.gg/example"]
    finally:
        win.close()


def test_switch_tab_changes_stack_and_checked_state(qapp, window):
    window.switch_tab("MODS")
    assert window._stack.currentIndex() == window._pages["MODS"]
    assert window._navButtons["MODS"].isChecked()
    assert not window._navButtons["NEWS"].isChecked()


def test_switch_tab_unknown_name_is_noop(qapp, window):
    before = window._stack.currentIndex()
    window.switch_tab("UNKNOWN")
    assert window._stack.currentIndex() == before


def test_update_progress_renders_in_update_panel(qapp, window):
    hub = window._hub
    hub.dispatcher.post(
        ProgressChanged(
            0.5,
            "patch.mpq",
            phase="Downloading",
            transport="BitTorrent",
            current_file="patch.mpq",
            downloaded=512 * 1024,
            total=1024 * 1024,
            speed=2 * 1024 * 1024,
            peers=4,
        )
    )
    QTest.qWait(200)

    panel = window._stack.widget(window._pages["UPDATE"])
    assert panel._phase.text() == "Downloading"
    assert panel._transport.text() == "BitTorrent"
    assert panel._file.text() == "patch.mpq"
    assert panel._progress.value() == 50
    assert panel._peers.text() == "4"


def test_empty_footer_progress_does_not_reset_update_phase(qapp, window):
    hub = window._hub
    hub.dispatcher.post(StatusChanged("Updating…"))
    hub.dispatcher.post(ProgressChanged(0.0, ""))
    QTest.qWait(200)

    panel = window._stack.widget(window._pages["UPDATE"])
    assert panel._phase.text() == "Updating…"


def test_status_and_progress_events_reach_footer(qapp, window):
    hub = window._hub
    hub.dispatcher.post(StatusChanged("Ready to update"))
    hub.dispatcher.post(ProgressChanged(0.5, "Downloading…"))
    QTest.qWait(200)

    assert window._statusLabel.text() == "Ready to update"
    assert window._progressBar.value() == 50
    assert window._progressLabel.text() == "Downloading…"
    assert window._progressBar.isVisible()


def test_progress_bar_hides_when_idle(qapp, window):
    hub = window._hub
    hub.dispatcher.post(ProgressChanged(0.5, "Downloading…"))
    QTest.qWait(200)
    assert window._progressBar.isVisible()

    hub.dispatcher.post(ProgressChanged(0.0, ""))
    QTest.qWait(200)
    assert not window._progressBar.isVisible()
    assert window._progressBar.value() == 0

    hub.dispatcher.post(ProgressChanged(1.0, ""))
    QTest.qWait(200)
    assert not window._progressBar.isVisible()
    assert window._progressBar.value() == 100


def test_operation_events_flip_button_state(qapp, window, monkeypatch):
    hub = window._hub
    # Launch available + no manifest yet → PLAY (the folder may be ready).
    import vanilla_wow_launcher.controllers.update as update_controller

    monkeypatch.setattr(update_controller, "can_launch_client", lambda: True)
    window._refresh_ready_state()
    assert window._updateButton.text() == "PLAY"

    # A finished update marks the client ready on the controller before the
    # event is posted — the footer mirrors that real state.
    hub.updater.state.client_ready = True
    hub.updater.state.manifest_available = True
    hub.dispatcher.post(StatusChanged("all up to date"))
    hub.dispatcher.post(OperationFinished("update", True, "done"))
    QTest.qWait(200)
    assert window._updateButton.text() == "PLAY"

    # A failed update drops readiness back down.
    hub.updater.state.client_ready = False
    hub.dispatcher.post(OperationFailed("update", "boom"))
    QTest.qWait(200)
    assert window._updateButton.text() == "UPDATE"


def test_terminate_readiness_shows_red_enabled_button(
    qapp, window, monkeypatch
):
    import vanilla_wow_launcher.controllers.update as update_controller
    from vanilla_wow_launcher.controllers.update import Readiness

    monkeypatch.setattr(update_controller, "can_launch_client", lambda: True)
    monkeypatch.setattr(
        window._hub.updater,
        "compute_readiness",
        lambda addons_installing=False: Readiness(
            "terminate",
            "TERMINATE",
            "Running WoW.exe — click TERMINATE to quit",
        ),
    )
    window._refresh_ready_state()

    assert window._updateButton.text() == "TERMINATE"
    assert window._updateButton.isEnabled()
    assert "#bf6969" in window._updateButton.styleSheet()


def test_game_events_flip_footer_between_play_and_terminate(
    qapp, window, monkeypatch
):
    import vanilla_wow_launcher.controllers.update as update_controller

    monkeypatch.setattr(update_controller, "can_launch_client", lambda: True)
    hub = window._hub
    hub.updater.state.client_ready = True
    hub.updater.state.manifest_available = True

    # Game starts → footer offers TERMINATE.
    hub.updater.state.game_running = True
    hub.dispatcher.post(
        StatusChanged("Running WoW.exe — click TERMINATE to quit")
    )
    QTest.qWait(200)
    assert window._updateButton.text() == "TERMINATE"
    assert window._updateButton.isEnabled()
    assert "Running WoW.exe" in window._statusLabel.text()

    # Game exits → footer returns to PLAY.
    hub.updater.state.game_running = False
    hub.dispatcher.post(StatusChanged("Game exited."))
    QTest.qWait(200)
    assert window._updateButton.text() == "PLAY"
    assert "Game exited." in window._statusLabel.text()


def test_close_stops_bridge(qapp, window):
    window.close()
    assert not window._hub.bridge._timer.isActive()

    window._hub.dispatcher.post(StatusChanged("after close"))
    QTest.qWait(200)
    assert window._statusLabel.text() != "after close"


def test_startup_tasks_schedule_addons_verify_on_first_run(
    qapp, window, monkeypatch
):
    """The ADDONS verify runs unconditionally, so a first-launch user with an
    uninitialized config still sees the catalog list (the old code skipped it
    when the config had no 'addons' key yet)."""
    hub = window._hub
    hub.settings.state.first_run = True
    hub.settings.state.first_run_verify_pending = True  # defer updater verify
    hub.settings.state.config.pop("addons", None)
    addons_verify = Mock()
    monkeypatch.setattr(hub.addons, "verify", addons_verify)
    monkeypatch.setattr(hub.news, "load", Mock())
    monkeypatch.setattr(hub.mods, "load_latest_versions", Mock())
    monkeypatch.setattr(hub.updater, "check_updater_update", Mock())

    window.schedule_startup_tasks()
    QTest.qWait(1700)

    addons_verify.assert_called_once_with(force=True)


# ── header wordmark ─────────────────────────────────────────────────────────


def test_wordmark_shows_server_name_without_logo(qapp, window):
    assert window._wordmark.text() == "Test Server"


def test_wordmark_shows_themed_logo_pixmap(qapp, tmp_path, monkeypatch):
    launcher.configure_from_dict(
        {
            "server": {"name": "OctoWoW", "base_url": "https://octowow.st"},
            "theme": {"logo": "https://octowow.st/logo.png"},
        }
    )
    png = tmp_path / "logo.png"
    img = QImage(64, 32, QImage.Format_RGB32)
    img.fill(QColor(255, 0, 0))
    assert img.save(str(png), "PNG")
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.logo.fetch_logo", lambda url: str(png)
    )

    hub = ControllerHub()
    win = MainWindow(hub)
    win.show()
    try:
        QTest.qWait(200)
        pix = win._wordmark.pixmap()
        assert pix is not None and not pix.isNull()
        assert pix.height() <= 28
    finally:
        win.close()


def test_wordmark_keeps_server_name_when_logo_fails(
    qapp, tmp_path, monkeypatch
):
    launcher.configure_from_dict(
        {
            "server": {"name": "OctoWoW", "base_url": "https://octowow.st"},
            "theme": {"logo": "https://octowow.st/logo.png"},
        }
    )
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.logo.fetch_logo", lambda url: None
    )

    hub = ControllerHub()
    win = MainWindow(hub)
    win.show()
    try:
        QTest.qWait(200)
        assert win._wordmark.pixmap().isNull()
        assert win._wordmark.text() == "OctoWoW"
    finally:
        win.close()
