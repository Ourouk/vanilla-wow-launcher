"""Headless Qt tests for the mods panel (qt_mods_panel).

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. MODS_REGISTRY is swapped for a tiny fake registry and
ModsLoaded snapshots are posted straight onto the shared EventDispatcher
(bypassing the network fetchers); the bridge QTimer delivers them to the
panel via QTest.qWait.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QWidget

import vanilla_wow_launcher.services.mods as mods_module
from vanilla_wow_launcher.state.events import ModsLoaded
from vanilla_wow_launcher.state.models import ModsState, ModState
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.main_window import MainWindow
from vanilla_wow_launcher.ui.qt.mods_panel import ModsPanel

FAKE_REGISTRY = [
    {
        "id": "AlphaMod",
        "name": "AlphaMod",
        "essential": True,
        "repo_url": "https://example.invalid/alpha",
        "description": "First essential mod.",
        "source": {"kind": "github_release"},
    },
    {
        "id": "BetaMod",
        "name": "BetaMod",
        "essential": False,
        "repo_url": "https://example.invalid/beta",
        "description": "Second mod.",
        "source": {"kind": "github_release"},
    },
    {
        "id": "GammaMod",
        "name": "GammaMod",
        "essential": False,
        "repo_url": "https://example.invalid/gamma",
        "description": "Third mod.",
        "source": {"kind": "github_release"},
    },
    {
        "id": "NoRepoMod",
        "name": "NoRepoMod",
        "essential": False,
        "description": "Mod with no repo URL.",
        "source": {"kind": "github_release"},
    },
]


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def registry(monkeypatch):
    monkeypatch.setattr(
        mods_module, "mods_registry", lambda *a, **k: FAKE_REGISTRY
    )
    monkeypatch.setattr(
        mods_module,
        "fetch_mod_latest_version_cached",
        lambda mod, force=False: None,
    )
    return FAKE_REGISTRY


@pytest.fixture()
def hub(qapp, registry):
    hub = ControllerHub()
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def _panel(window) -> ModsPanel:
    panel = window._stack.widget(window._pages["MODS"])
    assert isinstance(panel, ModsPanel)
    return panel


def _post(hub, state):
    hub.mods.state = state
    hub.dispatcher.post(ModsLoaded(state))
    QTest.qWait(200)


# ── build ───────────────────────────────────────────────────────────────


def test_panel_replaces_the_mods_placeholder(qapp, window):
    assert window._pages["UPDATE"] == MainWindow.TABS.index("UPDATE")
    panel = _panel(window)
    assert panel.objectName() == "modsPanel"
    assert panel.scroll.objectName() == "modsScroll"
    # One row per registry entry, plus the Apply footer button.
    for mod in FAKE_REGISTRY:
        assert panel.findChild(QWidget, f"modsRow_{mod['id']}") is not None
    assert panel.findChild(QPushButton, "modsApply") is not None


def test_rerender_rebuilds_rows(qapp, window, hub):
    panel = _panel(window)
    _post(hub, ModsState())
    for mod in FAKE_REGISTRY:
        assert panel.findChild(QWidget, f"modsRow_{mod['id']}") is not None
    assert panel.findChild(QLabel, "modsName_AlphaMod").text() == "AlphaMod"


# ── rendering state ─────────────────────────────────────────────────────


def test_installed_mod_name_is_green_and_essential_shows_star(
    qapp, window, hub
):
    state = ModsState(
        records={
            "AlphaMod": ModState(
                enabled=True, installed_version="1.2", present=True
            )
        },
        latest_versions={"AlphaMod": "9.9"},
    )
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    name = panel.findChild(QLabel, "modsName_AlphaMod")
    assert panel._palette.mod_hl.name() in name.styleSheet()
    # Installed mods show their installed version.
    assert panel.findChild(QLabel, "modsVer_AlphaMod").text() == "  1.2"
    # Essential mods carry the gold star; others keep a blank slot.
    assert panel.findChild(QLabel, "modsStar_AlphaMod").text() == "★"
    assert panel.findChild(QLabel, "modsStar_BetaMod").text() == ""


def test_untracked_mod_name_is_not_highlighted(qapp, window, hub):
    state = ModsState(
        records={"AlphaMod": ModState(enabled=True, installed_version="1.2")},
        latest_versions={},
    )
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    name = panel.findChild(QLabel, "modsName_AlphaMod")
    # Filesystem-truth absent → shown as not installed, no version highlight.
    assert panel._palette.mod_hl.name() not in name.styleSheet()


def test_unknown_mods_section_lists_detected_entries_and_removes(
    qapp, window, hub, monkeypatch
):
    state = ModsState(unknown=["mystery.dll", "other.dll"])
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    assert panel.findChild(QLabel, "modsUnknownHeader") is not None
    for name in ("mystery.dll", "other.dll"):
        assert panel.findChild(QWidget, f"modsUnknownRow_{name}") is not None
        assert (
            panel.findChild(QLabel, f"modsUnknownName_{name}").text() == name
        )

    remove_mock = Mock()
    monkeypatch.setattr(hub.mods, "remove_unknown", remove_mock)
    panel.findChild(QPushButton, "modsUnknownRemove_mystery.dll").click()
    remove_mock.assert_called_once_with("mystery.dll")


def test_error_mod_shows_red_name_and_error_line(qapp, window, hub):
    window.switch_tab("MODS")
    state = ModsState(
        records={
            "AlphaMod": ModState(
                enabled=True, installed_version="1.0", error="boom"
            )
        },
        latest_versions={},
    )
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    name = panel.findChild(QLabel, "modsName_AlphaMod")
    assert panel._palette.err.name() in name.styleSheet()
    error = panel.findChild(QLabel, "modsError_AlphaMod")
    assert error.isVisible()
    assert error.text() == "  \u26a0  boom"


# ── action labels ───────────────────────────────────────────────────────


def test_action_labels_follow_action_for(qapp, window, hub):
    state = ModsState(
        records={
            "AlphaMod": ModState(
                enabled=True, installed_version="1.0", error="boom"
            ),
            "BetaMod": ModState(enabled=True, installed_version="1.0"),
            "GammaMod": ModState(enabled=True, installed_version="2.0"),
        },
        latest_versions={"BetaMod": "2.0", "GammaMod": "2.0"},
    )
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    retry = panel.findChild(QPushButton, "modsAction_AlphaMod")
    assert retry is not None and retry.text() == "Retry"
    update = panel.findChild(QPushButton, "modsAction_BetaMod")
    assert update is not None and update.text() == "Update"
    # Up-to-date mods get no action button.
    assert panel.findChild(QPushButton, "modsAction_GammaMod") is None


# ── checkbox interactivity ──────────────────────────────────────────────


def test_enable_checkbox_forwards_toggle(qapp, window, hub):
    panel = _panel(window)
    check = panel.findChild(QCheckBox, "modsCheck_AlphaMod")
    assert not check.isChecked()
    check.setChecked(True)
    QTest.qWait(0)
    pend = hub.mods.state.pending.get("AlphaMod")
    assert pend is not None and pend.enabled is True


# ── apply button ────────────────────────────────────────────────────────


def test_apply_visibility_follows_pending_and_errors(qapp, window, hub):
    window.switch_tab("MODS")
    panel = _panel(window)
    apply_btn = panel.findChild(QPushButton, "modsApply")
    assert not apply_btn.isVisible()

    check = panel.findChild(QCheckBox, "modsCheck_AlphaMod")
    check.setChecked(True)
    assert apply_btn.isVisible()

    # A clean snapshot (post-apply) hides it again.
    clean = ModsState()
    hub.mods.state = clean
    _post(hub, clean)
    assert not apply_btn.isVisible()


def test_apply_visible_when_mod_has_error(qapp, window, hub):
    window.switch_tab("MODS")
    panel = _panel(window)
    state = ModsState(
        records={"AlphaMod": ModState(error="boom")}, latest_versions={}
    )
    hub.mods.state = state
    _post(hub, state)
    assert panel.findChild(QPushButton, "modsApply").isVisible()


def test_apply_button_calls_controller(qapp, window, hub, monkeypatch):
    window.switch_tab("MODS")
    panel = _panel(window)
    apply_mock = Mock()
    monkeypatch.setattr(hub.mods, "apply", apply_mock)

    panel.findChild(QCheckBox, "modsCheck_AlphaMod").setChecked(True)
    apply_btn = panel.findChild(QPushButton, "modsApply")
    assert apply_btn.isVisible()
    apply_btn.click()
    assert apply_mock.call_count == 1
    assert apply_mock.call_args.args == ()


def test_action_button_calls_apply_for_that_mod(
    qapp, window, hub, monkeypatch
):
    window.switch_tab("MODS")
    state = ModsState(
        records={
            "AlphaMod": ModState(
                enabled=True, installed_version="1.0", error="boom"
            )
        },
        latest_versions={},
    )
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    apply_mock = Mock()
    monkeypatch.setattr(hub.mods, "apply", apply_mock)
    retry = panel.findChild(QPushButton, "modsAction_AlphaMod")
    assert retry is not None and retry.text() == "Retry"
    retry.click()
    assert apply_mock.call_count == 1
    assert apply_mock.call_args.kwargs == {"only_mod_id": "AlphaMod"}


def test_apply_button_disabled_while_running_then_reenabled(
    qapp, window, hub, monkeypatch
):
    window.switch_tab("MODS")
    panel = _panel(window)
    monkeypatch.setattr(hub.mods, "apply", Mock())
    apply_btn = panel.findChild(QPushButton, "modsApply")
    panel.findChild(QCheckBox, "modsCheck_AlphaMod").setChecked(True)
    assert apply_btn.isEnabled()

    apply_btn.click()
    assert not apply_btn.isEnabled()

    panel._on_operation_finished("mods", True, "")
    assert apply_btn.isEnabled()


# ── nav badge ───────────────────────────────────────────────────────────


def test_badge_shows_update_count_and_hides_at_zero(qapp, window, hub):
    badge = window.findChild(QLabel, "tabBadge_MODS")
    assert badge is not None
    assert not badge.isVisible()

    _post(hub, ModsState(updates_count=2))
    assert badge.text() == "2"
    assert badge.isVisible()

    _post(hub, ModsState(updates_count=0))
    assert not badge.isVisible()


# ── install-essential button ─────────────────────────────────────────────


def test_install_essential_button_present_and_enabled_when_missing(
    qapp, window, hub
):
    window.switch_tab("MODS")
    panel = _panel(window)
    btn = panel.findChild(QPushButton, "modsInstallRecommended")
    assert btn is not None
    # No records posted → the essential AlphaMod is not installed → enabled.
    _post(hub, ModsState())
    assert btn.isEnabled()


def test_install_essential_button_disabled_when_all_installed(
    qapp, window, hub
):
    window.switch_tab("MODS")
    panel = _panel(window)
    btn = panel.findChild(QPushButton, "modsInstallRecommended")
    _post(hub, ModsState(records={"AlphaMod": ModState(present=True)}))
    assert not btn.isEnabled()


def test_install_essential_button_calls_controller(
    qapp, window, hub, monkeypatch
):
    window.switch_tab("MODS")
    panel = _panel(window)
    install_mock = Mock()
    monkeypatch.setattr(hub.mods, "apply_essential_mods", install_mock)
    panel.findChild(QPushButton, "modsInstallRecommended").click()
    install_mock.assert_called_once_with()


def test_mod_without_repo_url_hides_source_link(qapp, window, hub):
    """A mod with no repo_url must not render the ⧉ source link."""
    state = ModsState(
        records={"NoRepoMod": ModState(present=False)},
        latest_versions={"NoRepoMod": "1.0"},
    )
    hub.mods.state = state
    _post(hub, state)

    panel = _panel(window)
    row = panel.findChild(QWidget, "modsRow_NoRepoMod")
    assert row is not None
    link = row.findChild(QLabel, "modsLink_NoRepoMod")
    assert link is None
