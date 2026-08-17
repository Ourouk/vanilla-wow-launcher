"""Headless Qt tests for the settings dialog (qt_settings_dialog).

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The dialog is opened from the main window's gear
button and driven through the same controller methods the Tk overlay drove;
platform gates (Defender exclusions, launch-on-close checkboxes) are flipped
via monkeypatch, and every controller side-effect is mocked so no worker or
network request ever runs.
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
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)

import vanilla_wow_launcher.core.platform_support as platform_support
from vanilla_wow_launcher.core import launcher
from vanilla_wow_launcher.state.events import MirrorStatusChanged
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
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
    # Force a deterministic non-first-run state so closing the Settings
    # dialog never triggers the first-run auto-install prompt (which is
    # modal and would block the offscreen event loop).
    hub.settings.state.first_run = False
    hub.settings.state.first_run_av_pending = False
    hub.settings.state.first_run_verify_pending = False
    hub.settings.state.first_run_auto_install_pending = False
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def _open(window) -> SettingsDialog:
    QTest.mouseClick(window._gearButton, Qt.LeftButton)
    dialog = window._settingsDialog
    assert isinstance(dialog, SettingsDialog)
    QTest.qWait(20)
    return dialog


# ── gear → dialog ───────────────────────────────────────────────────────


def test_gear_opens_settings_dialog(qapp, window):
    assert window._settingsDialog is None
    dialog = _open(window)
    assert dialog.objectName() == "settingsDialog"
    assert dialog.isVisible()
    assert dialog.windowTitle() == "Settings"
    for name in (
        "settingsPath",
        "settingsChange",
        "settingsOpenFolder",
        "settingsMirrorRefresh",
        "settingsVerify",
        "settingsLogs",
        "settingsKoFi",
        "settingsBmc",
        "settingsClose",
        "settingsClientUpdate",
    ):
        assert dialog.findChild(QWidget, name) is not None


def test_gear_reuses_open_dialog(qapp, window):
    first = _open(window)
    QTest.mouseClick(window._gearButton, Qt.LeftButton)
    assert window._settingsDialog is first
    assert first.isVisible()


# ── game folder ─────────────────────────────────────────────────────────


def test_path_field_shows_state_path(qapp, window):
    dialog = _open(window)
    path = dialog.findChild(QLineEdit, "settingsPath")
    assert path.text() == window._hub.settings.state.path
    assert path.isReadOnly()


def test_open_folder_calls_open_client_folder(qapp, window, monkeypatch):
    hub = window._hub
    open_client = Mock()
    monkeypatch.setattr(hub.settings, "open_client_folder", open_client)
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QWidget, "settingsOpenFolder"), Qt.LeftButton
    )
    open_client.assert_called_once()


def test_change_updates_path(qapp, window, monkeypatch, tmp_path):
    hub = window._hub
    chosen = str(tmp_path / "game folder")
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: chosen
    )
    set_path = Mock(return_value=True)
    monkeypatch.setattr(hub.settings, "set_path", set_path)
    dialog = _open(window)

    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsChange"), Qt.LeftButton
    )
    set_path.assert_called_once_with(os.path.normpath(chosen))
    assert dialog.findChild(
        QLineEdit, "settingsPath"
    ).text() == os.path.normpath(chosen)


def test_change_cancelled_leaves_path(qapp, window, monkeypatch):
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: ""
    )
    dialog = _open(window)
    before = window._hub.settings.state.path
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsChange"), Qt.LeftButton
    )
    assert dialog.findChild(QLineEdit, "settingsPath").text() == before


# ── download mirrors ─────────────────────────────────────────────────────


def test_mirror_rows_render_configured_sources(qapp, window):
    hub = window._hub
    dialog = _open(window)
    assert dialog.findChild(QLabel, "settingsMirrorStatus_Backup") is not None
    assert dialog.findChild(QLabel, "settingsMirrorStatus_Backup") is not None
    assert hub.settings._http_mirror_names() == ["Backup"]


def test_no_http_mirrors_shows_direct_server_hint(qapp, window):
    launcher.configure_from_dict(
        {
            "server": {
                "name": "OctoWoW",
                "base_url": "https://octowow.test",
                "torrent_url": "https://dl.octowow.test/client.torrent",
            },
            "mirrors": [],
        }
    )
    dialog = _open(window)
    hint = dialog.findChild(QLabel, "settingsMirrorEmpty")
    assert hint is not None
    assert hint.text() == (
        "No HTTP mirrors configured — update uses the server directly."
    )
    assert dialog.findChild(QLabel, "settingsMirrorStatus_Backup") is None
    assert not dialog.findChild(
        QToolButton, "settingsMirrorRefresh"
    ).isVisible()


def test_mirror_status_renders_initial_state(qapp, window):
    hub = window._hub
    hub.settings.mirror_statuses = {"Backup": "online"}
    dialog = _open(window)
    status = dialog.findChild(QLabel, "settingsMirrorStatus_Backup")
    assert status.text() == "online"
    p = Palette()
    assert p.ok.name() in status.styleSheet()


def test_mirror_status_updates_on_event(qapp, window):
    hub = window._hub
    dialog = _open(window)
    status = dialog.findChild(QLabel, "settingsMirrorStatus_Backup")
    p = Palette()

    hub.settings.mirror_statuses = {
        "Backup": "online",
    }
    hub.dispatcher.post(MirrorStatusChanged(True, "online"))
    QTest.qWait(200)
    assert status.text() == "online"
    assert p.ok.name() in status.styleSheet()

    hub.settings.mirror_statuses = {
        "Backup": "offline",
    }
    hub.dispatcher.post(MirrorStatusChanged(False, "offline"))
    QTest.qWait(200)
    assert status.text() == "offline"
    assert p.err.name() in status.styleSheet()


def test_mirror_refresh_calls_check_mirror(qapp, window, monkeypatch):
    hub = window._hub
    check = Mock()
    monkeypatch.setattr(hub.settings, "check_mirror", check)
    dialog = _open(window)
    dialog.findChild(QToolButton, "settingsMirrorRefresh").click()
    check.assert_called_once()
    assert (
        dialog.findChild(QLabel, "settingsMirrorStatus_Backup").text()
        == "checking…"
    )


# ── troubleshooting rows ────────────────────────────────────────────────


def test_verify_row_calls_verify_files(qapp, window, monkeypatch):
    hub = window._hub
    verify = Mock()
    monkeypatch.setattr(hub.settings, "verify_files", verify)
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QWidget, "settingsVerify"), Qt.LeftButton
    )
    verify.assert_called_once()


def test_logs_row_emits_show_logs_requested(qapp, window):
    dialog = _open(window)
    spy = Mock()
    dialog.showLogsRequested.connect(spy)
    QTest.mouseClick(dialog.findChild(QWidget, "settingsLogs"), Qt.LeftButton)
    spy.assert_called_once()


def test_av_row_absent_when_cannot_manage_antivirus(qapp, window):
    dialog = _open(window)
    assert dialog.findChild(QWidget, "settingsAv") is None


def test_av_row_calls_allow_through_antivirus(qapp, window, monkeypatch):
    monkeypatch.setattr(platform_support, "can_manage_antivirus", lambda: True)
    hub = window._hub
    allow = Mock()
    monkeypatch.setattr(hub.settings, "allow_through_antivirus", allow)
    dialog = _open(window)
    row = dialog.findChild(QWidget, "settingsAv")
    assert row is not None
    QTest.mouseClick(row, Qt.LeftButton)
    allow.assert_called_once()


# ── support links ───────────────────────────────────────────────────────


def test_support_links_call_open_url(qapp, window, monkeypatch):
    hub = window._hub
    open_url = Mock()
    monkeypatch.setattr(hub.settings, "open_url", open_url)
    dialog = _open(window)

    QTest.mouseClick(dialog.findChild(QWidget, "settingsKoFi"), Qt.LeftButton)
    open_url.assert_called_once_with("https://ko-fi.com/rebased")
    QTest.mouseClick(dialog.findChild(QWidget, "settingsBmc"), Qt.LeftButton)
    open_url.assert_called_with("https://buymeacoffee.com/rebased")


# ── general checkboxes ─────────────────────────────────────────────────


def test_checkboxes_reflect_config(qapp, window, monkeypatch):
    hub = window._hub
    hub.settings.state.config = {
        "clear_wdb_on_launch": True,
        "close_on_launch": False,
    }
    monkeypatch.setattr(platform_support, "can_launch_client", lambda: True)
    dialog = _open(window)

    assert dialog.findChild(QCheckBox, "settingsClearWdb").isChecked() is True
    assert (
        dialog.findChild(QCheckBox, "settingsCloseOnLaunch").isChecked()
        is False
    )


def test_launch_checkboxes_absent_when_cannot_launch_client(
    qapp, window, monkeypatch
):
    monkeypatch.setattr(platform_support, "can_launch_client", lambda: False)
    dialog = _open(window)
    assert dialog.findChild(QCheckBox, "settingsClearWdb") is None
    assert dialog.findChild(QCheckBox, "settingsCloseOnLaunch") is None


def test_client_update_checkbox_reflects_and_persists_setting(
    qapp, window, monkeypatch
):
    hub = window._hub
    hub.settings.state.config = {"client_update_enabled": False}
    set_enabled = Mock()
    monkeypatch.setattr(hub.settings, "set_client_update_enabled", set_enabled)
    dialog = _open(window)
    check = dialog.findChild(QCheckBox, "settingsClientUpdate")
    assert check.isChecked() is False
    check.setChecked(True)
    set_enabled.assert_called_once_with(True)


def test_toggle_clear_wdb_calls_set_clear_wdb(qapp, window, monkeypatch):
    monkeypatch.setattr(platform_support, "can_launch_client", lambda: True)
    hub = window._hub
    set_wdb = Mock()
    monkeypatch.setattr(hub.settings, "set_clear_wdb", set_wdb)
    dialog = _open(window)
    check = dialog.findChild(QCheckBox, "settingsClearWdb")
    check.setChecked(True)
    set_wdb.assert_called_once_with(True)


# ── close ──────────────────────────────────────────────────────────────


def test_close_works_headlessly(qapp, window):
    dialog = _open(window)
    assert dialog.isVisible()
    QTest.mouseClick(dialog.findChild(QWidget, "settingsClose"), Qt.LeftButton)
    QTest.qWait(20)
    assert not dialog.isVisible()
    # Reopening via the gear reuses the same (hidden) dialog instance.
    QTest.mouseClick(window._gearButton, Qt.LeftButton)
    assert window._settingsDialog is dialog
    assert dialog.isVisible()
    dialog.close()
    QTest.qWait(20)
    assert not dialog.isVisible()


# ── catalog registries ───────────────────────────────────────────────────────


def test_registry_section_widgets_present(qapp, window):
    dialog = _open(window)
    for name in (
        "settingsAddonRegistryUrl",
        "settingsAddonRegistryApply",
        "settingsAddonRegistryReload",
        "settingsAddonRegistryReset",
        "settingsAddonRegistryOpenCustom",
        "settingsAddonRegistryClearCustom",
        "settingsModRegistryUrl",
        "settingsModRegistryApply",
        "settingsModRegistryReload",
        "settingsModRegistryReset",
        "settingsModRegistryOpenCustom",
        "settingsModRegistryClearCustom",
        "settingsRegistryStatus",
    ):
        assert dialog.findChild(QWidget, name) is not None, name


def test_registry_url_fields_prefilled(qapp, window):
    hub = window._hub
    dialog = _open(window)
    addon_edit = dialog.findChild(QLineEdit, "settingsAddonRegistryUrl")
    mod_edit = dialog.findChild(QLineEdit, "settingsModRegistryUrl")
    assert addon_edit.text() == hub.settings.addons_registry_url()
    assert mod_edit.text() == hub.settings.mods_registry_url()


def test_registry_apply_calls_set_and_clears_status(qapp, window, monkeypatch):
    hub = window._hub
    set_url = Mock(return_value=None)
    monkeypatch.setattr(hub.settings, "set_addons_registry_url", set_url)
    dialog = _open(window)
    edit = dialog.findChild(QLineEdit, "settingsAddonRegistryUrl")
    edit.setText("https://example.com/addons.json")
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsAddonRegistryApply"),
        Qt.LeftButton,
    )
    set_url.assert_called_once_with("https://example.com/addons.json")
    assert dialog.findChild(QLabel, "settingsRegistryStatus").text() == ""


def test_registry_apply_shows_error(qapp, window, monkeypatch):
    hub = window._hub
    set_url = Mock(return_value="Catalog URL must use https.")
    monkeypatch.setattr(hub.settings, "set_addons_registry_url", set_url)
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsAddonRegistryApply"),
        Qt.LeftButton,
    )
    assert "https" in dialog.findChild(QLabel, "settingsRegistryStatus").text()


def test_registry_reset_calls_reset_and_refills(qapp, window, monkeypatch):
    hub = window._hub
    reset = Mock()
    monkeypatch.setattr(hub.settings, "reset_addons_registry_url", reset)
    monkeypatch.setattr(
        hub.settings,
        "addons_registry_url",
        lambda: "https://launcher.test/api/addons.json",
    )
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QToolButton, "settingsAddonRegistryReset"),
        Qt.LeftButton,
    )
    reset.assert_called_once()
    assert (
        dialog.findChild(QLineEdit, "settingsAddonRegistryUrl").text()
        == "https://launcher.test/api/addons.json"
    )


def test_registry_reload_calls_reload(qapp, window, monkeypatch):
    hub = window._hub
    reload = Mock()
    monkeypatch.setattr(hub.settings, "reload_mods_registry", reload)
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QToolButton, "settingsModRegistryReload"),
        Qt.LeftButton,
    )
    reload.assert_called_once()


def test_registry_open_custom_calls_open(qapp, window, monkeypatch):
    hub = window._hub
    open_custom = Mock()
    monkeypatch.setattr(hub.settings, "open_addons_custom_file", open_custom)
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QWidget, "settingsAddonRegistryOpenCustom"),
        Qt.LeftButton,
    )
    open_custom.assert_called_once()


def test_registry_clear_custom_calls_clear(qapp, window, monkeypatch):
    hub = window._hub
    clear_custom = Mock()
    monkeypatch.setattr(hub.settings, "clear_mods_custom", clear_custom)
    dialog = _open(window)
    QTest.mouseClick(
        dialog.findChild(QWidget, "settingsModRegistryClearCustom"),
        Qt.LeftButton,
    )
    clear_custom.assert_called_once()


# ── Linux umu-launcher section ───────────────────────────────────────────────


def _linux_dialog(window, monkeypatch):
    monkeypatch.setattr(platform_support, "is_linux", lambda: True)
    return _open(window)


def test_linux_section_present_on_linux(qapp, window, monkeypatch):
    monkeypatch.setattr(
        window._hub.settings, "resolve_umu_binary", lambda: "/usr/bin/umu-run"
    )
    dialog = _linux_dialog(window, monkeypatch)
    for name in (
        "settingsLinuxTitle",
        "settingsProton",
        "settingsProtonApply",
        "settingsUmuGameId",
        "settingsUmuPath",
        "settingsUmuBrowse",
        "settingsUmuPathApply",
        "settingsUmuHint",
    ):
        assert dialog.findChild(QWidget, name) is not None, name


def test_linux_section_absent_on_other_platforms(qapp, window, monkeypatch):
    monkeypatch.setattr(platform_support, "is_linux", lambda: False)
    dialog = _open(window)
    assert dialog.findChild(QWidget, "settingsLinuxTitle") is None


def test_umu_hint_reports_missing_binary(qapp, window, monkeypatch):
    monkeypatch.setattr(window._hub.settings, "resolve_umu_binary", lambda: "")
    dialog = _linux_dialog(window, monkeypatch)
    assert "not found" in dialog.findChild(QLabel, "settingsUmuHint").text()


def test_umu_proton_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_proton = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_proton", set_proton)
    dialog = _linux_dialog(window, monkeypatch)
    edit = dialog.findChild(QLineEdit, "settingsProton")
    edit.setText("GE-Proton9-4")
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsProtonApply"), Qt.LeftButton
    )
    set_proton.assert_called_once_with("GE-Proton9-4")


def test_umu_gameid_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_gameid = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_game_id", set_gameid)
    dialog = _linux_dialog(window, monkeypatch)
    edit = dialog.findChild(QLineEdit, "settingsUmuGameId")
    edit.setText("umu-custom")
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsUmuGameIdApply"), Qt.LeftButton
    )
    set_gameid.assert_called_once_with("umu-custom")


def test_umu_path_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_path = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_binary_path", set_path)
    dialog = _linux_dialog(window, monkeypatch)
    edit = dialog.findChild(QLineEdit, "settingsUmuPath")
    edit.setText("/opt/umu-run")
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsUmuPathApply"), Qt.LeftButton
    )
    set_path.assert_called_once_with("/opt/umu-run")


def test_umu_browse_sets_binary_path(qapp, window, monkeypatch, tmp_path):
    hub = window._hub
    set_path = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_binary_path", set_path)
    chosen = str(tmp_path / "umu-run")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *a, **k: (chosen, "")
    )
    dialog = _linux_dialog(window, monkeypatch)
    QTest.mouseClick(
        dialog.findChild(QPushButton, "settingsUmuBrowse"), Qt.LeftButton
    )
    set_path.assert_called_once_with(chosen)
