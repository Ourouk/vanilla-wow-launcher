"""HTTP client update backend: manifest verification and incremental update.

`VerifyWorker` fetches the manifest from the selected download source and
reports which files differ. `UpdateWorker` downloads (resumably) the changed
files, verifies SHA-1s, and clears the WDB cache. When the manifest cannot be
fetched but the active source advertises a ``torrent_url`` (and libtorrent is
available) the update falls back to a full BitTorrent recovery download of the
client files, verified against the torrent's piece hashes. Both speak to the
GUI exclusively through the log/progress queues using the documented message
protocol.
"""

import hashlib
import json
import os
import queue
import shutil
import time
import urllib.request

from ...core.config_store import load_cache, save_cache
from ...core.constants import (
    DOWNLOAD_RETRY,
    DOWNLOAD_TIMEOUT,
    UA,
)
from ...core.filesystem import (
    cached_sha1,
    get_client_version,
    remove_wdb,
    sha1_file,
)
from ...core.helpers import fmt_size, fmt_speed
from ...core.security_http import allowed_download_hosts, secure_urlopen
from ..tweaks import write_config_wtf
from . import markers
from .sources import DownloadSource, _download_source

# Re-exported for compatibility: controllers and tests resolve these through
# this module (and monkeypatch `_download_source` on it).
__all__ = [
    "DownloadSource",
    "UpdateWorker",
    "VerifyWorker",
    "torrent_recovery_available",
    "_download_source",
]


TORRENT_VALIDATION_CACHE_KEY = "__torrent_validation__"


def _torrent_available() -> bool:
    """Whether the BitTorrent backend can run (libtorrent installed)."""
    try:
        from .torrent_update import available

        return available()
    except Exception:
        return False


def torrent_recovery_available() -> bool:
    """Whether a manifest-less full re-download via BitTorrent is possible:
    some configured source advertises a ``torrent_url`` and libtorrent is
    importable. Network-free (no mirror probing) so it's safe to call from
    the readiness path."""
    from ...core import launcher

    cfg = launcher.config()
    return bool(cfg and cfg.has_torrent() and _torrent_available())


