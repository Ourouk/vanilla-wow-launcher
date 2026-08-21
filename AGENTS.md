# AGENTS.md

Vanilla WoW Launcher — a PySide6 desktop app (updater + mod manager for the
Vanilla WoW client). Runtime deps: PySide6 (GUI) and libtorrent (the
BitTorrent backend for client updates, imported lazily — the client update
path degrades to per-file HTTP downloads when it isn't installed, so tests
never need it); business logic is otherwise pure stdlib.

## Commands

```bash
uv sync                          # installs the package editable + PySide6
uv run vanilla-wow-launcher      # run the app
uv run python -m vanilla_wow_launcher # equivalent
uv run pytest                    # full suite; the 6 e2e tests skip unless RUN_E2E=1
uv run ruff format .             # pep8-style 79-col wrapping ([tool.ruff])
uv run ruff check .              # lint gate: E4/E7/E9/F/I/W/UP/B — run after edits
```

- Qt widget tests set `QT_QPA_PLATFORM=offscreen` themselves; no display needed.
- **Ruff is the only lint/format gate — there is no type checker** (no mypy/
  pyright in the toolchain). `ruff check` selects E4/E7/E9/F/I/W/UP/B with
  `line-length = 79` and `target-version = "py310"` (see `pyproject.toml`).
- Real-display checks are opt-in and skipped by default:
  `QT_QPA_PLATFORM=xcb RUN_QT_DISPLAY_TESTS=1 uv run pytest tests/test_qt_display.py -k display`
- **E2E tests** (`tests/test_torrent_update_e2e.py`, marked `e2e`) exercise the
  *real* libtorrent against `context/client` + `context/wow-client.torrent`.
  They skip unless `RUN_E2E=1` and both artifacts exist; CI runs
  `uv run pytest -m "not e2e"`. Run them with `RUN_E2E=1 uv run pytest -m e2e`.
- Windows build: `uv run pyinstaller --noconfirm --clean VanillaWoWLauncher.spec`
- Linux AppImage: `./packaging/linux/build-appimage.sh` → `dist/VanillaWoWLauncher-$(uname -m).AppImage`
- macOS DMG (universal2, build on macOS): `./packaging/macos/build-dmg.sh` → `dist/VanillaWoWLauncher-universal2.dmg`
- CI/CD (GitHub Actions): `ci.yml` = pytest on push/PR (no ruff step — the
  lint/format gate is local-only); `release.yml` = on `v*` tag push, builds
  Windows/Linux/macOS and creates a GitHub Release
- Manual run against a real server config (the only example in the repo):
  `uv run vanilla-wow-launcher --launcher-config examples/octowow.json`

## Layout (`src/` layout)

```
src/vanilla_wow_launcher/
  cli.py          # entry point: config wiring + window loop
  core/           # constants, config_store, launcher, security_http, filesystem, helpers, log_sink, platform_support, errors
  services/       # catalog, addons, mods, news, tweaks, update_backend, self_update
  controllers/    # update, news, mods, addons, settings, tweaks (toolkit-agnostic)
  state/          # models.py (state dataclasses), events.py (dispatcher)
  ui/qt/          # app, main_window, bridge, theme, panels, dialogs
```

- `context/` holds third-party reference sources (Deluge, OctoLauncher) plus
  the real `client/` + `wow-client.torrent` used by e2e — all git-ignored,
  not part of the package, never executed. Leave it alone; don't lint/format/
  refactor anything under it.
- Inside the package use **relative** imports; tests import via
  `vanilla_wow_launcher.*` absolute paths (e.g.
  `from vanilla_wow_launcher.services.mods import ...`).
- Tests monkeypatch by dotted path with the FULL package name (e.g.
  `"vanilla_wow_launcher.ui.qt.addons_panel.QMessageBox.question"`), not the
  bare module name.
- `core/constants.py` computes `APP_DIR`: repo root (3 dirs up from the file)
  when run from source, exe dir when frozen. Config and cache live in separate
  per-user dirs via `platform_support.config_dir()/cache_dir()`: Linux config
  `~/.vanilla-wow-launcher`, Windows `%APPDATA%\VanillaWoWLauncher`, macOS
  `~/Library/Application Support`; cache is Linux XDG / `%LOCALAPPDATA%` /
  `~/Library/Caches`. Superseded (next-to-exe, old XDG) and pre-rename
  (`octo-updater`) files are migrated on first run via the `LEGACY_*_FILES`
  tuples in `core/constants.py`, plus `legacy_custom_pairs()` for the custom
  catalog files that move with the config dir.
