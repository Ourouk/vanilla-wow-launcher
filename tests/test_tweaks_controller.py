"""Unit tests for the tweaks controller (tweaks_controller).

No Tk involved: the controller is driven directly and its effects are read
from the shared EventDispatcher. Backends (config store, Config.wtf writer)
are swapped for fakes via monkeypatch so nothing touches the real filesystem
or config.
"""

import time

import pytest

import vanilla_wow_launcher.controllers.tweaks as tc
from vanilla_wow_launcher.controllers.tweaks import TweaksController
from vanilla_wow_launcher.services.tweaks import (
    TWEAKS_DEFAULTS,
    fov_default_for_display,
)
from vanilla_wow_launcher.state.events import (
    EventDispatcher,
    LogMessage,
    OperationFinished,
)


@pytest.fixture
def backends(monkeypatch):
    """Shared fake backends: config store + Config.wtf writer."""
    state = {"out_dir": "/tmp/octo-game"}
    monkeypatch.setattr(tc.config_store, "load_config", lambda: state)
    monkeypatch.setattr(
        tc.config_store,
        "update_config",
        lambda mutator: (mutator(state), state)[1],
    )
    monkeypatch.setattr(
        tc.tweaks,
        "load_tweaks_config",
        lambda: (
            state.get("tweaks")
            or {**TWEAKS_DEFAULTS, "fieldOfView": fov_default_for_display()}
        ),
    )
    monkeypatch.setattr(
        tc.tweaks, "update_config_wtf", lambda client_dir, tweaks: None
    )
    monkeypatch.setattr(
        tc.tweaks,
        "save_tweaks_config",
        lambda values: state.__setitem__("tweaks", values),
    )
    return state


@pytest.fixture
def controller(backends):
    return TweaksController(
        EventDispatcher(), get_out_dir=lambda: backends["out_dir"]
    )


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


# ── values ─────────────────────────────────────────────────────────────


def test_values_returns_saved_config(controller, backends):
    saved = dict(TWEAKS_DEFAULTS)
    saved["fieldOfView"] = 150
    backends["tweaks"] = saved
    assert controller.values() == saved


def test_default_get_out_dir_reads_config(backends):
    backends["out_dir"] = "/from/config"
    ctrl = TweaksController(EventDispatcher())
    assert ctrl._get_out_dir() == "/from/config"


# ── validate_entries ────────────────────────────────────────────────────


def test_validate_entries_clamps_out_of_range(controller):
    any_bad, ui = controller.validate_entries(
        {
            "fieldOfView": "500",
            "nameplateRange": "10",
        }
    )
    assert any_bad is True
    assert ui["fieldOfView"] == 180  # clamped to max
    assert ui["nameplateRange"] == 10  # in range, untouched


def test_validate_entries_flags_below_min(controller):
    any_bad, ui = controller.validate_entries({"cameraDistance": "10"})
    assert any_bad is True
    assert ui["cameraDistance"] == 50  # clamped to min


def test_validate_entries_unparseable_falls_back_to_default(controller):
    any_bad, ui = controller.validate_entries({"farClip": ""})
    assert any_bad is True
    assert ui["farClip"] == TWEAKS_DEFAULTS["farClip"]


def test_validate_entries_bools_normalized(controller):
    any_bad, ui = controller.validate_entries(
        {
            "soundInBackground": "True",
            "nameplateRange": "41",
        }
    )
    assert any_bad is False
    assert ui["soundInBackground"] is True
    assert ui["nameplateRange"] == 41


def test_validate_entries_clean_ui_is_not_bad(controller):
    any_bad, ui = controller.validate_entries(
        {
            "fieldOfView": 110,
            "nameplateRange": 41,
        }
    )
    assert any_bad is False
    assert ui == {"fieldOfView": 110, "nameplateRange": 41}


# ── dirty_and_custom ───────────────────────────────────────────────────


def test_dirty_and_custom_matches_saved_config(controller, backends):
    backends["tweaks"] = dict(TWEAKS_DEFAULTS)
    ui = dict(TWEAKS_DEFAULTS)
    ui["fieldOfView"] = fov_default_for_display()
    assert controller.dirty_and_custom(ui) == (False, False)


def test_dirty_and_custom_detects_changes(controller, backends):
    backends["tweaks"] = dict(TWEAKS_DEFAULTS)
    ui = dict(TWEAKS_DEFAULTS)
    ui["fieldOfView"] = fov_default_for_display()
    ui["nameplateRange"] = 30
    assert controller.dirty_and_custom(ui) == (True, True)


