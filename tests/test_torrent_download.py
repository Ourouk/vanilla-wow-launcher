"""Tests for the BitTorrent download backend (services/torrent_download) and
its wiring into the client update engine.

libtorrent is never required here: a fake `lt` module is injected into
sys.modules and the availability probe / TorrentDownloader are monkeypatched.
"""

import hashlib
import importlib.util
import os
import queue
import sys
import urllib.error
from types import SimpleNamespace

import pytest

import vanilla_wow_launcher.services.update_backend.http_update as client_update
import vanilla_wow_launcher.services.update_backend.torrent_update as td
from vanilla_wow_launcher.core import launcher
from vanilla_wow_launcher.services.update_backend.http_update import (
    DownloadSource,
    UpdateWorker,
)

SHA1_X = "11F6AD8EC52A2984ABAAFD7C3B516503785C2072"


def _mk_client(tmp_path):
    d = tmp_path / "client"
    d.mkdir()
    return d


def _resp(content: bytes):
    class Response:
        def __init__(self, content):
            self.content = content
            self.pos = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if self.pos >= len(self.content):
                return b""
            if n < 0:
                result = self.content[self.pos :]
                self.pos = len(self.content)
            else:
                result = self.content[self.pos : self.pos + n]
                self.pos += len(result)
            return result

    return Response(content)


# ── availability probe ───────────────────────────────────────────────────────


def test_available_true_when_find_spec_finds_libtorrent(monkeypatch):
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "libtorrent" else None,
    )
    # Also need to mock the actual import since available() now tries to import
    import sys

    class FakeLT:
        def session(self):
            return object()

        add_torrent_params = object
        torrent_info = object
        torrent_status = object

        class alert:
            class category_t:
                error_notification = 1

    monkeypatch.setitem(sys.modules, "libtorrent", FakeLT())
    assert td.available() is True


def test_available_false_when_find_spec_misses(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    # Mock libtorrent import to fail

    def _fail_import(name, *args, **kwargs):
        if name == "libtorrent":
            raise ImportError("No module named 'libtorrent'")
        return __import__(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fail_import)
    assert td.available() is False


def test_available_false_on_probe_error(monkeypatch):
    def boom(name):
        raise ValueError("nope")

    monkeypatch.setattr(importlib.util, "find_spec", boom)

    def _fail_import(name, *args, **kwargs):
        if name == "libtorrent":
            raise ImportError("No module named 'libtorrent'")
        return __import__(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fail_import)
    assert td.available() is False


# ── launcher config parsing ──────────────────────────────────────────────────


def test_config_parses_server_and_mirror_torrent_urls():
    launcher.configure_from_dict(
        {
            "server": {
                "base_url": "https://srv.example",
                "torrent_url": "https://dl.example/client/client.torrent",
            },
            "mirrors": [
                {
                    "name": "A",
                    "base_url": "https://a.example",
                    "torrent_url": "https://a.example/t/client.torrent",
                },
                {"name": "B", "base_url": "https://b.example"},
            ],
        }
    )
    cfg = launcher.config()
    assert cfg.torrent_url == "https://dl.example/client/client.torrent"
    assert cfg.mirrors[0].torrent_url == "https://a.example/t/client.torrent"
    assert cfg.mirrors[1].torrent_url is None


def test_config_rejects_non_https_torrent_url():
    launcher.configure_from_dict(
        {
            "server": {
                "base_url": "https://srv.example",
                "torrent_url": "http://insecure.example/client.torrent",
            },
            "mirrors": [
                {
                    "name": "A",
                    "base_url": "https://a.example",
                    "torrent_url": "not a url",
                }
            ],
        }
    )
    cfg = launcher.config()
    assert cfg.torrent_url is None
    assert cfg.mirrors[0].torrent_url is None


def test_torrent_hosts_join_download_allowlist():
    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {
                    "name": "A",
                    "base_url": "https://a.example",
                    "torrent_url": "https://torrent.example/client.torrent",
                }
            ],
        }
    )
    hosts = launcher.config().download_hosts()
    assert "torrent.example" in hosts


# ── DownloadSource propagation ───────────────────────────────────────────────


def test_download_source_uses_mirror_torrent_url(monkeypatch):
    launcher.configure_from_dict(
        {
            "server": {"base_url": "https://srv.example"},
            "mirrors": [
                {
                    "name": "A",
                    "base_url": "https://a.example",
                    "torrent_url": "https://a.example/client.torrent",
                }
            ],
        }
    )
    monkeypatch.setattr(
        client_update,
        "secure_urlopen",
        lambda req, timeout=5, allowed_hosts=None: _resp(b"{}"),
    )
    src = client_update._download_source()
    assert src.torrent_url == "https://a.example/client.torrent"


def test_download_source_falls_back_to_server_torrent_url(monkeypatch):
    launcher.configure_from_dict(
        {
            "server": {
                "base_url": "https://srv.example",
                "torrent_url": "https://dl.example/client.torrent",
            },
            "mirrors": [{"name": "A", "base_url": "https://a.example"}],
        }
    )

    def down(req, timeout=5, allowed_hosts=None):
        raise ConnectionError("down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)
    src = client_update._download_source()
    assert src.torrent_url == "https://dl.example/client.torrent"


# ── TorrentDownloader unit tests (fake libtorrent) ──────────────────────────


def _make_fake_lt(finished_after=3):
    class FakeStatus:
        def __init__(self, finished=False, pieces_done=0):
            self.name = "client"
            self.total_wanted = 10
            self.total_wanted_done = 10 if finished else 0
            self.download_rate = 0
            self.num_peers = 0
            self.is_finished = finished
            # libtorrent 2.1 fields for verification
            self.verified_pieces = pieces_done
            self.checking_files = not finished

    class FakeHandle:
        def __init__(self):
            self.cancelled = False
            self.paused = False
            self.status_calls = 0

        def status(self):
            self.status_calls += 1
            return FakeStatus(
                self.status_calls >= finished_after,
                pieces_done=self.status_calls,
            )

        def cancel(self):
            self.cancelled = True

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

    class FakeFiles:
        def __init__(self):
            self.paths = [
                "client/Data/a.bin",
                "client/Data/b.mpq",
                "client/WoW.exe",
            ]
            self.sizes = [1024, 2048, 4096]

        def num_files(self):
            return len(self.paths)

        def file_path(self, i):
            return self.paths[i]

        def file_offset(self, i):
            return sum(self.sizes[:i])

        def file_size(self, i):
            return self.sizes[i]

    class FakeTorrentInfo:
        def files(self):
            return FakeFiles()

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

    return FakeLT()


def _install_fake_lt(monkeypatch, **kwargs):
    fake = _make_fake_lt(**kwargs)
    monkeypatch.setitem(sys.modules, "libtorrent", fake)
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )
    return fake


