"""Unit tests for the client update engine (VerifyWorker/UpdateWorker)."""

import json
import os
import queue
import urllib.request

import pytest

import vanilla_wow_launcher.services.update_backend.http_update as client_update
import vanilla_wow_launcher.services.update_backend.torrent_update as td
from vanilla_wow_launcher.services.update_backend.http_update import (
    UpdateWorker,
    VerifyWorker,
)


def _mk_client(tmp_path):
    d = tmp_path / "client"
    d.mkdir()
    return d


def test_verify_worker_up_to_date(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    f = client / "data.bin"
    f.write_bytes(b"x")

    manifest = {
        "root": {
            "files": [
                {
                    "type": "file",
                    "name": "data.bin",
                    "hash": "11F6AD8EC52A2984ABAAFD7C3B516503785C2072",  # sha1("x")
                    "size": 1,
                },
            ]
        }
    }
    fake_resp = type(
        "R",
        (),
        {
            "__enter__": lambda s: s,
            "__exit__": lambda *a: None,
            "read": lambda s: json.dumps(manifest).encode(),
        },
    )
    monkeypatch.setattr(
        client_update, "secure_urlopen", lambda *a, **k: fake_resp()
    )

    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = VerifyWorker(str(client), log_q, prog_q)
    vw.run()

    msgs = [log_q.get_nowait()[0] for _ in range(log_q.qsize())]
    assert "__UP_TO_DATE__" in msgs
    assert "__MANIFEST_AVAILABLE__" in msgs
    assert "__DIFF_TREE__" not in msgs


def test_verify_worker_detects_stale_file(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    (client / "data.bin").write_bytes(b"old")

    manifest = {
        "root": {
            "files": [
                {
                    "type": "file",
                    "name": "data.bin",
                    "hash": "11F6AD8EC52A2984ABAAFD7C3B516503785C2072",  # sha1("x")
                    "size": 1,
                },
            ]
        }
    }
    fake_resp = type(
        "R",
        (),
        {
            "__enter__": lambda s: s,
            "__exit__": lambda *a: None,
            "read": lambda s: json.dumps(manifest).encode(),
        },
    )
    monkeypatch.setattr(
        client_update, "secure_urlopen", lambda *a, **k: fake_resp()
    )

    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = VerifyWorker(str(client), log_q, prog_q)
    vw.run()

    msgs = [log_q.get_nowait()[0] for _ in range(log_q.qsize())]
    assert "__UPDATE_NEEDED__" in msgs
    assert "__MANIFEST_AVAILABLE__" in msgs
    assert "__DIFF_TREE__" in msgs


def test_verify_worker_manifest_failure_marks_unavailable(
    tmp_path, monkeypatch
):
    client = _mk_client(tmp_path)

    def boom(*a, **k):
        raise urllib.error.HTTPError(
            "https://srv.example/m.json", 404, "not found", {}, None
        )

    monkeypatch.setattr(client_update, "secure_urlopen", boom)

    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = VerifyWorker(str(client), log_q, prog_q)
    vw.run()

    msgs = [log_q.get_nowait()[0] for _ in range(log_q.qsize())]
    assert "__MANIFEST_UNAVAILABLE__" in msgs
    assert "__UPDATE_NEEDED__" not in msgs
    assert "__MANIFEST_AVAILABLE__" not in msgs


def test_verify_worker_config_wtf_created_when_missing(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    manifest = {"root": {"files": []}}
    fake_resp = type(
        "R",
        (),
        {
            "__enter__": lambda s: s,
            "__exit__": lambda *a: None,
            "read": lambda s: json.dumps(manifest).encode(),
        },
    )
    monkeypatch.setattr(
        client_update, "secure_urlopen", lambda *a, **k: fake_resp()
    )

    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = VerifyWorker(str(client), log_q, prog_q)
    vw.run()
    assert (client / "WTF" / "Config.wtf").exists()


def test_update_worker_downloads_and_verifies(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    payload = b"hello world"

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            # Return the payload once, then EOF — mirrors a real socket.
            if getattr(self, "_exhausted", False):
                return b""
            self._exhausted = True
            return payload

        def getcode(self):
            return 200

    calls = {"n": 0}

    def fake_urlopen(req, timeout, allowed_hosts=None):
        calls["n"] += 1
        assert req.full_url.endswith("/data.bin")
        return FakeResp()

    monkeypatch.setattr(client_update, "secure_urlopen", fake_urlopen)

    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    import hashlib

    digest = worker.download(
        "https://launcher.test/client/latest/data.bin",
        str(client / "data.bin"),
        len(payload),
    )
    assert digest == hashlib.sha1(payload).hexdigest().upper()
    assert (client / "data.bin").read_bytes() == payload


def test_update_worker_traverse_skips_up_to_date(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    f = client / "data.bin"
    f.write_bytes(b"x")
    node = {
        "type": "file",
        "name": "data.bin",
        "size": 1,
        "hash": "11F6AD8EC52A2984ABAAFD7C3B516503785C2072",
    }

    def fail(*a, **k):
        raise AssertionError(
            "download must not be attempted for a matching file"
        )

    monkeypatch.setattr(client_update, "secure_urlopen", fail)
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    worker.traverse(node, [])
    assert (client / "data.bin").read_bytes() == b"x"


def test_verify_worker_cancelled_torrent_posts_error_not_failure_marker(
    tmp_path, monkeypatch
):
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: client_update.DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)
    monkeypatch.setattr(
        client_update,
        "secure_urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConnectionError("manifest down")
        ),
    )

    class CancelledVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise RuntimeError("Cancelled")

    monkeypatch.setattr(td, "TorrentVerifier", CancelledVerifier)
    worker._cancel = True

    worker.run()

    messages = [msg for msg, _tag in log_q.queue]
    assert "__ERROR__" in messages
    assert "__TORRENT_VERIFY_FAILED__" not in messages


def test_update_worker_uses_verified_torrent_paths_without_manifest(
    tmp_path, monkeypatch
):
    client = _mk_client(tmp_path)
    source = client_update.DownloadSource(
        "https://launcher.test/manifest.json",
        "https://launcher.test/client",
        "https://launcher.test/client.torrent",
    )
    monkeypatch.setattr(client_update, "_download_source", lambda: source)
    monkeypatch.setattr(
        client_update,
        "secure_urlopen",
        lambda *args, **kwargs: pytest.fail("manifest must not be fetched"),
    )
    recovered = []
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        worker,
        "_recovery_download",
        lambda wanted: recovered.append(wanted),
    )

    worker.run(None, {"Data/a.bin"})

    assert recovered == [{"Data/a.bin"}]
    value, label, details = prog_q.get_nowait()
    assert value == 0.02
    assert label == "Downloading via BitTorrent…"
    assert details["phase"] == "BitTorrent"


# ── mirror failover ──────────────────────────────────────────────────────────


def _resp():
    return type(
        "R",
        (),
        {
            "__enter__": lambda s: s,
            "__exit__": lambda *x: False,
            "read": lambda s, n=1: b"{}"[:n],
        },
    )()


def test_download_source_fails_over_to_first_reachable_mirror(monkeypatch):
    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {"name": "A", "base_url": "https://a.example"},
                {"name": "B", "base_url": "https://b.example"},
            ],
        }
    )
    calls = []

    def fake_urlopen(req, timeout, allowed_hosts=None):
        calls.append(req.full_url)
        if req.full_url.startswith("https://a.example"):
            raise ConnectionError("down")
        return _resp()

    monkeypatch.setattr(client_update, "secure_urlopen", fake_urlopen)
    src = client_update._download_source()
    assert (
        src.manifest_url == "https://b.example/api/file/latest/manifest.json"
    )
    assert src.client_url == "https://b.example/client/latest"
    assert calls[0].startswith("https://a.example")
    assert calls[1].startswith("https://b.example")


