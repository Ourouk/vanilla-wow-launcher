"""Headless Qt tests for the addons panel (qt_addons_panel).

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. Controller state is seeded directly on the hub and
AddonsLoaded snapshots are posted straight onto the shared EventDispatcher;
the bridge QTimer delivers them to the panel via QTest.qWait.
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
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from vanilla_wow_launcher.state.events import AddonsLoaded
from vanilla_wow_launcher.state.models import AddonsState, AddonState
from vanilla_wow_launcher.ui.qt.addons_panel import AddonsPanel
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.main_window import MainWindow

MIX_ADDONS = {
    "SellValue": dict(
        status="outOfDate",
        git="https://github.com/octo/SellValue",
        toc={"Title": "Sell Value", "Interface": "11200"},
    ),
    "Downloading": dict(
        status="downloading", git="https://github.com/octo/Downloading"
    ),
    "ManualInstall": dict(
        status="unknown", git="https://github.com/octo/ManualInstall"
    ),
    "Magnify": dict(status="unknown"),
}
MIX_AVAILABLE = [
    dict(
        folder="pfUI",
        status="available",
        git="https://github.com/brues-code/pfUI",
        description="Everything you need",
    ),
    dict(
        folder="Bagsort",
        status="available",
        git="https://github.com/octo/Bagsort",
    ),
    dict(
        folder="Broken",
        status="available",
        git="https://github.com/octo/Broken",
        error="Download blocked",
    ),
]


def _make_state(addons=None, available=None, **kw):
    return AddonsState(
        addons={
            folder: AddonState.from_dict(dict(rec, folder=folder))
            for folder, rec in (addons or {}).items()
        },
        available=[
            AddonState.from_dict(dict(rec, folder=rec["folder"]))
            for rec in (available or [])
        ],
        **kw,
    )


def _post(hub, state):
    hub.addons.state = state
    hub.dispatcher.post(AddonsLoaded(state))
    QTest.qWait(200)


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def hub(qapp):
    hub = ControllerHub()
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def _panel(window) -> AddonsPanel:
    panel = window._stack.widget(window._pages["ADDONS"])
    assert isinstance(panel, AddonsPanel)
    return panel


# ── build ───────────────────────────────────────────────────────────────


def test_panel_replaces_the_addons_placeholder(qapp, window):
    assert window._pages["UPDATE"] == MainWindow.TABS.index("UPDATE")
    panel = _panel(window)
    assert panel.objectName() == "addonsPanel"
    assert panel.scroll.objectName() == "addonsScroll"
    assert panel.findChild(QLineEdit, "addonsFilter") is not None
    assert panel.findChild(QLabel, "addonsCheck") is not None
    assert panel.findChild(QLabel, "addonsCustom") is not None
    assert panel.findChild(QLabel, "addonsFooter") is not None
    assert panel.findChild(QPushButton, "addonsApply") is not None
    # Both collapsible sections render.
    assert panel.findChild(QWidget, "addonsSection_INSTALLED") is not None
    assert panel.findChild(QWidget, "addonsSection_AVAILABLE") is not None


# ── row rendering ───────────────────────────────────────────────────────


def test_mixed_rows_render(qapp, window, hub):
    window.switch_tab("ADDONS")
    # pfUI is flagged recommended in this catalog (the curated list is empty
    # now — flags come from the catalog/custom file via a verify).
    hub.addons._recommended = {"pfUI"}
    _post(hub, _make_state(addons=MIX_ADDONS, available=MIX_AVAILABLE))
    panel = _panel(window)

    # Installed out-of-date addon: an Update status label + a checked checkbox.
    assert panel.findChild(QLabel, "addonsStatus_SellValue").text() == "Update"
    checkbox = panel.findChild(QCheckBox, "addonsCheck_SellValue")
    assert checkbox is not None and checkbox.isChecked()
    assert panel.findChild(QLabel, "addonsLink_SellValue") is not None

    # Available addons get unchecked checkboxes.
    assert panel.findChild(QLabel, "addonsStar_pfUI").text() == "★"
    assert (
        panel.findChild(QLabel, "addonsStar_pfUI").toolTip()
        == "Recommended addon"
    )
    assert panel.findChild(QLabel, "addonsStar_Bagsort").text() == ""
    pfui_check = panel.findChild(QCheckBox, "addonsCheck_pfUI")
    assert pfui_check is not None and not pfui_check.isChecked()
    bagsort_check = panel.findChild(QCheckBox, "addonsCheck_Bagsort")
    assert bagsort_check is not None and not bagsort_check.isChecked()

    # downloading / error / couldn't-check status texts.
    assert (
        panel.findChild(QLabel, "addonsStatus_Downloading").text()
        == "downloading…"
    )
    assert (
        panel.findChild(QLabel, "addonsStatus_Broken").text()
        == "⛔ Addon error"
    )
    assert (
        panel.findChild(QLabel, "addonsStatus_ManualInstall").text()
        == "⟳ Couldn't check"
    )
    # An addon neither in the catalog nor recorded is just not tracked.
    assert (
        panel.findChild(QLabel, "addonsStatus_Magnify").text() == "Not tracked"
    )
    # The couldn't-check status has no red error line under the row.
    assert (
        panel.findChild(QLabel, "addonsError_ManualInstall").isVisible()
        is False
    )

    # Error line sits under the failing row.
    error = panel.findChild(QLabel, "addonsError_Broken")
    assert error.isVisible()
    assert error.text() == "  \u26a0  Download blocked"
    # A coloured title is rendered from the .toc Title.
    assert (
        panel.findChild(QLabel, "addonsName_SellValue").text() == "Sell Value"
    )


def test_couldnt_check_status_retry_triggers_verify(
    qapp, window, hub, monkeypatch
):
    window.switch_tab("ADDONS")
    _post(hub, _make_state(addons=MIX_ADDONS))
    panel = _panel(window)
    status = panel.findChild(QLabel, "addonsStatus_ManualInstall")
    assert status.text() == "⟳ Couldn't check"

    verify = Mock(return_value=True)
    monkeypatch.setattr(hub.addons, "verify", verify)
    QTest.mouseClick(status, Qt.LeftButton)
    verify.assert_called_once_with(force=True)


def test_warning_status_for_interface_mismatch(qapp, window, hub):
    addons = {
        "OldAddon": dict(
            status="upToDate", toc={"Title": "OldAddon", "Interface": "11000"}
        )
    }
    _post(hub, _make_state(addons=addons))
    panel = _panel(window)
    status = panel.findChild(QLabel, "addonsStatus_OldAddon")
    assert status.text() == "⚠ Made for client 11000"


# ── collapsible sections ────────────────────────────────────────────────


def test_section_toggle_collapses_and_expands_rows(qapp, window, hub):
    _post(hub, _make_state(addons=MIX_ADDONS, available=MIX_AVAILABLE))
    panel = _panel(window)
    toggle = panel.findChild(QWidget, "addonsToggle_INSTALLED")
    assert toggle.text() == "▾"
    assert panel.findChild(QWidget, "addonsRow_SellValue") is not None

    toggle.click()
    QTest.qWait(20)
    assert hub.addons.state.sections_open["INSTALLED"] is False
    assert panel.findChild(QWidget, "addonsRow_SellValue") is None
    # Available rows are untouched.
    assert panel.findChild(QWidget, "addonsRow_pfUI") is not None

    toggle = panel.findChild(QWidget, "addonsToggle_INSTALLED")
    assert toggle.text() == "▸"
    toggle.click()
    QTest.qWait(20)
    assert hub.addons.state.sections_open["INSTALLED"] is True
    assert panel.findChild(QWidget, "addonsRow_SellValue") is not None


# ── search filter ───────────────────────────────────────────────────────


def test_filter_debounces_and_restores_rows(qapp, window, hub):
    _post(hub, _make_state(addons=MIX_ADDONS, available=MIX_AVAILABLE))
    panel = _panel(window)
    entry = panel.findChild(QLineEdit, "addonsFilter")

    QTest.keyClicks(entry, "sell")
    QTest.qWait(400)  # past the 250 ms debounce
    assert panel.findChild(QWidget, "addonsRow_SellValue") is not None
    assert panel.findChild(QWidget, "addonsRow_pfUI") is None
    assert panel.findChild(QWidget, "addonsRow_Bagsort") is None

    entry.clear()
    QTest.qWait(400)
    assert panel.findChild(QWidget, "addonsRow_pfUI") is not None
    assert panel.findChild(QWidget, "addonsRow_Bagsort") is not None


def test_filter_matches_title_space_insensitively(qapp, window, hub):
    _post(hub, _make_state(addons=MIX_ADDONS))
    panel = _panel(window)
    entry = panel.findChild(QLineEdit, "addonsFilter")

    # "sellvalue" (no space) still finds the "Sell Value" title.
    QTest.keyClicks(entry, "sellvalue")
    QTest.qWait(400)
    assert panel.findChild(QWidget, "addonsRow_SellValue") is not None
    assert panel.findChild(QWidget, "addonsRow_ManualInstall") is None


# ── footer ──────────────────────────────────────────────────────────────


def test_footer_states_follow_snapshots(qapp, window, hub):
    panel = _panel(window)
    footer = panel.findChild(QLabel, "addonsFooter")
    assert footer.text() == "Everything up to date"
    assert not footer.isEnabled()

    _post(hub, _make_state(addons=MIX_ADDONS))
    assert footer.text() == "Update all"
    assert footer.isEnabled()

    _post(hub, _make_state())
    assert footer.text() == "Everything up to date"
    assert not footer.isEnabled()


def test_check_for_updates_shows_busy_state(qapp, window, hub, monkeypatch):
    panel = _panel(window)
    footer = panel.findChild(QLabel, "addonsFooter")

    def fake_verify(force=False, remote_checks=True):
        hub.addons.state.busy = True
        hub.addons.state.state = "verifying"
        return True

    monkeypatch.setattr(hub.addons, "verify", fake_verify)
    check = panel.findChild(QLabel, "addonsCheck")
    QTest.mouseClick(check, Qt.LeftButton)
    assert footer.text() == "Checking…"


# ── checkbox interactivity ──────────────────────────────────────────


def test_checkbox_toggle_forwards_pending(qapp, window, hub):
    panel = _panel(window)
    _post(hub, _make_state(available=MIX_AVAILABLE))
    checkbox = panel.findChild(QCheckBox, "addonsCheck_pfUI")
    assert checkbox is not None
    assert not checkbox.isChecked()
    checkbox.setChecked(True)
    QTest.qWait(0)
    assert hub.addons.state.pending.get("pfUI") is True


def test_checkbox_uncheck_marks_for_removal(qapp, window, hub):
    panel = _panel(window)
    _post(hub, _make_state(addons=MIX_ADDONS))
    checkbox = panel.findChild(QCheckBox, "addonsCheck_SellValue")
    assert checkbox is not None
    assert checkbox.isChecked()
    checkbox.setChecked(False)
    QTest.qWait(0)
    assert hub.addons.state.pending.get("SellValue") is False


# ── apply button ────────────────────────────────────────────────────


def test_apply_visibility_follows_pending(qapp, window, hub):
    window.switch_tab("ADDONS")
    _post(hub, _make_state(available=MIX_AVAILABLE))
    panel = _panel(window)
    apply_btn = panel.findChild(QPushButton, "addonsApply")
    assert not apply_btn.isVisible()

    checkbox = panel.findChild(QCheckBox, "addonsCheck_pfUI")
    checkbox.setChecked(True)
    QTest.qWait(0)
    # The pending state is set by the toggle.
    assert hub.addons.state.pending.get("pfUI") is True
    # The Apply button becomes visible.
    assert apply_btn.isVisible()

    # A clean snapshot (post-apply) hides it again.
    clean = AddonsState()
    hub.addons.state = clean
    _post(hub, clean)
    assert not apply_btn.isVisible()


def test_apply_button_calls_apply_pending(qapp, window, hub, monkeypatch):
    window.switch_tab("ADDONS")
    _post(hub, _make_state(available=MIX_AVAILABLE))
    panel = _panel(window)
    apply_mock = Mock()
    monkeypatch.setattr(hub.addons, "apply_pending", apply_mock)

    checkbox = panel.findChild(QCheckBox, "addonsCheck_pfUI")
    checkbox.setChecked(True)
    QTest.qWait(0)
    assert hub.addons.state.pending.get("pfUI") is True
    apply_btn = panel.findChild(QPushButton, "addonsApply")
    assert apply_btn.isVisible()
    apply_btn.click()
    assert apply_mock.call_count == 1
    assert apply_mock.call_args.args == ()


def test_apply_button_disabled_while_running_then_reenabled(
    qapp, window, hub, monkeypatch
):
    _post(hub, _make_state(available=MIX_AVAILABLE))
    panel = _panel(window)
    monkeypatch.setattr(hub.addons, "apply_pending", Mock())
    apply_btn = panel.findChild(QPushButton, "addonsApply")
    checkbox = panel.findChild(QCheckBox, "addonsCheck_pfUI")
    checkbox.setChecked(True)
    QTest.qWait(0)
    assert apply_btn.isEnabled()

    apply_btn.click()
    assert not apply_btn.isEnabled()

    panel._on_operation_finished("addons", True, "")
    assert apply_btn.isEnabled()


# ── install-recommended button ──────────────────────────────────────────


def test_install_recommended_button_present_and_enabled_when_missing(
    qapp, window, hub
):
    window.switch_tab("ADDONS")
    panel = _panel(window)
    btn = panel.findChild(QPushButton, "addonsInstallRecommended")
    assert btn is not None
    hub.addons._recommended = {"pfUI"}
    _post(hub, _make_state(available=MIX_AVAILABLE))
    assert btn.isEnabled()


def test_install_recommended_button_disabled_when_all_installed(
    qapp, window, hub
):
    window.switch_tab("ADDONS")
    panel = _panel(window)
    btn = panel.findChild(QPushButton, "addonsInstallRecommended")
    hub.addons._recommended = {"pfUI"}
    _post(hub, _make_state(addons={"pfUI": MIX_AVAILABLE[0]}))
    assert not btn.isEnabled()


def test_install_recommended_button_calls_controller(
    qapp, window, hub, monkeypatch
):
    window.switch_tab("ADDONS")
    panel = _panel(window)
    hub.addons._recommended = {"pfUI"}
    _post(hub, _make_state(available=MIX_AVAILABLE))
    install_mock = Mock()
    monkeypatch.setattr(hub.addons, "apply_recommended_addons", install_mock)
    panel.findChild(QPushButton, "addonsInstallRecommended").click()
    install_mock.assert_called_once_with()


def test_addon_without_git_hides_source_link(qapp, window, hub):
    """An addon with no git URL must not render the ⧉ source link."""
    state = AddonsState(
        addons={
            "NoGitAddon": AddonState.from_dict(
                {
                    "folder": "NoGitAddon",
                    "status": "unknown",
                }
            )
        },
        available=[],
        updates_count=0,
    )
    hub.addons.state = state
    hub.dispatcher.post(AddonsLoaded(state))
    QTest.qWait(200)

    panel = _panel(window)
    row = panel._rows.get("NoGitAddon")
    assert row is not None
    link = row.findChild(QLabel, "addonsLink_NoGitAddon")
    assert link is None
