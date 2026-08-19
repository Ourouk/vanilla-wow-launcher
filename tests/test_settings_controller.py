"""Unit tests for the settings/game-folder controller (settings_controller).

No Tk involved: the controller is driven directly and its effects are read
from the shared EventDispatcher and its SettingsState. Backends (config store,
filesystem, platform support, browser, mirror/AV network calls) are swapped
for fakes via monkeypatch so nothing touches the network or the real config.
"""

import time

import pytest

import vanilla_wow_launcher.controllers.settings as sc
from vanilla_wow_launcher.controllers.settings import SettingsController
from vanilla_wow_launcher.state.events import (
    EventDispatcher,
    LogMessage,
    MirrorStatusChanged,
)

# Small synthetic registries so tests don't depend on the real ones.


class _FakeModsState:
    busy = False


class _FakeAddonsState:
    busy = False


class _FakeMods:
    def __init__(self):
        self.resets = 0
        self.toggled = []
        self.applied = 0
        self.invalidated = 0
        self.reloaded = 0
        self.state = _FakeModsState()

    def reset(self):
        self.resets += 1

    def toggle(self, mod_id, enabled):
        self.toggled.append((mod_id, enabled))

    def apply(self, only_mod_id=None):
        self.applied += 1

    def invalidate(self):
        self.invalidated += 1

    def load_latest_versions(self):
        self.reloaded += 1


class _FakeAddons:
    def __init__(self):
        self.resets = 0
        self.applied = 0
        self.applied_recs = []
        self.invalidated = 0
        self.verify_calls = 0
        self.recommended_recs = 0
        self.state = _FakeAddonsState()

    def reset(self):
        self.resets += 1

    def apply(self, recs):
        self.applied += 1
        self.applied_recs.append(recs)

    def apply_recommended_addons(self):
        self.recommended_recs += 1
        return True

    def invalidate(self):
        self.invalidated += 1

    def verify(self, **kwargs):
        self.verify_calls += 1
        return True


class _FakeNews:
    def __init__(self):
        self.invalidate_calls = 0

    def invalidate(self):
        self.invalidate_calls += 1


class _FakeUpdater:
    def __init__(self):
        self.running = False
        self.invalidate_calls = 0
        self.verify_calls = []

    def invalidate(self):
        self.invalidate_calls += 1

    def start_verify(self, overwrite_config=False):
        self.verify_calls.append(overwrite_config)


class _Fakes:
    def __init__(self):
        self.dispatcher = EventDispatcher()
        self.updater = _FakeUpdater()
        self.mods = _FakeMods()
        self.addons = _FakeAddons()
        self.news = _FakeNews()


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    state = {"out_dir": "/tmp/octo-game"}
    monkeypatch.setattr(sc.config_store, "load_config", lambda: state)
    monkeypatch.setattr(
        sc.config_store,
        "update_config",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(sc, "CONFIG_FILE", str(tmp_path / "no-config.json"))
    return state


@pytest.fixture
def fakes():
    return _Fakes()


@pytest.fixture
def controller(cfg, fakes):
    return SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )


class _OkCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _drain_for(dispatcher, predicate, timeout=2.0):
    """Drain until an event matching `predicate` arrives; return everything
    drained along the way (assertion failure on timeout)."""
    deadline = time.monotonic() + timeout
    collected = []
    while True:
        collected.extend(dispatcher.drain())
        if any(predicate(e) for e in collected):
            return collected
        if time.monotonic() > deadline:
            raise AssertionError("expected event never arrived")
        time.sleep(0.005)


def _log_texts(events):
    return [e.text for e in events if isinstance(e, LogMessage)]


# ── first-run flags ────────────────────────────────────────────────────────


