"""End-to-end headless Qt smoke suite — the full QtVanillaWoWLauncherApp.

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. The QApplication is created once and shared through
the create_qt_app() singleton. Each test builds a real QtVanillaWoWLauncherApp
(window + ControllerHub + all four panels) with every network/disk backend
monkeypatched and the config redirected into tmp_path, then drives it through
the real event loop with QTest.qWait — no display, no network, no filesystem
writes outside tmp_path. run()/exec() is never called; QTest.qWait and the
bridge's QTimer deliver dispatcher events exactly as in production.
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLabel, QWidget

import vanilla_wow_launcher.controllers.news as news_controller
import vanilla_wow_launcher.controllers.settings as settings_controller
import vanilla_wow_launcher.controllers.update as update_controller
import vanilla_wow_launcher.core.config_store as config_store
import vanilla_wow_launcher.core.constants as constants
import vanilla_wow_launcher.core.platform_support as platform_support
import vanilla_wow_launcher.services.addons as addons_module
import vanilla_wow_launcher.services.mods as mods_module
import vanilla_wow_launcher.services.news as news_module
import vanilla_wow_launcher.ui.qt.main_window as mw
from vanilla_wow_launcher.state.events import (
    AddonsLoaded,
    ModsLoaded,
    OperationFinished,
    ProgressChanged,
    StatusChanged,
)
from vanilla_wow_launcher.state.models import (
    AddonsState,
    AddonState,
    ModsState,
)
from vanilla_wow_launcher.ui.qt.app import (
    QtVanillaWoWLauncherApp,
    create_qt_app,
)
from vanilla_wow_launcher.ui.qt.settings_dialog import SettingsDialog


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def qt_env(monkeypatch, tmp_path):
    """Redirect the config/cache files into tmp_path so nothing touches the
    real per-user config, and swap every network/disk backend for a fake.

    The news fetchers are patched both on the `news` module and on
    `news_controller` (which `from news import …`-ed them at import time);
    mods/addons are called through their modules so one patch each suffices.
    The updater's verify/update/launch entry points are patched per-hub in
    build_app() so their worker threads never start.
    """
    cfg = tmp_path / "config.json"
    cache = tmp_path / "hash_cache.json"
    config_store.configure(str(cfg), str(cache))
    monkeypatch.setattr(constants, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(constants, "CACHE_FILE", str(cache))
    monkeypatch.setattr(settings_controller, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(settings_controller, "CACHE_FILE", str(cache))

    featured = {
        "title": "1.16.2 is live",
        "author": "Staff",
        "date": "2026-08-13",
        "html": "<p>Patch is out.</p>",
        "url": "https://example.invalid/news/1",
    }
    items = [
        {
            "title": "Patch notes",
            "date": "2026-08-13",
            "author": "Staff",
            "body": "Full notes here",
            "url": "https://example.invalid/news/2",
        }
    ]

    monkeypatch.setattr(news_module, "fetch_featured_post", lambda: featured)
    monkeypatch.setattr(news_module, "fetch_news_items", lambda: items)
    monkeypatch.setattr(
        news_controller, "fetch_featured_post", lambda: featured
    )
    monkeypatch.setattr(news_controller, "fetch_news_items", lambda: items)

    monkeypatch.setattr(
        mods_module,
        "fetch_mod_latest_version_cached",
        lambda mod, force=False: "1.2.3",
    )
    monkeypatch.setattr(
        mods_module,
        "mods_registry",
        lambda *a, **k: [
            {
                "id": "VanillaFixes",
                "name": "VanillaFixes",
                "essential": True,
                "description": "Fixes stutter",
                "repo_url": "https://example.invalid/vf",
                "source": {
                    "kind": "github_release",
                    "owner": "o",
                    "repo": "r",
                    "asset_pattern": "*.zip",
                    "prefer_no": None,
                    "extract_map": None,
                },
            }
        ],
    )

    catalog = [
        {
            "name": "pfUI",
            "git": "https://github.com/brues-code/pfUI",
            "branch": "master",
            "ref": "HEAD",
            "toc": {},
            "description": "Everything you need",
        }
    ]
    monkeypatch.setattr(
        addons_module, "fetch_addons_catalog", lambda force=False: catalog
    )
    monkeypatch.setattr(
        addons_module,
        "addon_remote_sha",
        lambda git_url, branch=None, ref=None, force=False, raise_errors=False: (
            "deadbeef"
        ),
    )
    monkeypatch.setattr(
        addons_module,
        "addon_cached_sha",
        lambda git_url, branch=None, ref=None: "deadbeef",
    )

    monkeypatch.setattr(
        update_controller, "fetch_updater_latest_tag", Mock(return_value="1.2")
    )
    monkeypatch.setattr(
        update_controller, "updater_update_available", lambda tag: False
    )
    monkeypatch.setattr(update_controller, "can_launch_client", lambda: True)
    monkeypatch.setattr(platform_support, "can_launch_client", lambda: True)
    monkeypatch.setattr(
        platform_support, "can_manage_antivirus", lambda: False
    )

    yield cfg
    config_store.configure("", "")


@pytest.fixture()
def build_app(qapp, monkeypatch, qt_env):
    """A factory for full QtVanillaWoWLauncherApp instances with safe backends.

    A non-first-run config is pre-seeded with the game folder and the flags
    that arm the background mod/addon checks, so the whole startup schedule
    runs. When first_run=True the config file is left absent so Settings
    auto-opens and verification is deferred, exactly like a real first run.
    """

    def _build(*, startup=True, first_run=False):
        cfg = qt_env
        if not first_run:
            config_store.save_config(
                {
                    "out_dir": str(cfg.parent / "game"),
                    "mod_release_cache": {
                        "VanillaFixes": {"timestamp": 0, "release": {}}
                    },
                    "addons": {},
                }
            )
        app = QtVanillaWoWLauncherApp()
        app._window.show()
        hub = app._hub
        monkeypatch.setattr(hub.updater, "start_verify", Mock())
        monkeypatch.setattr(hub.updater, "start_update", Mock())
        monkeypatch.setattr(hub.updater, "check_updater_update", Mock())
        monkeypatch.setattr(
            hub.updater, "launch_game", Mock(return_value=(True, False))
        )
        if not startup:
            app._window._stop_timers()
        return app

    return _build


@pytest.fixture()
def app(build_app):
    app = build_app()
    yield app
    app.close()
    app._hub.close()


@pytest.fixture()
def app_no_startup(build_app):
    app = build_app(startup=False)
    yield app
    app.close()
    app._hub.close()


def _wait_until(predicate, timeout_ms=4000):
    """Pump the Qt event loop until `predicate` holds (worker threads post
    dispatcher events the bridge drains on the main thread)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        QTest.qWait(25)
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")


