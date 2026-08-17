"""Real-files end-to-end torrent verification.

These tests exercise the *real* :class:`TorrentVerifier` (and the real
``VerifyWorker`` orchestration) against the actual World of Warcraft client
checked out under ``context/client`` and the real octowow snapshot at
``context/wow-client.torrent``.

They are fully offline:

* the verifier session has no peers/trackers (empty ``listen_interfaces``,
  DHT/LSD/UPnP/NAT-PMP off), so ``force_recheck()`` only hashes the on-disk
  files;
* the ``.torrent`` is loaded from disk, never fetched over the network.

The whole module is skipped unless ``RUN_E2E=1`` is set *and* both real
artifacts are present, so CI never runs it (the ``context/`` tree is also
git-ignored). This is what makes the 0/9012 "stuck at verifying" regression
provable on real data instead of synthetic unit fakes.

Note: ``context/client`` is a *lightly* modded client — it ships a few loader
mods (VanillaFixes/transmogfix/…) but those mod files are themselves part of the
``wow-client.torrent`` snapshot, and the vanilla files are byte-identical to it.
So against the real snapshot the verifier must report the client **up to date**
(empty stale set). The e2e asserts exactly that for the real client, and uses an
empty target for the stale/recovery/validation paths (where every file is
genuinely missing).

The original 0/9012 "stuck at verifying" regression was a *path* bug: libtorrent
mapped the torrent's ``client/`` root onto ``out_dir`` and read at
``out_dir/client/...`` (a double prefix), so every real file looked missing. The
verifier now strips the root so files resolve under ``out_dir`` directly; the
real-client test both proves the stall is gone (progress advances and completes)
and that the reference client verifies clean.
"""

import hashlib
import io
import json
import os
import queue
import shutil
import tempfile
from pathlib import Path

import pytest

import vanilla_wow_launcher.services.update_backend.http_update as client_update
import vanilla_wow_launcher.services.update_backend.torrent_update as torrent_update
from vanilla_wow_launcher.services.update_backend.http_update import (
    DownloadSource,
    VerifyWorker,
)
from vanilla_wow_launcher.services.update_backend.torrent_update import (
    TorrentSnapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = REPO_ROOT / "context" / "client"
TORRENT_FILE = REPO_ROOT / "context" / "wow-client.torrent"

_E2E_ENABLED = (
    os.environ.get("RUN_E2E") == "1"
    and CLIENT_DIR.is_dir()
    and TORRENT_FILE.is_file()
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _E2E_ENABLED,
        reason="e2e only: set RUN_E2E=1 and provide context/client "
        "+ context/wow-client.torrent",
    ),
]


def _messages(log_q: queue.Queue) -> list[str]:
    return [message for message, _tag in list(log_q.queue)]


def _local_snapshot(
    torrent_url: str,
    log,
    content_hash: str | None = None,
    info_hash: str | None = None,
) -> TorrentSnapshot:
    """Build a real ``TorrentSnapshot`` from the on-disk octowow ``.torrent``.

    ``content_hash``/``info_hash`` may be overridden to simulate a snapshot
    that changed identity at the same URL (the replacement test)."""
    import libtorrent as lt

    data = TORRENT_FILE.read_bytes()
    ch = content_hash or hashlib.sha256(data).hexdigest()
    fd, tmp = tempfile.mkstemp(suffix=".torrent")
    try:
        os.close(fd)
        with open(tmp, "wb") as f:
            f.write(data)
        ti = lt.torrent_info(tmp)
    finally:
        os.remove(tmp)
    ih = info_hash or torrent_update._info_hash_hex(ti)
    return TorrentSnapshot(
        url=torrent_url,
        content_hash=ch,
        info_hash=ih,
        torrent_bytes=data,
        torrent_info=ti,
    )


def _patch_fetch(monkeypatch, content_hash=None, info_hash=None):
    """Replace the network torrent fetch with the local ``.torrent``.

    Returns the list of snapshots actually produced, so a test can assert the
    recorded identity without recomputing it."""
    captured = []

    def fake(url, log):
        snap = _local_snapshot(
            url, log, content_hash=content_hash, info_hash=info_hash
        )
        captured.append(snap)
        return snap

    monkeypatch.setattr(torrent_update, "_fetch_torrent", fake)
    return captured


def _patch_source(
    monkeypatch, torrent_url="file://context/wow-client.torrent"
):
    source = DownloadSource(
        "https://server.test/manifest.json",
        "https://server.test/client",
        torrent_url,
    )
    monkeypatch.setattr(client_update, "_download_source", lambda: source)
    monkeypatch.setattr(client_update, "_torrent_available", lambda: True)