- **There are no hardcoded server/mod/addon values.** Everything is configured
  by `core/launcher.py` reading `vanilla_wow_launcher.json` (server, news,
  realm, registry URLs, mirrors; auto-discovered next to the exe / repo root,
  or `--launcher-config`). Missing/invalid config with no `--launcher-config`
  opens a **modal first-launch wizard** (`ui/qt/launcher_config_dialog.py`,
  driven by `cli._pick_launcher_config()`); an explicit `--launcher-config`
  that is missing/invalid is a hard `cli.main()` error (no wizard). The wizard
  validates via `launcher.validate_path()` (no global-state side effect) and
  the selection is persisted to `launcher.user_config_path()` (the per-user
  config dir) via `launcher.persist()`, taking precedence over auto-discovery
  on later runs. The download host allowlist
  (`security_http.allowed_download_hosts()`) is built from the launcher's
  server+mirror hosts plus the git hosts.
- The mods/addons lists come from remote JSON catalogs (`services/catalog.py`
  holds the shared validation/merge logic; the fetch entry points live in
  `services/mods.py` / `services/addons.py`). `mods.mods_registry()` is
  network-free on non-forced calls (cache → empty list). Catalogs auto-refresh
  at most weekly (`catalog.CATALOG_TTL`): startup serves the persisted cache
  instantly (ADDONS via a preview snapshot posted before the verify scan) and
  refetches only when `catalog_is_stale()` / the per-URL TTL says so — explicit
  Settings "Reload" always forces, the MODS panel's header ⟳ uses
  `mods.reload_catalog()`, and the single ADDONS ⟳ ("Check for updates", in
  the header next to the age tag) runs `verify(force=True)`, which refetches
  the online catalog(s) AND rescans SHAs. Panel headers show a "Catalog
  updated …" age tag. There is no bundled registry/recommended list — tests
  provide one by monkeypatching `mods.mods_registry()`.
- The ADDONS list is sectioned, not flat: stale installs get a **NEED
  UPDATE** section rendered above **INSTALLED** (only when non-empty),
  followed by **AVAILABLE**; each header shows its count and collapses
  independently (persisted in `AddonsState.sections_open`, new titles
  default open). There is deliberately no per-row "Up to date" label — the
  categories carry that meaning; stale rows keep their gold clickable
  "Update" action. The row website-link glyph (⧉, shared with MODS via
  `list_panel.add_row_link`) is sized by the `PT_LINK_ICON` metrics token.
  `services/addons.addon_remote_sha()` refuses any git URL whose host is
  outside `ADDON_GIT_HOSTS` before opening an API connection or spawning
  `git ls-remote`.
- Client updates get a second download backend: when the active download
  source advertises a `torrent_url` (launcher config, server or mirror) and
  libtorrent is importable, `UpdateWorker` bulk-downloads the stale files via
  `services/update_backend/torrent_update.py` before its per-file HTTP `traverse()`, which
  re-verifies every file and HTTP-resumes anything the torrent missed. Unit
  tests inject a fake `libtorrent` via `sys.modules["libtorrent"]`; libtorrent
  is never needed to run the suite (only the e2e tests use the real one).
- The torrent root is **auto-detected** from the unique `WoW.exe` position in
  the torrent: the parent of `WoW.exe` (case-insensitive) is the root prefix
  stripped from every torrent path when mapping to the selected WoW folder
  (e.g. `client/WoW.exe` → `<wow_folder>/WoW.exe`). A `TorrentLayoutError`
  is raised when `WoW.exe` is missing, duplicated, or any file escapes the root.
  **This stripping is applied to the actual read/write target** via
  `_remap_torrent_to_out_dir()` (in both `verify()` and `download()`), which
  remaps the torrent's file paths to `out_dir/local` with `torrent_info.remap_files`.
  Without it, libtorrent reads at `out_dir/client/...` (double prefix) and the
  whole client reports stale — the bug that spawned `BITTORRENT_UPDATER_NOTES.md`.
  The remap is guarded by `hasattr(ti, "remap_files")` so the unit-test fakes
  (which lack it) are a no-op. See `BITTORRENT_UPDATER_NOTES.md` for the full
  libtorrent pitfall list.
- **libtorrent 2.x gotchas** (verified against `2.1.1.0`): `torrent_status.pieces`
  is a `list[bool]` → count present with `sum(pieces)`, never `p.count()` (no-arg
  `TypeError`); `force_recheck()` must be followed by `resume()` or the recheck
  never proceeds (Deluge pattern); `verified_pieces` is seed-mode-only and stays
  `0` during verification — use `have_piece()`/piece count for progress.
