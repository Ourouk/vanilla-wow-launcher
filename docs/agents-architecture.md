# Agent guide: architecture & update pipeline

Scope: package layout, launcher config, catalogs, client-update backends,
game launch. Read together with `AGENTS.md` (commands + conventions).
The libtorrent pitfall list lives in `docs/BITTORRENT_UPDATER_NOTES.md`.

## Layout & configuration

```
src/vanilla_wow_launcher/
  cli.py          # entry point: config wiring + window loop
  core/           # constants, config_store, launcher, security_http, filesystem, helpers, log_sink, platform_support, errors, themes
  services/       # catalog, addons, mods, news, tweaks, self_update, server_index, umu, logo, update_backend/
  controllers/    # update, news, mods, addons, settings, tweaks (toolkit-agnostic)
  state/          # models.py (state dataclasses), events.py (dispatcher)
  ui/qt/          # app, main_window, bridge, theme, panels, dialogs
```

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
- Default client folder comes from ONE helper,
  `platform_support.default_game_folder(server_name)` (`~/Games/<Server>` with
  a configured server, else `DEFAULT_OUT_DIR`) — used by both
  `cli._ensure_default_game_folder()` and the Settings fallback. Don't
  reintroduce divergent defaults.

## Catalogs (mods/addons)

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

## Client-update backends

- Controllers are toolkit-agnostic: they post dataclass *events* to a shared
  `EventDispatcher` (`state/events.py`) from worker threads; they never touch
  widgets. The Qt side (`ui/qt/bridge.py`) converts events to Qt signals on the
  main thread.
- Client updates get a second download backend: when the active download
  source advertises a `torrent_url` (launcher config, server or mirror) and
  libtorrent is importable, `UpdateWorker` bulk-downloads the stale files via
  `services/update_backend/torrent_update.py`, then re-verifies exactly the
  delivered files against the manifest's SHA-1 (`_reverify_torrent_files`)
  and HTTP-refetches any mismatch; the whole-client per-file HTTP
  `traverse()` runs only when the torrent backend wasn't used. In the
  manifest-less recovery path there is no manifest to check — the TLS-fetched
  torrent's piece hashes are the guarantee.
- The torrent root is **auto-detected** from the unique `WoW.exe` position in
  the torrent: the parent of `WoW.exe` (case-insensitive) is the root prefix
  stripped from every torrent path when mapping to the selected WoW folder
  (e.g. `client/WoW.exe` → `<wow_folder>/WoW.exe`). A `TorrentLayoutError`
  is raised when `WoW.exe` is missing, duplicated, or any file escapes the root.
  **This stripping is applied to the actual read/write target** via
  `_remap_torrent_to_out_dir()` (in both `verify()` and `download()`), which
  remaps the torrent's file paths to `out_dir/local` with `torrent_info.remap_files`.
  Without it, libtorrent reads at `out_dir/client/...` (double prefix) and the
  whole client reports stale — the bug that spawned
  `docs/BITTORRENT_UPDATER_NOTES.md`. The remap is guarded by
  `hasattr(ti, "remap_files")` so the unit-test fakes (which lack it) are a
  no-op. See that file for the full libtorrent pitfall list.
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
  per-file SHA-1. It posts `markers.TORRENT_RECOVERY_DONE` (controller keeps
  `manifest_available=False`); a failed verify offers this via an enabled
  UPDATE button when `torrent_recovery_available()` (`LauncherConfig.has_torrent()`
  + libtorrent present, network-free) and the client isn't known-ready.
- Download-source probing lives in `update_backend/sources.py`
  (`DownloadSource`/`_download_source`, re-exported by `http_update` so
  controllers/tests keep importing from there).
- The launcher never binary-patches `WoW.exe` — runtime client fixes are left
  to the VanillaFixes loader mod. The only tweak channel is `Config.wtf`.

## Update lifecycle & game launch

- **Update workers are queue-based**: `UpdateController.start_verify()/start_update()`
  write to internal queues drained by `UpdateController.poll()`. The Qt
  `MainWindow._pollTimer` calls `hub.updater.poll()` every 50 ms — if you add a
  new path that bypasses the window, remember the controller is not polled
  automatically. Completion markers only clear the busy state via poll.
- **Marker protocol is constants-only** (`services/update_backend/markers.py`):
  every worker→controller control string (`__DONE__`, `__TORRENT_*__`,
  `__VERSION__…`) must be referenced via its `markers.*` constant — emit sites
  put `(markers.X, tag)` on the log queue, and
  `UpdateController._handle_log` dispatches through the `_MARKER_HANDLERS`
  dict (one `_on_*` method per marker). Adding a lifecycle outcome = new
  constant + `_on_*` method + table entry; `tests/test_markers.py` fails the
  suite on raw `"__…__"` literals anywhere else in `src/`, on unhandled
  markers, and on table entries without a constant. Never change marker
  strings — they are a wire format shared with tests.
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
  it. Renderer maps to `PROTON_DXVK_D3D8`/`PROTON_USE_WINED3D` env vars and the
  `Config.wtf` `gxApi`; GameMode wraps launch in `gamemoderun` (only if
  installed); Wayland sets `PROTON_ENABLE_WAYLAND=1` when the `umu_wayland`
  setting is true — `controllers/update.py` passes it as `launch(wayland=...)`,
  and `umu.launch` forwards it to `build_env`.
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
- Keep the poll/log-drain timers stopped and workers cancelled in
  `MainWindow` teardown (idempotent `_teardown()`).