def test_download_completes_and_sets_file_priorities(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    fake = _install_fake_lt(monkeypatch)
    log_q, prog_q = queue.Queue(), queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, prog_q)
    result = d.download("https://srv.example/client.torrent", {"Data/a.bin"})

    assert result == []
    ses = fake.last_session
    assert ses.atp.save_path == str(client)
    # Only the stale file is wanted; the shared leading dir is stripped.
    assert ses.atp.file_priorities == [7, 0, 0]
    assert len(ses.removed) == 1  # never seeded — removed after completion


def test_download_whole_torrent_when_wanted_none(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    fake = _install_fake_lt(monkeypatch)
    log_q, prog_q = queue.Queue(), queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, prog_q)
    d.download("https://srv.example/client.torrent", None)

    ses = fake.last_session
    assert ses.atp.save_path == str(client)
    # wanted=None → every file at max priority (recovery download).
    assert ses.atp.file_priorities == [7, 7, 7]


def test_download_fetches_torrent_over_allowlisted_https(
    monkeypatch, tmp_path
):
    client = _mk_client(tmp_path)
    _install_fake_lt(monkeypatch)
    seen = {}

    def fake_urlopen(req, timeout=10, allowed_hosts=None):
        seen["url"] = req.full_url
        seen["hosts"] = allowed_hosts
        return _resp(b"fake")

    monkeypatch.setattr(td, "secure_urlopen", fake_urlopen)
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    d.download("https://torrent.example/client.torrent", {"Data/a.bin"})
    assert seen["url"] == "https://torrent.example/client.torrent"
    assert seen["hosts"] == set()


def test_download_cancelled_raises(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    fake = _install_fake_lt(monkeypatch, finished_after=10**9)
    log_q, prog_q = queue.Queue(), queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, prog_q)
    d._cancel = True
    with pytest.raises(RuntimeError, match="Cancelled"):
        d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert len(fake.last_session.removed) == 1


def test_download_stall_raises(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    _install_fake_lt(monkeypatch, finished_after=10**9)
    monkeypatch.setattr(td, "STALL_TIMEOUT", -1)
    monkeypatch.setattr(td, "DISCOVERY_TIMEOUT", -1)
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    with pytest.raises(td.TorrentStalledError, match="Stalled"):
        d.download("https://srv.example/client.torrent", {"Data/a.bin"})


def test_download_stall_does_not_fire_within_discovery_timeout(
    tmp_path, monkeypatch
):
    """Stall timer uses DISCOVERY_TIMEOUT (180s) before first byte transfer.
    With a long DISCOVERY_TIMEOUT the download completes normally."""
    client = _mk_client(tmp_path)
    _install_fake_lt(monkeypatch, finished_after=3)
    monkeypatch.setattr(td, "DISCOVERY_TIMEOUT", 9999)
    monkeypatch.setattr(td, "STALL_TIMEOUT", -1)
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    result = d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert result == []


def test_download_stall_resets_on_peer_connection(tmp_path, monkeypatch):
    """Stall timer resets when peers connect — the session is alive."""
    client = _mk_client(tmp_path)

    class FakeStatus:
        def __init__(self):
            self.name = "client"
            self.total_wanted = 10
            self.total_wanted_done = 0
            self.download_rate = 0
            self.num_peers = 0
            self.is_finished = False

    class FakeHandle:
        def __init__(self):
            self.status_calls = 0

        def status(self):
            self.status_calls += 1
            s = FakeStatus()
            # Peers appear on 3rd call — stall timer should reset
            if self.status_calls >= 3:
                s.num_peers = 5
            return s

        def cancel(self):
            pass

        def pause(self):
            pass

        def resume(self):
            self.paused = False

    class FakeFiles:
        def __init__(self):
            self.paths = ["client/Data/a.bin", "client/WoW.exe"]
            self.sizes = [1024, 4096]

        def num_files(self):
            return len(self.paths)

        def file_path(self, i):
            return self.paths[i]

        def file_offset(self, i):
            return sum(self.sizes[:i])

        def file_size(self, i):
            return self.sizes[i]

    class FakeTorrentInfo:
        def files(self):
            return FakeFiles()

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

    fake = FakeLT()
    monkeypatch.setitem(sys.modules, "libtorrent", fake)
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )
    # Very short stall timeout — but peers appear before it fires
    monkeypatch.setattr(td, "STALL_TIMEOUT", 0.1)
    monkeypatch.setattr(td, "DISCOVERY_TIMEOUT", 0.1)
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    # Should raise RuntimeError (Cancelled) not TorrentStalledError
    # because the fake never finishes — peers reset the timer.
    with pytest.raises(RuntimeError, match="Cancelled"):
        d._cancel = True
        d.download("https://srv.example/client.torrent", {"Data/a.bin"})


def test_download_does_not_treat_read_piece_alert_as_error(
    tmp_path, monkeypatch
):
    """read_piece_alert fires on explicit read_piece() — it is NOT a disk
    error and must not raise TorrentDiskError."""

    client = _mk_client(tmp_path)

    class FakeAlert:
        def __init__(self):
            self.name = "client"
            self.total_wanted = 10
            self.total_wanted_done = 10
            self.download_rate = 0
            self.num_peers = 0
            self.is_finished = True

        def category(self):
            return 8  # storage_notification

        def __class__(self):
            pass

    class FakeReadPieceAlert:
        """Mimics libtorrent.read_piece_alert — in the storage category."""

        def __init__(self):
            pass

        def category(self):
            return 8  # storage_notification

        def message(self):
            return "read piece 0"

    class FakeHandle:
        def __init__(self):
            self.paused = False
            self.removed = False

        def status(self):
            return FakeAlert()

        def cancel(self):
            pass

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

    class FakeFiles:
        def __init__(self):
            self.paths = ["client/Data/a.bin", "client/WoW.exe"]
            self.sizes = [1024, 4096]

        def num_files(self):
            return len(self.paths)

        def file_path(self, i):
            return self.paths[i]

        def file_offset(self, i):
            return sum(self.sizes[:i])

        def file_size(self, i):
            return self.sizes[i]

    class FakeTorrentInfo:
        def files(self):
            return FakeFiles()

    _alert_iter = iter([FakeReadPieceAlert()])

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            try:
                return [next(_alert_iter)]
            except StopIteration:
                return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

    fake = FakeLT()
    monkeypatch.setitem(sys.modules, "libtorrent", fake)
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    # The read_piece_alert fires on first poll but download is already
    # finished — must NOT raise TorrentDiskError.
    result = d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert result == []


def test_download_session_has_dht_bootstrap_nodes(tmp_path, monkeypatch):
    """Download session is configured with DHT bootstrap nodes for fast
    peer discovery."""
    client = _mk_client(tmp_path)
    fake = _install_fake_lt(monkeypatch)
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    ses = fake.last_session
    assert "dht_bootstrap_nodes" in ses.settings
    assert "router.libtorrent.org" in ses.settings["dht_bootstrap_nodes"]
    assert "router.bittorrent.com" in ses.settings["dht_bootstrap_nodes"]


