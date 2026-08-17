# Vanilla WoW Launcher — Developer Guide

Technical documentation for developers, server operators, and packagers. For
end-user installation and usage, see the [README](../README.md).

## Overview

Vanilla WoW Launcher is a PySide6 desktop app (updater + mod manager for the
Vanilla WoW client). Runtime dependencies are **PySide6** (GUI) and
**libtorrent** (the BitTorrent backend for client updates). libtorrent is
imported lazily — the client-update path degrades to per-file HTTP downloads
when it isn't installed, so tests never need it. Business logic is otherwise
pure stdlib.

### Source layout (`src/` layout)

```
src/vanilla_wow_launcher/
  cli.py          # entry point: config wiring + window loop
  core/           # constants, config_store, launcher, security_http, filesystem, helpers, log_sink, platform_support, errors
  services/       # catalog, addons, mods, news, tweaks, update_backend, self_update
  controllers/    # update, news, mods, addons, settings, tweaks (toolkit-agnostic)
  state/          # models.py (state dataclasses), events.py (dispatcher)
  ui/qt/          # app, main_window, bridge, theme, panels, dialogs
```

### Architecture rules

- **Controllers are toolkit-agnostic**: they post dataclass *events* to a
  shared `EventDispatcher` (`state/events.py`) from worker threads; they never
  touch widgets. The Qt side (`ui/qt/bridge.py`) converts events to Qt signals
  on the main thread.
- Inside the package use **relative** imports; tests import via
  `vanilla_wow_launcher.*` absolute paths. Tests monkeypatch by dotted path
  with the FULL package name (e.g.
  `vanilla_wow_launcher.ui.qt.addons_panel.QMessageBox.question`), not the bare
  module name.
- **There are no hardcoded server/mod/addon values.** Everything is configured
  by `core/launcher.py` reading `vanilla_wow_launcher.json`.
- The launcher never binary-patches `WoW.exe` — runtime client fixes are left
  to the VanillaFixes loader mod. The only tweak channel is `Config.wtf`.

## Launcher Configuration

Everything the app talks to (client updates, news, mod/addon catalogs, realm,
downloads, torrents) comes from a single JSON file, so a distribution only
needs to ship one file to point the launcher at its own server.

The file is `vanilla_wow_launcher.json`, discovered next to the executable
(frozen) or in the repo root (running from source), or passed explicitly via
`--launcher-config`. A configuration chosen through the first-launch wizard is
persisted into the per-user config directory and takes precedence over
auto-discovery on later runs.

Only `server.base_url` is required; every other URL is derived from it unless
overridden:

```json
{
  "server": {
    "name": "My Server",
    "base_url": "https://server.example",
    "realm": "server.example",
    "manifest_url": "https://server.example/api/file/latest/manifest.json",
    "client_url": "https://server.example/client/latest",
    "torrent_url": "https://server.example/client/latest/client.torrent",
    "news_url": "https://server.example/news",
    "featured_news_url": "https://server.example/news/featured",
    "mods_registry_url": "https://server.example/api/mods.json",
    "addons_registry_url": "https://server.example/api/addons.json",
    "addons_registry_urls": [
      "https://server.example/api/addons.json",
      "https://server.example/addons-overrides.json"
    ]
  },
  "discord_url": "https://discord.gg/example",
  "theme": {
    "C_GOLD": "#d4a02f",
    "logo": "https://server.example/logo.png"
  },
  "mirrors": [
    {
      "name": "Backup",
      "base_url": "https://mirror.example",
      "manifest_url": "https://mirror.example/api/file/latest/manifest.json",
      "client_url": "https://dl.mirror.example/client/latest",
      "torrent_url": "https://dl.mirror.example/client/latest/client.torrent"
    }
  ]
}
```

Key points:

- The manifest and client files come from the configured endpoints; a mirror's
  `client_url` may point at a separate CDN host. Mirrors are optional (the
  server is the fallback).
- The optional `torrent_url` (on the server or a mirror) advertises a
  BitTorrent snapshot of the client files. A mirror's `torrent_url` takes
  precedence over the server's.