def _patch_cache(monkeypatch):
    """Redirect the validation cache to an in-memory store so the real
    per-user cache is never touched."""

    store: dict = {}

    def load_cache():
        return dict(store)

    def save_cache(cache):
        store.clear()
        store.update(cache)

    monkeypatch.setattr(client_update, "load_cache", load_cache)
    monkeypatch.setattr(client_update, "save_cache", save_cache)
    return store


def _patch_nomutate(monkeypatch):
    """Prevent ``VerifyWorker`` from writing ``WTF/Config.wtf`` into the real
    reference client (it is read-only for the e2e run)."""
    monkeypatch.setattr(
        client_update, "write_config_wtf", lambda *a, **k: None
    )


def _manifest_unavailable(monkeypatch):
    monkeypatch.setattr(
        client_update,
        "secure_urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            ConnectionError("manifest unavailable")
        ),
    )


def test_e2e_verifier_rechecks_real_client_and_reports_up_to_date(
    tmp_path, monkeypatch
):
    """End-to-end proof for the 0/9012 regression and the double-prefix path
    bug: a real ``force_recheck()`` of the multi-GB client must (a) advance past
    0 pieces and finish — no stall guard trip — and (b) report the reference
    client as **up to date** (empty stale set), because the vanilla files match
    the snapshot and the mod files are part of the snapshot too.

    This is the test that actually exercises the root-stripping fix: before it,
    libtorrent read at ``out_dir/client/...`` and reported every file stale."""
    _patch_source(monkeypatch)
    _patch_fetch(monkeypatch)

    import libtorrent as lt

    log_q, prog_q = queue.Queue(), queue.Queue()
    verifier = torrent_update.TorrentVerifier(str(CLIENT_DIR), log_q, prog_q)
    stale = verifier.verify("file://context/wow-client.torrent")

    assert isinstance(stale, list)
    assert "__TORRENT_STALLED__" not in _messages(log_q)

    # Progress must have advanced past 0 and the recheck completed.
    max_pieces = 0
    for item in list(prog_q.queue):
        details = item[2] if len(item) == 3 else {}
        total = details.get("total_pieces")
        verified = details.get("verified_pieces")
        if total:
            max_pieces = max(max_pieces, verified or 0)
    assert max_pieces > 0, "recheck never progressed past 0 pieces"

    ti = lt.torrent_info(str(TORRENT_FILE))
    n_files = ti.files().num_files()

    # Diagnostics: surface what the real recheck decided.
    print(
        f"[e2e] real client: torrent_files={n_files} stale={len(stale)} "
        f"max_pieces={max_pieces}/{ti.num_pieces()}",
        file=__import__("sys").stderr,
    )
    if stale:
        print(f"[e2e] stale files: {stale}", file=__import__("sys").stderr)

    # The reference client is byte-identical to the snapshot for every one of
    # the torrent's files (mods included) — so the verdict is up to date.
    assert stale == [], (
        f"reference client should verify up to date, got {len(stale)} stale: "
        f"{stale}"
    )


def test_e2e_verifier_reports_all_files_stale_for_missing_client(
    tmp_path, monkeypatch
):
    """Against an empty target the verifier must report every mapped file as
    stale. Read-only and needs no copy of the multi-GB client."""
    _patch_source(monkeypatch)
    _patch_fetch(monkeypatch)

    import libtorrent as lt

    empty = tmp_path / "empty"
    empty.mkdir()
    log_q, prog_q = queue.Queue(), queue.Queue()
    verifier = torrent_update.TorrentVerifier(str(empty), log_q, prog_q)
    stale = verifier.verify("file://context/wow-client.torrent")

    ti = lt.torrent_info(str(TORRENT_FILE))
    assert len(stale) == ti.files().num_files()
    assert "WoW.exe" in stale


def test_e2e_recovery_path_reaches_torrent_verdict_when_manifest_unavailable(
    tmp_path, monkeypatch
):
    """With no manifest, ``VerifyWorker`` falls back to the torrent snapshot and
    the real verifier produces a verdict (here: all files stale, since the
    target is empty) without tripping the stall guard."""
    _patch_source(monkeypatch)
    _patch_fetch(monkeypatch)
    _patch_cache(monkeypatch)
    _patch_nomutate(monkeypatch)
    _manifest_unavailable(monkeypatch)

    empty = tmp_path / "empty"
    empty.mkdir()
    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = VerifyWorker(str(empty), log_q, prog_q)
    worker.run()
    assert "__TORRENT_REACHABLE__" in _messages(log_q)
    assert "__TORRENT_DIFF__" in _messages(log_q)
    assert "__TORRENT_STALLED__" not in _messages(log_q)