- **Torrent verification is offline**: the verification session uses an empty
  `listen_interfaces` and disables DHT/LSD/UPnP/NAT-PMP and all peer
  connections. No P2P activity occurs before the user presses UPDATE. Only the
  download session enables networking.
- When the manifest itself can't be fetched, the update falls back to a
  manifest-less **BitTorrent recovery**: if the active source advertises a
  `torrent_url` and libtorrent is importable, `UpdateWorker._recovery_download()`
  downloads the *whole* torrent (`TorrentDownloader.download(url, None)`), whose
  piece hashes (the `.torrent` arrived over TLS) stand in for the manifest's
  per-file SHA-1. It posts `__TORRENT_RECOVERY_DONE__` (controller keeps
  `manifest_available=False`); a failed verify offers this via an enabled
  UPDATE button when `torrent_recovery_available()` (`LauncherConfig.has_torrent()`
  + libtorrent present, network-free) and the client isn't known-ready.
- The launcher never binary-patches `WoW.exe` — runtime client fixes are left
  to the VanillaFixes loader mod. The only tweak channel is `Config.wtf`.
- Tests get a launcher config from the autouse `_launcher_env` fixture in
  `tests/conftest.py` (server `https://launcher.test` + a "Backup" mirror) —
  never rely on real network in tests. Launcher state is **process-global**:
  `_launcher_env` calls `launcher.reset()` + `launcher.configure_from_dict(...)`
  before and after each test, so override `launcher.*` the same way.

## Architecture rules

- Controllers are toolkit-agnostic: they post dataclass *events* to a shared
  `EventDispatcher` (`state/events.py`) from worker threads; they never touch
  widgets. The Qt side (`ui/qt/bridge.py`) converts events to Qt signals on the
  main thread.
- **Linux game launch goes through umu-launcher** (`services/umu.py`): the PLAY
  button is gated on `core.platform_support.can_launch_client()`, which is now
  True on Windows (native) and on Linux only when `umu.umu_available()` finds
  `umu-run` on PATH (or `~/.local/bin/umu-run`). `controllers/update.py`'s
  `launch_game()` splits into `_launch_game_windows()` (Popen, DXVK notice,
  VanillaFixes.exe preference) and `_launch_game_via_umu()` (WoW.exe under
  Proton in the launcher-wide `data_dir()/wineprefix`, no DXVK notice). All umu
  settings live in the config's `"launch"` key and are edited via
  `SettingsController.set_umu_*`: `umu_proton` (defaults to `UMU-Proton`, the
  newest installed Proton — `services/umu.py` `DEFAULT_PROTON`/`default_proton()`),
  `umu_renderer` (`auto`/`dxvk-d3d8`/`wined3d-opengl`), `umu_gamemode`,
  `umu_wayland`, `umu_binary_path`, `umu_game_id`. They render in a **dedicated
  `LinuxSettingsDialog`** (`ui/qt/linux_settings_dialog.py`) opened by the
  "Linux (UMU) Settings…" button in the main Settings dialog — *not* a section of
  it.   Renderer maps to `PROTON_DXVK_D3D8`/`PROTON_USE_WINED3D` env vars and the
  `Config.wtf` `gxApi`; GameMode wraps launch in `gamemoderun` (only if
  installed); Wayland sets `PROTON_ENABLE_WAYLAND=1` when the `umu_wayland`
  setting is true — `controllers/update.py` passes it as `launch(wayland=...)`,
  and `umu.launch` forwards it to `build_env` (it was silently dropped before
  the 2026-08-21 fix).
  Tests patch the FULL path, e.g. `"vanilla_wow_launcher.services.umu.launch"`
  (the controller imports the umu module lazily inside the launch method).
- **One game process at a time**: `umu.launch()` returns `(pid, pgid, proc)`;
  `UpdateController` records it in `state.game_*`, posts `GameLaunched`, and
  spawns a daemon `_watch_game()` thread that `proc.wait()`s and posts
  `GameExited` (clearing the running state). `compute_readiness()` returns
  mode `"terminate"` while a game runs — the footer shows an enabled red
  TERMINATE button (`_terminate_game()` → `umu.kill_game()`: SIGTERM to the
  process group, SIGKILL after 2 s). A second `launch_game()` while one is
  running is refused.
- **Update workers are queue-based**: `UpdateController.start_verify()/start_update()`
  write to internal queues drained by `UpdateController.poll()`. The Qt
  `MainWindow._pollTimer` calls `hub.updater.poll()` every 50 ms — if you add a
  new path that bypasses the window, remember the controller is not polled
  automatically. Completion markers (`__DONE__` etc.) only clear the busy state
  via poll.