- The optional `theme` object overrides the app's color theme per server:
  color slots named like `C_GOLD` (each a `#rrggbb` hex) plus an optional
  `logo` URL shown as the header wordmark. It is cosmetic and never validated
  strictly — a malformed theme falls back to the default palette.
- Endpoint URLs must be HTTPS (non-HTTPS or missing-host URLs are rejected).
- Missing or invalid configuration with no `--launcher-config` opens a modal
  first-launch wizard; an explicit `--launcher-config` that is missing or
  invalid is a hard `cli.main()` error (no wizard). The wizard validates via
  `launcher.validate_path()` (no global-state side effect) and persists the
  selection to `launcher.user_config_path()` via `launcher.persist()`.
- The download-host allowlist (`security_http.allowed_download_hosts()`) is
  built from the launcher's server + mirror hosts plus the git hosts.

## Client Update Pipeline

The client-update engine lives in `services/update_backend/http_update.py`.
The shared backend package also defines the lightweight `UpdateBackend`
protocol in `services/update_backend/protocol.py`:

1. **Verify / diff**: `VerifyWorker` fetches the manifest from the selected
   download source and reports which files differ against the local SHA-1
   cache.
2. **Torrent bulk-download**: when the active source advertises a
   `torrent_url` and libtorrent is importable, `UpdateWorker` bulk-downloads
   the stale files via `services/update_backend/torrent_update.py` before its per-file HTTP
   `traverse()`. Per-file priorities keep only the pieces covering stale files
   downloading (`wanted` set).
3. **HTTP resume + re-verify**: `traverse()` still re-verifies every file
   against the manifest's SHA-1 and HTTP-resumes anything the torrent missed.
4. **Recovery**: when the manifest itself can't be fetched but the active
   source advertises a `torrent_url` and libtorrent is present,
   `UpdateWorker._recovery_download()` downloads the *whole* torrent
   (`TorrentDownloader.download(url, None)`). The torrent's piece hashes (the
   `.torrent` arrived over TLS) stand in for the manifest's per-file SHA-1. It
   posts `__TORRENT_RECOVERY_DONE__` (the controller keeps
   `manifest_available=False`). A failed verify offers this via an enabled
   UPDATE button when `torrent_recovery_available()`
   (`LauncherConfig.has_torrent()` + libtorrent present, network-free) and the
   client isn't known-ready.

### BitTorrent backend (`services/update_backend/torrent_update.py`)

- The `.torrent` is fetched over HTTPS through the same hardened, allowlisted
  transport as the HTTP downloads.
- Peers in the swarm are untrusted — a malicious peer can only inject data
  that fails the piece hashes embedded in the `.torrent`. The caller still
  re-verifies every file against the manifest's SHA-1 afterwards, so the
  torrent backend cannot weaken the integrity guarantee of the HTTP path.
- **Torrent root auto-detection**: the launcher locates the unique `WoW.exe`
  file in the torrent (case-insensitive). Its parent directory is the *root*
  — all torrent paths are mapped into the selected WoW folder by stripping
  that root prefix. For OctoWoW: `client/WoW.exe` → `<wow_folder>/WoW.exe`.
  A `TorrentLayoutError` is raised when `WoW.exe` is missing, duplicated, or
  any file escapes the detected root.
- **Offline verification**: the verification session uses an empty listen
  interface and disables DHT, LSD, UPnP, NAT-PMP, and all peer connections.
  No P2P activity occurs before the user presses UPDATE. Only the download
  session enables networking.
- `download(url, wanted)` accepts `wanted=None` to mean the whole torrent
  (every file at max priority) — used by the no-manifest recovery path.
  File priorities are computed from the auto-detected root mapping.
- An inactivity guard (`STALL_TIMEOUT`) falls back to HTTP if no wanted bytes
  arrive, and `h.cancel()` on user cancellation.
- Disk/storage errors (e.g., `file_error_alert`, `storage_moved_failed_alert`,
  `save_resume_data_failed_alert`, `read_piece_alert`) are detected via
  libtorrent's storage notification category and raised as `TorrentDiskError`
  so the caller can fall back to HTTP. Successful storage alerts like
  `file_completed_alert` are logged but not treated as errors.
