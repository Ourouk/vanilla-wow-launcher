"""Unit tests for the separate Linux (UMU) settings dialog."""

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

import vanilla_wow_launcher.core.platform_support as platform_support
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
def hub(qapp):
    hub = ControllerHub()
    hub.settings.state.first_run = False
    hub.settings.state.first_run_av_pending = False
    hub.settings.state.first_run_verify_pending = False
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win


def _open_linux(window, monkeypatch):
    """Open the main Settings dialog (on Linux) and click through to the
    separate Linux (UMU) settings window; returns (settings_dialog, linux)."""
    monkeypatch.setattr(platform_support, "is_linux", lambda: True)
    QTest.mouseClick(window._gearButton, Qt.LeftButton)
    settings = window._settingsDialog
    assert isinstance(settings, QWidget)
    QTest.qWait(20)
    QTest.mouseClick(
        settings.findChild(QPushButton, "settingsLinuxButton"), Qt.LeftButton
    )
    linux = settings.findChild(QDialog, "linuxSettingsDialog")
    assert isinstance(linux, QDialog)
    return settings, linux


# ── presence / gating ─────────────────────────────────────────────────────


def test_linux_dialog_present_on_linux(qapp, window, monkeypatch):
    monkeypatch.setattr(window._hub.settings, "resolve_umu_binary", lambda: "")
    _, linux = _open_linux(window, monkeypatch)
    for name in (
        "linuxSettingsTitle",
        "settingsProton",
        "settingsProtonApply",
        "settingsRenderer",
        "settingsRendererApply",
        "settingsGamemode",
        "settingsWayland",
        "settingsUmuGameId",
        "settingsUmuPath",
        "settingsUmuBrowse",
        "settingsUmuPathApply",
        "settingsUmuHint",
    ):
        assert linux.findChild(QWidget, name) is not None, name


def test_gamemode_disabled_when_not_installed(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.settings,
        "linux_features",
        lambda: {"gamemode_available": False, "wayland_session": True},
    )
    _, linux = _open_linux(window, monkeypatch)
    check = linux.findChild(QCheckBox, "settingsGamemode")
    assert check is not None
    assert check.isEnabled() is False
    assert linux.findChild(QLabel, "settingsGamemodeHint") is not None


def test_wayland_disabled_when_not_on_wayland(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.settings,
        "linux_features",
        lambda: {"gamemode_available": True, "wayland_session": False},
    )
    _, linux = _open_linux(window, monkeypatch)
    check = linux.findChild(QCheckBox, "settingsWayland")
    assert check is not None
    assert check.isEnabled() is False
    assert linux.findChild(QLabel, "settingsWaylandHint") is not None


def test_gamemode_and_wayland_enabled_when_available(
    qapp, window, monkeypatch
):
    hub = window._hub
    monkeypatch.setattr(
        hub.settings,
        "linux_features",
        lambda: {"gamemode_available": True, "wayland_session": True},
    )
    _, linux = _open_linux(window, monkeypatch)
    assert linux.findChild(QCheckBox, "settingsGamemode").isEnabled() is True
    assert linux.findChild(QCheckBox, "settingsWayland").isEnabled() is True


# ── field apply handlers ─────────────────────────────────────────────────


def test_umu_hint_reports_missing_binary(qapp, window, monkeypatch):
    monkeypatch.setattr(window._hub.settings, "resolve_umu_binary", lambda: "")
    _, linux = _open_linux(window, monkeypatch)
    assert "not found" in linux.findChild(QLabel, "settingsUmuHint").text()


def test_umu_proton_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_proton = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_proton", set_proton)
    monkeypatch.setattr(
        hub.settings,
        "available_protons",
        lambda: ["GE-Proton9-4", "UMU-Proton"],
    )
    _, linux = _open_linux(window, monkeypatch)
    combo = linux.findChild(QComboBox, "settingsProton")
    combo.setCurrentText("GE-Proton9-4")
    QTest.mouseClick(
        linux.findChild(QPushButton, "settingsProtonApply"), Qt.LeftButton
    )
    set_proton.assert_called_once_with("GE-Proton9-4")


def test_umu_renderer_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_renderer = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_renderer", set_renderer)
    _, linux = _open_linux(window, monkeypatch)
    combo = linux.findChild(QComboBox, "settingsRenderer")
    combo.setCurrentText("WineD3D (OpenGL)")
    QTest.mouseClick(
        linux.findChild(QPushButton, "settingsRendererApply"), Qt.LeftButton
    )
    set_renderer.assert_called_once_with("wined3d-opengl")


def test_umu_proton_preserves_unlisted_custom_value(qapp, window, monkeypatch):
    hub = window._hub
    monkeypatch.setattr(
        hub.settings, "available_protons", lambda: ["UMU-Proton"]
    )
    hub.settings.launch.umu_proton = "/custom/proton"
    _, linux = _open_linux(window, monkeypatch)
    combo = linux.findChild(QComboBox, "settingsProton")
    assert combo.currentText() == "/custom/proton"
    assert "/custom/proton" in [
        combo.itemText(i) for i in range(combo.count())
    ]


def test_umu_gamemode_toggle_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_gamemode = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_gamemode", set_gamemode)
    monkeypatch.setattr(
        hub.settings,
        "linux_features",
        lambda: {"gamemode_available": True, "wayland_session": True},
    )
    _, linux = _open_linux(window, monkeypatch)
    check = linux.findChild(QCheckBox, "settingsGamemode")
    check.setChecked(False)
    set_gamemode.assert_called_once_with(False)


def test_umu_wayland_toggle_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_wayland = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_wayland", set_wayland)
    monkeypatch.setattr(
        hub.settings,
        "linux_features",
        lambda: {"gamemode_available": True, "wayland_session": True},
    )
    _, linux = _open_linux(window, monkeypatch)
    check = linux.findChild(QCheckBox, "settingsWayland")
    check.setChecked(False)
    set_wayland.assert_called_once_with(False)


def test_umu_gameid_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_gameid = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_game_id", set_gameid)
    _, linux = _open_linux(window, monkeypatch)
    edit = linux.findChild(QLineEdit, "settingsUmuGameId")
    edit.setText("umu-custom")
    QTest.mouseClick(
        linux.findChild(QPushButton, "settingsUmuGameIdApply"), Qt.LeftButton
    )
    set_gameid.assert_called_once_with("umu-custom")


def test_umu_path_apply_calls_setter(qapp, window, monkeypatch):
    hub = window._hub
    set_path = Mock()
    monkeypatch.setattr(hub.settings, "set_umu_binary_path", set_path)
    _, linux = _open_linux(window, monkeypatch)
    edit = linux.findChild(QLineEdit, "settingsUmuPath")
    edit.setText("/opt/umu-run")
    QTest.mouseClick(
        linux.findChild(QPushButton, "settingsUmuPathApply"), Qt.LeftButton
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
    _, linux = _open_linux(window, monkeypatch)
    QTest.mouseClick(
        linux.findChild(QPushButton, "settingsUmuBrowse"), Qt.LeftButton
    )
    set_path.assert_called_once_with(chosen)
