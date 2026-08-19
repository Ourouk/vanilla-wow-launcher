"""Unit tests for the tweaks module (Config.wtf)."""

import vanilla_wow_launcher.core.config_store as config_store
import vanilla_wow_launcher.services.tweaks as tweaks


def test_tweak_limits_cover_all_numeric_items():
    for (
        tid,
        _label,
        kind,
        _rec,
        _d,
        _desc,
        lo,
        hi,
        _step,
    ) in tweaks.TWEAKS_ITEMS:
        if tid is not None and kind == "number":
            assert tweaks.TWEAKS_LIMITS[tid] == (lo, hi)


def test_load_tweaks_config_merges_defaults(tmp_path):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({"tweaks": {"farClip": 1000}})
    cfg = tweaks.load_tweaks_config()
    assert cfg["farClip"] == 1000
    assert cfg["nameplateRange"] == tweaks.TWEAKS_DEFAULTS["nameplateRange"]
    assert "fieldOfView" in cfg


def test_save_tweaks_config(tmp_path):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    tweaks.save_tweaks_config({"farClip": 42})
    assert config_store.load_config()["tweaks"] == {"farClip": 42}


def test_write_config_wtf_writes_file(tmp_path):
    client = tmp_path / "client"
    tweaks.write_config_wtf(str(client), tweaks.TWEAKS_DEFAULTS)
    cfg = client / "WTF" / "Config.wtf"
    assert cfg.exists()
    content = cfg.read_text(encoding="utf-8")
    assert 'SET realmList "launcher.test"' in content
    assert 'SET farClip "777"' in content


def test_update_config_wtf_creates_when_missing(tmp_path):
    client = tmp_path / "client"
    tweaks.update_config_wtf(str(client), tweaks.TWEAKS_DEFAULTS)
    assert (client / "WTF" / "Config.wtf").exists()


def test_update_config_wtf_updates_existing_values(tmp_path):
    client = tmp_path / "client"
    tweaks.write_config_wtf(str(client), tweaks.TWEAKS_DEFAULTS)
    cfg = client / "WTF" / "Config.wtf"
    cfg.write_text(
        'SET farClip "777"\nSET NameplateRange "41"\n', encoding="utf-8"
    )
    tweaks.update_config_wtf(
        str(client), dict(tweaks.TWEAKS_DEFAULTS, farClip=1000)
    )
    content = cfg.read_text(encoding="utf-8")
    assert 'SET farClip "1000"' in content
    # Unrelated lines are preserved.
    assert 'SET NameplateRange "41"' in content


def test_fov_default_for_display_matches_display():
    # Falls back to 16:9 defaults when the display can't be queried (or is
    # non-Windows); must still return a valid FOV.
    fov = tweaks.fov_default_for_display()
    assert isinstance(fov, int)
    assert 90 <= fov <= 180


def _write_with_renderer(monkeypatch, tmp_path, renderer):
    monkeypatch.setattr(
        tweaks,
        "load_config",
        lambda: {"launch": {"umu_renderer": renderer}},
    )
    client = tmp_path / "client"
    tweaks.write_config_wtf(str(client), tweaks.TWEAKS_DEFAULTS)
    return (client / "WTF" / "Config.wtf").read_text(encoding="utf-8")


def test_config_wtf_no_gxapi_when_auto(monkeypatch, tmp_path):
    content = _write_with_renderer(monkeypatch, tmp_path, "auto")
    assert "gxApi" not in content


def test_config_wtf_gxapi_d3d8_for_dxvk(monkeypatch, tmp_path):
    content = _write_with_renderer(monkeypatch, tmp_path, "dxvk-d3d8")
    assert 'SET gxApi "d3d8"' in content


def test_config_wtf_gxapi_opengl_for_wined3d(monkeypatch, tmp_path):
    content = _write_with_renderer(monkeypatch, tmp_path, "wined3d-opengl")
    assert 'SET gxApi "opengl"' in content