def test_alert_mask_uses_libtorrent_21_category_values():
    assert td.ALERT_MASK == 1 | 8 | 16 | 64 | 1024


def test_download_session_uses_correct_listen_interfaces(
    tmp_path, monkeypatch
):
    """Download session binds to 0.0.0.0:0 (no IPv6)."""
    client = _mk_client(tmp_path)
    fake = _install_fake_lt(monkeypatch)
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    ses = fake.last_session
    assert ses.settings["listen_interfaces"] == "0.0.0.0:0"


def test_detect_torrent_root_maps_paths():
    """_detect_torrent_root auto-detects the root from WoW.exe and maps paths."""

    class FakeFiles:
        def num_files(self):
            return 3

        def file_path(self, i):
            return [
                "client/Data/a.bin",
                "client/Data/b.mpq",
                "client/WoW.exe",
            ][i]

    root, mapping = td._detect_torrent_root(FakeFiles())
    assert root == "client"
    assert mapping == {
        "client/Data/a.bin": "Data/a.bin",
        "client/Data/b.mpq": "Data/b.mpq",
        "client/WoW.exe": "WoW.exe",
    }


def test_detect_torrent_root_flat_torrent():
    """WoW.exe at the root level → empty root, no stripping."""

    class FakeFiles:
        def num_files(self):
            return 2

        def file_path(self, i):
            return ["a.bin", "WoW.exe"][i]

    root, mapping = td._detect_torrent_root(FakeFiles())
    assert root == ""
    assert mapping == {"a.bin": "a.bin", "WoW.exe": "WoW.exe"}


def test_detect_torrent_root_deep_nesting():
    """WoW.exe in a nested directory."""

    class FakeFiles:
        def num_files(self):
            return 2

        def file_path(self, i):
            return ["a/b/c/WoW.exe", "a/b/c/Data/x.bin"][i]

    root, mapping = td._detect_torrent_root(FakeFiles())
    assert root == "a/b/c"
    assert mapping == {
        "a/b/c/WoW.exe": "WoW.exe",
        "a/b/c/Data/x.bin": "Data/x.bin",
    }


def test_detect_torrent_root_missing_exe():
    """No WoW.exe → TorrentLayoutError."""

    class FakeFiles:
        def num_files(self):
            return 1

        def file_path(self, i):
            return "Data/a.bin"

    with pytest.raises(td.TorrentLayoutError, match="no WoW.exe"):
        td._detect_torrent_root(FakeFiles())


def test_detect_torrent_root_duplicate_exe():
    """Multiple WoW.exe entries → TorrentLayoutError."""

    class FakeFiles:
        def num_files(self):
            return 3

        def file_path(self, i):
            return ["client/WoW.exe", "other/WoW.exe", "Data/a.bin"][i]

    with pytest.raises(td.TorrentLayoutError, match="multiple WoW.exe"):
        td._detect_torrent_root(FakeFiles())


def test_detect_torrent_root_file_outside_root():
    """A file not under the WoW.exe parent → TorrentLayoutError."""

    class FakeFiles:
        def num_files(self):
            return 3

        def file_path(self, i):
            return ["client/WoW.exe", "client/Data/a.bin", "other/b.bin"][i]

    with pytest.raises(
        td.TorrentLayoutError, match="outside detected torrent root"
    ):
        td._detect_torrent_root(FakeFiles())


def test_detect_torrent_root_path_traversal():
    """Path with .. inside a valid root → TorrentLayoutError."""

    class FakeFiles:
        def num_files(self):
            return 2

        def file_path(self, i):
            return ["client/WoW.exe", "client/../../../etc/passwd"][i]

    with pytest.raises(td.TorrentLayoutError, match="Path traversal"):
        td._detect_torrent_root(FakeFiles())


def test_map_torrent_paths_returns_only_mapping():
    """_map_torrent_paths returns the {torrent: local} dict."""

    class FakeFiles:
        def num_files(self):
            return 2

        def file_path(self, i):
            return ["client/WoW.exe", "client/Data/a.bin"][i]

    mapping = td._map_torrent_paths(FakeFiles())
    assert mapping == {
        "client/WoW.exe": "WoW.exe",
        "client/Data/a.bin": "Data/a.bin",
    }


# ── UpdateWorker wiring ──────────────────────────────────────────────────────


def _recording_downloader(monkeypatch):
    calls = []

    def fake_download(self, torrent_url, wanted):
        calls.append((torrent_url, wanted))
        return []

    monkeypatch.setattr(td.TorrentDownloader, "download", fake_download)
    return calls


def test_torrent_download_collects_stale_files(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    (client / "Data").mkdir()
    (client / "Data" / "ok.bin").write_bytes(b"x")
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)
    calls = _recording_downloader(monkeypatch)
    worker._source = DownloadSource(
        "https://srv/manifest.json",
        "https://srv/client",
        "https://srv/client.torrent",
    )

    nodes = [
        {
            "type": "dir",
            "name": "Data",
            "files": [
                {"type": "file", "name": "ok.bin", "hash": SHA1_X, "size": 1},
                {
                    "type": "file",
                    "name": "stale.bin",
                    "hash": "A" * 40,
                    "size": 9,
                },
            ],
        },
        {"type": "mpq", "name": "Patch", "hash": "B" * 40, "size": 9},
        {"type": "del", "name": "old.bin"},
    ]
    assert worker._torrent_download(nodes) is True
    assert calls == [
        (
            "https://srv/client.torrent",
            {"Data/stale.bin", "Patch.mpq"},
        )
    ]
    assert "[torrent]" in log_q.queue[0][0]