# ── construction ──────────────────────────────────────────────────────────


def test_construction_builds_full_app(qapp, app):
    win = app._window
    assert win.windowTitle() == "Vanilla WoW Launcher"
    assert win._stack.count() == 5
    assert win._pages["UPDATE"] == mw.MainWindow.TABS.index("UPDATE")
    assert win._stack.currentIndex() == 0
    assert win._navButtons["NEWS"].isChecked()
    assert win._gearButton is not None

    # Footer chrome is wired up. The client isn't verified yet and no manifest
    # has been fetched, but launch is available (umu on PATH), so the button
    # offers PLAY — the game folder may already be ready to run.
    assert win._updateButton.objectName() == "updateButton"
    assert win._updateButton.text() == "PLAY"
    assert win._updateButton.isEnabled()
    assert win._statusLabel.text() == "Manifest unavailable"
    assert win._progressBar is not None

    for name, obj in (
        ("NEWS", "newsPanel"),
        ("TWEAKS", "tweaksPanel"),
        ("ADDONS", "addonsPanel"),
        ("MODS", "modsPanel"),
    ):
        assert win._stack.widget(win._pages[name]).objectName() == obj


def test_news_panel_auto_renders_loading_state(qapp, app):
    win = app._window
    news_panel = win._stack.widget(0)
    assert news_panel.objectName() == "newsPanel"
    assert news_panel.featured_panel.status_label.text() == "Loading…"
    assert news_panel.featured_panel.status_label.isVisible()
    assert news_panel.announcements_panel.status_label.text() == "Loading…"
    assert news_panel.announcements_panel.status_label.isVisible()