def test_first_run_flags_initialized(cfg, monkeypatch):
    c = SettingsController(
        EventDispatcher(),
        _FakeUpdater(),
        _FakeMods(),
        _FakeAddons(),
        _FakeNews(),
    )
    assert c.state.first_run is True
    assert c.state.first_run_verify_pending is True
    assert c.state.first_run_av_pending is False  # can_manage_antivirus → off


def test_client_updates_default_to_enabled(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    assert c.client_update_enabled is True
    assert c.state.first_run_verify_pending is True


def test_client_updates_setting_persists_and_controls_first_run(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_client_update_enabled(False)
    assert cfg["client_update_enabled"] is False
    assert c.client_update_enabled is False
    assert c.state.first_run_verify_pending is False
    c.set_client_update_enabled(True)
    assert cfg["client_update_enabled"] is True
    assert c.state.first_run_verify_pending is True


# ── umu-launcher settings ─────────────────────────────────────────────────


def test_launch_settings_defaults(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    assert c.launch.umu_proton == "UMU-Proton"
    assert c.launch.umu_renderer == "auto"
    assert c.launch.umu_binary_path == ""
    assert c.launch.umu_game_id == "umu-vanilla-wow"


def test_launch_settings_loaded_from_config(cfg, fakes):
    cfg["launch"] = {
        "umu_proton": "UMU-Proton",
        "umu_binary_path": "/opt/umu-run",
        "umu_game_id": "umu-custom",
    }
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    assert c.launch.umu_proton == "UMU-Proton"
    assert c.launch.umu_binary_path == "/opt/umu-run"
    assert c.launch.umu_game_id == "umu-custom"


def test_set_umu_proton_persists(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_proton("GE-Proton9-4")
    assert cfg["launch"]["umu_proton"] == "GE-Proton9-4"
    assert c.launch.umu_proton == "GE-Proton9-4"
    c.set_umu_proton("  ")
    assert c.launch.umu_proton == "UMU-Proton"


def test_set_umu_binary_path_persists(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_binary_path("/opt/umu-run")
    assert cfg["launch"]["umu_binary_path"] == "/opt/umu-run"
    assert c.launch.umu_binary_path == "/opt/umu-run"
    c.set_umu_binary_path("  ")
    assert c.launch.umu_binary_path == ""


def test_set_umu_game_id_persists(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_game_id("umu-test")
    assert cfg["launch"]["umu_game_id"] == "umu-test"
    assert c.launch.umu_game_id == "umu-test"
    c.set_umu_game_id("  ")
    assert c.launch.umu_game_id == "umu-vanilla-wow"


def test_set_umu_renderer_persists(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_renderer("wined3d-opengl")
    assert cfg["launch"]["umu_renderer"] == "wined3d-opengl"
    assert c.launch.umu_renderer == "wined3d-opengl"
    c.set_umu_renderer("bogus")
    assert c.launch.umu_renderer == "auto"
    c.set_umu_renderer("  ")
    assert c.launch.umu_renderer == "auto"


def test_available_protons_includes_detected_builds(cfg, fakes, monkeypatch):
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.available_protons",
        lambda: ["GE-Proton9-4", "UMU-Proton"],
    )
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    assert c.available_protons() == ["GE-Proton9-4", "UMU-Proton"]


def test_launch_settings_renderer_and_feature_defaults(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    assert c.launch.umu_renderer == "auto"
    assert c.launch.umu_gamemode is True
    assert c.launch.umu_wayland is True


def test_set_umu_gamemode_persists(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_gamemode(False)
    assert cfg["launch"]["umu_gamemode"] is False
    assert c.launch.umu_gamemode is False
    c.set_umu_gamemode(True)
    assert cfg["launch"]["umu_gamemode"] is True


def test_set_umu_wayland_persists(cfg, fakes):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_wayland(False)
    assert cfg["launch"]["umu_wayland"] is False
    assert c.launch.umu_wayland is False
    c.set_umu_wayland(True)
    assert cfg["launch"]["umu_wayland"] is True


def test_linux_features_delegates_to_umu(cfg, fakes, monkeypatch):
    features = {
        "gamemode_available": True,
        "wayland_session": False,
    }
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.scan_linux_features",
        lambda: features,
    )
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    assert c.linux_features() == features


def test_resolve_umu_binary_prefers_override(cfg, fakes, monkeypatch):
    c = SettingsController(
        fakes.dispatcher, fakes.updater, fakes.mods, fakes.addons, fakes.news
    )
    c.set_umu_binary_path("/opt/umu-run")
    assert c.resolve_umu_binary() == "/opt/umu-run"
    c.set_umu_binary_path("")
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.find_umu",
        lambda: "/usr/bin/umu-run",
    )
    assert c.resolve_umu_binary() == "/usr/bin/umu-run"


def test_first_run_av_pending_on_windows(cfg, monkeypatch):
    monkeypatch.setattr(
        sc.platform_support, "can_manage_antivirus", lambda: True
    )
    c = SettingsController(
        EventDispatcher(),
        _FakeUpdater(),
        _FakeMods(),
        _FakeAddons(),
        _FakeNews(),
    )
    assert c.state.first_run is True
    assert c.state.first_run_av_pending is True


# ── set_path ───────────────────────────────────────────────────────────────


def test_set_path_same_is_noop(controller, cfg, fakes):
    assert controller.set_path(cfg["out_dir"]) is False
    assert controller.set_path(f"{cfg['out_dir']}/") is False
    assert fakes.updater.verify_calls == []
    assert fakes.mods.resets == 0
    assert controller._dispatcher.drain() == []


def test_set_path_resets_for_new_folder(
    controller, cfg, fakes, monkeypatch, tmp_path
):
    cache = tmp_path / "hash.json"
    cache.write_text("{}")
    monkeypatch.setattr(sc, "CACHE_FILE", str(cache))
    game = tmp_path / "game"
    game.mkdir()
    (game / "WoW.exe").write_bytes(b"MZ")
    cfg["mods"] = {"VanillaFixes": {"enabled": True}}
    cfg["addons"] = {"pfUI": {"git": "x"}}
    wdb_calls = []
    monkeypatch.setattr(
        sc.filesystem, "remove_wdb", lambda d: wdb_calls.append(d)
    )

    assert controller.set_path(str(game)) is True

    assert not cache.exists()
    assert wdb_calls == [str(game)]
    assert cfg["out_dir"] == str(game)
    assert "mods" not in cfg
    assert "addons" not in cfg
    assert controller.state.path == str(game)
    assert controller.state.config == cfg
    assert fakes.updater.invalidate_calls == 1
    assert fakes.updater.verify_calls == [True]
    assert fakes.mods.resets == 1
    assert fakes.addons.resets == 1
    assert fakes.news.invalidate_calls == 1
    assert controller.state.first_run_verify_pending is False
    assert controller.state.first_run_av_pending is False
    events = controller._dispatcher.drain()
    assert any("Game folder changed" in t for t in _log_texts(events))


def test_set_path_without_wow_exe_skips_wdb(
    controller, cfg, fakes, monkeypatch, tmp_path
):
    game = tmp_path / "game"
    game.mkdir()
    wdb_calls = []
    monkeypatch.setattr(
        sc.filesystem, "remove_wdb", lambda d: wdb_calls.append(d)
    )
    controller.set_path(str(game))
    assert wdb_calls == []


def test_set_path_rejected_while_update_running(
    controller, cfg, fakes, monkeypatch, tmp_path
):
    """A folder change during a running update is ignored wholesale."""
    cache = tmp_path / "hash.json"
    cache.write_text("{}")
    monkeypatch.setattr(sc, "CACHE_FILE", str(cache))
    game = tmp_path / "game"
    game.mkdir()
    cfg["mods"] = {"VanillaFixes": {"enabled": True}}
    fakes.updater.running = True

    assert controller.set_path(str(game)) is False

    assert cache.exists()
    assert "mods" in cfg
    assert cfg["out_dir"] == "/tmp/octo-game"
    assert controller.state.path == "/tmp/octo-game"
    assert fakes.updater.invalidate_calls == 0
    assert fakes.updater.verify_calls == []
    assert fakes.mods.resets == 0
    assert fakes.addons.resets == 0
    events = controller._dispatcher.drain()
    assert any("update is running" in t for t in _log_texts(events))


# ── AV exclusion gating ────────────────────────────────────────────────────


def test_should_prompt_av_reflects_platform(controller, monkeypatch):
    monkeypatch.setattr(
        sc.platform_support, "can_manage_antivirus", lambda: True
    )
    assert controller.should_prompt_av() is True
    monkeypatch.setattr(
        sc.platform_support, "can_manage_antivirus", lambda: False
    )
    assert controller.should_prompt_av() is False


def test_av_prompt_dismissed_clears_pending(controller):
    controller.state.first_run_av_pending = True
    controller.av_prompt_dismissed()
    assert controller.state.first_run_av_pending is False


def test_allow_through_antivirus_off_windows_posts_error(
    controller, monkeypatch
):
    monkeypatch.setattr(
        sc.platform_support, "can_manage_antivirus", lambda: False
    )
    controller.allow_through_antivirus()
    events = controller._dispatcher.drain()
    assert any("not available" in t for t in _log_texts(events))


def test_allow_through_antivirus_on_windows(controller, monkeypatch):
    import ctypes
    from types import SimpleNamespace

    class _Shell:
        def __init__(self, rc):
            self.rc = rc
            self.calls = []

        def ShellExecuteW(self, *a, **k):
            self.calls.append(a)
            return self.rc

    shell = _Shell(42)
    monkeypatch.setattr(
        sc.platform_support, "can_manage_antivirus", lambda: True
    )
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(shell32=shell), raising=False
    )
    controller.state.path = "C:/Games/VanillaWoW"
    controller.state.first_run_av_pending = True
    controller.allow_through_antivirus()
    assert controller.state.first_run_av_pending is False
    assert shell.calls and "Add-MpPreference" in shell.calls[0][3]
    events = controller._dispatcher.drain()
    assert any("Requested Defender exclusion" in t for t in _log_texts(events))


def test_allow_through_antivirus_cancelled(controller, monkeypatch):
    import ctypes
    from types import SimpleNamespace

    class _Shell:
        def ShellExecuteW(self, *a, **k):
            return 0

    monkeypatch.setattr(
        sc.platform_support, "can_manage_antivirus", lambda: True
    )
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(shell32=_Shell()), raising=False
    )
    controller.state.path = "C:/Games/VanillaWoW"
    controller.allow_through_antivirus()
    events = controller._dispatcher.drain()
    assert any(
        "Antivirus exclusion cancelled" in t for t in _log_texts(events)
    )


# ── toggles ────────────────────────────────────────────────────────────────


def test_toggles_persist_config_keys(controller, cfg):
    controller.set_clear_wdb(True)
    controller.set_close_on_launch(False)
    assert cfg["clear_wdb_on_launch"] is True
    assert cfg["close_on_launch"] is False


# ── verify_files ───────────────────────────────────────────────────────────


def test_verify_files_delegates(controller, cfg, fakes, monkeypatch, tmp_path):
    cache = tmp_path / "hash.json"
    cache.write_text("{}")
    monkeypatch.setattr(sc, "CACHE_FILE", str(cache))
    controller.verify_files()
    assert not cache.exists()
    assert fakes.updater.invalidate_calls == 1
    assert fakes.updater.verify_calls == [False]
    events = controller._dispatcher.drain()
    assert any("Verify game files" in t for t in _log_texts(events))


def test_verify_files_skips_when_running(controller, fakes):
    fakes.updater.running = True
    controller.verify_files()
    assert fakes.updater.verify_calls == []
    assert controller._dispatcher.drain() == []


# ── check_mirror ───────────────────────────────────────────────────────────


def test_check_mirror_posts_online(controller, monkeypatch):
    monkeypatch.setattr(sc, "secure_urlopen", lambda req, timeout=6: _OkCtx())
    controller.check_mirror()
    events = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, MirrorStatusChanged)
    )
    assert any(
        isinstance(e, MirrorStatusChanged)
        and e.ok is True
        and e.text == "online"
        for e in events
    )
    assert controller.mirror_statuses == {
        "Backup": "online",
    }


def test_check_mirror_posts_offline(controller, monkeypatch):
    def boom(req, timeout=6):
        raise ConnectionError("offline")

    monkeypatch.setattr(sc, "secure_urlopen", boom)
    controller.check_mirror()
    events = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, MirrorStatusChanged)
    )
    assert any(
        isinstance(e, MirrorStatusChanged)
        and e.ok is False
        and e.text == "offline"
        for e in events
    )
    assert controller.mirror_statuses == {
        "Backup": "offline",
    }