def test_torrent_download_skipped_without_torrent_url(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    worker = UpdateWorker(str(client), queue.Queue(), queue.Queue())
    calls = _recording_downloader(monkeypatch)
    worker._source = DownloadSource(
        "https://srv/manifest.json", "https://srv/client"
    )
    worker._torrent_download(
        [{"type": "file", "name": "a.bin", "hash": "A" * 40, "size": 1}]
    )
    assert calls == []


def test_torrent_download_skipped_without_libtorrent(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    worker = UpdateWorker(str(client), queue.Queue(), queue.Queue())
    calls = _recording_downloader(monkeypatch)
    monkeypatch.setattr(client_update, "_torrent_available", lambda: False)
    worker._source = DownloadSource(
        "https://srv/manifest.json",
        "https://srv/client",
        "https://srv/client.torrent",
    )
    worker._torrent_download(
        [{"type": "file", "name": "a.bin", "hash": "A" * 40, "size": 1}]
    )
    assert calls == []


def test_torrent_download_falls_back_on_failure(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)

    def boom(self, torrent_url, wanted):
        raise RuntimeError("swarm dead")

    monkeypatch.setattr(td.TorrentDownloader, "download", boom)
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    worker._source = DownloadSource(
        "https://srv/manifest.json",
        "https://srv/client",
        "https://srv/client.torrent",
    )
    nodes = [{"type": "file", "name": "a.bin", "hash": "A" * 40, "size": 1}]
    assert worker._torrent_download(nodes) is False
    msgs = [log_q.get_nowait()[0] for _ in range(log_q.qsize())]
    assert any("Falling back to HTTP" in m for m in msgs)


def test_run_invokes_torrent_then_skips_covered_files(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    (client / "data.bin").write_bytes(b"x")
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "load_cache", lambda: {})
    monkeypatch.setattr(client_update, "save_cache", lambda c: None)
    torrent_calls = []

    def fake_torrent_download(nodes):
        torrent_calls.append(nodes)
        return True

    monkeypatch.setattr(worker, "_torrent_download", fake_torrent_download)

    nodes = [
        {
            "type": "file",
            "name": "data.bin",
            "hash": SHA1_X,
            "size": 1,
        }
    ]
    worker.run(nodes)
    assert torrent_calls == [nodes]
    msgs = [m[0] for m in log_q.queue]
    assert "__DONE__" in msgs
    assert "__ERROR__" not in msgs


# ── manifest-less recovery ───────────────────────────────────────────────────


def test_run_recovers_full_torrent_when_manifest_down(tmp_path, monkeypatch):
    """Manifest fetch fails but the source advertises a torrent → the whole
    torrent (wanted=None) is downloaded and the recovery marker posted."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)
    monkeypatch.setattr(client_update, "load_cache", lambda: {})
    monkeypatch.setattr(client_update, "save_cache", lambda c: None)
    calls = []

    def fake_download(self, url, wanted):
        calls.append((url, wanted))
        (client / "WoW.exe").write_bytes(b"recovered-client")
        return []

    monkeypatch.setattr(td.TorrentDownloader, "download", fake_download)

    def down(*a, **k):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    worker.run()

    assert calls == [("https://srv/client.torrent", None)]
    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_RECOVERY_DONE__" in msgs
    assert "__MANIFEST_AVAILABLE__" not in msgs
    assert "__ERROR__" not in msgs
    assert any("Manifest unavailable" in m for m in msgs)
    # A fresh recovery install seeds a missing Config.wtf.
    assert (client / "WTF" / "Config.wtf").exists()


def test_recovery_fails_when_snapshot_lacks_wanted_file(tmp_path, monkeypatch):
    """A replaced torrent that omits a previously-stale path (which still
    exists locally) must fail recovery — never mark the client ready or
    cache a clean verdict for it."""
    client = _mk_client(tmp_path)
    (client / "Data").mkdir(parents=True)
    (client / "Data" / "old.bin").write_bytes(b"stale-from-old-version")
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "load_cache", lambda: {})
    saved = {}
    monkeypatch.setattr(client_update, "save_cache", lambda c: saved.update(c))

    def fake_download(self, url, wanted):
        raise td.TorrentSnapshotMismatchError(
            "Torrent replaced — wanted file(s) not in the new snapshot"
        )

    monkeypatch.setattr(td.TorrentDownloader, "download", fake_download)

    worker.run(None, {"Data/old.bin"})

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_VERIFY_FAILED__" in msgs
    assert "__TORRENT_RECOVERY_DONE__" not in msgs
    assert "__ERROR__" not in msgs
    assert client_update.TORRENT_VALIDATION_CACHE_KEY not in saved


def test_run_errors_when_manifest_down_without_torrent(tmp_path, monkeypatch):
    """Manifest fetch fails and no torrent is advertised → hard error, no
    recovery."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json", "https://srv/client"
        ),
    )
    monkeypatch.setattr(client_update, "load_cache", lambda: {})
    monkeypatch.setattr(client_update, "save_cache", lambda c: None)
    calls = _recording_downloader(monkeypatch)

    def down(*a, **k):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    worker.run()

    assert calls == []
    msgs = [m[0] for m in log_q.queue]
    assert "__ERROR__" in msgs
    assert "__TORRENT_RECOVERY_DONE__" not in msgs


