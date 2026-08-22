"""Dedicated tests for services/self_update (daily GitHub release check)."""

import vanilla_wow_launcher.core.config_store as config_store
import vanilla_wow_launcher.services.self_update as self_update


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._buf = _Bytes(payload)

    def __enter__(self):
        return self._buf

    def __exit__(self, *exc):
        return False


class _Bytes:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


def _configure(tmp_path):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})


def _fake_urlopen(calls: list, payload: dict):
    def fake(req, timeout=10, **kwargs):
        calls.append(req)
        import json

        return _FakeResponse(json.dumps(payload).encode())

    return fake


def test_fetch_parses_tag_and_caches(tmp_path, monkeypatch):
    _configure(tmp_path)
    calls = []

    monkeypatch.setattr(
        self_update,
        "secure_urlopen",
        _fake_urlopen(calls, {"tag_name": "v9.9.9"}),
    )

    assert self_update.fetch_updater_latest_tag() == "v9.9.9"
    assert len(calls) == 1
    # Within the TTL the cached tag is served without touching the network.
    assert self_update.fetch_updater_latest_tag() == "v9.9.9"
    assert len(calls) == 1


def test_force_bypasses_cache(tmp_path, monkeypatch):
    _configure(tmp_path)
    calls = []
    payload = {"tag_name": "v1.0.0"}

    def fake(req, timeout=10, **kwargs):
        calls.append(req)
        import json

        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(self_update, "secure_urlopen", fake)

    assert self_update.fetch_updater_latest_tag() == "v1.0.0"
    payload["tag_name"] = "v2.0.0"
    assert self_update.fetch_updater_latest_tag(force=True) == "v2.0.0"
    assert len(calls) == 2


def test_fetch_error_returns_none(tmp_path, monkeypatch):
    _configure(tmp_path)

    def fail(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(self_update, "secure_urlopen", fail)
    assert self_update.fetch_updater_latest_tag() is None


def test_update_available_compares_against_current_version():
    # Strictly newer → available; equal/older/empty → not.
    assert self_update.updater_update_available("v99.0.0")
    assert not self_update.updater_update_available("v0.0.1")
    current = self_update.UPDATER_VERSION
    assert not self_update.updater_update_available(current)
    assert self_update.updater_update_available(current + ".1")
