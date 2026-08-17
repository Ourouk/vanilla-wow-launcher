"""Unit tests for the first-launch server index fetching."""

import json

from vanilla_wow_launcher.services import server_index


def _fake_text(text):
    def _get(url, timeout=server_index.SERVER_INDEX_TIMEOUT):
        return text

    return _get


def test_fetch_servers_index_parses_valid(monkeypatch):
    index = json.dumps(
        [
            {"id": "a", "name": "Server A", "config_url": "https://x/a.json"},
            {"id": "b", "name": "Server B", "config_url": "https://x/b.json"},
        ]
    )
    monkeypatch.setattr(server_index, "_https_get", _fake_text(index))
    servers = server_index.fetch_servers_index()
    assert [s["id"] for s in servers] == ["a", "b"]
    assert servers[0]["name"] == "Server A"
    assert servers[0]["config_url"] == "https://x/a.json"


def test_fetch_servers_index_drops_invalid_entries(monkeypatch):
    index = json.dumps(
        [
            {"id": "a", "name": "Server A", "config_url": "https://x/a.json"},
            {"name": "NoId", "config_url": "https://x/b.json"},
            {"id": "c", "name": "BadUrl", "config_url": "http://x/c.json"},
            "notadict",
        ]
    )
    monkeypatch.setattr(server_index, "_https_get", _fake_text(index))
    servers = server_index.fetch_servers_index()
    assert [s["id"] for s in servers] == ["a"]


def test_fetch_servers_index_empty_on_network_error(monkeypatch):
    def _boom(url, timeout=server_index.SERVER_INDEX_TIMEOUT):
        raise OSError("nope")

    monkeypatch.setattr(server_index, "_https_get", _boom)
    assert server_index.fetch_servers_index() == []


def test_fetch_servers_index_empty_on_non_list(monkeypatch):
    monkeypatch.setattr(server_index, "_https_get", _fake_text("{}"))
    assert server_index.fetch_servers_index() == []


def test_fetch_server_config_returns_data_and_raw(monkeypatch):
    payload = '{"server": {"base_url": "https://x"}}'
    monkeypatch.setattr(server_index, "_https_get", _fake_text(payload))
    data, raw, err = server_index.fetch_server_config("https://x/c.json")
    assert err == ""
    assert data == {"server": {"base_url": "https://x"}}
    assert raw == payload


def test_fetch_server_config_error_on_failure(monkeypatch):
    def _boom(url, timeout=server_index.SERVER_INDEX_TIMEOUT):
        raise OSError("nope")

    monkeypatch.setattr(server_index, "_https_get", _boom)
    data, raw, err = server_index.fetch_server_config("https://x/c.json")
    assert data is None and raw is None and err