def test_run_errors_when_manifest_down_without_libtorrent(
    tmp_path, monkeypatch
):
    """Manifest fetch fails, a torrent is advertised, but libtorrent is
    missing → no recovery, hard error."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = UpdateWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: False)
    monkeypatch.setattr(client_update, "load_cache", lambda: {})
    calls = _recording_downloader(monkeypatch)

    def down(*a, **k):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    worker.run()

    assert calls == []
    msgs = [m[0] for m in log_q.queue]
    assert "__ERROR__" in msgs
    assert "__TORRENT_RECOVERY_DONE__" not in msgs


# ── TorrentVerifier (manifest-less verify against the snapshot) ──────────────


def _verifier_fake_lt(stale_file: int | None, piece_count: int = 3):
    """A libtorrent fake tailored to TorrentVerifier. Two torrent files:
    ``client/Data/a.bin`` (pieces 0..1) and ``client/WoW.exe`` (piece 2).
    ``stale_file`` (if not None) is a piece index ``have_piece`` reports as
    missing after the recheck."""

    class FakeFiles:
        def __init__(self):
            self.paths = ["client/Data/a.bin", "client/WoW.exe"]
            self.sizes = [512, 256]

        def num_files(self):
            return len(self.paths)

        def file_path(self, i):
            return self.paths[i]

        def file_offset(self, i):
            return sum(self.sizes[:i])

        def file_size(self, i):
            return self.sizes[i]

    class FakeTorrentInfo:
        def files(self):
            return FakeFiles()

        def piece_length(self):
            return 256

        def num_pieces(self):
            return piece_count

    class FakeStatus:
        verified_pieces = [True] * piece_count
        state = "finished"
        progress = 1.0
        num_pieces = piece_count

    class FakeHandle:
        def __init__(self):
            self.force_rechecked = False
            self.cancelled = False

        def force_recheck(self):
            self.force_rechecked = True

        def cancel(self):
            self.cancelled = True

        def status(self):
            return FakeStatus()

        def have_piece(self, i):
            return i != stale_file

        def pause(self):
            pass

        def resume(self):
            self.paused = False

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

    return FakeLT()


def _install_verifier_fake(monkeypatch, **kwargs):
    fake = _verifier_fake_lt(**kwargs)
    monkeypatch.setitem(sys.modules, "libtorrent", fake)
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )
    return fake


def test_verifier_returns_stale_files(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    fake = _install_verifier_fake(monkeypatch, stale_file=1)
    v = td.TorrentVerifier(str(client), queue.Queue(), queue.Queue())
    stale = v.verify("https://srv.example/client.torrent")

    assert stale == ["Data/a.bin"]
    # Verification needs every piece wanted (priority 7) so force_recheck()
    # actually hashes the on-disk files — the offline session guarantees no
    # download or peer activity.
    assert fake.last_session.atp.file_priorities == [7, 7]
    assert fake.last_session.atp.save_path == str(client)
    assert fake.last_session.settings["listen_interfaces"] == ""
    assert len(fake.last_session.removed) == 1


def test_verifier_session_does_not_listen(tmp_path, monkeypatch):
    """Verification is read-only and offline: no listen socket, no P2P."""
    client = _mk_client(tmp_path)
    fake = _install_verifier_fake(monkeypatch, stale_file=None)
    v = td.TorrentVerifier(str(client), queue.Queue(), queue.Queue())
    v.verify("https://srv.example/client.torrent")

    settings = fake.last_session.settings
    assert settings["listen_interfaces"] == ""
    assert settings["enable_dht"] is False
    assert settings["enable_lsd"] is False
    assert settings["enable_upnp"] is False
    assert settings["enable_natpmp"] is False


def test_verifier_sets_max_priorities_for_recheck(tmp_path, monkeypatch):
    """Regression guard: the verifier must mark every file wanted (priority
    7), not 0. libtorrent skips priority-0 pieces during force_recheck(),
    so a 0-priority verify would never hash anything and stall at 0/N
    pieces. The offline session keeps this read-only (no download)."""
    client = _mk_client(tmp_path)
    fake = _install_verifier_fake(monkeypatch, stale_file=None)
    v = td.TorrentVerifier(str(client), queue.Queue(), queue.Queue())
    v.verify("https://srv.example/client.torrent")
    assert fake.last_session.atp.file_priorities == [7, 7]


def test_verifier_up_to_date_returns_empty(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    _install_verifier_fake(monkeypatch, stale_file=None)
    v = td.TorrentVerifier(str(client), queue.Queue(), queue.Queue())
    assert v.verify("https://srv.example/client.torrent") == []


def test_verifier_cancelled_raises(tmp_path, monkeypatch):
    client = _mk_client(tmp_path)
    fake = _install_verifier_fake(monkeypatch, stale_file=None)
    v = td.TorrentVerifier(str(client), queue.Queue(), queue.Queue())
    v._cancel = True
    with pytest.raises(RuntimeError, match="Cancelled"):
        v.verify("https://srv.example/client.torrent")
    assert fake.last_session.removed[0].cancelled is True


def test_verify_worker_uses_torrent_when_manifest_down(tmp_path, monkeypatch):
    """Manifest fetch fails but a torrent is advertised and libtorrent is
    present → VerifyWorker verifies against the torrent and posts the stale
    file marker instead of a blind manifest-unavailable."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    verifier_calls = []

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            self.out_dir = out_dir

        def verify(self, url):
            verifier_calls.append(url)
            return ["Data/a.bin", "Patch.mpq"]

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    assert verifier_calls == ["https://srv/client.torrent"]
    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_DIFF__" in msgs
    assert "__MANIFEST_UNAVAILABLE__" not in msgs
    assert "__DIFF_TREE__" not in msgs


def test_verify_worker_torrent_up_to_date(tmp_path, monkeypatch):
    """Manifest down but the torrent verify finds nothing stale → the
    up-to-date marker is posted, not manifest-unavailable."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            return []

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_UP_TO_DATE__" in msgs
    assert "__MANIFEST_UNAVAILABLE__" not in msgs


def test_verify_worker_rechecks_even_when_torrent_unchanged(
    tmp_path, monkeypatch
):
    """Explicit verification never trusts a cached verdict: even when the
    snapshot at the URL is unchanged since the last verify (same content hash),
    the libtorrent recheck still runs and the fresh verdict supersedes the
    cached stale list. The cached record only seeds snapshot identity/resume
    cleanup, not the reported files."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    cached = {
        client_update.TORRENT_VALIDATION_CACHE_KEY: {
            "content_hash": "abc123",
            "info_hash": "ih1",
            "url": "https://srv/client.torrent",
            "out_dir": os.path.abspath(str(client)),
            "stale": ["Data/old.bin"],
        }
    }
    saved = {}
    vw._cache = dict(cached)
    monkeypatch.setattr(client_update, "save_cache", lambda c: saved.update(c))

    snapshot = SimpleNamespace(
        url="https://srv/client.torrent",
        content_hash="abc123",
        info_hash="ih1",
        torrent_bytes=b"",
        torrent_info=None,
    )
    monkeypatch.setattr(td, "_fetch_torrent", lambda url, log: snapshot)

    verifier_calls = []

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            verifier_calls.append(url)
            return ["Data/other.bin"]

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    assert verifier_calls == ["https://srv/client.torrent"]
    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_DIFF__" in msgs
    assert "__TORRENT_UP_TO_DATE__" not in msgs
    assert saved[client_update.TORRENT_VALIDATION_CACHE_KEY]["stale"] == [
        "Data/other.bin"
    ]


def test_verify_worker_runs_recheck_when_snapshot_changed(
    tmp_path, monkeypatch
):
    """A different content hash at the same URL invalidates the cached verdict
    and the full libtorrent recheck runs again."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    cached = {
        client_update.TORRENT_VALIDATION_CACHE_KEY: {
            "content_hash": "oldhash",
            "info_hash": "ih1",
            "url": "https://srv/client.torrent",
            "out_dir": os.path.abspath(str(client)),
            "stale": ["Data/old.bin"],
        }
    }
    vw._cache = dict(cached)

    snapshot = SimpleNamespace(
        url="https://srv/client.torrent",
        content_hash="newhash",
        info_hash="ih2",
        torrent_bytes=b"",
        torrent_info=None,
    )
    monkeypatch.setattr(td, "_fetch_torrent", lambda url, log: snapshot)

    verifier_calls = []

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            verifier_calls.append(url)
            return ["Data/other.bin"]

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    assert verifier_calls == ["https://srv/client.torrent"]
    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_DIFF__" in msgs


def test_verify_worker_torrent_failure_falls_back(tmp_path, monkeypatch):
    """Manifest down, torrent verify raises a non-fetch error (e.g. libtorrent
    recheck failure) → the verify-failed marker is posted so the UI doesn't
    offer a dead recovery download."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise RuntimeError("swarm dead")

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_VERIFY_FAILED__" in msgs
    assert "__MANIFEST_UNAVAILABLE__" not in msgs
    assert "__TORRENT_DIFF__" not in msgs


def test_verify_worker_torrent_unreachable_posts_marker(tmp_path, monkeypatch):
    """Manifest down and the .torrent can't be fetched (HTTP error) → the
    unreachable marker is posted so the UI stops offering a dead recovery
    download."""
    from vanilla_wow_launcher.services.update_backend.torrent_update import (
        TorrentFetchError,
    )

    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise TorrentFetchError("HTTP Error 404: Not Found")

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_UNREACHABLE__" in msgs
    assert "__MANIFEST_UNAVAILABLE__" not in msgs
    assert "__TORRENT_DIFF__" not in msgs