def test_download_source_falls_back_to_server_when_all_down(monkeypatch):
    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [{"name": "A", "base_url": "https://a.example"}],
        }
    )

    def boom(req, timeout, allowed_hosts=None):
        raise ConnectionError("down")

    monkeypatch.setattr(client_update, "secure_urlopen", boom)
    src = client_update._download_source()
    assert (
        src.manifest_url == "https://srv.example/api/file/latest/manifest.json"
    )
    assert src.client_url == "https://srv.example/client/latest"


def test_download_source_none_without_launcher(monkeypatch):
    from vanilla_wow_launcher.core import launcher

    launcher.reset()
    assert client_update._download_source() is None


def test_download_source_uses_mirror_endpoint_overrides(monkeypatch):
    """A mirror's custom manifest/client URLs are what the updater uses, not
    the reconstructed defaults."""
    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {
                    "name": "CDN",
                    "base_url": "https://m1.example",
                    "manifest_url": "https://m1.example/custom/manifest.json",
                    "client_url": "https://dl.example/client/latest",
                }
            ],
        }
    )

    monkeypatch.setattr(
        client_update,
        "secure_urlopen",
        lambda req, timeout=5, allowed_hosts=None: _resp(),
    )
    src = client_update._download_source()
    assert src.manifest_url == "https://m1.example/custom/manifest.json"
    assert src.client_url == "https://dl.example/client/latest"


