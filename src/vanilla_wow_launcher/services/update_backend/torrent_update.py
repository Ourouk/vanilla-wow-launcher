"""BitTorrent update backend for client updates (libtorrent).

`TorrentDownloader` fetches a ``.torrent`` over HTTPS (through the same
hardened, allowlisted transport as the HTTP downloads) and uses libtorrent to
bulk-download the files the manifest flagged as stale. Peers in the swarm are
untrusted — a malicious peer can only inject data that fails the piece hashes
embedded in the ``.torrent`` (which itself came over TLS).

Integrity layering: when a manifest diff tree exists, the caller re-verifies
the delivered files' SHA-1s against the manifest and re-fetches any mismatch
over HTTPS, so the torrent backend cannot weaken the manifest's guarantee. In
the manifest-less recovery path there is no per-file hash list to check
against — there, the TLS-fetched torrent's piece hashes are the integrity
guarantee by themselves.

The session otherwise follows libtorrent's default storage and connection
configuration. The torrent is paused and removed from the session once every
wanted piece is in place.
"""

import hashlib
import os
import queue
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ...core.constants import DOWNLOAD_TIMEOUT, UA
from ...core.helpers import fmt_size, fmt_speed
from ...core.platform_support import cache_dir
from ...core.security_http import allowed_download_hosts, secure_urlopen

# Inactivity guard: if no wanted bytes arrive for this long, the swarm is dead
# and the caller should fall back to per-file HTTP downloads.
STALL_TIMEOUT = 60
# Grace period before the stall check kicks in, allowing time for DHT
# bootstrap, tracker announces, peer discovery, and the first piece transfer.
DISCOVERY_TIMEOUT = 180
# Known DHT bootstrap nodes — accelerates initial peer discovery.
DHT_BOOTSTRAP_NODES = (
    "router.libtorrent.org:6881,"
    "router.bittorrent.com:6881,"
    "dht.transmissionbt.com:6881"
)
# Use OS-assigned ephemeral ports to avoid conflicts; server can use fixed
# ports if needed. The default is empty to let the OS pick.
LISTEN_INTERFACES = "0.0.0.0:0"
ALERT_POLL_MS = 250
# upload_rate_limit is in bytes/sec; 0 (and -1) mean unlimited in libtorrent.
# A near-zero value (e.g. 1) starves upload, so peers choke the client under
# BitTorrent's tit-for-tat and the download hangs at 0 B/s despite connected
# peers. If a good-citizen cap is ever wanted, use a real bytes/sec value
# (KB/s * 1024, per Deluge's _on_set_max_upload_speed), never ~0.
UPLOAD_RATE_LIMIT = -1
# Alert categories we care about: error, storage (for failures), status (for
# state changes), and tracker/DHT if we want more detail.
ALERT_MASK = (
    1  # error_notification
    | 8  # storage_notification
    | 16  # tracker_notification
    | 64  # status_notification
    | 1024  # dht_notification
)
VERIFIER_ALERT_MASK = (
    1  # error_notification
    | 8  # storage_notification
    | 64  # status_notification
)


def available() -> bool:
    """Whether the libtorrent python module can be imported and used.
    Probed lazily so the app degrades gracefully to HTTP when it isn't installed
    or cannot be loaded. Side-effect free — does not create a session."""
    try:
        import libtorrent as lt

        # Verify the module has the symbols we use at runtime without
        # constructing a session or binding ports.
        _ = lt.session
        _ = lt.add_torrent_params
        _ = lt.torrent_info
        _ = lt.torrent_status
        _ = lt.alert.category_t.error_notification
        return True
    except (ImportError, ValueError, OSError, RuntimeError, AttributeError):
        return False


class TorrentFetchError(Exception):
    """Raised when the ``.torrent`` file cannot be fetched (HTTP error,
    DNS failure, allowlist rejection, etc.).  Distinguishes *network*
    failures from libtorrent verification failures so the caller can
    mark the snapshot as unreachable vs. simply failed."""

    pass


class TorrentCorruptError(TorrentFetchError):
    """Raised when the downloaded ``.torrent`` file is malformed or cannot
    be parsed by libtorrent (e.g. truncated, not a valid bencoded dict)."""

    pass