def test_verify_worker_torrent_reachable_posts_marker(tmp_path, monkeypatch):
    """A successful torrent verify posts the reachable marker alongside the
    stale-file diff."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            return ["Data/a.bin"]

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_REACHABLE__" in msgs
    assert "__TORRENT_DIFF__" in msgs
    assert "__TORRENT_UNREACHABLE__" not in msgs


def test_fetch_torrent_wraps_http_error(tmp_path, monkeypatch):
    """_fetch_torrent wraps HTTP errors in TorrentFetchError."""
    from vanilla_wow_launcher.services.update_backend.torrent_update import (
        TorrentFetchError,
    )

    def failing_urlopen(req, timeout=10, allowed_hosts=None):
        raise urllib.error.HTTPError(
            "https://srv/client.torrent", 404, "Not Found", None, None
        )

    monkeypatch.setattr(td, "secure_urlopen", failing_urlopen)
    with pytest.raises(TorrentFetchError, match="404"):
        td._fetch_torrent("https://srv/client.torrent", lambda m, t="": None)


def test_fetch_torrent_wraps_runtime_error(tmp_path, monkeypatch):
    """_fetch_torrent wraps RuntimeError (allowlist rejection) in
    TorrentFetchError."""
    from vanilla_wow_launcher.services.update_backend.torrent_update import (
        TorrentFetchError,
    )

    def failing_urlopen(req, timeout=10, allowed_hosts=None):
        raise RuntimeError("Refusing download from unexpected host: evil.com")

    monkeypatch.setattr(td, "secure_urlopen", failing_urlopen)
    with pytest.raises(TorrentFetchError, match="unexpected host"):
        td._fetch_torrent(
            "https://evil.com/client.torrent", lambda m, t="": None
        )


# ── Typed exception unit tests ───────────────────────────────────────────────


def test_torrent_corrupt_error_on_malformed_torrent(tmp_path, monkeypatch):
    """A downloaded .torrent that can't be parsed → TorrentCorruptError."""
    import io

    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda *a, **k: io.BytesIO(b"not a real torrent file"),
    )
    monkeypatch.setattr(
        "libtorrent.torrent_info",
        lambda *_: (_ for _ in ()).throw(RuntimeError("invalid torrent")),
    )

    with pytest.raises(
        td.TorrentCorruptError, match="Failed to parse torrent"
    ):
        td._fetch_torrent("https://srv/client.torrent", lambda m, t="": None)


def test_torrent_stalled_error_includes_peers():
    """TorrentStalledError carries the peer count."""
    e = td.TorrentStalledError(peers=3)
    assert e.peers == 3
    assert "3 peers" in str(e)


def test_torrent_session_error_on_session_fail(tmp_path, monkeypatch):
    """lt.session() failure → TorrentSessionError."""
    from vanilla_wow_launcher.services.update_backend.torrent_update import (
        TorrentSessionError,
    )

    def _bad_session():
        raise RuntimeError("address already in use")

    q = queue.Queue()
    v = td.TorrentVerifier(str(tmp_path), q, q)

    class FakeTI:
        def files(self):
            return self

        def num_files(self):
            return 0

        def piece_length(self):
            return 1

        def num_pieces(self):
            return 0

    monkeypatch.setattr(v, "_session", _bad_session)
    monkeypatch.setattr(
        td,
        "_fetch_torrent",
        lambda url, log: td.TorrentSnapshot(
            url=url,
            content_hash="c",
            info_hash=None,
            torrent_bytes=b"",
            torrent_info=FakeTI(),
        ),
    )

    with pytest.raises(TorrentSessionError, match="session"):
        v.verify("https://srv/client.torrent")


def test_torrent_disk_error_on_errno(tmp_path):
    """OSError with errno 28 → TorrentDiskError."""
    from vanilla_wow_launcher.services.update_backend.torrent_update import (
        TorrentDiskError,
    )

    assert td.TorrentDiskError("disk full")
    assert issubclass(TorrentDiskError, RuntimeError)


# ── VerifyWorker wiring for typed exceptions ────────────────────────────────


def test_verify_worker_torrent_corrupt_posts_marker(tmp_path, monkeypatch):
    """Malformed .torrent → __TORRENT_CORRUPT__ marker with detail."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise td.TorrentCorruptError("not a valid torrent")

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_CORRUPT__" in msgs
    assert "__TORRENT_UNREACHABLE__" not in msgs
    assert "__TORRENT_VERIFY_FAILED__" not in msgs


def test_verify_worker_torrent_stalled_posts_marker(tmp_path, monkeypatch):
    """Verification stalled → __TORRENT_STALLED__ marker with peer count."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise td.TorrentStalledError(peers=0)

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_STALLED__" in msgs
    assert "__TORRENT_UNREACHABLE__" not in msgs


def test_verify_worker_torrent_session_error_posts_marker(
    tmp_path, monkeypatch
):
    """Session creation failure → __TORRENT_SESSION_ERROR__ marker."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise td.TorrentSessionError("address in use")

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_SESSION_ERROR__" in msgs
    assert "__TORRENT_UNREACHABLE__" not in msgs


def test_verify_worker_torrent_disk_error_posts_marker(tmp_path, monkeypatch):
    """Disk I/O error → __TORRENT_DISK_ERROR__ marker."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise td.TorrentDiskError("No space left on device")

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    msgs = [m[0] for m in log_q.queue]
    assert "__TORRENT_DISK_ERROR__" in msgs
    assert "__TORRENT_UNREACHABLE__" not in msgs


def test_verify_worker_error_detail_in_tag(tmp_path, monkeypatch):
    """Error detail is passed in the second element of log queue tuples."""
    client = _mk_client(tmp_path)
    log_q, prog_q = queue.Queue(), queue.Queue()
    vw = client_update.VerifyWorker(str(client), log_q, prog_q)
    monkeypatch.setattr(
        client_update,
        "_download_source",
        lambda: DownloadSource(
            "https://srv/manifest.json",
            "https://srv/client",
            "https://srv/client.torrent",
        ),
    )
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)

    def down(req, timeout=10, allowed_hosts=None):
        raise ConnectionError("manifest down")

    monkeypatch.setattr(client_update, "secure_urlopen", down)

    class FakeVerifier:
        def __init__(self, out_dir, log_q, prog_q):
            pass

        def verify(self, url):
            raise td.TorrentCorruptError("truncated data")

    monkeypatch.setattr(td, "TorrentVerifier", FakeVerifier)
    vw.run()

    detail = [m[1] for m in log_q.queue if m[0] == "__TORRENT_CORRUPT__"]
    assert detail and "truncated data" in detail[0]


