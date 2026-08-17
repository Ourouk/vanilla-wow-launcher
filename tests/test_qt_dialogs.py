"""Headless Qt tests for the session-log window and custom-addon dialog.

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The QApplication is shared through create_qt_app();
the hub/window fixtures force a non-first-run state so the auto-opened
Settings dialog is never scheduled, and the first-run flow is exercised in
its own dedicated test with the right state monkeypatched in.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
)

import vanilla_wow_launcher.controllers.settings as settings_controller
import vanilla_wow_launcher.core.platform_support as platform_support
import vanilla_wow_launcher.ui.qt.main_window as mw
from vanilla_wow_launcher.core.log_sink import log
from vanilla_wow_launcher.state.events import LogMessage
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.auto_install_dialog import AutoInstallDialog
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.custom_addon_dialog import CustomAddonDialog
from vanilla_wow_launcher.ui.qt.log_window import LogWindow
from vanilla_wow_launcher.ui.qt.main_window import MainWindow
from vanilla_wow_launcher.ui.qt.settings_dialog import SettingsDialog
from vanilla_wow_launcher.ui.qt.theme import Palette


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def hub(qapp):
    hub = ControllerHub()
    hub.settings.state.first_run = False
    hub.settings.state.first_run_av_pending = False
    hub.settings.state.first_run_auto_install_pending = False
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def _text(win: LogWindow) -> str:
    return win.findChild(QPlainTextEdit, "logText").toPlainText()


# ── LogWindow ─────────────────────────────────────────────────────────────


def test_log_window_appends_ok_line(qapp):
    win = LogWindow(Palette())
    win.append("hello ok\n", "ok")
    assert "hello ok" in _text(win)
    assert win.objectName() == "logWindow"


def test_log_window_seed_renders_buffer(qapp):
    win = LogWindow(Palette())
    win.seed([("one\n", "ok"), ("two\n", "err"), ("three\n", "")])
    text = _text(win)
    assert "one\ntwo\nthree" in text


# ── MainWindow session log ────────────────────────────────────────────────


def test_show_logs_creates_and_reuses_window(qapp, window):
    window._on_show_logs_requested()
    first = window._logWindow
    assert isinstance(first, LogWindow)
    assert first.isVisible()
    window._on_show_logs_requested()
    assert window._logWindow is first


def test_show_logs_recreates_after_close(qapp, window):
    window._on_show_logs_requested()
    first = window._logWindow
    first.close()
    QTest.qWait(50)
    assert window._logWindow is None
    window._on_show_logs_requested()
    assert window._logWindow is not None
    assert window._logWindow is not first


def test_log_message_event_reaches_log_window(qapp, window):
    window._on_show_logs_requested()
    win = window._logWindow
    window._hub.dispatcher.post(LogMessage("controller line\n", "ok"))
    QTest.qWait(200)
    assert "controller line" in _text(win)
    assert ("controller line\n", "ok") in window._log_buffer


def test_log_message_untagged_gets_auto_tag(qapp, window):
    window._on_show_logs_requested()
    win = window._logWindow
    window._hub.dispatcher.post(LogMessage("some failure happened", ""))
    QTest.qWait(200)
    assert ("some failure happened\n", "err") in window._log_buffer
    assert "some failure happened" in _text(win)


def test_global_log_sink_queue_reaches_log_window(qapp, window):
    window._on_show_logs_requested()
    win = window._logWindow
    log("worker line\n", "err")
    QTest.qWait(200)
    assert "worker line" in _text(win)
    assert ("worker line\n", "err") in window._log_buffer


def test_log_window_seeds_existing_buffer_on_open(qapp, window):
    window._hub.dispatcher.post(LogMessage("before open\n", "acct"))
    QTest.qWait(200)
    window._on_show_logs_requested()
    win = window._logWindow
    assert "before open" in _text(win)


# ── CustomAddonDialog ─────────────────────────────────────────────────────


def _open_dialog():
    dlg = CustomAddonDialog(Palette())
    dlg.show()
    return dlg


def test_custom_addon_valid_url_emits_and_closes(qapp):
    dlg = _open_dialog()
    spy = Mock()
    dlg.addonRequested.connect(spy)
    dlg.findChild(QLineEdit, "customAddonUrl").setText(
        "https://github.com/Org/Repo"
    )
    QTest.mouseClick(
        dlg.findChild(QPushButton, "customAddonInstall"), Qt.LeftButton
    )
    spy.assert_called_once()
    rec = spy.call_args[0][0]
    assert rec == {
        "folder": "Repo",
        "status": "available",
        "git": "https://github.com/Org/Repo",
        "branch": None,
        "ref": None,
        "toc": {},
        "description": None,
        "error": None,
    }
    assert not dlg.isVisible()


def test_custom_addon_normalizes_git_and_trailing_slash(qapp):
    cases = (
        ("https://github.com/Org/Repo.git", "https://github.com/Org/Repo"),
        ("https://github.com/Org/Repo/", "https://github.com/Org/Repo"),
        ("https://github.com/Org/Repo.git/", "https://github.com/Org/Repo"),
    )
    for url, expected in cases:
        dlg = _open_dialog()
        spy = Mock()
        dlg.addonRequested.connect(spy)
        dlg.findChild(QLineEdit, "customAddonUrl").setText(url)
        dlg.findChild(QPushButton, "customAddonInstall").click()
        spy.assert_called_once()
        rec = spy.call_args[0][0]
        assert rec["git"] == expected, url
        assert rec["folder"] == "Repo", url


def test_custom_addon_bad_host_shows_error_and_stays(qapp):
    dlg = _open_dialog()
    spy = Mock()
    dlg.addonRequested.connect(spy)
    dlg.findChild(QLineEdit, "customAddonUrl").setText(
        "https://example.com/Org/Repo"
    )
    QTest.mouseClick(
        dlg.findChild(QPushButton, "customAddonInstall"), Qt.LeftButton
    )
    spy.assert_not_called()
    assert dlg.isVisible()
    assert (
        dlg.findChild(QLabel, "customAddonError").text()
        == "URL must be https from an allowed host."
    )


def test_custom_addon_host_only_url_folder_error(qapp):
    dlg = _open_dialog()
    spy = Mock()
    dlg.addonRequested.connect(spy)
    dlg.findChild(QLineEdit, "customAddonUrl").setText("https://github.com")
    QTest.mouseClick(
        dlg.findChild(QPushButton, "customAddonInstall"), Qt.LeftButton
    )
    spy.assert_not_called()
    assert dlg.isVisible()
    assert (
        dlg.findChild(QLabel, "customAddonError").text()
        == "Could not derive addon folder name."
    )


def test_custom_addon_hint_lists_allowed_hosts(qapp):
    import vanilla_wow_launcher.services.addons as addons

    dlg = CustomAddonDialog(Palette())
    hint = dlg.findChild(QLabel, "customAddonHint")
    assert hint.text() == "Allowed hosts: " + ", ".join(addons.ADDON_GIT_HOSTS)


# ── MainWindow custom addon ───────────────────────────────────────────────


def test_custom_addon_from_main_window_applies(qapp, window, monkeypatch):
    hub = window._hub
    apply_mock = Mock()
    monkeypatch.setattr(hub.addons, "apply", apply_mock)

    window._on_custom_addon_requested()
    dlg = window._customAddonDialog
    assert isinstance(dlg, CustomAddonDialog)
    dlg.findChild(QLineEdit, "customAddonUrl").setText(
        "https://github.com/Org/Repo"
    )
    dlg.findChild(QPushButton, "customAddonInstall").click()
    apply_mock.assert_called_once()
    rec = apply_mock.call_args[0][0][0]
    assert rec["folder"] == "Repo"
    assert rec["git"] == "https://github.com/Org/Repo"


# ── first-run flow ────────────────────────────────────────────────────────


def test_first_run_opens_settings_and_av_prompt(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(platform_support, "can_manage_antivirus", lambda: True)
    monkeypatch.setattr(
        settings_controller, "CONFIG_FILE", str(tmp_path / "no-config.json")
    )
    hub = ControllerHub()
    assert hub.settings.state.first_run is True
    assert hub.settings.state.first_run_av_pending is True

    win = MainWindow(hub)
    win.show()
    try:
        QTest.qWait(800)
        dialog = win._settingsDialog
        assert isinstance(dialog, SettingsDialog)
        assert dialog.isVisible()

        asked = []
        monkeypatch.setattr(
            QMessageBox,
            "question",
            staticmethod(
                lambda *a, **k: (asked.append(True), QMessageBox.Yes)[1]
            ),
        )
        # The first-run auto-install prompt fires before the AV prompt on the
        # same settings close — skip it so only the AV flow is exercised.
        _FakeAutoInstallDialog.created = 0
        _FakeAutoInstallDialog.result = QDialog.Rejected
        monkeypatch.setattr(mw, "AutoInstallDialog", _FakeAutoInstallDialog)
        allow = Mock()
        dismissed = Mock()
        monkeypatch.setattr(hub.settings, "allow_through_antivirus", allow)
        monkeypatch.setattr(hub.settings, "av_prompt_dismissed", dismissed)

        dialog.close()
        QTest.qWait(50)
        assert asked
        allow.assert_called_once()
        dismissed.assert_called_once()
    finally:
        win.close()
        hub.close()


# ── first-run auto-install prompt ─────────────────────────────────────────


class _FakeAutoInstallDialog:
    """Stand-in for AutoInstallDialog used by MainWindow._maybe_prompt_auto_install.

    Result / checkbox answers live on the class so each test can set them
    before driving the prompt; `created` counts instantiations so tests can
    assert whether the prompt was shown at all.
    """

    result = QDialog.Accepted
    mods_checked = True
    addons_checked = True
    created = 0

    def __init__(self, *args, **kwargs):
        type(self).created += 1

    def exec(self):
        return self.result


@pytest.fixture()
def _isolated_config(monkeypatch, tmp_path):
    """In-memory config store + a non-existent CONFIG_FILE so the settings
    controller never touches the real per-user config."""
    state = {"out_dir": "/tmp/game-folder"}
    monkeypatch.setattr(
        settings_controller.config_store, "load_config", lambda: state
    )
    monkeypatch.setattr(
        settings_controller.config_store,
        "update_config",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        settings_controller, "CONFIG_FILE", str(tmp_path / "no-config.json")
    )
    return state


def test_auto_install_dialog_defaults_and_properties(qapp):
    dlg = AutoInstallDialog(Palette())
    assert dlg.objectName() == "autoInstallDialog"
    mods = dlg.findChild(QCheckBox, "autoInstallMods")
    addons = dlg.findChild(QCheckBox, "autoInstallAddons")
    assert mods is not None and mods.isChecked()
    assert addons is not None and addons.isChecked()
    assert dlg.mods_checked is True
    assert dlg.addons_checked is True

    mods.setChecked(False)
    assert dlg.mods_checked is False
    assert dlg.addons_checked is True


def test_first_run_prompt_accept_installs_checked(
    qapp, monkeypatch, _isolated_config
):
    hub = ControllerHub()
    hub.settings.state.first_run = True
    hub.settings.state.first_run_verify_pending = False
    hub.settings.state.first_run_av_pending = False
    hub.settings.state.first_run_auto_install_pending = True
    _FakeAutoInstallDialog.created = 0
    _FakeAutoInstallDialog.result = QDialog.Accepted
    _FakeAutoInstallDialog.mods_checked = True
    _FakeAutoInstallDialog.addons_checked = True
    monkeypatch.setattr(mw, "AutoInstallDialog", _FakeAutoInstallDialog)

    win = MainWindow(hub)
    win.show()
    try:
        win._on_settings_finished()
        assert _FakeAutoInstallDialog.created == 1
        assert hub.settings.state.first_run_auto_install_pending is False
        assert hub.settings.state.pending_auto_mods is True
        assert hub.settings.state.pending_auto_addons is True
    finally:
        win.close()
        hub.close()


def test_first_run_prompt_accept_partial_install(
    qapp, monkeypatch, _isolated_config
):
    hub = ControllerHub()
    hub.settings.state.first_run_auto_install_pending = True
    hub.settings.state.first_run_verify_pending = False
    hub.settings.state.first_run_av_pending = False
    _FakeAutoInstallDialog.created = 0
    _FakeAutoInstallDialog.result = QDialog.Accepted
    _FakeAutoInstallDialog.mods_checked = True
    _FakeAutoInstallDialog.addons_checked = False
    monkeypatch.setattr(mw, "AutoInstallDialog", _FakeAutoInstallDialog)

    win = MainWindow(hub)
    win.show()
    try:
        win._on_settings_finished()
        assert hub.settings.state.pending_auto_mods is True
        assert hub.settings.state.pending_auto_addons is False
    finally:
        win.close()
        hub.close()


def test_first_run_prompt_skip_disables_both(
    qapp, monkeypatch, _isolated_config
):
    hub = ControllerHub()
    hub.settings.state.first_run_auto_install_pending = True
    hub.settings.state.first_run_verify_pending = False
    hub.settings.state.first_run_av_pending = False
    _FakeAutoInstallDialog.created = 0
    _FakeAutoInstallDialog.result = QDialog.Rejected
    monkeypatch.setattr(mw, "AutoInstallDialog", _FakeAutoInstallDialog)

    win = MainWindow(hub)
    win.show()
    try:
        win._on_settings_finished()
        assert _FakeAutoInstallDialog.created == 1
        assert hub.settings.state.first_run_auto_install_pending is False
        assert hub.settings.state.pending_auto_mods is False
        assert hub.settings.state.pending_auto_addons is False
    finally:
        win.close()
        hub.close()


def test_first_run_prompt_skipped_when_not_pending(
    qapp, monkeypatch, _isolated_config
):
    hub = ControllerHub()
    hub.settings.state.first_run_verify_pending = False
    hub.settings.state.first_run_av_pending = False
    hub.settings.state.first_run_auto_install_pending = False
    _FakeAutoInstallDialog.created = 0
    monkeypatch.setattr(mw, "AutoInstallDialog", _FakeAutoInstallDialog)

    win = MainWindow(hub)
    win.show()
    try:
        win._on_settings_finished()
        assert _FakeAutoInstallDialog.created == 0
        assert hub.settings.state.pending_auto_mods is False
        assert hub.settings.state.pending_auto_addons is False
    finally:
        win.close()
        hub.close()