- Torrent verification uses libtorrent 2.1 status fields (`verified_pieces`
  counted via `sum()`, `state` compared against
  `checking_files`, `checking_resume_data`, `queued_for_checking`) instead of
  the deprecated `num_pieces_checked` / `verifying` fields.
- Availability probing (`available()`) is side-effect free — it only imports
  libtorrent and validates required symbols, without constructing a session or
  binding ports.
- Session configuration uses an explicit alert mask (error, storage, status,
  tracker, DHT categories). Download sessions use ephemeral listening ports
  (`0.0.0.0:0,[::]:0`) to avoid port conflicts; verification sessions use an
  empty `listen_interfaces` to disable all sockets.
- Torrent metadata fetching is hardened: streaming response with a 5 MiB size
  cap, explicit `urllib.error` handling, and temp-file write failures converted
  to `TorrentDiskError`.
- Typed exceptions distinguish failure modes:
  - `TorrentFetchError` (network/TLS/allowlist) → torrent unreachable
  - `TorrentCorruptError` (malformed .torrent) → torrent unreachable
  - `TorrentLayoutError` (missing/duplicate WoW.exe, path traversal) → torrent
    unreachable (subclass of `TorrentCorruptError`)
  - `TorrentStalledError` (no progress, includes peer count) → torrent reachable
  - `TorrentSessionError` (session/add_torrent failure) → torrent reachable
  - `TorrentDiskError` (disk I/O) → torrent reachable
  The controller uses the active operation (`verify` vs `update`) when posting
  completion events so the UI state matches the actual flow.
- Torrent state (`torrent_reachable`, `torrent_error`, `torrent_stale`) is
  cleared at the start of each verify/update attempt and when the game folder
  is invalidated, preventing stale state from leaking between attempts.

## Tweaks

The **TWEAKS** tab applies preferences via `Config.wtf` only
(`services/tweaks.py`): field of view, render distance, nameplate range,
camera distance, ground-clutter distance, and background sounds. The launcher
**never binary-patches `WoW.exe`** — runtime client fixes are left to the
VanillaFixes loader mod where installed. The `Config.wtf` writer is the only
tweak channel.

## Platform Support

- **Windows**: the client is a Windows binary launched natively; Defender
  exclusions are Windows-only.
- **Linux**: game launch goes through **umu-launcher** (`services/umu.py`).
  The PLAY button is gated on `core.platform_support.can_launch_client()`,
  which is True on Windows (native) and on Linux only when `umu.umu_available()`
  finds `umu-run` on PATH (or `~/.local/bin/umu-run`). WoW.exe runs under
  Proton in the launcher-wide `data_dir()/wineprefix`. umu settings
  (`GE-Proton` codename, binary override, GAMEID) live in the config's
  `"launch"` key.
- **macOS**: no game launch.

### One game process at a time

`umu.launch()` returns `(pid, pgid, proc)`; `UpdateController` records it in
`state.game_*`, posts `GameLaunched`, and spawns a daemon `_watch_game()`
thread that `proc.wait()`s and posts `GameExited` (clearing the running
state). `compute_readiness()` returns mode `"terminate"` while a game runs —
the footer shows an enabled red TERMINATE button (`_terminate_game()` →
`umu.kill_game()`: SIGTERM to the process group, SIGKILL after 2 s). A second
`launch_game()` while one is running is refused.

## Security Model

- Downloads use HTTPS and host restrictions derived from the selected
  configuration (`core/security_http.py`); redirects remain HTTPS-only.
- Downloaded archives are extracted with protection against path traversal.
- Settings and caches live in separate per-user dirs via
  `platform_support.config_dir()` / `cache_dir()` (never next to the exe),
  with `LEGACY_*_FILES` migration and `legacy_custom_pairs()` for the custom
  catalog files that move with the config dir.
- Configuration changes are written safely to reduce corruption after an
  interruption.

## Build, Run & Test

```bash
uv sync                          # installs the package editable + PySide6
uv run vanilla-wow-launcher      # run the app
uv run python -m vanilla_wow_launcher # equivalent
uv run pytest                    # full suite
uv run ruff format .             # pep8-style 79-col wrapping ([tool.ruff])
uv run ruff check .              # lint gate: E4/E7/E9/F/I/W/UP/B
```