# ── startup schedule ──────────────────────────────────────────────────────


def test_startup_schedule_runs_the_full_launch_chain(qapp, app):
    win = app._window
    hub = app._hub
    news_panel = win._stack.widget(mw.MainWindow.TABS.index("NEWS"))
    mods_panel = win._stack.widget(mw.MainWindow.TABS.index("MODS"))
    addons_panel = win._stack.widget(mw.MainWindow.TABS.index("ADDONS"))

    # 300 ms → background verify; 2000 ms → self-update check (the real
    # check_updater_update runs — the timer captured the bound method — so
    # the assertion is on the patched fetch it calls).
    _wait_until(lambda: hub.updater.start_verify.call_count == 1)
    hub.updater.start_verify.assert_called_once_with(False)
    _wait_until(lambda: update_controller.fetch_updater_latest_tag.called)

    # 600 ms → news load: loading state is replaced by the fetched post.
    _wait_until(lambda: hub.news.state.featured is not None)
    assert hub.news.state.items == [
        {
            "title": "Patch notes",
            "date": "2026-08-13",
            "author": "Staff",
            "body": "Full notes here",
            "url": "https://example.invalid/news/2",
        }
    ]
    assert "Patch is out." in news_panel.featured_panel.body.toPlainText()
    assert not news_panel.featured_panel.status_label.isVisible()
    assert news_panel.announcements_panel.scroll.isVisible()

    # 900 ms → mod latest-version fetch (mod_release_cache present).
    _wait_until(lambda: bool(hub.mods.state.latest_versions))
    assert hub.mods.state.latest_versions["VanillaFixes"] == "1.2.3"
    assert "VanillaFixes" in mods_panel._rows
    assert "1.2.3" in mods_panel._rows["VanillaFixes"].version_label.text()

    # 1500 ms → addons verify against the fake catalog.
    _wait_until(lambda: hub.addons.state.state == "done")
    assert addons_panel._rows.get("pfUI") is not None


def test_first_run_defers_verify_until_settings_close(
    qapp, build_app, monkeypatch
):
    app = build_app(first_run=True)
    try:
        win = app._window
        hub = app._hub
        assert hub.settings.state.first_run is True
        assert hub.settings.state.first_run_verify_pending is True

        # Settings auto-opens at 500 ms; verification stays deferred.
        _wait_until(lambda: win._settingsDialog is not None)
        assert win._settingsDialog.isVisible()
        hub.updater.start_verify.assert_not_called()

        # Closing it arms the deferred verify (overwrite_config=True). The
        # first-run auto-install prompt fires on the same close — patch it so
        # it doesn't block the offscreen event loop.
        class _SkipAutoInstallDialog:
            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return QDialog.Rejected

        monkeypatch.setattr(mw, "AutoInstallDialog", _SkipAutoInstallDialog)
        win._settingsDialog.close()
        _wait_until(lambda: hub.updater.start_verify.call_count == 1)
        hub.updater.start_verify.assert_called_once_with(True)
        assert hub.settings.state.first_run_verify_pending is False
    finally:
        app.close()
        app._hub.close()


# ── tab switching ─────────────────────────────────────────────────────────


def test_nav_buttons_switch_tabs_and_expose_panels(qapp, app_no_startup):
    win = app_no_startup._window
    for name, obj in (
        ("NEWS", "newsPanel"),
        ("TWEAKS", "tweaksPanel"),
        ("ADDONS", "addonsPanel"),
        ("MODS", "modsPanel"),
    ):
        index = mw.MainWindow.TABS.index(name)
        QTest.mouseClick(win._navButtons[name], Qt.LeftButton)
        assert win._stack.currentIndex() == index
        assert win._navButtons[name].isChecked()
        assert win._stack.widget(index).objectName() == obj
    assert not win._navButtons["NEWS"].isChecked()