# ── torrent identity + resume data (snapshot-aware backend) ────────────────


def _make_snapshot_fake_lt(
    info_hash="aa" * 20, resume_atp=None, save_alert=False
):
    """A libtorrent fake exposing info_hashes plus the resume-data APIs."""

    class _InfoHashes:
        def __init__(self):
            self.v1 = info_hash
            self.v2 = ""

    class FakeFiles:
        def __init__(self):
            self.paths = ["client/Data/a.bin", "client/WoW.exe"]
            self.sizes = [1024, 4096]

        def num_files(self):
            return len(self.paths)

        def file_path(self, i):
            return self.paths[i]

        def file_offset(self, i):
            return sum(self.sizes[:i])

        def file_size(self, i):
            return self.sizes[i]

    class FakeTorrentInfo:
        def info_hashes(self):
            return _InfoHashes()

        def files(self):
            return FakeFiles()

        def piece_length(self):
            return 256

        def num_pieces(self):
            return 3

    class FakeStatus:
        def __init__(self, finished=False):
            self.name = "client"
            self.total_wanted = 10
            self.total_wanted_done = 10 if finished else 0
            self.download_rate = 0
            self.num_peers = 0
            self.is_finished = finished

    class FakeHandle:
        def __init__(self):
            self.status_calls = 0
            self.resume_requested = False

        def status(self):
            self.status_calls += 1
            return FakeStatus(self.status_calls >= 3)

        def cancel(self):
            pass

        def pause(self):
            pass

        def resume(self):
            self.paused = False

        def save_resume_data(self):
            self.resume_requested = True

    class save_resume_data_alert:
        def __init__(self):
            self.params = SimpleNamespace()

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []
            self._alert = save_resume_data_alert() if save_alert else None
            self._pops = 0

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            # The download pump drains alerts first; the save-resume alert is
            # only produced after _save_resume() requests it.
            self._pops += 1
            if self._alert is not None and self._pops >= 4:
                self._alert = None
                return [save_resume_data_alert()]
            return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None
            self.resume_atp = resume_atp

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

        def read_resume_data(self, buf):
            if self.resume_atp is None:
                raise ValueError("no resume data")
            return self.resume_atp

        def write_resume_data_buf(self, params):
            return b"resume-bytes"

    return FakeLT()


def _install_snapshot_fake(
    monkeypatch, info_hash="aa" * 20, resume_atp=None, save_alert=False
):
    fake = _make_snapshot_fake_lt(info_hash, resume_atp, save_alert)
    monkeypatch.setitem(sys.modules, "libtorrent", fake)
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )
    return fake


def test_fetch_torrent_computes_identity_and_persists(monkeypatch, tmp_path):
    """_fetch_torrent returns a snapshot with content hash + info hash and
    persists the raw torrent bytes under the cache keyed by info hash."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(td, "cache_dir", lambda: str(cache_root))
    _install_snapshot_fake(monkeypatch, info_hash="ab" * 20)
    snap = td._fetch_torrent(
        "https://srv.example/client.torrent", lambda m, t="": None
    )
    assert snap.content_hash == hashlib.sha256(b"fake").hexdigest()
    assert snap.info_hash == "ab" * 20
    assert (
        cache_root / "torrents" / f"{'ab' * 20}.torrent"
    ).read_bytes() == b"fake"


def test_fetch_torrent_skips_persistence_without_info_hash(
    monkeypatch, tmp_path
):
    """A torrent whose binding exposes no info hash is still usable — just
    not persisted by identity."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(td, "cache_dir", lambda: str(cache_root))
    _install_fake_lt(monkeypatch)  # no info_hashes() on the fake
    snap = td._fetch_torrent(
        "https://srv.example/client.torrent", lambda m, t="": None
    )
    assert snap.info_hash is None
    assert snap.content_hash
    torrents_dir = cache_root / "torrents"
    assert not torrents_dir.exists() or not list(torrents_dir.glob("*"))


def test_downloader_loads_matching_resume_data(tmp_path, monkeypatch):
    """Resume data whose info hash matches the snapshot is merged into the
    add_torrent_params before the torrent is added."""
    client = _mk_client(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(td, "cache_dir", lambda: str(cache_root))
    info_hash = "aa" * 20
    resume_atp = SimpleNamespace(
        info_hashes=SimpleNamespace(v1=info_hash, v2=""),
        have_pieces=[True, False, True],
    )
    fake = _install_snapshot_fake(
        monkeypatch, info_hash=info_hash, resume_atp=resume_atp
    )
    td.write_resume_bytes(info_hash, b"resume")
    log_q = queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, queue.Queue())
    d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert fake.last_session.atp.have_pieces == [True, False, True]
    assert any("Resuming from cached resume data" in m[0] for m in log_q.queue)


