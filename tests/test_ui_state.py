"""Unit tests for the shared application-state models (ui_state)."""

from vanilla_wow_launcher.state.models import (
    AddonError,
    AddonsState,
    AddonState,
    AppState,
    LogEntry,
    ModPending,
    ModsState,
    ModState,
    NewsState,
    SettingsState,
    UpdateState,
)

# ── defaults ──────────────────────────────────────────────────────────────


def test_update_state_defaults():
    s = UpdateState()
    assert s.status == "Ready to update"
    assert s.progress == 0.0
    assert s.progress_label == ""
    assert s.running is False
    assert s.client_ready is False
    assert s.diff_nodes is None
    assert s.client_version == ""


def test_news_state_defaults():
    n = NewsState()
    assert n.featured is None
    assert n.items is None
    assert n.feat_ts == 0.0
    assert n.news_ts == 0.0


def test_mods_state_defaults():
    m = ModsState()
    assert m.records == {}
    assert m.latest_versions == {}
    assert m.pending == {}
    assert m.updates_count == 0
    assert m.has_errors is False
    assert m.has_pending_changes is False
    assert m.latest_version("VanillaFixes") is None


def test_addons_state_defaults():
    a = AddonsState()
    assert a.state == "idle"
    assert a.addons == {}
    assert a.available == []
    assert a.busy is False
    assert a.installing is False
    assert a.verified_ts == 0.0
    assert a.errors == {}
    assert a.sections_open == {"INSTALLED": True, "AVAILABLE": True}
    assert a.updates_count == 0
    assert a.out_of_date_count() == 0


def test_settings_state_defaults():
    s = SettingsState()
    assert s.path == ""
    assert s.config == {}
    assert s.first_run is False
    assert s.first_run_av_pending is False
    assert s.first_run_verify_pending is False


def test_app_state_defaults():
    st = AppState()
    assert isinstance(st.update, UpdateState)
    assert isinstance(st.news, NewsState)
    assert isinstance(st.mods, ModsState)
    assert isinstance(st.addons, AddonsState)
    assert isinstance(st.settings, SettingsState)
    assert st.log_buffer == []
    assert st.log_lines() == []


# ── update flow ───────────────────────────────────────────────────────────


def test_update_state_construction():
    s = UpdateState(
        status="Downloading…",
        progress=0.42,
        progress_label="WoW.exe",
        running=True,
        client_ready=False,
        diff_nodes=[],
        client_version="1.16.1",
    )
    assert s.status == "Downloading…"
    assert s.progress == 0.42
    assert s.progress_label == "WoW.exe"
    assert s.running is True
    assert s.client_ready is False
    assert s.diff_nodes == []
    assert s.client_version == "1.16.1"


# ── news ─────────────────────────────────────────────────────────────────


def test_news_state_with_sample_data():
    featured = {
        "id": 42,
        "title": "1.16.2 is live",
        "author": "Staff",
        "date": "2026-08-01T00:00:00+00:00",
        "url": "…",
        "html": "<p>…</p>",
    }
    items = [
        {
            "id": 7,
            "title": "Patch notes",
            "date": "2026-08-02T00:00:00+00:00",
            "body": "…",
            "author": "Staff",
        }
    ]
    n = NewsState(featured=featured, items=items, feat_ts=100.0, news_ts=200.0)
    assert n.featured["id"] == 42
    assert n.featured["title"] == "1.16.2 is live"
    assert n.items[0]["title"] == "Patch notes"
    assert n.feat_ts == 100.0
    assert n.news_ts == 200.0


# ── mods ─────────────────────────────────────────────────────────────────


def test_mod_state_record():
    rec = ModState(
        enabled=True,
        installed_version="2.0.1",
        installed_files=["d3d9.dll"],
        ignore_updates=False,
        error=None,
    )
    assert rec.enabled is True
    assert rec.installed_version == "2.0.1"
    assert rec.installed_files == ["d3d9.dll"]
    assert rec.ignore_updates is False
    assert rec.error is None
    assert rec.has_error is False


def test_mod_state_error_flag():
    rec = ModState(enabled=False, error="Download blocked")
    assert rec.has_error is True


def test_mod_pending_partial_change():
    p = ModPending(enabled=True)
    assert p.enabled is True
    assert p.ignore_updates is None
    q = ModPending(ignore_updates=True)
    assert q.enabled is None
    assert q.ignore_updates is True