# ── settings dialog ───────────────────────────────────────────────────────


def test_settings_dialog_opens_from_gear_and_closes(qapp, app_no_startup):
    win = app_no_startup._window
    assert win._settingsDialog is None
    QTest.mouseClick(win._gearButton, Qt.LeftButton)
    dialog = win._settingsDialog
    assert isinstance(dialog, SettingsDialog)
    assert dialog.isVisible()
    assert dialog.windowTitle() == "Settings"

    QTest.mouseClick(dialog.findChild(QWidget, "settingsClose"), Qt.LeftButton)
    QTest.qWait(50)
    assert not dialog.isVisible()


# ── update status → progress → finish cycle ───────────────────────────────


def test_update_status_progress_finish_cycle(qapp, app_no_startup):
    win = app_no_startup._window
    hub = app_no_startup._hub
    # No manifest fetched yet, but launch is available → PLAY (the folder
    # may already be ready to run).
    assert win._updateButton.text() == "PLAY"

    hub.dispatcher.post(StatusChanged("Ready to update"))
    QTest.qWait(120)
    assert win._statusLabel.text() == "Ready to update"
    assert win._updateButton.text() == "PLAY"

    hub.dispatcher.post(ProgressChanged(0.5, "Downloading…"))
    QTest.qWait(120)
    assert win._progressBar.value() == 50
    assert win._progressLabel.text() == "Downloading…"
    assert win._progressBar.isVisible()

    # The update worker sets client_ready and posts 100% progress before the
    # finish event; the footer mirrors that real controller state.
    hub.updater.state.client_ready = True
    hub.updater.state.manifest_available = True
    hub.dispatcher.post(ProgressChanged(1.0, ""))
    QTest.qWait(120)
    assert not win._progressBar.isVisible()
    assert win._progressBar.value() == 100

    hub.dispatcher.post(OperationFinished("update", True, ""))
    QTest.qWait(120)
    assert win._updateButton.text() == "PLAY"
    assert win._updateButton.isEnabled()
    assert win._statusLabel.text() == "Everything up to date!"


# ── mods / addons snapshots → rows + nav badges ───────────────────────────


def test_mods_loaded_renders_rows_and_updates_badge(qapp, app_no_startup):
    win = app_no_startup._window
    hub = app_no_startup._hub
    panel = win._stack.widget(win._pages["MODS"])

    state = ModsState(latest_versions={"VanillaFixes": "2.0"}, updates_count=3)
    hub.mods.state = state
    hub.dispatcher.post(ModsLoaded(state))
    QTest.qWait(200)

    assert "VanillaFixes" in panel._rows
    assert "2.0" in panel._rows["VanillaFixes"].version_label.text()
    badge = win._tabBadges["MODS"]
    assert badge.text() == "3"
    assert badge.isVisible()


def test_addons_loaded_renders_rows_and_updates_badge(qapp, app_no_startup):
    win = app_no_startup._window
    hub = app_no_startup._hub
    panel = win._stack.widget(win._pages["ADDONS"])

    state = AddonsState(
        addons={
            "SellValue": AddonState.from_dict(
                {
                    "folder": "SellValue",
                    "status": "outOfDate",
                    "git": "https://github.com/octo/SellValue",
                    "toc": {"Title": "Sell Value", "Interface": "11200"},
                }
            )
        },
        available=[],
        updates_count=1,
    )
    hub.addons.state = state
    hub.dispatcher.post(AddonsLoaded(state))
    QTest.qWait(200)

    row = panel._rows["SellValue"]
    status = row.findChild(QLabel, "addonsStatus_SellValue")
    assert status.text() == "Update"
    badge = win._tabBadges["ADDONS"]
    assert badge.text() == "1"
    assert badge.isVisible()