- Keep the poll/log-drain timers stopped and workers cancelled in
  `MainWindow` teardown (idempotent `_teardown()`).

## Packaging gotchas

- The PyInstaller specs (`VanillaWoWLauncher.spec` = Windows onefile,
  `VanillaWoWLauncher-linux.spec` = onedir for AppImage,
  `VanillaWoWLauncher-macos.spec` = universal2 onedir + `.app` BUNDLE) freeze
  the entry script as **`packaging/pyinstaller_entry.py`** (a top-level shim),
  NOT `src/vanilla_wow_launcher/cli.py` directly — relative imports inside the
  package fail if `cli.py` is run as the frozen script. Specs use
  `pathex=["src"]` and package-qualified hidden imports.
- AppImage uses `linuxdeploy`, which names the output after the desktop entry's
  `Name` (spaces→underscores) and drops it in CWD; `build-appimage.sh` relocates
  it to `dist/`. Requires `magick` (IMv7) and `linuxdeploy` on PATH or
  `LINUXDEPLOY=` pointing at it.
- macOS: `build-dmg.sh` must run on macOS with a *universal* Python/PySide6
  (`lipo -archs` verifies both arm64+x86_64 and fails otherwise). UPX is off in
  `VanillaWoWLauncher-macos.spec` (unsupported on macOS); the `.icns` is built
  by `build-icons.sh` from `packaging/icons/VanillaWoWLauncher.png`. The result
  is unsigned by default — signing/notarization are opt-in via env vars.

## Version consistency

`UPDATER_VERSION` in `src/vanilla_wow_launcher/core/constants.py` MUST equal
`pyproject.toml` `[project] version` — `tests/test_baseline.py` enforces it.
Keep them in sync when bumping.

## Test quirks

- Known flaky: `tests/test_addons_controller.py::test_apply_failure_records_error_and_posts_finished`
  times out intermittently under full-suite load but passes in isolation.
  Do not "fix" by disabling.
- Qt tests share one `QApplication` via `create_qt_app()` (a second instance
  aborts Qt); widget assertions use `objectName`s set in the widgets.
- Tests redirect config to `tmp_path` via `config_store.configure(...)` and
  monkeypatch `CONFIG_FILE`/`CACHE_FILE` on both `core.constants` and
  `controllers.settings` (that module imports them by name).
- **Testing discipline**: don't overtest. Run only the test(s) that cover the
  code you changed (e.g. `uv run pytest tests/test_foo.py::test_bar`) while
  iterating. Run the full suite (`uv run pytest`) only once, right before
  committing, to catch cross-test interactions. Avoid re-running the whole
  suite on every edit — it's slow and the known-flaky `test_addons_controller`
  case makes repeated full runs noisy.

## Code style gotchas

- The QSS in `ui/qt/main_window.py` is built from **f-strings that mix CSS
  braces with `{p.*.name()}` interpolations**: an opening `{` must be `{{` and
  a literal closing `}` must be `}}`. A single unescaped `}` is a hard
  `SyntaxError` at import time that takes down every Qt test — it survives
  `ruff format` too (which aborts on the unparseable file).
- **Qt settings dialogs are plain `QDialog`s** (no frameless flag), so they
  already get a native title-bar close button. Do NOT add a custom `✕` close
  `QPushButton`/`QToolButton` — it renders a second close button beside the
  native one. Close via the native title bar or `dialog.close()`; tests close
  via `dialog.close()` (see `test_qt_settings_dialog.py` /
  `test_qt_smoke.py`). The main `SettingsDialog` and `LinuxSettingsDialog`
  follow this.
- **UI conventions (2026-08-21 refresh)**: button language is all-caps for
  primary/global actions (`UPDATE`/`PLAY`, nav tabs) and Title Case for panel
  actions ("Apply", "Retry") — map controller machine strings to labels in the
  UI layer, never render raw `"retry"`/`"update"`. Recurring button looks come
  from QSS variants — `setProperty("variant", "primary"|"positive"|"outline"|`
  `"compact")` styled by `theme_qss` — not per-widget stylesheets. Dividers are
  `list_panel.make_hairline()`; section titles set `role="sectionTitle"`. Point
  sizes and paddings use the tokens in `ui/qt/metrics.py` (PT_*/PAD_*) — no ad
  hoc sizes. All palette colors (incl. pink/warn/btn_text) are themable slots
  in `core/themes.py`; never hardcode hex in widgets. Icon-only controls get a
  tooltip + `setAccessibleName`. The LinuxSettingsDialog uses the
  `linuxSettings*` objectName prefix (tests assert it). Footer pseudo-actions
  are real `QToolButton`s, not clickable labels.