class VerifyWorker:
    def __init__(
        self,
        out_dir: str,
        log_q: queue.Queue,
        prog_q: queue.Queue,
        overwrite_config: bool = False,
    ):
        self.out_dir = out_dir
        self.log_q = log_q
        self.prog_q = prog_q
        self._cancel = False
        self.overwrite_config = overwrite_config
        self._cache: dict = load_cache()

    def cancel(self):
        self._cancel = True

    def log(self, msg, tag=""):
        self.log_q.put((msg, tag))

    def progress(self, value, label="", **details):
        item = (value, label, details) if details else (value, label)
        self.prog_q.put(item)

    def _file_ok(self, dest, server_hash):
        if not os.path.exists(dest):
            return False
        local_hash = cached_sha1(dest, self._cache)
        if local_hash == server_hash:
            return True
        return False

    def _traverse(self, node, path_parts):
        if self._cancel:
            return None
        t = node["type"]
        name = node["name"]
        cur = path_parts + [name]

        if t == "dir":
            stale = [
                c
                for child in node.get("files", [])
                if (c := self._traverse(child, cur)) is not None
            ]
            return {**node, "files": stale} if stale else None

        dest = os.path.join(self.out_dir, os.path.join(*cur))

        if t == "del":
            return node if os.path.exists(dest) else None

        if t == "file":
            return None if self._file_ok(dest, node["hash"]) else node

        if t == "mpq":
            mpq_dest = os.path.join(
                self.out_dir, os.path.join(*(path_parts + [name + ".mpq"]))
            )
            return None if self._file_ok(mpq_dest, node["hash"]) else node

        return None

    def run(self):
        manifest_ok = False
        try:
            self.progress(0.0, "Verifying…", phase="Verifying")
            self.log("Verifying files...", "acct")
            src = _download_source()
            if src is None:
                raise RuntimeError("No download source configured.")
            req = urllib.request.Request(
                src.manifest_url, headers={"User-Agent": UA}
            )
            with secure_urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r:
                manifest = json.load(r)
            manifest_ok = True
            self.log_q.put((markers.MANIFEST_AVAILABLE, ""))
            self.progress(0.0, "Verifying…", phase="Verifying")

            stale_nodes = [
                c
                for child in manifest["root"].get("files", [])
                if (c := self._traverse(child, [])) is not None
            ]

            # The bar is reserved for the actual download of the files that
            # need updating; verification only reports its phase, never a 0→100
            # sweep over the whole client.
            self.progress(0.0, "", phase="Verified")
            save_cache(self._cache)

            # Config.wtf isn't part of the manifest — it's user game config.
            # Create it when missing, or overwrite it when the user
            # committed to this folder.
            cfg_wtf = os.path.join(self.out_dir, "WTF", "Config.wtf")
            if self.overwrite_config or not os.path.exists(cfg_wtf):
                write_config_wtf(self.out_dir)

            if stale_nodes:
                self.log("Update available.", "acct")
                self.log_q.put((markers.UPDATE_NEEDED, ""))
                self.log_q.put((markers.DIFF_TREE, stale_nodes))
            else:
                self.log("Everything is up to date!", "ok")
                self.log_q.put((markers.UP_TO_DATE, ""))
        except Exception as e:
            self.log(f"Verification failed: {e}", "err")
            # A failed manifest fetch must not masquerade as "update needed":
            # the controller uses __MANIFEST_UNAVAILABLE__ to gray out the
            # update button. Failures *after* the manifest parsed are a
            # genuine "update needed" verdict.
            if not manifest_ok:
                if self._torrent_verify(src):
                    return
                self.log_q.put((markers.MANIFEST_UNAVAILABLE, ""))
                if torrent_recovery_available():
                    self.log(
                        "Manifest unavailable — a full re-download via "
                        "BitTorrent is available (UPDATE).",
                        "dim",
                    )
            else:
                self.log_q.put((markers.UPDATE_NEEDED, ""))
            self.log_q.put((markers.DIFF_TREE, None))

    def _torrent_verify(self, src) -> bool:
        """When no manifest is available, verify the client against the
        torrent's piece hashes (libtorrent recheck) and report the stale
        files. Returns True when the torrent check ran and its verdict was
        posted; False when it's not possible and the caller should fall back
        to the plain manifest-unavailable path.

        The cached validation record is identity-aware: a verdict is never
        reused for a snapshot whose content/info hash differs, and resume
        data for a replaced snapshot is discarded. Explicit verification
        always rechecks the on-disk files (the client may have drifted since
        the last verify even when the remote snapshot is unchanged), so the
        cached record only seeds identity/resume cleanup, never the reported
        stale list.

        Posting:
        * reachable snapshot with stale files → ``__TORRENT_REACHABLE__``
          + ``__TORRENT_DIFF__``
        * reachable snapshot, nothing stale → ``__TORRENT_REACHABLE__``
          + ``__TORRENT_UP_TO_DATE__``
        * snapshot cannot be fetched (network/TLS/allowlist) →
          ``__TORRENT_UNREACHABLE__``
        * snapshot fetched but libtorrent recheck failed →
          ``__TORRENT_VERIFY_FAILED__``
        * snapshot corrupt → ``__TORRENT_CORRUPT__``
        * verification stalled → ``__TORRENT_STALLED__``
        * session error → ``__TORRENT_SESSION_ERROR__``
        * disk I/O error → ``__TORRENT_DISK_ERROR__``"""
        if src is None or not src.torrent_url:
            return False
        if not _torrent_available():
            return False
        from .torrent_update import (
            TorrentCorruptError,
            TorrentDiskError,
            TorrentFetchError,
            TorrentSessionError,
            TorrentStalledError,
            TorrentVerifier,
            _fetch_torrent,
            remove_resume_data,
        )

        cached = self._cache.get(TORRENT_VALIDATION_CACHE_KEY)

        # When a previous verify established a record for this exact URL + game
        # folder, fetch the .torrent once (cheap) to detect snapshot
        # replacement: if the info hash differs, the old resume data is
        # discarded. The libtorrent recheck itself always runs afterwards —
        # explicit verification never trusts a cached verdict.
        if (
            isinstance(cached, dict)
            and cached.get("url") == src.torrent_url
            and cached.get("out_dir") == os.path.abspath(self.out_dir)
        ):
            try:
                snapshot = _fetch_torrent(src.torrent_url, self.log)
            except TorrentCorruptError as e:
                if self._cancel:
                    return self._cancel_torrent_verify()
                self.log(f"Torrent file corrupt: {e}", "err")
                self.log(
                    "No usable torrent snapshot — update unavailable.",
                    "err",
                )
                self.log_q.put((markers.TORRENT_CORRUPT, str(e)))
                return True
            except TorrentFetchError as e:
                if self._cancel:
                    return self._cancel_torrent_verify()
                self.log(f"BitTorrent snapshot unreachable: {e}", "err")
                self.log(
                    "No manifest and no reachable torrent — update "
                    "unavailable via BitTorrent.",
                    "err",
                )
                self.log_q.put((markers.TORRENT_UNREACHABLE, str(e)))
                return True

            identity: dict = {
                "content_hash": snapshot.content_hash,
                "info_hash": snapshot.info_hash or "",
            }
            old_hash = cached.get("info_hash")
            new_hash = identity.get("info_hash")
            if old_hash and new_hash and old_hash != new_hash:
                remove_resume_data(old_hash)
                self.log(
                    "[torrent] Snapshot changed at URL — discarding old "
                    "validation state and resume data.",
                    "dim",
                )
            elif old_hash and not new_hash:
                self.log(
                    "[torrent] Snapshot identity unavailable — validation "
                    "state cleared.",
                    "dim",
                )
            # Explicit verification never trusts a cached verdict: even when the
            # snapshot is unchanged since the last verify, the on-disk client
            # may have drifted, so the libtorrent recheck always runs. The
            # identity/resume-data cleanup above still applies.

        # Snapshot changed, uncached, or fetch skipped — run the full
        # libtorrent recheck of the on-disk files.
        try:
            verifier = TorrentVerifier(self.out_dir, self.log_q, self.prog_q)
            self.log(
                "Manifest unavailable — verifying client against the "
                "BitTorrent snapshot…",
                "acct",
            )
            stale = verifier.verify(src.torrent_url)
        except TorrentCorruptError as e:
            if self._cancel:
                return self._cancel_torrent_verify()
            self.log(f"Torrent file corrupt: {e}", "err")
            self.log(
                "No usable torrent snapshot — update unavailable.",
                "err",
            )
            self.log_q.put((markers.TORRENT_CORRUPT, str(e)))
            return True
        except TorrentFetchError as e:
            if self._cancel:
                return self._cancel_torrent_verify()
            self.log(f"BitTorrent snapshot unreachable: {e}", "err")
            self.log(
                "No manifest and no reachable torrent — update "
                "unavailable via BitTorrent.",
                "err",
            )
            self.log_q.put((markers.TORRENT_UNREACHABLE, str(e)))
            return True
        except TorrentStalledError as e:
            if self._cancel:
                return self._cancel_torrent_verify()
            self.log(f"BitTorrent verification stalled: {e}", "err")
            self.log(
                "No manifest and torrent verification stalled — "
                "update unavailable.",
                "err",
            )
            self.log_q.put((markers.TORRENT_STALLED, str(e)))
            return True
        except TorrentSessionError as e:
            if self._cancel:
                return self._cancel_torrent_verify()
            self.log(f"BitTorrent session error: {e}", "err")
            self.log(
                "No manifest and torrent session error — update unavailable.",
                "err",
            )
            self.log_q.put((markers.TORRENT_SESSION_ERROR, str(e)))
            return True
        except TorrentDiskError as e:
            if self._cancel:
                return self._cancel_torrent_verify()
            self.log(f"Disk I/O error: {e}", "err")
            self.log(
                "No manifest and disk error — update unavailable.",
                "err",
            )
            self.log_q.put((markers.TORRENT_DISK_ERROR, str(e)))
            return True
        except Exception as e:
            if self._cancel:
                return self._cancel_torrent_verify()
            self.log(f"BitTorrent verification failed: {e}", "err")
            self.log(
                "Torrent snapshot fetched but verification failed — "
                "update unavailable via BitTorrent.",
                "err",
            )
            self.log_q.put((markers.TORRENT_VERIFY_FAILED, str(e)))
            return True

        snapshot = getattr(verifier, "snapshot", None)
        identity = {}
        if snapshot is not None:
            identity = {
                "content_hash": snapshot.content_hash,
                "info_hash": snapshot.info_hash or "",
            }
        self._cache[TORRENT_VALIDATION_CACHE_KEY] = {
            **identity,
            "url": src.torrent_url,
            "out_dir": os.path.abspath(self.out_dir),
            "stale": sorted(stale),
        }
        save_cache(self._cache)
        self.log("[torrent] Validation verdict cached.", "dim")
        self._post_torrent_verdict(stale)
        return True

    def _post_torrent_verdict(self, stale: list[str]):
        self.log_q.put((markers.TORRENT_REACHABLE, ""))
        if not stale:
            self.log("Everything is up to date (BitTorrent snapshot).", "ok")
            self.log_q.put((markers.TORRENT_UP_TO_DATE, ""))
        else:
            self.log(
                f"Update available — {len(stale)} stale file(s) vs the "
                "BitTorrent snapshot.",
                "acct",
            )
            self.log_q.put((markers.TORRENT_DIFF, sorted(stale)))

    def _cancel_torrent_verify(self) -> bool:
        """Finish a cancelled torrent verify without reporting a failure."""
        self.log("\nVerify cancelled.", "err")
        self.log_q.put((markers.ERROR, ""))
        return True