def test_check_mirror_http_error_still_online(controller, monkeypatch):
    """An HTTP error status from a source (e.g. a CDN root returning 404)
    still proves it is reachable — only transport failures are offline."""
    from urllib.error import HTTPError

    def http_error(req, timeout=6):
        raise HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(sc, "secure_urlopen", http_error)
    controller.check_mirror()
    events = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, MirrorStatusChanged)
    )
    assert any(
        isinstance(e, MirrorStatusChanged)
        and e.ok is True
        and e.text == "online"
        for e in events
    )
    assert controller.mirror_statuses == {
        "Backup": "online",
    }


def test_http_mirror_names_follow_launcher(controller):
    assert controller._http_mirror_names() == ["Backup"]


# ── open helpers ───────────────────────────────────────────────────────────


def test_open_client_folder_opens_and_logs(controller, monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    controller.state.path = str(game)
    opened = []
    monkeypatch.setattr(
        sc.platform_support, "open_folder", lambda p: opened.append(p)
    )
    controller.open_client_folder()
    assert opened == [str(game)]
    events = controller._dispatcher.drain()
    assert any("Opened folder" in t for t in _log_texts(events))


def test_open_client_folder_missing_logs_error(controller, tmp_path):
    controller.state.path = str(tmp_path / "nope")
    controller.open_client_folder()
    events = controller._dispatcher.drain()
    assert any("Folder not found" in t for t in _log_texts(events))


def test_open_client_folder_oserror_logs(controller, monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    controller.state.path = str(game)

    def boom(p):
        raise OSError("no opener")

    monkeypatch.setattr(sc.platform_support, "open_folder", boom)
    controller.open_client_folder()
    events = controller._dispatcher.drain()
    assert any("Could not open folder" in t for t in _log_texts(events))


def test_open_url_launches_browser(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(sc.webbrowser, "open", lambda url: calls.append(url))
    controller.open_url("https://example.com")
    assert calls == ["https://example.com"]


# ── catalog registries ───────────────────────────────────────────────────────


def test_registry_urls_use_launcher_defaults(controller, cfg):
    assert (
        controller.addons_registry_url()
        == "https://launcher.test/api/addons.json"
    )
    assert (
        controller.mods_registry_url() == "https://launcher.test/api/mods.json"
    )


def test_set_registry_url_persists(controller, cfg):
    assert (
        controller.set_addons_registry_url("https://example.com/addons.json")
        is None
    )
    assert cfg["addons_registry_url"] == "https://example.com/addons.json"
    assert (
        controller.set_mods_registry_url("https://example.com/mods.json")
        is None
    )
    assert cfg["mods_registry_url"] == "https://example.com/mods.json"


def test_set_registry_url_rejects_insecure(controller, cfg):
    err = controller.set_addons_registry_url("http://example.com/x")
    assert err is not None
    assert "addons_registry_url" not in cfg


def test_reset_registry_url(controller, cfg):
    cfg["addons_registry_url"] = "https://example.com/a.json"
    cfg["mods_registry_url"] = "https://example.com/m.json"
    controller.reset_addons_registry_url()
    controller.reset_mods_registry_url()
    assert "addons_registry_url" not in cfg
    assert "mods_registry_url" not in cfg
    events = controller._dispatcher.drain()
    assert any("reset to default" in t for t in _log_texts(events))


def test_reload_addons_registry_fetches_and_rescans(
    controller, cfg, fakes, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        sc.addons,
        "fetch_addons_catalog",
        lambda force=False: (
            calls.append(force)
            or [{"name": "Foo", "git": "https://github.com/x/y"}]
        ),
    )
    controller.reload_addons_registry()
    _drain_for(
        controller._dispatcher,
        lambda e: (
            isinstance(e, LogMessage) and "✓ Addon catalog reloaded" in e.text
        ),
    )
    assert calls == [True]
    assert fakes.addons.invalidated == 1
    assert fakes.addons.verify_calls == 1


def test_reload_addons_registry_failure_logs(
    controller, cfg, fakes, monkeypatch
):
    def boom(force=False):
        raise ConnectionError("offline")

    monkeypatch.setattr(sc.addons, "fetch_addons_catalog", boom)
    controller.reload_addons_registry()
    _drain_for(
        controller._dispatcher,
        lambda e: isinstance(e, LogMessage) and "reload failed" in e.text,
    )
    assert fakes.addons.verify_calls == 0


def test_reload_addons_registry_skips_when_busy(controller, cfg, fakes):
    fakes.addons.state.busy = True
    controller.reload_addons_registry()
    events = controller._dispatcher.drain()
    assert any("finish first" in t for t in _log_texts(events))
    assert fakes.addons.verify_calls == 0


def test_reload_mods_registry_fetches_and_rerenders(
    controller, cfg, fakes, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        sc.mods,
        "fetch_mods_catalog",
        lambda force=False: calls.append(force) or [],
    )
    controller.reload_mods_registry()
    _drain_for(
        controller._dispatcher,
        lambda e: (
            isinstance(e, LogMessage) and "✓ Mod catalog reloaded" in e.text
        ),
    )
    assert calls == [True]
    assert fakes.mods.invalidated == 1
    assert fakes.mods.reloaded == 1


def test_reload_mods_registry_failure_logs(
    controller, cfg, fakes, monkeypatch
):
    def boom(force=False):
        raise ConnectionError("offline")

    monkeypatch.setattr(sc.mods, "fetch_mods_catalog", boom)
    controller.reload_mods_registry()
    _drain_for(
        controller._dispatcher,
        lambda e: isinstance(e, LogMessage) and "reload failed" in e.text,
    )
    assert fakes.mods.reloaded == 0


def test_open_custom_file_creates_and_opens(controller, fakes, monkeypatch):
    created = []
    opened = []
    monkeypatch.setattr(
        sc.addons, "open_custom_file", lambda: created.append(1) or True
    )
    monkeypatch.setattr(
        sc.platform_support, "open_folder", lambda p: opened.append(p)
    )
    controller.open_addons_custom_file()
    assert created == [1]
    assert opened
    events = controller._dispatcher.drain()
    assert any(
        "Created the custom addon file" in t for t in _log_texts(events)
    )


def test_clear_custom_addons_logs(controller, monkeypatch):
    cleared = []
    monkeypatch.setattr(
        sc.addons, "clear_custom_file", lambda: cleared.append(1) or True
    )
    controller.clear_addons_custom()
    assert cleared == [1]
    events = controller._dispatcher.drain()
    assert any("Custom addon entries cleared" in t for t in _log_texts(events))
