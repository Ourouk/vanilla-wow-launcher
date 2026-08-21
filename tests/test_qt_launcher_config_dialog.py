"""Headless Qt tests for the first-launch launcher config dialog.

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The QApplication is shared through create_qt_app().
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
)

from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.launcher_config_dialog import (
    LauncherConfigDialog,
)


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


def _write_config(path):
    path.write_text(
        json.dumps({"server": {"base_url": "https://launcher.test"}}),
        encoding="utf-8",
    )
    return str(path)


def test_dialog_widgets_present(qapp, tmp_path):
    path = tmp_path / "vanilla_wow_launcher.json"
    dlg = LauncherConfigDialog(initial_path=_write_config(path))
    dlg.show()
    try:
        assert isinstance(dlg.findChild(QLabel, "launcherConfigTitle"), QLabel)
        assert isinstance(dlg.findChild(QLabel, "launcherConfigIntro"), QLabel)
        path_edit = dlg.findChild(QLineEdit, "launcherConfigPath")
        assert path_edit.isReadOnly()
        assert path_edit.text() == str(path)
        assert isinstance(
            dlg.findChild(QPushButton, "launcherConfigBrowse"), QPushButton
        )
        assert isinstance(
            dlg.findChild(QPushButton, "launcherConfigOk"), QPushButton
        )
        assert isinstance(
            dlg.findChild(QPushButton, "launcherConfigCancel"), QPushButton
        )
        assert not dlg.findChild(QLabel, "launcherConfigError").isVisible()
    finally:
        dlg.close()


def test_ok_without_path_shows_error(qapp):
    dlg = LauncherConfigDialog()
    dlg.show()
    try:
        # Nothing selected: the OK button is disabled, so exercise the
        # submit handler directly to verify it surfaces an error.
        dlg._submit()
        assert dlg.result() != QDialog.DialogCode.Accepted
        error = dlg.findChild(QLabel, "launcherConfigError")
        assert error.isVisible()
        assert error.text()
    finally:
        dlg.close()


def test_ok_with_valid_config_accepts(qapp, tmp_path):
    path = tmp_path / "vanilla_wow_launcher.json"
    dlg = LauncherConfigDialog()
    try:
        dlg.findChild(QLineEdit, "launcherConfigPath").setText(
            _write_config(path)
        )
        dlg.findChild(QPushButton, "launcherConfigOk").click()
        assert dlg.result() == QDialog.DialogCode.Accepted
        assert dlg.selected_path() == str(path)
    finally:
        dlg.close()


def test_ok_with_invalid_config_shows_error(qapp, tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b"not json")
    dlg = LauncherConfigDialog()
    dlg.show()
    try:
        dlg.findChild(QLineEdit, "launcherConfigPath").setText(str(path))
        dlg.findChild(QPushButton, "launcherConfigOk").click()
        assert dlg.result() != QDialog.DialogCode.Accepted
        error = dlg.findChild(QLabel, "launcherConfigError")
        assert error.isVisible()
        assert error.text()
    finally:
        dlg.close()


def test_browse_updates_path_and_validates(qapp, tmp_path, monkeypatch):
    import vanilla_wow_launcher.ui.qt.launcher_config_dialog as dialog_module

    path = tmp_path / "vanilla_wow_launcher.json"
    valid = _write_config(path)
    monkeypatch.setattr(
        dialog_module.QFileDialog,
        "getOpenFileName",
        staticmethod(
            lambda *a, **k: (valid, "Launcher configuration (*.json)")
        ),
    )
    dlg = LauncherConfigDialog()
    try:
        dlg.findChild(QPushButton, "launcherConfigBrowse").click()
        assert dlg.findChild(QLineEdit, "launcherConfigPath").text() == valid
    finally:
        dlg.close()


def test_cancel_rejects(qapp):
    dlg = LauncherConfigDialog()
    try:
        dlg.findChild(QPushButton, "launcherConfigCancel").click()
        assert dlg.result() == QDialog.DialogCode.Rejected
    finally:
        dlg.close()


def test_empty_servers_shows_offline_status_and_disables_list(qapp):
    dlg = LauncherConfigDialog(servers=[])
    dlg.show()
    try:
        status = dlg.findChild(QLabel, "launcherConfigStatus")
        assert status.isVisible()
        assert "offline" in status.text().lower()
        assert not dlg.findChild(
            QListWidget, "launcherConfigServers"
        ).isEnabled()
    finally:
        dlg.close()


def _wait_until(qapp, cond, timeout_ms=4000):
    """Pump the event loop until `cond()` is true (async fetch polling)."""
    deadline = __import__("time").monotonic() + timeout_ms / 1000
    while not cond():
        if __import__("time").monotonic() > deadline:
            return False
        qapp.processEvents()
        __import__("time").sleep(0.01)
    return True


def test_server_selection_accepts_remote(qapp, monkeypatch):
    import vanilla_wow_launcher.core.launcher as launcher_mod
    import vanilla_wow_launcher.services.server_index as server_index_module

    servers = [
        {
            "id": "octowow",
            "name": "OctoWoW",
            "config_url": "https://example.invalid/octowow.json",
        }
    ]
    monkeypatch.setattr(
        server_index_module,
        "fetch_server_config",
        lambda url: ({"server": {"base_url": "https://x"}}, "{}", ""),
    )
    monkeypatch.setattr(
        launcher_mod, "validate_dict", lambda data: (object(), "")
    )
    dlg = LauncherConfigDialog(servers=servers)
    dlg.show()
    try:
        dlg._list.setCurrentItem(dlg._list.item(0))
        assert dlg._list.item(0).isSelected()
        dlg.findChild(QPushButton, "launcherConfigOk").click()
        assert _wait_until(
            qapp,
            lambda: dlg.result() == QDialog.DialogCode.Accepted,
        )
        sel = dlg.selection()
        assert sel is not None
        assert sel["kind"] == "remote"
        assert sel["config_url"] == "https://example.invalid/octowow.json"
        assert sel["name"] == "OctoWoW"
    finally:
        dlg.close()


def test_remote_fetch_failure_shows_error(qapp, monkeypatch):
    import vanilla_wow_launcher.services.server_index as server_index_module

    servers = [
        {
            "id": "octowow",
            "name": "OctoWoW",
            "config_url": "https://example.invalid/octowow.json",
        }
    ]
    monkeypatch.setattr(
        server_index_module,
        "fetch_server_config",
        lambda url: (
            None,
            None,
            "Could not fetch the server configuration: boom",
        ),
    )
    dlg = LauncherConfigDialog(servers=servers)
    dlg.show()
    error = dlg.findChild(QLabel, "launcherConfigError")
    try:
        dlg._list.setCurrentItem(dlg._list.item(0))
        dlg.findChild(QPushButton, "launcherConfigOk").click()
        assert _wait_until(qapp, lambda: error.isVisible() and error.text())
        assert dlg.result() != QDialog.DialogCode.Accepted
    finally:
        dlg.close()


def test_cancel_during_fetch_never_accepts(qapp, monkeypatch):
    """A fetch result arriving after Cancel must not re-accept the dialog."""
    import threading
    import time as time_mod

    import vanilla_wow_launcher.services.server_index as server_index_module

    release = threading.Event()

    def slow_fetch(url):
        release.wait(2)
        return ({"server": {}}, "{}", "")

    monkeypatch.setattr(server_index_module, "fetch_server_config", slow_fetch)
    servers = [
        {
            "id": "octowow",
            "name": "OctoWoW",
            "config_url": "https://example.invalid/octowow.json",
        }
    ]
    dlg = LauncherConfigDialog(servers=servers)
    dlg.show()
    try:
        dlg._list.setCurrentItem(dlg._list.item(0))
        dlg.findChild(QPushButton, "launcherConfigOk").click()
        # Fetch in flight: give the worker a moment, then cancel.
        deadline = time_mod.monotonic() + 0.2
        while time_mod.monotonic() < deadline:
            qapp.processEvents()
            time_mod.sleep(0.01)
        dlg.reject()
        release.set()
        # Pump long enough for the fetch to finish and the poll to fire.
        deadline = time_mod.monotonic() + 1.0
        while time_mod.monotonic() < deadline:
            qapp.processEvents()
            time_mod.sleep(0.01)
        assert dlg.result() != QDialog.DialogCode.Accepted
        assert dlg.selection() is None
    finally:
        dlg.close()
