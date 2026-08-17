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
    QLabel,
    QLineEdit,
    QMessageBox,
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

    # Installed out-of-date addon: an Update status label + a trash action.
    assert panel.findChild(QLabel, "addonsStatus_SellValue").text() == "Update"
    trash = panel.findChild(QWidget, "addonsAction_SellValue")
    assert trash is not None and trash.toolTip() == "Remove addon"
    assert panel.findChild(QLabel, "addonsLink_SellValue") is not None

    # Recommended addons carry the gold star + tooltip; plain ones keep a
    # blank slot. Both available rows get a download action.
    assert panel.findChild(QLabel, "addonsStar_pfUI").text() == "★"
    assert (
        panel.findChild(QLabel, "addonsStar_pfUI").toolTip()
        == "Recommended addon"
    )
    assert panel.findChild(QLabel, "addonsStar_Bagsort").text() == ""
    download = panel.findChild(QWidget, "addonsAction_pfUI")
    assert download is not None and download.toolTip() == "Install addon"
    assert panel.findChild(QWidget, "addonsAction_Bagsort") is not None

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


# ── actions ─────────────────────────────────────────────────────────────


def test_install_action_calls_apply_with_record(
    qapp, window, hub, monkeypatch
):
    _post(hub, _make_state(available=MIX_AVAILABLE))
    panel = _panel(window)
    apply_mock = Mock()
    monkeypatch.setattr(hub.addons, "apply", apply_mock)

    panel.findChild(QWidget, "addonsAction_pfUI").click()
    expected = AddonState.from_dict(
        dict(MIX_AVAILABLE[0], folder=MIX_AVAILABLE[0]["folder"])
    ).to_dict()
    assert apply_mock.call_count == 1
    assert apply_mock.call_args.args == ([expected],)


def test_update_status_calls_apply(qapp, window, hub, monkeypatch):
    _post(hub, _make_state(addons=MIX_ADDONS))
    panel = _panel(window)
    apply_mock = Mock()
    monkeypatch.setattr(hub.addons, "apply", apply_mock)

    QTest.mouseClick(
        panel.findChild(QLabel, "addonsStatus_SellValue"), Qt.LeftButton
    )
    expected = AddonState.from_dict(
        dict(MIX_ADDONS["SellValue"], folder="SellValue")
    ).to_dict()
    assert apply_mock.call_args.args == ([expected],)


def test_remove_action_confirms_then_removes(qapp, window, hub, monkeypatch):
    _post(hub, _make_state(addons=MIX_ADDONS))
    panel = _panel(window)
    remove_mock = Mock()
    monkeypatch.setattr(hub.addons, "remove", remove_mock)
    monkeypatch.setattr(
        "vanilla_wow_launcher.ui.qt.addons_panel.QMessageBox.question",
        lambda *a, **k: QMessageBox.Yes,
    )

    panel.findChild(QWidget, "addonsAction_ManualInstall").click()
    assert remove_mock.call_args.args == ("ManualInstall",)


def test_remove_action_skips_when_declined(qapp, window, hub, monkeypatch):
    _post(hub, _make_state(addons=MIX_ADDONS))
    panel = _panel(window)
    remove_mock = Mock()
    monkeypatch.setattr(hub.addons, "remove", remove_mock)
    monkeypatch.setattr(
        "vanilla_wow_launcher.ui.qt.addons_panel.QMessageBox.question",
        lambda *a, **k: QMessageBox.No,
    )

    panel.findChild(QWidget, "addonsAction_ManualInstall").click()
    remove_mock.assert_not_called()


def test_custom_addon_button_emits_signal(qapp, window, hub):
    _post(hub, _make_state(addons=MIX_ADDONS))
    panel = _panel(window)
    spy = Mock()
    panel.customAddonRequested.connect(spy)

    QTest.mouseClick(panel.findChild(QLabel, "addonsCustom"), Qt.LeftButton)
    spy.assert_called_once()


# ── nav badge ───────────────────────────────────────────────────────────


def test_badge_shows_updates_count_and_hides_at_zero(qapp, window, hub):
    badge = window.findChild(QLabel, "tabBadge_ADDONS")
    assert badge is not None
    assert not badge.isVisible()

    _post(hub, _make_state(addons=MIX_ADDONS, updates_count=2))
    assert badge.text() == "2"
    assert badge.isVisible()

    _post(hub, _make_state())
    assert not badge.isVisible()


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
