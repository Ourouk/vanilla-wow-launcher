"""Unit tests for first-run launcher config validation, persistence, the
Games/<ServerName> default folder, and the cli first-launch remote flow."""

import json
import os

import vanilla_wow_launcher.core.config_store as config_store
import vanilla_wow_launcher.core.launcher as launcher
import vanilla_wow_launcher.core.platform_support as platform_support
from vanilla_wow_launcher import cli
from vanilla_wow_launcher.services import server_index as server_index_module

# ── launcher.validate_dict ──────────────────────────────────────────────


def test_validate_dict_accepts_valid():
    config, err = launcher.validate_dict(
        {"server": {"base_url": "https://launcher.test"}}
    )
    assert err == ""
    assert config is not None
    assert config.server_url == "https://launcher.test"


def test_validate_dict_rejects_missing_base_url():
    config, err = launcher.validate_dict({"server": {}})
    assert config is None
    assert err


# ── launcher.persist_text ───────────────────────────────────────────────


def test_persist_text_writes_valid_config(tmp_path, monkeypatch):
    dest = tmp_path / "vanilla_wow_launcher.json"
    monkeypatch.setattr(launcher, "user_config_path", lambda: str(dest))
    text = json.dumps({"server": {"base_url": "https://launcher.test"}})
    out, err = launcher.persist_text(text)
    assert err == ""
    assert out == str(dest)
    assert (
        json.loads(dest.read_text(encoding="utf-8"))["server"]["base_url"]
        == "https://launcher.test"
    )


def test_persist_text_rejects_invalid(tmp_path, monkeypatch):
    dest = tmp_path / "vanilla_wow_launcher.json"
    monkeypatch.setattr(launcher, "user_config_path", lambda: str(dest))
    out, err = launcher.persist_text("not json")
    assert out == ""
    assert err


# ── platform_support Games folder ───────────────────────────────────────


def test_games_dir_is_under_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    assert platform_support.games_dir() == os.path.join(
        "/home/tester", "Games"
    )


def test_server_games_dir_sanitizes(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    assert platform_support.server_games_dir("OctoWoW") == os.path.join(
        "/home/tester", "Games", "OctoWoW"
    )
    # illegal path characters are stripped
    assert platform_support.server_games_dir('a/b:c*?"<>|') == os.path.join(
        "/home/tester", "Games", "abc"
    )
    # a blank name falls back to VanillaWoW
    assert platform_support.server_games_dir("   ") == os.path.join(
        "/home/tester", "Games", "VanillaWoW"
    )


# ── cli first-run ────────────────────────────────────────────────────────


def test_ensure_default_game_folder_sets_games_subfolder(
    tmp_path, monkeypatch
):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    monkeypatch.setattr(launcher, "server_name", lambda: "OctoWoW")
    monkeypatch.setattr(
        platform_support,
        "server_games_dir",
        lambda name: os.path.join(str(tmp_path), "Games", name),
    )
    cli._ensure_default_game_folder()
    assert config_store.load_config()["out_dir"] == os.path.join(
        str(tmp_path), "Games", "OctoWoW"
    )


def test_first_launch_remote_persists_and_sets_folder(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    monkeypatch.setattr(
        launcher, "user_config_path", lambda: str(tmp_path / "cfg.json")
    )
    monkeypatch.setattr(cli, "_run_backend", lambda: 0)
    monkeypatch.setattr(
        cli,
        "_pick_launcher_config",
        lambda: {
            "kind": "remote",
            "config_url": "https://example.invalid/octowow.json",
            "name": "OctoWoW",
        },
    )
    captured = {}

    def fake_fetch(url):
        captured["url"] = url
        return (
            {"server": {"base_url": "https://launcher.test"}},
            '{"server": {"base_url": "https://launcher.test"}}',
            "",
        )

    monkeypatch.setattr(server_index_module, "fetch_server_config", fake_fetch)
    monkeypatch.setattr(launcher, "server_name", lambda: "OctoWoW")
    monkeypatch.setattr(
        platform_support,
        "server_games_dir",
        lambda name: os.path.join(str(tmp_path), "Games", name),
    )
    try:
        rc = cli._first_launch()
    finally:
        launcher.reset()
    assert rc == 0
    assert captured["url"] == "https://example.invalid/octowow.json"
    assert config_store.load_config()["out_dir"] == os.path.join(
        str(tmp_path), "Games", "OctoWoW"
    )
    assert (
        json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))[
            "server"
        ]["base_url"]
        == "https://launcher.test"
    )