def test_mods_state_construction():
    records = {
        "VanillaFixes": ModState(enabled=True, installed_version="2.0"),
        "dxvk": ModState(enabled=False, error="API rate limit"),
    }
    latest = {"VanillaFixes": "2.0", "dxvk": "1.10.5", "ClassicAPI": "1.3"}
    pending = {"dxvk": ModPending(enabled=True)}
    m = ModsState(
        records=records,
        latest_versions=latest,
        pending=pending,
        updates_count=2,
    )
    assert m.updates_count == 2
    assert m.latest_version("ClassicAPI") == "1.3"
    assert m.latest_version("nope") is None
    assert m.has_errors is True
    assert m.has_pending_changes is True


def test_mods_state_no_errors_or_pending():
    m = ModsState(records={"VanillaFixes": ModState(enabled=True)})
    assert m.has_errors is False
    assert m.has_pending_changes is False


# ── addons ───────────────────────────────────────────────────────────────

ADDON_REC = {
    "folder": "pfUI",
    "status": "upToDate",
    "git": "https://github.com/brues-code/pfUI",
    "branch": "master",
    "ref": "abc123",
    "toc": {"Title": "pfUI", "Interface": "11200"},
    "description": "Full interface",
    "error": None,
}


def test_addon_state_round_trips_dict():
    rec = AddonState.from_dict(ADDON_REC)
    assert rec.folder == "pfUI"
    assert rec.status == "upToDate"
    assert rec.git.endswith("/pfUI")
    assert rec.branch == "master"
    assert rec.ref == "abc123"
    assert rec.toc["Title"] == "pfUI"
    assert rec.error is None
    assert rec.to_dict() == ADDON_REC


def test_addons_state_from_and_to_status_dict():
    status = {
        "state": "done",
        "addons": {
            "pfUI": dict(ADDON_REC, status="outOfDate"),
            "ShaguDPS": dict(ADDON_REC, folder="ShaguDPS"),
        },
        "available": [dict(ADDON_REC, folder="AtlasLoot", status="available")],
    }
    st = AddonsState.from_status_dict(status)
    assert st.state == "done"
    assert set(st.addons) == {"pfUI", "ShaguDPS"}
    assert st.addons["pfUI"].status == "outOfDate"
    assert st.addons["ShaguDPS"].folder == "ShaguDPS"
    assert st.available[0].folder == "AtlasLoot"
    assert st.available[0].status == "available"
    assert st.to_status_dict() == status


def test_addons_state_out_of_date_count():
    st = AddonsState(
        addons={
            "pfUI": AddonState.from_dict(dict(ADDON_REC, status="outOfDate")),
            "ShaguDPS": AddonState.from_dict(
                dict(ADDON_REC, folder="ShaguDPS")
            ),
        }
    )
    assert st.out_of_date_count() == 1
    assert AddonsState().out_of_date_count() == 0


def test_addons_state_errors():
    st = AddonsState(
        errors={
            "pfUI": AddonError(
                error="Connection reset",
                git="https://github.com/brues-code/pfUI",
            )
        }
    )
    assert st.errors["pfUI"].error == "Connection reset"
    assert st.errors["pfUI"].git == "https://github.com/brues-code/pfUI"


# ── settings / log ───────────────────────────────────────────────────────


def test_settings_state_with_sample_data():
    s = SettingsState(
        path="C:/Games/WoW",
        config={"out_dir": "C:/Games/WoW"},
        first_run=True,
        first_run_av_pending=True,
        first_run_verify_pending=True,
    )
    assert s.path == "C:/Games/WoW"
    assert s.config["out_dir"] == "C:/Games/WoW"
    assert s.first_run is True
    assert s.first_run_av_pending is True
    assert s.first_run_verify_pending is True


def test_app_state_logging():
    st = AppState()
    st.add_log("Hello\n", "acct")
    st.add_log("World\n")
    assert len(st.log_buffer) == 2
    assert st.log_buffer[0] == LogEntry("Hello\n", "acct")
    assert st.log_buffer[1].tag == ""
    assert st.log_lines() == [("Hello\n", "acct"), ("World\n", "")]


def test_app_state_holds_realistic_snapshot():
    st = AppState()
    st.update.client_ready = True
    st.news.featured = {"id": 1, "title": "x"}
    st.mods.updates_count = 3
    st.addons.state = "verifying"
    st.settings.path = "C:/Games/WoW"
    st.add_log("log line\n", "dim")
    assert st.update.client_ready is True
    assert st.news.featured["title"] == "x"
    assert st.mods.updates_count == 3
    assert st.addons.state == "verifying"
    assert st.settings.path == "C:/Games/WoW"
    assert st.log_lines() == [("log line\n", "dim")]