def test_downloader_resume_does_not_override_selection(tmp_path, monkeypatch):
    """A cached resume's file_priorities never override the priorities
    computed for the current wanted set; compatible piece state still merges."""
    client = _mk_client(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(td, "cache_dir", lambda: str(cache_root))
    info_hash = "aa" * 20
    resume_atp = SimpleNamespace(
        info_hashes=SimpleNamespace(v1=info_hash, v2=""),
        have_pieces=[True, False],
        file_priorities=[0, 7],
    )
    fake = _install_snapshot_fake(
        monkeypatch,
        info_hash=info_hash,
        resume_atp=resume_atp,
        save_alert=True,
    )
    td.write_resume_bytes(info_hash, b"resume")
    d = td.TorrentDownloader(str(client), queue.Queue(), queue.Queue())
    d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert fake.last_session.atp.file_priorities == [7, 0]
    assert fake.last_session.atp.have_pieces == [True, False]


def test_downloader_discards_mismatched_resume_data(tmp_path, monkeypatch):
    """Resume data for a different info hash is removed and never merged."""
    client = _mk_client(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(td, "cache_dir", lambda: str(cache_root))
    info_hash = "aa" * 20
    resume_atp = SimpleNamespace(
        info_hashes=SimpleNamespace(v1="ff" * 20, v2=""),
        have_pieces=[True],
    )
    fake = _install_snapshot_fake(
        monkeypatch, info_hash=info_hash, resume_atp=resume_atp
    )
    td.write_resume_bytes(info_hash, b"resume")
    log_q = queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, queue.Queue())
    d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert not os.path.exists(td.resume_path(info_hash))
    assert not hasattr(fake.last_session.atp, "have_pieces")
    assert any("info hash mismatch" in m[0] for m in log_q.queue)


def test_downloader_saves_resume_data_on_completion(tmp_path, monkeypatch):
    """On a finished download the resume buffer is written before the handle
    is removed, keyed by the snapshot's info hash."""
    client = _mk_client(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(td, "cache_dir", lambda: str(cache_root))
    info_hash = "aa" * 20
    fake = _install_snapshot_fake(
        monkeypatch, info_hash=info_hash, save_alert=True
    )
    log_q = queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, queue.Queue())
    d.download("https://srv.example/client.torrent", {"Data/a.bin"})
    assert (
        cache_root / "torrents" / f"{info_hash}.resume"
    ).read_bytes() == b"resume-bytes"
    assert fake.last_session.removed  # handle removed, never seeded
    assert any("Resume data saved" in m[0] for m in log_q.queue)


def test_priorities_raises_for_absent_wanted_files(tmp_path, monkeypatch):
    """A wanted path missing from the snapshot (torrent replaced between
    verify and update) is a hard mismatch, not a silent skip."""
    client = _mk_client(tmp_path)
    _install_fake_lt(monkeypatch)
    log_q = queue.Queue()
    d = td.TorrentDownloader(str(client), log_q, queue.Queue())
    with pytest.raises(td.TorrentSnapshotMismatchError):
        d.download("https://srv.example/client.torrent", {"Data/nope.bin"})
    assert any("absent from this snapshot" in m[0] for m in log_q.queue)


def test_verifier_progress_reports_piece_counts(tmp_path, monkeypatch):
    """Verification progress carries verified_pieces/total_pieces, never the
    byte-style downloaded/total fields."""
    client = _mk_client(tmp_path)

    class FakeFiles:
        def num_files(self):
            return 2

        def file_path(self, i):
            return ["client/Data/a.bin", "client/WoW.exe"][i]

        def file_offset(self, i):
            return 0

        def file_size(self, i):
            return 512

    class FakeTorrentInfo:
        def files(self):
            return FakeFiles()

        def piece_length(self):
            return 256

        def num_pieces(self):
            return 4

    class FakeStatus:
        def __init__(self, num_pieces, state):
            # verified_pieces left as the real-bug shape (a populated list) is
            # fine here; the verifier must NOT read it for progress. The live
            # verified count comes from the "have" bitfield (num_pieces).
            self.verified_pieces = [True] * 4
            self.num_pieces = num_pieces
            self.progress = num_pieces / 4
            self.state = state
            self.num_peers = 0

    class FakeHandle:
        def __init__(self):
            self.calls = 0

        def force_recheck(self):
            pass

        def cancel(self):
            pass

        def pause(self):
            pass

        def resume(self):
            self.paused = False

        def status(self):
            self.calls += 1
            total = 4
            done = min(self.calls, total)
            # Checking for the first three polls, then finished.
            state = "checking_files" if self.calls < total else "finished"
            return FakeStatus(done, state)

        def have_piece(self, i):
            return True

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "libtorrent", FakeLT())
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )

    prog_q = queue.Queue()
    v = td.TorrentVerifier(str(client), queue.Queue(), prog_q)
    assert v.verify("https://srv.example/client.torrent") == []
    updates = [
        it[2]
        for it in list(prog_q.queue)
        if len(it) == 3 and it[2].get("phase") == "Verifying"
    ]
    assert updates
    assert all("verified_pieces" in d for d in updates)
    assert all("total_pieces" in d for d in updates)
    assert all("downloaded" not in d for d in updates)
    # Progress must advance off zero (regression: verified_pieces bitfield
    # stays empty during an offline recheck, so the numerator must come from
    # status.progress, not verified_pieces).
    nums = [d["verified_pieces"] for d in updates]
    assert max(nums) == 3
    assert min(nums) >= 1


def test_verifier_does_not_stall_when_verified_pieces_unpopulated(
    tmp_path, monkeypatch
):
    """Regression: with libtorrent 2.1.1.0 the verified_pieces bitfield is not
    populated during an offline force_recheck() (it is only set in seed mode),
    so a verifier keyed on it would hang at 0/N pieces forever. The verifier
    must derive its numerator from the live "have" bitfield (num_pieces) and
    finish once the recheck leaves the checking states."""
    client = _mk_client(tmp_path)

    class FakeFiles:
        def num_files(self):
            return 2

        def file_path(self, i):
            return ["client/Data/a.bin", "client/WoW.exe"][i]

        def file_offset(self, i):
            return 0

        def file_size(self, i):
            return 512

    class FakeTorrentInfo:
        def files(self):
            return FakeFiles()

        def piece_length(self):
            return 256

        def num_pieces(self):
            return 4

    class FakeStatus:
        def __init__(self, num_pieces, state):
            # The real-bug shape: verified_pieces is empty/unpopulated (it is
            # only set in seed mode, which the verifier is not). The live
            # verified count must come from the "have" bitfield (num_pieces).
            self.verified_pieces = []
            self.num_pieces = num_pieces
            self.progress = num_pieces / 4
            self.state = state
            self.num_peers = 0

    class FakeHandle:
        def __init__(self):
            self.calls = 0
            self.force_rechecked = False

        def force_recheck(self):
            self.force_rechecked = True

        def cancel(self):
            pass

        def pause(self):
            pass

        def resume(self):
            self.paused = False

        def status(self):
            self.calls += 1
            total = 4
            done = min(self.calls, total)
            state = "checking_files" if self.calls < total else "finished"
            return FakeStatus(done, state)

        def have_piece(self, i):
            return True

    class FakeSession:
        def __init__(self, settings):
            self.settings = settings
            self.atp = None
            self.removed = []

        def add_torrent(self, atp):
            self.atp = atp
            return FakeHandle()

        def pop_alerts(self):
            return []

        def wait_for_alert(self, ms):
            return None

        def remove_torrent(self, h):
            self.removed.append(h)

    class FakeLT:
        class alert:
            class category_t:
                error_notification = 1
                storage_notification = 8
                status_notification = 16

        class torrent_status:
            class states:
                checking_files = "checking_files"
                checking_resume_data = "checking_resume_data"
                queued_for_checking = "queued_for_checking"
                downloading = "downloading"
                finished = "finished"

        def __init__(self):
            self.last_session = None

        def torrent_info(self, path):
            return FakeTorrentInfo()

        def session(self, settings):
            self.last_session = FakeSession(settings)
            return self.last_session

        def add_torrent_params(self):
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "libtorrent", FakeLT())
    monkeypatch.setattr(td, "allowed_download_hosts", lambda: set())
    monkeypatch.setattr(
        td,
        "secure_urlopen",
        lambda req, timeout=10, allowed_hosts=None: _resp(b"fake"),
    )

    prog_q = queue.Queue()
    v = td.TorrentVerifier(str(client), queue.Queue(), prog_q)
    stale = v.verify("https://srv.example/client.torrent")
    assert stale == []
    updates = [
        it[2]
        for it in list(prog_q.queue)
        if len(it) == 3 and it[2].get("phase") == "Verifying"
    ]
    assert updates
    nums = [d["verified_pieces"] for d in updates]
    assert max(nums) >= 3
    assert min(nums) >= 1