def test_dirty_and_custom_out_of_range_is_dirty_even_if_clamped_equal(
    controller, backends
):
    # saved = defaults (fov 110); typed 110 stays in range, so use the
    # out-of-range case from the Tk comment: saved 180, typed 192 clamps to
    # 180 — dirty must still be True because the entry is bad.
    backends["tweaks"] = {
        "soundInBackground": True,
        "nameplateRange": 41,
        "fieldOfView": 180,
        "farClip": 777,
        "frillDistance": 70,
        "cameraDistance": 50,
    }
    ui = {
        "soundInBackground": True,
        "nameplateRange": 41,
        "fieldOfView": "192",
        "farClip": 777,
        "frillDistance": 70,
        "cameraDistance": 50,
    }
    dirty, custom = controller.dirty_and_custom(ui)
    assert dirty is True
    assert custom is True


# ── apply ──────────────────────────────────────────────────────────────


def test_apply_persists_clamped_and_posts_finished(controller, backends):
    spawned = controller.apply(
        {
            "nameplateRange": 41,
            "fieldOfView": "500",
            "farClip": 777,
            "frillDistance": 70,
            "cameraDistance": 50,
            "soundInBackground": True,
        }
    )
    assert spawned is True
    assert backends["tweaks"]["fieldOfView"] == 180  # clamped on save

    collected = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert OperationFinished("tweaks", True, "") in collected
    assert LogMessage("\nTweaks applied.\n", "ok") in collected
    assert any(isinstance(e, LogMessage) for e in collected)
    assert controller.running is False


def test_apply_writes_config_wtf(controller, backends, monkeypatch):
    written = []

    def record(client_dir, tweak_values):
        written.append((client_dir, tweak_values))

    monkeypatch.setattr(tc.tweaks, "update_config_wtf", record)

    values = dict(TWEAKS_DEFAULTS)
    values["fieldOfView"] = fov_default_for_display()
    spawned = controller.apply(values)
    assert spawned is True
    _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert written == [(backends["out_dir"], values)]


def test_apply_without_folder_posts_error_and_does_not_spawn(
    controller, backends, monkeypatch
):
    backends["out_dir"] = "   "
    spawned = controller.apply(dict(TWEAKS_DEFAULTS))
    assert spawned is False
    events = controller._dispatcher.drain()
    assert LogMessage("Game folder not set.\n", "err") in events
    assert not any(isinstance(e, OperationFinished) for e in events)


def test_apply_posts_failure_events_on_error(
    controller, backends, monkeypatch
):
    def boom(client_dir, tweaks):
        raise RuntimeError("disk full")

    monkeypatch.setattr(tc.tweaks, "update_config_wtf", boom)

    controller.apply(dict(TWEAKS_DEFAULTS))
    collected = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert OperationFinished("tweaks", False, "disk full") in collected
    assert any(isinstance(e, tc.OperationFailed) for e in collected)
    assert controller.running is False


def test_apply_guards_reentry(controller, backends, monkeypatch):
    started = []

    def slow_worker(client_dir, tweaks):
        started.append(client_dir)
        time.sleep(0.3)

    monkeypatch.setattr(controller, "_apply_worker", slow_worker)

    assert controller.apply(dict(TWEAKS_DEFAULTS)) is True
    assert controller.running is True
    # Second apply while running is refused.
    assert controller.apply(dict(TWEAKS_DEFAULTS)) is False
    deadline = time.monotonic() + 1.0
    while controller.running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.running is False
    assert len(started) == 1


# ── reset ──────────────────────────────────────────────────────────────


def test_reset_saves_defaults_and_spawns(controller, backends):
    defaults = dict(TWEAKS_DEFAULTS)
    defaults["fieldOfView"] = fov_default_for_display()
    spawned = controller.reset(defaults)
    assert spawned is True
    assert backends["tweaks"] == defaults
    collected = _drain_for(
        controller._dispatcher, lambda e: isinstance(e, OperationFinished)
    )
    assert LogMessage("\nTweaks applied.\n", "ok") in collected
    assert controller.running is False


def test_reset_builds_defaults_when_omitted(controller, backends):
    spawned = controller.reset()
    assert spawned is True
    assert backends["tweaks"]["fieldOfView"] == fov_default_for_display()


def test_reset_without_folder_is_noop(controller, backends):
    backends["out_dir"] = ""
    assert controller.reset() is False
    assert controller._dispatcher.drain() == []
