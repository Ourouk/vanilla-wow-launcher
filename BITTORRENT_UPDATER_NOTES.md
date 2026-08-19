# BitTorrent-based updater — lessons & reference

A practical cheat-sheet for building a client/asset updater on top of
**libtorrent** (the tech used by `vanilla_wow_launcher`). Written after fixing
two real bugs that only surface against a *real* client + *real* `.torrent`:
the "stuck at verifying 0/9012" stall and a double-prefix path bug that made
the whole client look stale.

Environment these notes were validated against: libtorrent `2.1.1.0`,
Python `3.13.x`. The reference implementation lives in
`src/vanilla_wow_launcher/services/update_backend/torrent_update.py`
(`TorrentVerifier` + `TorrentDownloader`).

## Reference sources (in this repo's `context/`)

- **Deluge** (`context/deluge/...`) — the canonical libtorrent-backed client.
  - `deluge/core/torrent.py:~1472` does `handle.force_recheck()` then
    `handle.resume()` — this is the proven pattern (see Pitfall P2).
  - `deluge/core/torrentmanager.py:~1335` (`on_alert_torrent_checked`) —
    completion is signalled by `torrent_checked_alert`, not by polling alone.
- **OctoLauncher** (`context/OctoLauncher/update_reimplementation.md:223`) —
  downloads into `userData/torrent-root/client/` where `client/` is a *junction*
  pointing at the chosen WoW folder, and `aria2 --dir=torrent-root` so the
  torrent's `client/` root lands on the real folder. This is the "root must
  resolve to the target dir" idea (see Pitfall P1).

## Architecture (what works)

1. **Verify offline first.** Add the torrent to a session with
   `listen_interfaces=""`, DHT/LSD/UPnP/NAT-PMP off, no trackers, and
   `file_priorities = [7]*n`. Call `force_recheck()` + `resume()`; libtorrent
   then *only hashes the on-disk files* against the `.torrent`'s per-piece
   hashes — no peer connections, no writes.
2. **A file is "stale" when any piece covering it is not present**
   (`not all(h.have_piece(p) for p in piece_range)`). Return the stale file
   list (root stripped) so the caller can correlate with the manifest.
3. **Download only the missing pieces.** Re-add the torrent with per-file
   priorities `7` for wanted/stale files and `0` for the rest, so libtorrent
   fetches just the gaps. The piece hashes still guarantee final integrity.
4. **Authoritative per-file check is the HTTP manifest** (per-file SHA-1). The
   torrent check is piece-granular: a stale piece straddling two files marks
   both, so the follow-up manifest pass is the real correctness gate.

## Pitfalls (the expensive ones)

### P1 — Double-prefix path / root stripping (the big one)
A `.torrent` often wraps files in a root dir, e.g. `client/WoW.exe`. libtorrent
maps `client/WoW.exe` → `save_path/client/WoW.exe`. If you set
`save_path = out_dir` (the target folder), libtorrent reads/writes at
`out_dir/client/...` — a **double prefix** — so every real file looks missing
and the whole client reports stale. The documented contract
(`client/WoW.exe → <out_dir>/WoW.exe`) must be applied to the *read/write
target*, not just the output labels.

**Fix:** detect the root from the unique `WoW.exe` position and **remap the
torrent's file paths** to `out_dir/local` before adding:

```python
def _remap_torrent_to_out_dir(ti, out_dir):
    if not hasattr(ti, "remap_files"):
        return  # test fakes lack it -> no-op
    import libtorrent as lt

    files = ti.files()
    mapping = _map_torrent_paths(files)  # {torrent_path: local_path}
    fs = lt.file_storage()
    for i in range(files.num_files()):
        rel = mapping[files.file_path(i).replace("\\", "/")]
        fs.add_file(os.path.join(out_dir, rel), files.file_size(i))  # ABSOLUTE
    ti.remap_files(fs)
```

Call it right after `ti = snapshot.torrent_info` in **both** verify() and
download(). `remap_files` does not change piece hashes or the info hash.

### P2 — `force_recheck()` needs `resume()` right after
Some bindings add the torrent paused; without `resume()` the recheck may never
proceed (silent stall). Mirror Deluge:

```python
h.force_recheck()
h.resume()
_wait_for_recheck(ses, h, total_pieces)
```

### P3 — `torrent_status.pieces` is a `list[bool]` in libtorrent 2.x
`status.pieces` is a `list[bool]`, so `status.pieces.count()` (no-arg) raises
`TypeError`. Use:

```python
have = sum(status.pieces)  # primary
# num_pieces as fallback only (seed-mode-only verified_pieces is useless here)
```

Do **not** use `status.verified_pieces` for progress — it is populated only in
seed mode and stays `0` during a verifier recheck.

### P4 — `file_storage.add_file` mangles relative paths under `remap_files`
Building the remap `file_storage` from *relative* paths produced garbage
(`unicows.dll/Credits.html`). Use **absolute** paths in `fs.add_file(...)`
(see P1). Also note libtorrent 2.x `file_storage` has a `root()` concept
(per-file roots) — keep it simple with absolute paths.

### P5 — info_hash stability
`remap_files` (P1) and `add_torrent_params.ti` do **not** change the info hash,
so a snapshot's identity is stable across remaps. Resume data is **intentionally
not** persisted between launches: instead of loading a cached `have_pieces`,
`TorrentDownloader.download()` lets libtorrent re-derive piece state from the
on-disk files on add (resume-on-add), which also preserves partial progress and
avoids trusting a possibly-stale cache. If resume persistence is ever reinstated,
key it by info hash and discard it on a mismatched snapshot.

### P6 — offline verification session
```python
lt.session(
    {
        "listen_interfaces": "",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        # alert_mask: VERIFIER_ALERT_MASK (or ALERT_MASK for the downloader)
    }
)
```

### P7 — stall guard / progress monitoring
Watch `state` leaving `checking_files`/`allocating`; track max pieces verified
over time. Trip a stall only if progress hasn't advanced for a bounded window
(pieces do hash slowly on multi-GB clients — minutes, not seconds). Use
`h.have_piece(p)` per piece, or `status.pieces`/`num_pieces`, never
`verified_pieces`.

### P8 — piece vs file granularity
A `.torrent` carries only per-piece SHA-1, not per-file hashes. Map each file
to its piece range (`file_offset(i)//piece_length` … `(offset+size-1)//piece_length`)
and treat it stale if any covered piece is missing. Tell users the torrent check
is advisory; the manifest recheck is authoritative.

### P9 — snapshot identity
A URL never stands in for identity. Persist `content_hash` + `info_hash` with the
verdict; a different snapshot at the same URL must invalidate the cached verdict.
Resume data is no longer persisted between launches (see P5), so there is no
resume cache to discard — the on-disk recheck on add handles staleness.

## Testing strategy (so the bugs don't come back)

- **Unit tests** replace `libtorrent` via
  `monkeypatch.setitem(sys.modules, "libtorrent", FakeLT())` and assert only
  events/behaviour, never real hashing. Guard any real-libtorrent-only call
  (e.g. `remap_files`) behind `hasattr(ti, "remap_files")` so fakes are a no-op.
- **End-to-end** uses the *real* client + *real* `.torrent` and the *real*
  libtorrent. Gate the whole module:
  `skipif(not (RUN_E2E=="1" and client_dir.exists() and torrent.exists()))`,
  mark `e2e`, and have CI run `pytest -m "not e2e"`. Keep the heavy artifacts
  in a git-ignored `context/` tree.
- The e2e test that actually proves the fixes: recheck the real client and
  assert `(stale == [])` (reference client is byte-identical to the snapshot)
  **and** `max_pieces > 0` (no stall). Log `stale`/`max_pieces` for visibility.
- An empty target is a fast way to assert "all files stale" and the recovery
  path, without copying gigabytes.

## Minimal skeleton

```python
import libtorrent as lt, os


def verify(out_dir, torrent_path):
    ti = lt.torrent_info(torrent_path)
    _remap_torrent_to_out_dir(ti, out_dir)  # P1
    ses = lt.session(
        {
            "listen_interfaces": "",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
        }
    )
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = out_dir
    atp.file_priorities = [7] * ti.files().num_files()
    h = ses.add_torrent(atp)
    h.force_recheck()
    h.resume()  # P2
    _wait_until_done(ses, h)  # P7
    stale = []
    pl = ti.piece_length()
    for i in range(ti.files().num_files()):
        off, sz = ti.files().file_offset(i), ti.files().file_size(i)
        rng = range(off // pl, (off + sz - 1) // pl + 1) if sz > 0 else ()
        if rng and not all(h.have_piece(p) for p in rng):
            stale.append(_local_path(ti, i))  # root stripped
    return stale
```