def test_download_source_probes_client_url_and_accepts_http_error(monkeypatch):
    """A CDN-only mirror is selected by probing its client-files endpoint, and
    an HTTP error status (e.g. 404 on the root path) still proves the host is
    reachable."""
    from urllib.error import HTTPError

    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {
                    "name": "CDN",
                    "base_url": "https://m1.example",
                    "client_url": "https://dl.example/client/latest",
                }
            ],
        }
    )
    probed = []

    def http_error(req, timeout=5, allowed_hosts=None):
        probed.append(req.full_url)
        raise HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(client_update, "secure_urlopen", http_error)
    src = client_update._download_source()
    assert probed == [
        "https://m1.example/api/file/latest/manifest.json",
        "https://dl.example/client/latest",
    ]
    assert src.client_url == "https://dl.example/client/latest"
    assert (
        src.manifest_url == "https://m1.example/api/file/latest/manifest.json"
    )


def test_verify_uses_selected_manifest_url(monkeypatch, tmp_path):
    """VerifyWorker must fetch the manifest from the selected source's
    configured manifest URL."""
    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {
                    "name": "CDN",
                    "base_url": "https://m1.example",
                    "manifest_url": "https://m1.example/custom/manifest.json",
                    "client_url": "https://dl.example/client/latest",
                }
            ],
        }
    )
    fetched = []

    def fake_urlopen(req, timeout, allowed_hosts=None):
        fetched.append(req.full_url)
        return _resp()

    monkeypatch.setattr(client_update, "secure_urlopen", fake_urlopen)
    monkeypatch.setattr(client_update, "load_cache", lambda: {})
    monkeypatch.setattr(client_update, "save_cache", lambda c: None)
    monkeypatch.setattr(client_update, "write_config_wtf", lambda d: None)

    client = tmp_path / "client"
    client.mkdir()
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = client_update.VerifyWorker(str(client), log_q, prog_q)
    worker.run()
    assert any(
        u.startswith("https://m1.example/custom/manifest.json")
        for u in fetched
    )


def test_traverse_downloads_from_mirror_client_url(monkeypatch, tmp_path):
    """File downloads must come from the selected mirror's client_url."""
    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {
                    "name": "CDN",
                    "base_url": "https://m1.example",
                    "client_url": "https://dl.example/client/latest",
                }
            ],
        }
    )

    monkeypatch.setattr(
        client_update,
        "secure_urlopen",
        lambda req, timeout=5, allowed_hosts=None: _resp(),
    )

    client = tmp_path / "client"
    client.mkdir()
    recorded = []

    class _RecordingWorker(client_update.UpdateWorker):
        def download(self, url, dest, size, name=""):
            recorded.append(url)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "wb").close()
            return "A" * 40

    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = _RecordingWorker(str(client), log_q, prog_q)
    worker.traverse(
        {"type": "file", "name": "data.bin", "size": 1, "hash": "A" * 40}, []
    )
    assert recorded == ["https://dl.example/client/latest/data.bin"]