class TorrentStalledError(RuntimeError):
    """Raised when libtorrent verification/download makes no progress for
    STALL_TIMEOUT seconds. Includes peer count for diagnostics."""

    def __init__(self, peers: int):
        self.peers = peers
        super().__init__(f"Stalled ({peers} peers)")


class TorrentSessionError(RuntimeError):
    """Raised when libtorrent session creation or add_torrent fails
    (port binding, resource limits, invalid torrent for session)."""

    pass


class TorrentDiskError(RuntimeError):
    """Raised when disk I/O fails during torrent operations (disk full,
    permission denied, etc.)."""

    pass


class TorrentSnapshotMismatchError(RuntimeError):
    """Raised when the fetched torrent snapshot no longer contains a wanted
    file path (the torrent was replaced between verify and download). The
    caller must re-verify against the new snapshot rather than trust an old
    local file the new snapshot cannot validate."""

    pass


@dataclass
class TorrentSnapshot:
    """One fetched and parsed ``.torrent``, with its identity.

    ``content_hash`` is the SHA-256 of the raw ``.torrent`` bytes (any change
    to the file — trackers, web seeds, metadata — changes it); ``info_hash``
    is the torrent's content identity from libtorrent. Together they let the
    launcher detect a snapshot that changed at the same URL. ``torrent_info``
    is the parsed libtorrent object; ``torrent_bytes`` the raw payload.
    """

    url: str
    content_hash: str
    info_hash: str | None
    torrent_bytes: bytes
    torrent_info: object


def _info_hash_hex(ti) -> str | None:
    """Best-effort hex info-hash of a parsed torrent (v1 preferred, then v2).

    Returns None when the binding doesn't expose an info hash (e.g. exotic
    torrents or a stubbed module in tests) — callers treat that as
    "identity unavailable" and simply never cache by identity."""
    try:
        ih = ti.info_hashes()
    except Exception:
        ih = None
    if ih is not None:
        for attr in ("v1", "v2"):
            try:
                value = str(getattr(ih, attr, None) or "")
            except Exception:
                continue
            if value and value != "0" * len(value):
                return value
    try:
        return str(ti.info_hash()) or None
    except Exception:
        return None


# ── torrent metadata persistence (identity + resume data) ──────────────────


def torrent_cache_dir() -> str:
    """Per-user cache directory for torrent metadata. Kept out of the game
    folder so reinstall/move never wipes the resume state."""
    return os.path.join(cache_dir(), "torrents")


def torrent_path(info_hash: str) -> str:
    return os.path.join(torrent_cache_dir(), f"{info_hash}.torrent")


def resume_path(info_hash: str) -> str:
    return os.path.join(torrent_cache_dir(), f"{info_hash}.resume")