class UpdateWorker:
    def __init__(
        self,
        out_dir: str,
        log_q: queue.Queue,
        prog_q: queue.Queue,
    ):
        self.out_dir = out_dir
        self.log_q = log_q
        self.prog_q = prog_q
        self._cancel = False
        self._cache: dict = load_cache()
        self._source: DownloadSource | None = None
        # Total bytes of the files that actually need downloading, and how many
        # have been fetched so far. The update progress bar spans 0→100 across
        # exactly these (the files that need updating), not the whole client.
        self._total = 0
        self._downloaded = 0
        # Bytes already counted toward ``_downloaded`` per destination, so a
        # hash-mismatch retry (which re-downloads the same file) isn't double
        # counted.
        self._counted: dict = {}
        # Relative paths the torrent backend delivered in the last bulk
        # download (empty when HTTP was used or no manifest tree exists).
        self._torrent_wanted: set[str] = set()

    def cancel(self):
        self._cancel = True

    def log(self, msg: str, tag: str = ""):
        self.log_q.put((msg, tag))

    def progress(self, value: float, label: str = "", **details):
        item = (value, label, details) if details else (value, label)
        self.prog_q.put(item)

    def download(self, url, dest, size, name=""):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        name = name or os.path.basename(dest)
        total_str = fmt_size(size) if size else "?"

        for attempt in range(1, DOWNLOAD_RETRY + 1):
            if self._cancel:
                raise RuntimeError("Cancelled")
            try:
                # Resume a previous partial download when one is present.
                got = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                if size and got >= size:
                    os.remove(tmp)  # oversized/stale leftover — start clean
                    got = 0

                headers = {"User-Agent": UA}
                mode = "wb"
                if got:
                    headers["Range"] = f"bytes={got}-"
                    mode = "ab"
                    self.log(f"  Resuming ({fmt_size(got)} / {total_str})…")
                else:
                    self.log(f"  Downloading ({total_str})…")

                req = urllib.request.Request(url, headers=headers)
                downloaded = got
                # Hash on the fly when starting from byte 0 — saves a full
                # re-read of the file for verification. A resumed download
                # can't be hashed incrementally (the prefix wasn't seen).
                hasher = hashlib.sha1() if not got else None
                # Speed sampling over a short sliding window.
                t0 = time.monotonic()
                bytes_at_t0 = downloaded
                speed_str = ""
                with secure_urlopen(
                    req,
                    timeout=DOWNLOAD_TIMEOUT,
                    allowed_hosts=allowed_download_hosts(),
                ) as r:
                    status = getattr(r, "status", None) or r.getcode()
                    if got and status != 206:
                        # Server ignored the Range header — start over.
                        downloaded, mode = 0, "wb"
                        hasher = hashlib.sha1()
                        bytes_at_t0 = 0
                    with open(tmp, mode) as f:
                        while True:
                            if self._cancel:
                                raise RuntimeError("Cancelled")
                            chunk = r.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            if hasher is not None:
                                hasher.update(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            dt = now - t0
                            if dt >= 0.5:
                                speed_str = "   •   " + fmt_speed(
                                    (downloaded - bytes_at_t0) / dt
                                )
                                t0, bytes_at_t0 = now, downloaded
                            if size:
                                if self._total:
                                    agg = self._downloaded + downloaded
                                    self.progress(
                                        agg / self._total,
                                        f"{name}   •   "
                                        f"{fmt_size(downloaded)}"
                                        f" / {total_str}{speed_str}",
                                        phase="Downloading",
                                        transport="HTTP",
                                        current_file=name,
                                        downloaded=agg,
                                        total=self._total,
                                        speed=(downloaded - bytes_at_t0) / dt
                                        if dt > 0
                                        else 0.0,
                                    )
                                else:
                                    self.progress(
                                        downloaded / size,
                                        f"{name}   •   "
                                        f"{fmt_size(downloaded)}"
                                        f" / {total_str}{speed_str}",
                                        phase="Downloading",
                                        transport="HTTP",
                                        current_file=name,
                                        downloaded=downloaded,
                                        total=size,
                                        speed=(downloaded - bytes_at_t0) / dt
                                        if dt > 0
                                        else 0.0,
                                    )

                # A dropped connection looks like a clean EOF — never accept
                # a short file as a finished download.
                if size and downloaded != size:
                    raise OSError(
                        "connection lost at "
                        f"{fmt_size(downloaded)} / {total_str}"
                    )

                shutil.move(tmp, dest)
                if self._total:
                    prev = self._counted.get(dest, 0)
                    self._counted[dest] = size
                    self._downloaded += size - prev
                    self.progress(
                        min(1.0, self._downloaded / self._total),
                        "Downloading…",
                        phase="Downloading",
                        transport="HTTP",
                        downloaded=self._downloaded,
                        total=self._total,
                    )
                if hasher is not None:
                    digest = hasher.hexdigest().upper()
                    try:
                        # Seed the verify cache so the next verify pass
                        # doesn't need to rehash this file either.
                        self._cache[dest] = [digest, os.path.getmtime(dest)]
                    except OSError:
                        self._cache.pop(dest, None)
                    return digest
                self._cache.pop(dest, None)
                return None
            except Exception as e:
                if self._cancel:
                    raise RuntimeError("Cancelled") from None
                # Keep tmp — the next attempt resumes from where this one
                # stopped instead of redownloading from zero.
                self.log(f"  Attempt {attempt} failed: {e}", "err")
                if attempt < DOWNLOAD_RETRY:
                    wait = min(2**attempt, 10)
                    part = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                    self.progress(
                        (part / size) if size else 0.0,
                        f"{name} — retrying ({attempt}/{DOWNLOAD_RETRY})…",
                        phase="Retrying",
                        transport="HTTP",
                        current_file=name,
                        downloaded=part,
                        total=size,
                    )
                    self.log(f"  Retrying in {wait} s…", "dim")
                    time.sleep(wait)
        raise RuntimeError(
            f"Download failed after {DOWNLOAD_RETRY} attempts: {url}"
        )

    def _skip_download(self, node, dest) -> bool:
        """Whether a file node can be skipped because the local copy already
        matches the manifest."""
        if not os.path.exists(dest):
            return False
        return cached_sha1(dest, self._cache) == node["hash"]

    def _torrent_download(self, nodes) -> bool:
        """Bulk-download the stale files via BitTorrent when the active source
        advertises a ``torrent_url`` and libtorrent is available. Returns True
        when the torrent backend ran; the delivered paths are remembered in
        ``_torrent_wanted`` for the caller's manifest SHA-1 re-check
        (``_reverify_torrent_files``). Returns False when the torrent backend
        was not used (no ``torrent_url``, libtorrent missing, or the download
        failed), in which case the caller falls back to ``traverse()`` for
        HTTP downloads with manifest re-verification."""
        src = self._source
        if src is None or not src.torrent_url:
            return False
        if not _torrent_available():
            self.log("  libtorrent not available — using HTTP.", "dim")
            return False
        wanted: set[str] = set()
        if nodes is not None:
            for child in nodes:
                self._collect_wanted(child, [], wanted)
        if not wanted:
            return False
        self.log(
            f"\n[torrent] Downloading {len(wanted)} stale file(s): "
            f"{', '.join(sorted(wanted))}"
            "\n",
            "acct",
        )
        try:
            from .torrent_update import TorrentDownloader

            dl = TorrentDownloader(self.out_dir, self.log_q, self.prog_q)
            dl.download(src.torrent_url, wanted)
            self._torrent_wanted = wanted
            self.log("[torrent] BitTorrent download complete.", "ok")
            return True
        except RuntimeError as e:
            if self._cancel:
                raise
            self.log(f"[torrent] BitTorrent download failed: {e}", "err")
            self.log("[torrent] Falling back to HTTP downloads.", "err")
            return False

    def _collect_wanted(self, node, path_parts, wanted):
        """Collect the relative paths of stale file/mpq nodes for the torrent
        backend, reusing the same up-to-date checks as `traverse`."""
        if self._cancel:
            return
        t = node["type"]
        cur = path_parts + [node["name"]]
        if t == "dir":
            for child in node.get("files", []):
                self._collect_wanted(child, cur, wanted)
        elif t == "file":
            rel = "/".join(cur)
            dest = os.path.join(self.out_dir, rel)
            if not self._skip_download(node, dest):
                wanted.add(rel)
        elif t == "mpq":
            rel = "/".join(cur) + ".mpq"
            dest = os.path.join(self.out_dir, rel)
            if not self._skip_download(node, dest):
                wanted.add(rel)

    def _reverify_torrent_files(self, nodes):
        """SHA-1 re-check of exactly the files BitTorrent delivered, against
        the manifest's hashes. Anything missing or mismatched is re-fetched
        over HTTPS with ``download``'s resume/retry, so a torrent bulk
        download cannot weaken the manifest's integrity guarantee. No-op when
        no manifest tree exists (the recovery path — there the torrent's
        piece hashes, received over TLS, are the only guarantee)."""
        wanted = self._torrent_wanted
        if not wanted:
            return

        suspects: list[tuple[dict, str]] = []

        def walk(node, cur):
            if self._cancel:
                return
            t = node["type"]
            if t == "dir":
                for child in node.get("files", []):
                    walk(child, cur + [node["name"]])
            elif t == "file":
                rel = "/".join(cur + [node["name"]])
                if rel in wanted:
                    suspects.append((node, rel))
            elif t == "mpq":
                rel = "/".join(cur + [node["name"]]) + ".mpq"
                if rel in wanted:
                    suspects.append((node, rel))

        for child in nodes:
            walk(child, [])

        self._total = sum(node["size"] for node, _ in suspects)
        self._downloaded = 0
        self.log(
            f"[torrent] Re-verifying {len(suspects)} file(s) against "
            "the manifest…",
            "acct",
        )
        src = self._source
        for node, rel in suspects:
            dest = os.path.join(self.out_dir, rel)
            if self._skip_download(node, dest):
                continue
            url = f"{src.client_url}/{rel}"
            got_hash = self.download(url, dest, node["size"], rel)
            if (got_hash or sha1_file(dest)) != node["hash"]:
                raise RuntimeError(
                    f"Hash mismatch after torrent download: {rel}"
                )
        self.log("[torrent] All files verified against manifest.", "ok")
        self._torrent_wanted = set()

    def _sum_needed_bytes(self, nodes) -> int:
        """Total bytes of the files that actually need downloading (those not
        already matching the manifest), so the update progress bar can span
        0→100 across exactly the files that need updating — not the whole
        client."""
        total = 0

        def walk(node, path_parts):
            nonlocal total
            if self._cancel:
                return
            t = node["type"]
            cur = path_parts + [node["name"]]
            if t == "dir":
                for child in node.get("files", []):
                    walk(child, cur)
            elif t == "file":
                dest = os.path.join(self.out_dir, os.path.join(*cur))
                if not self._skip_download(node, dest):
                    total += node["size"]
            elif t == "mpq":
                dest = os.path.join(
                    self.out_dir, os.path.join(*cur, node["name"] + ".mpq")
                )
                if not self._skip_download(node, dest):
                    total += node["size"]

        for n in nodes:
            walk(n, [])
        return total

    def traverse(self, node, path_parts):

        if self._cancel:
            return
        if self._source is None:
            self._source = _download_source()
        src = self._source
        if src is None:
            raise RuntimeError("No download source configured.")
        t = node["type"]
        name = node["name"]
        cur = path_parts + [name]

        rel = os.path.join(*cur)
        dest = os.path.join(self.out_dir, rel)

        if t == "dir":
            for child in node.get("files", []):
                self.traverse(child, cur)

        elif t == "file":
            self.log(f"[file] {rel}", "acct")
            url = f"{src.client_url}/{'/'.join(cur)}"

            if self._skip_download(node, dest):
                self.log("  Already up to date.", "dim")
                return

            got_hash = self.download(url, dest, node["size"], rel)
            if (got_hash or sha1_file(dest)) != node["hash"]:
                self.log("  Hash mismatch — retrying", "err")
                os.remove(dest)
                got_hash = self.download(url, dest, node["size"], rel)
                if (got_hash or sha1_file(dest)) != node["hash"]:
                    raise RuntimeError(
                        f"Hash mismatch after redownload: {rel}"
                    )

        elif t == "mpq":
            mpq_name = name + ".mpq"
            cur_mpq = path_parts + [mpq_name]
            rel = os.path.join(*cur_mpq)
            dest = os.path.join(self.out_dir, rel)
            url = f"{src.client_url}/{'/'.join(cur_mpq)}"
            self.log(f"[mpq]  {rel}", "acct")
            if self._skip_download(node, dest):
                self.log("  Already up to date.", "dim")
                return
            got_hash = self.download(url, dest, node["size"], rel)
            if (got_hash or sha1_file(dest)) != node["hash"]:
                self.log("  Hash mismatch — retrying", "err")
                os.remove(dest)
                got_hash = self.download(url, dest, node["size"], rel)
                if (got_hash or sha1_file(dest)) != node["hash"]:
                    raise RuntimeError(
                        f"Hash mismatch after redownload: {rel}"
                    )

        elif t == "del":
            self.log(f"[del]  {rel}", "dim")
            if os.path.exists(dest):
                os.remove(dest)

    def _recovery_download(self, wanted: set[str] | None = None):
        """Manifest-less recovery: download the client via BitTorrent.

        ``wanted`` is the set of stale file paths (from a prior torrent
        verify) to download; None means the whole torrent. The files are
        verified against the torrent's embedded piece hashes (the ``.torrent``
        itself arrived over TLS); there is no per-file manifest SHA-1 to check
        against in this degraded path. Raises on failure/cancellation, which
        the caller's except block turns into an ``__ERROR__``."""
        from .torrent_update import (
            TorrentCorruptError,
            TorrentDiskError,
            TorrentDownloader,
            TorrentFetchError,
            TorrentSessionError,
            TorrentStalledError,
        )

        dl = TorrentDownloader(self.out_dir, self.log_q, self.prog_q)
        scope = "full client" if wanted is None else f"{len(wanted)} file(s)"
        self.log(f"[torrent] Starting recovery download ({scope}).", "acct")
        try:
            dl.download(self._source.torrent_url, wanted)
        except TorrentCorruptError as e:
            if self._cancel:
                raise
            self.log(f"Torrent file corrupt: {e}", "err")
            self.log_q.put((markers.TORRENT_CORRUPT, str(e)))
            return
        except TorrentStalledError as e:
            if self._cancel:
                raise
            self.log(f"BitTorrent download stalled: {e}", "err")
            self.log_q.put((markers.TORRENT_STALLED, str(e)))
            return
        except TorrentSessionError as e:
            if self._cancel:
                raise
            self.log(f"BitTorrent session error: {e}", "err")
            self.log_q.put((markers.TORRENT_SESSION_ERROR, str(e)))
            return
        except TorrentDiskError as e:
            if self._cancel:
                raise
            self.log(f"Disk I/O error: {e}", "err")
            self.log_q.put((markers.TORRENT_DISK_ERROR, str(e)))
            return
        except TorrentFetchError as e:
            if self._cancel:
                raise
            self.log(f"Torrent unreachable during download: {e}", "err")
            self.log_q.put((markers.TORRENT_UNREACHABLE, str(e)))
            return
        except Exception as e:
            if self._cancel:
                raise
            self.log(f"BitTorrent download failed: {e}", "err")
            self.log_q.put((markers.TORRENT_VERIFY_FAILED, str(e)))
            return
        if self._cancel:
            self.log("\nUpdate cancelled.", "err")
            self.progress(0.0, "Cancelled")
            self.log_q.put((markers.ERROR, ""))
            return
        # The stale set came from an earlier snapshot; if any wanted file is
        # still missing after a selective download, fall back to the whole
        # torrent so a snapshot change can't leave the client half-installed.
        if wanted is not None:
            missing = sorted(
                rel
                for rel in wanted
                if not os.path.isfile(
                    os.path.join(self.out_dir, rel.replace("/", os.sep))
                )
            )
            if missing:
                self.log(
                    "[torrent] Recovery incomplete — re-downloading the "
                    f"full client ({len(missing)} file(s) missing).",
                    "err",
                )
                try:
                    dl.download(self._source.torrent_url, None)
                except (RuntimeError, OSError) as e:
                    if self._cancel:
                        raise
                    self.log(f"BitTorrent recovery failed: {e}", "err")
                    self.log_q.put((markers.TORRENT_VERIFY_FAILED, str(e)))
                    return
                if self._cancel:
                    self.log("\nUpdate cancelled.", "err")
                    self.progress(0.0, "Cancelled")
                    self.log_q.put((markers.ERROR, ""))
                    return
        # A recovered client without WoW.exe is useless — never mark ready.
        exe = os.path.join(self.out_dir, "WoW.exe")
        if not os.path.isfile(exe):
            self.log("Recovered client has no WoW.exe — update failed.", "err")
            self.log_q.put((markers.ERROR, ""))
            return
        self.log("  BitTorrent recovery download complete.", "ok")
        remove_wdb(self.out_dir)
        # A fresh recovery install has no Config.wtf — create it (a regular
        # update never touches user config, but this path has no verify step
        # to seed it).
        cfg_wtf = os.path.join(self.out_dir, "WTF", "Config.wtf")
        if not os.path.exists(cfg_wtf):
            write_config_wtf(self.out_dir)
        self.progress(1.0, "")
        snapshot = getattr(dl, "snapshot", None)
        identity: dict = {}
        if snapshot is not None:
            identity = {
                "content_hash": snapshot.content_hash,
                "info_hash": snapshot.info_hash or "",
            }
        self._cache[TORRENT_VALIDATION_CACHE_KEY] = {
            **identity,
            "url": self._source.torrent_url,
            "out_dir": os.path.abspath(self.out_dir),
            "stale": [],
        }
        save_cache(self._cache)
        self.log("[torrent] Recovery validation cached.", "dim")
        self.log(
            "\n✓  Client installed via BitTorrent (no manifest — files "
            "verified against the torrent's piece hashes).",
            "ok",
        )
        client_ver = get_client_version(self.out_dir)
        if client_ver:
            self.log(f"Client version: {client_ver}", "dim")
            self.log_q.put((markers.VERSION_PREFIX + client_ver, ""))
        else:
            self.log("Could not read client version from WoW.exe", "dim")
        self.log_q.put((markers.TORRENT_RECOVERY_DONE, ""))

    def run(self, diff_nodes=None, torrent_wanted: set[str] | None = None):

        try:
            torrent_recovery = False
            if diff_nodes is not None:
                self.log("\nStarting client update…\n")
                self.progress(0.05, "Downloading…", phase="Downloading")
                self._cache.pop(TORRENT_VALIDATION_CACHE_KEY, None)
                nodes = diff_nodes
            elif torrent_wanted is not None:
                # A prior torrent verification already established the stale
                # paths. Do not probe the manifest again: this is explicitly
                # the manifest-less BitTorrent update path.
                if not torrent_wanted:
                    self.log(
                        "[torrent] No stale files; torrent update skipped.",
                        "ok",
                    )
                    self.progress(1.0, "")
                    self.log_q.put((markers.TORRENT_UP_TO_DATE, ""))
                    return
                self.progress(
                    0.02,
                    "Downloading via BitTorrent…",
                    phase="BitTorrent",
                    transport="BitTorrent",
                )
                self.log("\nStarting BitTorrent update…\n", "acct")
                self._source = _download_source()
                if self._source is None or not self._source.torrent_url:
                    raise RuntimeError("No BitTorrent source configured.")
                self._recovery_download(torrent_wanted)
                return
            else:
                self.progress(
                    0.02, "Fetching manifest…", phase="Fetching manifest"
                )
                self.log("Fetching manifest.json…")
                self._source = _download_source()
                if self._source is None:
                    raise RuntimeError("No download source configured.")
                try:
                    req = urllib.request.Request(
                        self._source.manifest_url,
                        headers={"User-Agent": UA},
                    )
                    with secure_urlopen(
                        req,
                        timeout=DOWNLOAD_TIMEOUT,
                        allowed_hosts=allowed_download_hosts(),
                    ) as r:
                        manifest = json.load(r)
                except Exception:
                    # Manifest unavailable — fall back to a BitTorrent
                    # recovery download instead of failing outright.
                    if self._source.torrent_url and _torrent_available():
                        self.log(
                            "\nManifest unavailable — downloading the client "
                            "via BitTorrent…",
                            "acct",
                        )
                        torrent_recovery = True
                    else:
                        raise
                if not torrent_recovery:
                    self._cache.pop(TORRENT_VALIDATION_CACHE_KEY, None)
                    self.log_q.put((markers.MANIFEST_AVAILABLE, ""))
                    self.log("Manifest received.", "ok")
                    self.progress(0.05, "Downloading…", phase="Downloading")
                    self.log("\nStarting client update…\n")
                    nodes = manifest["root"].get("files", [])

            if torrent_recovery:
                self._recovery_download(torrent_wanted)
                return

            if self._source is None:
                self._source = _download_source()
            if self._source is None:
                raise RuntimeError("No download source configured.")
            ran_torrent = self._torrent_download(nodes)
            if ran_torrent:
                # Piece hashes prove what peers sent; when a manifest exists
                # its SHA-1s remain the final word — re-check just the
                # delivered files and HTTP-refetch any mismatch.
                self._reverify_torrent_files(nodes)
            else:
                # The BitTorrent backend didn't fetch the files, so fall back
                # to the per-file HTTP download (which re-verifies each file
                # against the manifest). The update progress bar spans 0→100
                # across exactly the files that need updating.
                self._total = self._sum_needed_bytes(nodes)
                self._downloaded = 0
                for child in nodes:
                    self.traverse(child, [])

            if self._cancel:
                self.log("\nUpdate cancelled.", "err")
                self.progress(0.0, "Cancelled")
                self.log_q.put((markers.ERROR, ""))
                return

            self.log("\nDownload complete.", "ok")
            remove_wdb(self.out_dir)

            self.progress(1.0, "")
            save_cache(self._cache)
            self.log("\n✓  Everything is up to date!", "ok")
            client_ver = get_client_version(self.out_dir)
            if client_ver:
                self.log(f"Client version: {client_ver}", "dim")
                self.log_q.put((markers.VERSION_PREFIX + client_ver, ""))
            else:
                self.log("Could not read client version from WoW.exe", "dim")
            self.log_q.put((markers.DONE, ""))

        except Exception as e:
            self.log(f"\n✗  {e}", "err")
            self.progress(0.0, "")
            self.log_q.put((markers.ERROR, ""))