- **Ruff is the only lint/format gate — there is no type checker.** `ruff
  check` selects E4/E7/E9/F/I/W/UP/B with `line-length = 79` and
  `target-version = "py310"` (see `pyproject.toml`).
- Qt widget tests set `QT_QPA_PLATFORM=offscreen` themselves; no display
  needed.
- Real-display checks are opt-in and skipped by default:
  `QT_QPA_PLATFORM=xcb RUN_QT_DISPLAY_TESTS=1 uv run pytest tests/test_qt_display.py -k display`
- Manual run against a real server config:
  `uv run vanilla-wow-launcher --launcher-config examples/octowow.json`

### Packaging

- The PyInstaller specs (`VanillaWoWLauncher.spec` = Windows onefile,
  `VanillaWoWLauncher-linux.spec` = onedir for AppImage,
  `VanillaWoWLauncher-macos.spec` = universal2 onedir + `.app` BUNDLE) freeze
  the entry script as **`packaging/pyinstaller_entry.py`** (a top-level shim),
  NOT `src/vanilla_wow_launcher/cli.py` directly — relative imports inside the
  package fail if `cli.py` is run as the frozen script. Specs use
  `pathex=["src"]` and package-qualified hidden imports.
- Windows build: `uv run pyinstaller --noconfirm --clean VanillaWoWLauncher.spec`
- Linux AppImage: `./packaging/linux/build-appimage.sh` →
  `dist/VanillaWoWLauncher-$(uname -m).AppImage` (uses `linuxdeploy`, which
  names the output after the desktop entry's `Name`, spaces→underscores, and
  drops it in CWD; requires `magick` IMv7 and `linuxdeploy` on PATH or
  `LINUXDEPLOY=` pointing at it).
- macOS DMG (universal2, build on macOS): `./packaging/macos/build-dmg.sh` →
  `dist/VanillaWoWLauncher-universal2.dmg` (requires a *universal*
  Python/PySide6 — `lipo -archs` verifies both arm64+x86_64 and fails
  otherwise; UPX is off; `.icns` built by `build-icons.sh`; unsigned by
  default — signing/notarization opt-in via env vars).
- CI/CD (GitHub Actions): `ci.yml` = pytest on push/PR (no ruff step — the
  lint/format gate is local-only); `release.yml` = on `v*` tag push, builds
  Windows/Linux/macOS and creates a GitHub Release.

### Version consistency

`UPDATER_VERSION` in `src/vanilla_wow_launcher/core/constants.py` MUST equal
`pyproject.toml` `[project] version` — `tests/test_baseline.py` enforces it.
Keep them in sync when bumping.

## Testing Notes

- Tests get a launcher config from the autouse `_launcher_env` fixture in
  `tests/conftest.py` (server `https://launcher.test` + a "Backup" mirror) —
  never rely on real network in tests. Launcher state is **process-global**:
  `_launcher_env` calls `launcher.reset()` + `launcher.configure_from_dict(...)`
  before and after each test.
- libtorrent is never needed to run the suite: a fake `lt` module is injected
  into `sys.modules`.
- Qt tests share one `QApplication` via `create_qt_app()` (a second instance
  aborts Qt); widget assertions use `objectName`s set in the widgets.
- Tests redirect config to `tmp_path` via `config_store.configure(...)` and
  monkeypatch `CONFIG_FILE`/`CACHE_FILE` on both `core.constants` and
  `controllers.settings` (that module imports them by name).
- Known flaky: `tests/test_addons_controller.py::test_apply_failure_records_error_and_posts_finished`
  times out intermittently under full-suite load but passes in isolation. Do
  not "fix" by disabling.

## Code Style Gotchas

- The QSS in `ui/qt/main_window.py` is built from **f-strings that mix CSS
  braces with `{p.*.name()}` interpolations**: an opening `{` must be `{{` and
  a literal closing `}` must be `}}`. A single unescaped `}` is a hard
  `SyntaxError` at import time that takes down every Qt test — it survives
  `ruff format` too (which aborts on the unparseable file).
- `context/` holds third-party reference sources (e.g. the OctoLauncher repo) —
  not part of the package, not packaged or executed. Leave it alone; don't
  lint/format/refactor anything under it.