def _atomic_write_bytes(path: str, data: bytes):
    """Write via a temp file + atomic rename so a crash mid-write can never
    leave a truncated file at `path`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_torrent_atomically(info_hash: str, data: bytes):
    _atomic_write_bytes(torrent_path(info_hash), data)


def write_resume_bytes(info_hash: str, buf: bytes):
    _atomic_write_bytes(resume_path(info_hash), buf)


def remove_resume_data(info_hash: str):
    try:
        os.remove(resume_path(info_hash))
    except OSError:
        pass


def _fetch_torrent(torrent_url: str, log) -> "TorrentSnapshot":
    """Fetch the ``.torrent`` over the allowlisted HTTPS transport, parse it
    with libtorrent, and return a :class:`TorrentSnapshot` carrying the raw
    bytes, their SHA-256 content hash, and the torrent's info hash.

    Network/security failures (HTTP errors, connection refused, DNS, TLS,
    allowlist rejection) are wrapped in :class:`TorrentFetchError` so the
    caller can distinguish a *missing* snapshot from a *failed* verification.

    The raw bytes are persisted under the launcher cache (keyed by info hash)
    on a best-effort basis so resume data always has a stable home.
    """
    import libtorrent as lt

    log(f"  Fetching torrent: {torrent_url}", "dim")
    req = urllib.request.Request(torrent_url, headers={"User-Agent": UA})
    try:
        with secure_urlopen(
            req,
            timeout=DOWNLOAD_TIMEOUT,
            allowed_hosts=allowed_download_hosts(),
        ) as r:
            # Stream the torrent file with a size cap to avoid loading a
            # malicious oversized response into memory.
            max_size = 5 * 1024 * 1024  # 5 MiB cap for .torrent files
            data = bytearray()
            for chunk in iter(lambda: r.read(65536), b""):
                data.extend(chunk)
                if len(data) > max_size:
                    raise TorrentFetchError(
                        f"Torrent file exceeds maximum size of {max_size} bytes"
                    )
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        RuntimeError,
    ) as exc:
        raise TorrentFetchError(str(exc)) from exc

    data = bytes(data)
    content_hash = hashlib.sha256(data).hexdigest()
    fd, tmp = tempfile.mkstemp(suffix=".torrent")
    try:
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(data)
        except OSError as e:
            raise TorrentDiskError(f"Failed to write torrent file: {e}") from e
        try:
            ti = lt.torrent_info(tmp)
        except Exception as e:
            raise TorrentCorruptError(f"Failed to parse torrent: {e}") from e
        info_hash = _info_hash_hex(ti)
        if info_hash:
            try:
                write_torrent_atomically(info_hash, data)
            except OSError as e:
                log(f"  Failed to cache torrent metadata: {e}", "dim")
        return TorrentSnapshot(
            url=torrent_url,
            content_hash=content_hash,
            info_hash=info_hash,
            torrent_bytes=data,
            torrent_info=ti,
        )
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class TorrentLayoutError(TorrentCorruptError):
    """Raised when the torrent's file layout cannot be mapped to the local
    client directory (missing or duplicate WoW.exe, path traversal, etc.)."""

    pass


def _detect_torrent_root(
    files,
) -> tuple[str, dict[str, str]]:
    """Detect the torrent root directory from the unique ``WoW.exe`` position.

    Scans every file path in the torrent (case-insensitive) looking for the
    single entry whose basename is ``WoW.exe``. The parent directory of that
    entry is the *root*; all other torrent paths are expected to live under
    the same root.

    Returns ``(torrent_root, {torrent_path: local_path})`` where:

    * ``torrent_root`` is the leading directory to strip (e.g. ``client``).
    * ``local_path`` is the path relative to the selected WoW folder
      (e.g. ``Data/foo.mpq``).

    Raises :class:`TorrentLayoutError` when:

    * no ``WoW.exe`` is found,
    * multiple ``WoW.exe`` entries exist, or
    * any file escapes the detected root directory.
    """
    num = files.num_files()
    exe_indices: list[int] = []
    normalized: list[str] = []
    for i in range(num):
        p = files.file_path(i).replace("\\", "/")
        normalized.append(p)
        if p.rsplit("/", 1)[-1].lower() == "wow.exe":
            exe_indices.append(i)

    if not exe_indices:
        raise TorrentLayoutError(
            "Torrent contains no WoW.exe — cannot detect root directory"
        )
    if len(exe_indices) > 1:
        paths = [normalized[i] for i in exe_indices]
        raise TorrentLayoutError(
            f"Torrent contains multiple WoW.exe entries: {paths}"
        )

    parts = normalized[exe_indices[0]].split("/")
    root = "/".join(parts[:-1])  # "" when WoW.exe is at the top level
    prefix = root + "/" if root else ""
    mapping: dict[str, str] = {}
    for p in normalized:
        if prefix and not p.startswith(prefix):
            raise TorrentLayoutError(
                f"File {p!r} is outside detected torrent root {root!r}"
            )
        local = p[len(prefix) :]
        if ".." in local.split("/"):
            raise TorrentLayoutError(f"Path traversal in torrent file: {p!r}")
        mapping[p] = local
    return root, mapping


def _map_torrent_paths(
    files,
) -> dict[str, str]:
    """Convenience wrapper around :func:`_detect_torrent_root` that returns
    only the ``{torrent_path: local_path}`` mapping."""
    _, mapping = _detect_torrent_root(files)
    return mapping


def _remap_torrent_to_out_dir(ti, out_dir: str) -> None:
    """Strip the auto-detected torrent root so the snapshot's files resolve
    directly under ``out_dir`` (e.g. torrent ``client/WoW.exe`` ->
    ``out_dir/WoW.exe``).

    libtorrent maps a torrent file ``client/WoW.exe`` to
    ``save_path/client/WoW.exe``. With ``save_path == out_dir`` that reads and
    writes at ``out_dir/client/...`` — a double prefix — so every real file
    looks missing and the whole client is reported stale. Remapping the torrent
    file paths to ``out_dir/local`` (root stripped) fixes the read/write target
    while leaving piece hashes and the info hash untouched.

    The root is auto-detected from the unique WoW.exe position (see
    :func:`_detect_torrent_root`); only the leading root directory is removed.
    Real libtorrent exposes ``torrent_info.remap_files``; the test fakes do
    not, so this is a no-op under unit tests."""
    if not hasattr(ti, "remap_files"):
        return
    import libtorrent as lt

    files = ti.files()
    mapping = _map_torrent_paths(files)
    fs = lt.file_storage()
    for i in range(files.num_files()):
        rel = mapping[files.file_path(i).replace("\\", "/")]
        fs.add_file(os.path.join(out_dir, rel), files.file_size(i))
    ti.remap_files(fs)


def _file_piece_ranges(files, piece_length: int) -> list[list[int]]:
    """Map each torrent file to the indices of the pieces covering it.

    Returns one list per file (in torrent order) of piece indices, derived
    from each file's byte ``offset`` and ``size`` and the torrent's fixed
    ``piece_length``."""
    ranges = []
    for i in range(files.num_files()):
        start = files.file_offset(i)
        size = files.file_size(i)
        if size <= 0:
            ranges.append([])
            continue
        first = start // piece_length
        last = (start + size - 1) // piece_length
        ranges.append(list(range(first, last + 1)))
    return ranges


class TorrentVerifier:
    """Torrent-only integrity check (no manifest needed).

    Fetches the ``.torrent`` and asks libtorrent to hash-check the existing
    files against the embedded piece hashes, without downloading anything
    (every file priority is 0). After the recheck completes, a file is stale
    when any piece covering it is not present on disk. Returns the stale file
    paths; an empty list means the client is already up to date.

    Like ``TorrentDownloader`` this verifies at *piece* granularity — a
    ``.torrent`` carries per-piece hashes, not per-file SHA-1s — so a stale
    piece that straddles two files can mark both. The follow-up download only
    fetches the missing pieces, and the piece hashes still guarantee the
    final integrity.
    """

    def __init__(self, out_dir: str, log_q: queue.Queue, prog_q: queue.Queue):
        self.out_dir = out_dir
        self.log_q = log_q
        self.prog_q = prog_q
        self._cancel = False
        self.snapshot: TorrentSnapshot | None = None

    def cancel(self):
        self._cancel = True

    def log(self, msg: str, tag: str = ""):
        self.log_q.put((msg, tag))

    def progress(self, value: float, label: str = "", **details):
        item = (value, label, details) if details else (value, label)
        self.prog_q.put(item)

    def _session(self):
        import libtorrent as lt

        return lt.session(
            {
                "listen_interfaces": "",
                "user_agent": UA,
                "upload_rate_limit": UPLOAD_RATE_LIMIT,
                "enable_dht": False,
                "enable_lsd": False,
                "enable_upnp": False,
                "enable_natpmp": False,
                "alert_mask": VERIFIER_ALERT_MASK,
            }
        )

    def _stale_files(self, h, files, piece_length: int) -> list[str]:
        """Files whose covering pieces are not all present after the recheck,
        with the torrent root directory stripped to match the manifest
        layout.  The root is auto-detected from the unique WoW.exe position."""
        mapping = _map_torrent_paths(files)
        ranges = _file_piece_ranges(files, piece_length)
        stale = []
        for i, pieces in enumerate(ranges):
            if pieces and not all(h.have_piece(p) for p in pieces):
                tp = files.file_path(i).replace("\\", "/")
                stale.append(mapping[tp])
        return stale

    def verify(self, torrent_url: str) -> list[str]:
        """Hash-check the local files against the torrent and return the stale
        (missing or differing) file paths. Raises RuntimeError on failure or
        cancellation. Never downloads or seeds — read-only. This is a
        torrent-piece check; the update controller performs the authoritative
        manifest hash check afterwards.

        The fetched :class:`TorrentSnapshot` is stored on ``self.snapshot`` so
        the caller can persist its identity alongside the verdict."""
        import libtorrent as lt

        snapshot = _fetch_torrent(torrent_url, self.log)
        self.snapshot = snapshot
        ti = snapshot.torrent_info
        _remap_torrent_to_out_dir(ti, self.out_dir)
        files = ti.files()
        piece_length = ti.piece_length()
        total_pieces = ti.num_pieces()

        try:
            ses = self._session()
        except Exception as e:
            raise TorrentSessionError(
                f"Failed to create libtorrent session: {e}"
            ) from e

        h = None
        try:
            atp = lt.add_torrent_params()
            atp.ti = ti
            atp.save_path = self.out_dir
            # Pieces must be "wanted" (priority > 0) for force_recheck() to
            # hash the on-disk files against the torrent's piece hashes. A
            # priority of 0 skips both download and verification, which would
            # leave every piece's verified state False and stall the recheck.
            # The verifier session is fully offline (empty listen_interfaces,
            # DHT/LSD/UPnP/NAT-PMP off, no trackers), so max priority only
            # triggers a read-only hash check — no peer connections or writes.
            atp.file_priorities = [7] * files.num_files()
            try:
                h = ses.add_torrent(atp)
            except Exception as e:
                raise TorrentSessionError(
                    f"Failed to add torrent to session: {e}"
                ) from e
            h.force_recheck()
            # Deluge's proven pattern: resume() after force_recheck() so the
            # recheck actually proceeds even if the torrent was added paused
            # (some bindings add it paused by default). The recheck is a
            # read-only hash of the on-disk files; resume() does not start any
            # peer connection in this offline session.
            h.resume()
            self._wait_for_recheck(ses, h, total_pieces)
            return self._stale_files(h, files, piece_length)
        except OSError as e:
            if e.errno in (28, 13):  # ENOSPC, EACCES
                raise TorrentDiskError(f"Disk I/O error: {e}") from e
            raise
        finally:
            if h is not None:
                try:
                    h.pause()
                    ses.remove_torrent(h)
                except Exception:
                    pass
            self._cleanup_part_files()

    def _wait_for_recheck(self, ses, h, total_pieces: int):
        """Block until libtorrent's hash recheck of the existing files is done
        (or the swarm/folder can't produce a finished recheck), honouring
        cancel and a stall guard."""
        import libtorrent as lt

        last_move = time.monotonic()
        last_checked = 0
        seen_checking = False
        # In libtorrent 2.1, checking can be in multiple states
        checking_states = {
            lt.torrent_status.states.checking_files,
            lt.torrent_status.states.checking_resume_data,
            lt.torrent_status.states.queued_for_checking,
        }
        while not self._cancel:
            for a in ses.pop_alerts():
                if a.category() & lt.alert.category_t.error_notification:
                    self.log(
                        f"  {type(a).__name__}: {getattr(a, 'message', str)()}",
                        "dim",
                    )
                # Storage errors (disk full, permission denied, etc.)
                # Only handle actual failure alerts, not successful ones like
                # file_completed_alert.
                if a.category() & lt.alert.category_t.storage_notification:
                    # Check if it's a failure alert (not read_piece_alert —
                    # that fires on explicit read_piece() calls and is normal).
                    if type(a).__name__ in (
                        "file_error_alert",
                        "file_rename_failed_alert",
                        "torrent_delete_failed_alert",
                        "storage_moved_failed_alert",
                        "save_resume_data_failed_alert",
                    ):
                        self.log(
                            f"  Storage error: {type(a).__name__}: {getattr(a, 'message', str)()}",
                            "err",
                        )
                        raise TorrentDiskError(
                            f"Storage error: {type(a).__name__}: {getattr(a, 'message', str)()}"
                        )
            # status() is synchronous in the Python binding. This worker is
            # isolated from the UI thread, so the direct snapshot is simpler
            # than coordinating post_status/state_update_alert callbacks.
            s = h.status()
            # The authoritative "pieces verified so far" during a force_recheck()
            # is the live "have" bitfield. In libtorrent 2.x status().pieces is a
            # list[bool] (sum() counts the verified pieces) and it advances as each
            # piece is hashed — this is what drives progress. torrent_status
            # .verified_pieces is ONLY populated in seed mode (the verifier is NOT
            # in seed mode), so it stays empty and must never be used. status
            # ().progress may also lag a recheck, so we take the max of the
            # have-count and progress.
            have = 0
            pieces = getattr(s, "pieces", None)
            if pieces is not None:
                have = sum(pieces)
            elif getattr(s, "num_pieces", None):
                have = s.num_pieces
            checked = (
                int(round(s.progress * total_pieces)) if total_pieces else 0
            )
            done = max(have, checked)
            if s.state in checking_states:
                # Actively hashing: never false-stall a slow multi-GB recheck,
                # and remember we entered checking so we only finish once it ends.
                seen_checking = True
                last_move = time.monotonic()
            elif seen_checking:
                # Recheck has left the checking states -> it is done. have_piece()
                # (used by _stale_files) is authoritative for the verdict, so we
                # don't require a non-zero count here. Emit a final 100% so the
                # progress label doesn't freeze below completion.
                self.progress(
                    1.0,
                    f"Verifying client against torrent…  {total_pieces} / "
                    f"{total_pieces} pieces",
                    phase="Verifying",
                    transport="BitTorrent",
                    verified_pieces=total_pieces,
                    total_pieces=total_pieces,
                )
                return
            if total_pieces and done >= total_pieces:
                # All pieces present (verified or assumed): recheck finished.
                # Emit a final 100% before returning so the bar reaches 100%.
                self.progress(
                    1.0,
                    f"Verifying client against torrent…  {total_pieces} / "
                    f"{total_pieces} pieces",
                    phase="Verifying",
                    transport="BitTorrent",
                    verified_pieces=total_pieces,
                    total_pieces=total_pieces,
                )
                return
            if done != last_checked:
                last_checked = done
                last_move = time.monotonic()
            self.progress(
                min(1.0, done / total_pieces) if total_pieces else 0.0,
                f"Verifying client against torrent…  {done} / "
                f"{total_pieces} pieces",
                phase="Verifying",
                transport="BitTorrent",
                verified_pieces=done,
                total_pieces=total_pieces,
            )
            if time.monotonic() - last_move > STALL_TIMEOUT:
                raise TorrentStalledError(peers=s.num_peers)
            ses.wait_for_alert(ALERT_POLL_MS)
        try:
            h.cancel()
        except Exception:
            pass
        raise RuntimeError("Cancelled")

    def _cleanup_part_files(self):
        pad = os.path.join(self.out_dir, ".torrents")
        try:
            if os.path.isdir(pad) and not os.listdir(pad):
                os.rmdir(pad)
        except OSError:
            pass


class TorrentDownloader:
    def __init__(self, out_dir: str, log_q: queue.Queue, prog_q: queue.Queue):
        self.out_dir = out_dir
        self.log_q = log_q
        self.prog_q = prog_q
        self._cancel = False
        self.snapshot: TorrentSnapshot | None = None

    def cancel(self):
        self._cancel = True

    def log(self, msg: str, tag: str = ""):
        self.log_q.put((msg, tag))

    def progress(self, value: float, label: str = "", **details):
        item = (value, label, details) if details else (value, label)
        self.prog_q.put(item)

    def _priorities(self, ti, wanted: set[str] | None) -> list[int]:
        """Per-file priorities: stale files at max priority, everything else
        skipped (0) so only the pieces covering the stale files download.
        ``wanted=None`` means the whole torrent (every file at max priority)
        — used by the no-manifest recovery path.  Uses the auto-detected
        WoW.exe root for path mapping.

        A wanted path absent from the snapshot is a hard mismatch
        (:class:`TorrentSnapshotMismatchError`) — the torrent was replaced
        between verify and download, so the client can never be reported
        recovered against a snapshot that no longer contains it."""
        files = ti.files()
        mapping = _map_torrent_paths(files)
        n = files.num_files()
        if wanted is None:
            return [7] * n
        local_to_index = {
            mapping[files.file_path(i).replace("\\", "/")]: i for i in range(n)
        }
        missing = sorted(w for w in wanted if w not in local_to_index)
        if missing:
            self.log(
                f"[torrent] {len(missing)} wanted file(s) absent from this "
                f"snapshot: {', '.join(missing)}",
                "err",
            )
            raise TorrentSnapshotMismatchError(
                f"Torrent replaced — {len(missing)} wanted file(s) not in "
                f"the new snapshot: {', '.join(missing)}"
            )
        return [
            7
            if mapping[files.file_path(i).replace("\\", "/")] in wanted
            else 0
            for i in range(n)
        ]

    def _session(self):
        import libtorrent as lt

        return lt.session(
            {
                "listen_interfaces": LISTEN_INTERFACES,
                "user_agent": UA,
                "upload_rate_limit": UPLOAD_RATE_LIMIT,
                "enable_dht": True,
                "dht_bootstrap_nodes": DHT_BOOTSTRAP_NODES,
                "enable_lsd": False,
                "enable_upnp": True,
                "enable_natpmp": True,
                "alert_mask": ALERT_MASK,
            }
        )

    def download(self, torrent_url: str, wanted: set[str] | None) -> list[str]:
        """Download the wanted files from the torrent at ``torrent_url`` into
        ``out_dir``. ``wanted=None`` downloads the whole torrent. Returns the
        an empty list on success and raises RuntimeError on failure or
        cancellation. The caller already knows the wanted paths. Completed
        files are still rechecked against the update manifest by the HTTP
        update worker.

        Resume data for the snapshot's info hash is loaded before the torrent
        is added and saved again before the handle is removed. The fetched
        :class:`TorrentSnapshot` is stored on ``self.snapshot``."""
        import libtorrent as lt

        if wanted is not None and not wanted:
            return []
        snapshot = _fetch_torrent(torrent_url, self.log)
        self.snapshot = snapshot
        ti = snapshot.torrent_info
        _remap_torrent_to_out_dir(ti, self.out_dir)

        try:
            ses = self._session()
        except Exception as e:
            raise TorrentSessionError(
                f"Failed to create libtorrent session: {e}"
            ) from e

        h = None
        try:
            atp = lt.add_torrent_params()
            priorities = self._priorities(ti, wanted)
            atp.ti = ti
            atp.save_path = self.out_dir
            atp.file_priorities = priorities
            files = ti.files()
            total_wanted = sum(
                files.file_size(i)
                for i in range(files.num_files())
                if priorities[i] > 0
            )
            wanted_count = sum(1 for p in priorities if p > 0)
            try:
                h = ses.add_torrent(atp)
            except Exception as e:
                raise TorrentSessionError(
                    f"Failed to add torrent to session: {e}"
                ) from e
            # The binding adds the torrent paused; resume() starts it so it
            # checks the on-disk files and then downloads only the wanted
            # pieces (mirrors the verify path's force_recheck()+resume()).
            # Resume data is intentionally not loaded, so libtorrent re-derives
            # piece state from disk instead of trusting a possibly-stale cache.
            h.resume()
            return self._pump(
                ses,
                h,
                total_wanted=total_wanted,
                wanted_count=wanted_count,
            )
        except OSError as e:
            if e.errno in (28, 13):  # ENOSPC, EACCES
                raise TorrentDiskError(f"Disk I/O error: {e}") from e
            raise
        finally:
            if h is not None:
                try:
                    h.pause()
                    ses.remove_torrent(h)
                except Exception:
                    pass
            self._cleanup_part_files()

    def _pump(
        self,
        ses,
        h,
        *,
        total_wanted: int,
        wanted_count: int,
    ) -> list[str]:
        """Alert loop: report progress, detect errors/stalls, honour cancel.
        Returns the wanted paths once the torrent is finished.

        *total_wanted* and *wanted_count* are pre-computed from the torrent
        file list and priorities — they provide a stable denominator from the
        first poll iteration without waiting for libtorrent's status."""
        import libtorrent as lt

        checking_states = {
            lt.torrent_status.states.checking_files,
            lt.torrent_status.states.checking_resume_data,
            lt.torrent_status.states.queued_for_checking,
        }

        last_wanted_done = 0
        last_move = time.monotonic()
        transfer_started = False
        name = ""
        while not self._cancel:
            for a in ses.pop_alerts():
                if a.category() & lt.alert.category_t.error_notification:
                    self.log(
                        f"  {type(a).__name__}: {getattr(a, 'message', str)()}",
                        "dim",
                    )
                # Storage errors (disk full, permission denied, etc.)
                # Only handle actual failure alerts, not successful ones like
                # file_completed_alert.
                if a.category() & lt.alert.category_t.storage_notification:
                    if type(a).__name__ in (
                        "file_error_alert",
                        "file_rename_failed_alert",
                        "torrent_delete_failed_alert",
                        "storage_moved_failed_alert",
                        "save_resume_data_failed_alert",
                    ):
                        self.log(
                            f"  Storage error: {type(a).__name__}: {getattr(a, 'message', str)()}",
                            "err",
                        )
                        raise TorrentDiskError(
                            f"Storage error: {type(a).__name__}: {getattr(a, 'message', str)()}"
                        )
            # status() is synchronous in the Python binding. This worker is
            # isolated from the UI thread, so the direct snapshot is simpler
            # than coordinating post_status/state_update_alert callbacks.
            s = h.status()
            name = s.name or name
            wanted_done = s.total_wanted_done
            if wanted_done != last_wanted_done:
                last_wanted_done = wanted_done
                last_move = time.monotonic()
                transfer_started = True
            # Reset stall timer when peers connect — the session is alive.
            if s.num_peers > 0:
                last_move = time.monotonic()
            # While libtorrent is hashing the on-disk files (the initial
            # recheck that re-derives piece state now that resume data is not
            # loaded), keep the stall timer alive so a slow multi-GB recheck
            # can't exceed DISCOVERY_TIMEOUT and raise TorrentStalledError
            # before any byte is downloaded. Mirrors _wait_for_recheck. Use
            # getattr so a status object lacking `.state` (e.g. a bare fake)
            # is treated as "not checking" rather than erroring.
            if getattr(s, "state", None) in checking_states:
                last_move = time.monotonic()
            if s.is_finished or (
                total_wanted > 0 and wanted_done >= total_wanted
            ):
                self.progress(
                    1.0,
                    name,
                    phase="Torrent complete",
                    transport="BitTorrent",
                    current_file=name,
                    downloaded=total_wanted,
                    total=total_wanted,
                    speed=s.download_rate,
                    peers=s.num_peers,
                )
                return []
            total = total_wanted or 1
            speed = fmt_speed(s.download_rate) if s.download_rate else ""
            peers = f"   •   {s.num_peers} peers" if s.num_peers else ""
            self.progress(
                min(1.0, wanted_done / total),
                f"{name}   •   {fmt_size(wanted_done)} / "
                f"{fmt_size(total_wanted)}"
                f"   •   {wanted_count} files"
                f"{'   •   ' + speed if speed else ''}{peers}",
                phase="Downloading",
                transport="BitTorrent",
                current_file=name,
                downloaded=wanted_done,
                total=total_wanted,
                speed=s.download_rate,
                peers=s.num_peers,
            )
            elapsed = time.monotonic() - last_move
            timeout = STALL_TIMEOUT if transfer_started else DISCOVERY_TIMEOUT
            if elapsed > timeout:
                raise TorrentStalledError(peers=s.num_peers)
            ses.wait_for_alert(ALERT_POLL_MS)
        try:
            h.cancel()
        except Exception:
            pass
        raise RuntimeError("Cancelled")

    def _cleanup_part_files(self):
        """Remove the empty `.torrents` piece-padding dir libtorrent may have
        left behind (a non-empty one holds incomplete pieces — keep it so a
        later run can resume from it)."""
        pad = os.path.join(self.out_dir, ".torrents")
        try:
            if os.path.isdir(pad) and not os.listdir(pad):
                os.rmdir(pad)
        except OSError:
            pass