def test_e2e_manifest_verify_reports_up_to_date_for_real_client(
    tmp_path, monkeypatch
):
    """Build a manifest from a few *real* client files (copied to a scratch
    dir, so the reference client is untouched) and confirm the manifest verify
    path reports up to date *without* invoking the torrent verifier."""

    relatives = ["WoW.exe", "d3d9.dll"]
    scratch = tmp_path / "client"
    present = []
    for rel in relatives:
        src = CLIENT_DIR / rel
        if not src.is_file():
            continue
        dst = scratch / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        present.append(rel)

    manifest = {
        "root": {
            "files": [
                {
                    "type": "file",
                    "name": rel,
                    "size": (scratch / rel).stat().st_size,
                    "hash": hashlib.sha1((scratch / rel).read_bytes())
                    .hexdigest()
                    .upper(),
                }
                for rel in present
            ]
        }
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *x):
            return False

        def read(self, n=-1):
            return io.BytesIO(json.dumps(manifest).encode()).read(n)

    _patch_source(monkeypatch)
    _patch_cache(monkeypatch)
    monkeypatch.setattr(
        client_update, "secure_urlopen", lambda *a, **k: _Resp()
    )

    torrent_calls = {"n": 0}
    real_verify = torrent_update.TorrentVerifier.verify

    def counting(self, url):
        torrent_calls["n"] += 1
        return real_verify(self, url)

    monkeypatch.setattr(torrent_update.TorrentVerifier, "verify", counting)

    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = VerifyWorker(str(scratch), log_q, prog_q)
    worker.run()

    assert "__UP_TO_DATE__" in _messages(log_q)
    assert torrent_calls["n"] == 0


def test_e2e_validation_rechecks_every_run_and_tracks_identity(
    tmp_path, monkeypatch
):
    """Explicit verification never trusts a cached verdict: every run fetches
    the snapshot and rechecks, and the cache records the real snapshot
    identity. Uses an empty target so the recheck is instant."""
    _patch_source(monkeypatch)
    captured = _patch_fetch(monkeypatch)
    store = _patch_cache(monkeypatch)
    _patch_nomutate(monkeypatch)
    _manifest_unavailable(monkeypatch)

    empty = tmp_path / "empty"
    empty.mkdir()

    calls = {"n": 0}
    real_verify = torrent_update.TorrentVerifier.verify

    def counting(self, url):
        calls["n"] += 1
        return real_verify(self, url)

    monkeypatch.setattr(torrent_update.TorrentVerifier, "verify", counting)

    log_q, prog_q = queue.Queue(), queue.Queue()
    worker = VerifyWorker(str(empty), log_q, prog_q)
    worker.run()
    worker.run()

    # No verdict shortcut — the recheck ran both times.
    assert calls["n"] == 2
    assert not any("Skipping recheck" in m for m in _messages(log_q))
    # Identity is persisted alongside the verdict (the real info hash).
    rec = store.get(client_update.TORRENT_VALIDATION_CACHE_KEY)
    assert rec is not None
    assert rec["info_hash"] == captured[0].info_hash
    assert rec["content_hash"] == captured[0].content_hash


def test_e2e_torrent_replacement_invalidates_verdict(tmp_path, monkeypatch):
    """A different snapshot served at the same URL is detected and the old
    resume data is discarded — the URL alone never stands in for identity.
    Uses an empty target so the rechecks are instant."""
    _patch_source(monkeypatch)
    captured_a = _patch_fetch(monkeypatch)
    _patch_cache(monkeypatch)
    _patch_nomutate(monkeypatch)
    _manifest_unavailable(monkeypatch)

    empty = tmp_path / "empty"
    empty.mkdir()

    # First run caches identity A and we drop some resume data for it.
    log_q, prog_q = queue.Queue(), queue.Queue()
    VerifyWorker(str(empty), log_q, prog_q).run()
    old_ih = captured_a[0].info_hash
    torrent_update.write_resume_bytes(old_ih, b"old resume data")
    assert os.path.exists(torrent_update.resume_path(old_ih))

    # Second run: snapshot at the same URL now has a *different* identity.
    _patch_fetch(monkeypatch, info_hash="d" * 40, content_hash="c" * 64)
    log_q2, prog_q2 = queue.Queue(), queue.Queue()
    VerifyWorker(str(empty), log_q2, prog_q2).run()

    assert not os.path.exists(torrent_update.resume_path(old_ih))
    assert any("Snapshot changed at URL" in m for m in _messages(log_q2))
