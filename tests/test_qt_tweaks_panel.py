"""Headless Qt tests for the tweaks panel (qt_tweaks_panel).

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The tweak config store is swapped for an in-memory
dict via monkeypatch, and TweaksController.apply/reset are mocked so no
worker ever patches anything.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtWidgets import QCheckBox, QLineEdit

import vanilla_wow_launcher.controllers.tweaks as tc
import vanilla_wow_launcher.services.tweaks as tweaks
from vanilla_wow_launcher.services.tweaks import TWEAKS_DEFAULTS
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.main_window import MainWindow
from vanilla_wow_launcher.ui.qt.tweaks_panel import TweaksPanel


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def tweak_backend(monkeypatch):
    """In-memory tweak config: defaults with the 16:9 FOV default, and a
    deterministic fov_default_for_display."""
    saved = dict(TWEAKS_DEFAULTS)
    saved["fieldOfView"] = 110
    monkeypatch.setattr(tweaks, "load_tweaks_config", lambda: dict(saved))
    monkeypatch.setattr(
        tweaks, "save_tweaks_config", lambda values: saved.update(values)
    )
    monkeypatch.setattr(tc, "fov_default_for_display", lambda: 110)
    return saved


@pytest.fixture()
def hub(qapp, tweak_backend):
    hub = ControllerHub()
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def _panel(window) -> TweaksPanel:
    panel = window._stack.widget(window._pages["TWEAKS"])
    assert isinstance(panel, TweaksPanel)
    return panel


def _check(panel, tid) -> QCheckBox:
    return panel.findChild(QCheckBox, f"tweaksCheck_{tid}")


def _entry(panel, tid) -> QLineEdit:
    return panel.findChild(QLineEdit, f"tweaksEntry_{tid}")


# ── build ───────────────────────────────────────────────────────────────


def test_panel_replaces_the_tweaks_placeholder(qapp, window):
    assert window._pages["UPDATE"] == MainWindow.TABS.index("UPDATE")
    panel = _panel(window)
    assert panel.objectName() == "tweaksPanel"
    # A checkbox row and a number row exist for known tweak ids.
    assert _check(panel, "soundInBackground") is not None
    assert _entry(panel, "fieldOfView") is not None
    assert _entry(panel, "cameraDistance") is not None


def test_rows_reflect_saved_config(qapp, window, tweak_backend):
    panel = _panel(window)
    assert _check(panel, "soundInBackground").isChecked() is True
    tweak_backend["soundInBackground"] = False
    panel._refresh_from_config()
    assert _check(panel, "soundInBackground").isChecked() is False
    assert _entry(panel, "fieldOfView").text() == "110"


def test_tab_switch_shows_the_tweaks_panel(qapp, window):
    window.switch_tab("TWEAKS")
    assert window._stack.currentIndex() == window._pages["TWEAKS"]
    assert window._navButtons["TWEAKS"].isChecked()
    assert window._stack.currentWidget() is _panel(window)


# ── clamping + red paint ────────────────────────────────────────────────


def test_out_of_range_entry_clamps_on_editing_finished(qapp, window):
    panel = _panel(window)
    entry = _entry(panel, "fieldOfView")
    entry.setText("500")
    entry.editingFinished.emit()
    assert entry.text() == "180"  # clamped to max


def test_out_of_range_entry_is_marked_red_until_clamped(qapp, window):
    panel = _panel(window)
    entry = _entry(panel, "fieldOfView")
    entry.setText("500")
    assert panel._entry_bad("fieldOfView") is True
    p = panel._palette
    assert p.err.name() in entry.styleSheet()
    entry.editingFinished.emit()
    assert entry.text() == "180"
    assert panel._entry_bad("fieldOfView") is False
    assert entry.styleSheet() == ""


def test_below_min_entry_clamps_to_min(qapp, window):
    panel = _panel(window)
    entry = _entry(panel, "cameraDistance")
    entry.setText("10")
    entry.editingFinished.emit()
    assert entry.text() == "50"  # clamped to min


def test_empty_entry_falls_back_to_default_on_clamp(qapp, window):
    panel = _panel(window)
    entry = _entry(panel, "farClip")
    entry.setText("")
    entry.editingFinished.emit()
    assert entry.text() == str(TWEAKS_DEFAULTS["farClip"])


# ── dirty / custom button rules ─────────────────────────────────────────


def test_checkbox_change_makes_apply_visible_and_revert_hides_it(qapp, window):
    window.switch_tab("TWEAKS")
    panel = _panel(window)
    assert not panel._apply_button.isVisible()
    assert not panel._reset_button.isVisible()

    _check(panel, "soundInBackground").setChecked(False)
    assert panel._apply_button.isVisible()
    assert panel._reset_button.isVisible()

    _check(panel, "soundInBackground").setChecked(True)
    assert not panel._apply_button.isVisible()
    assert not panel._reset_button.isVisible()


def test_number_edit_makes_apply_visible(qapp, window):
    window.switch_tab("TWEAKS")
    panel = _panel(window)
    _entry(panel, "nameplateRange").setText("30")
    assert panel._apply_button.isVisible()
    assert panel._reset_button.isVisible()

    _entry(panel, "nameplateRange").setText("41")
    assert not panel._apply_button.isVisible()
    assert not panel._reset_button.isVisible()


# ── apply / reset ───────────────────────────────────────────────────────


def test_apply_calls_controller_with_clamped_values(qapp, window, monkeypatch):
    window.switch_tab("TWEAKS")
    panel = _panel(window)
    apply_mock = Mock(return_value=False)
    monkeypatch.setattr(tc.TweaksController, "apply", apply_mock)

    # A dirty change first, so the Apply button is live.
    _check(panel, "soundInBackground").setChecked(False)
    _entry(panel, "fieldOfView").setText("500")  # clamped to 180 on apply

    panel._apply_button.click()

    assert apply_mock.call_count == 1
    values = apply_mock.call_args.args[0]
    assert values["soundInBackground"] is False
    assert values["fieldOfView"] == 180
    assert values["nameplateRange"] == 41


def test_reset_calls_controller_and_refreshes_form(qapp, window, monkeypatch):
    window.switch_tab("TWEAKS")
    panel = _panel(window)
    reset_mock = Mock(return_value=False)
    monkeypatch.setattr(tc.TweaksController, "reset", reset_mock)

    _check(panel, "soundInBackground").setChecked(False)
    assert panel._apply_button.isVisible()

    panel._reset_button.click()

    assert reset_mock.call_count == 1
    assert reset_mock.call_args.args == ()
    # The form is refreshed from the (unchanged) saved config.
    assert _check(panel, "soundInBackground").isChecked() is True
    assert _entry(panel, "fieldOfView").text() == "110"
    assert not panel._apply_button.isVisible()


def test_operation_finished_refreshes_values_and_enables_buttons(
    qapp, window, tweak_backend
):
    panel = _panel(window)
    panel._set_running(True)
    assert not panel._apply_button.isEnabled()

    tweak_backend["nameplateRange"] = 30
    panel._on_operation_finished("tweaks", True, "")
    assert panel._apply_button.isEnabled()
    assert panel._reset_button.isEnabled()
    # Values re-read from config.
    assert _entry(panel, "nameplateRange").text() == "30"
    assert panel._status_label.text() == "Tweaks applied."


def test_operation_failed_reports_failure(qapp, window):
    panel = _panel(window)
    panel._set_running(True)
    panel._on_operation_failed("tweaks", "boom")
    assert panel._apply_button.isEnabled()
    assert panel._status_label.text() == "Tweaks failed — check the log"


def test_other_kinds_of_operation_are_ignored(qapp, window):
    panel = _panel(window)
    panel._on_operation_finished("mods", True, "")
    assert panel._status_label.text() == ""
