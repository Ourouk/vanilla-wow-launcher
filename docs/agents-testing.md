# Agent guide: testing

Scope: how to run tests and the fixture/seam quirks that bite.
Commands live in `AGENTS.md`; read it first.

## Running tests

- Qt widget tests set `QT_QPA_PLATFORM=offscreen` themselves; no display needed.
- Real-display checks are opt-in and skipped by default:
  `QT_QPA_PLATFORM=xcb RUN_QT_DISPLAY_TESTS=1 uv run pytest tests/test_qt_display.py -k display`
- **E2E tests** (`tests/test_torrent_update_e2e.py`, marked `e2e`) exercise the
  *real* libtorrent against `context/client` + `context/wow-client.torrent`.
  They skip unless `RUN_E2E=1` and both artifacts exist; CI runs
  `uv run pytest -m "not e2e"`. Run them with `RUN_E2E=1 uv run pytest -m e2e`.
- **Testing discipline**: don't overtest. Run only the test(s) that cover the
  code you changed (e.g. `uv run pytest tests/test_foo.py::test_bar`) while
  iterating. Run the full suite (`uv run pytest`) only once, right before
  committing, to catch cross-test interactions. Avoid re-running the whole
  suite on every edit — it's slow and the known-flaky `test_addons_controller`
  case makes repeated full runs noisy.

## Fixture & seam quirks

- Tests get a launcher config from the autouse `_launcher_env` fixture in
  `tests/conftest.py` (server `https://launcher.test` + a "Backup" mirror) —
  never rely on real network in tests. Launcher state is **process-global**:
  `_launcher_env` calls `launcher.reset()` + `launcher.configure_from_dict(...)`
  before and after each test, so override `launcher.*` the same way.
- Tests monkeypatch by dotted path with the FULL package name (e.g.
  `"vanilla_wow_launcher.ui.qt.addons_panel.QMessageBox.question"`), not the
  bare module name. Same for services, e.g.
  `"vanilla_wow_launcher.services.umu.launch"` (the update controller imports
  the umu module lazily inside its launch method).
- Download-source probing lives in `update_backend/sources.py`. Faking the
  network for *mirror probing* requires patching
  `vanilla_wow_launcher.services.update_backend.sources.secure_urlopen`;
  patching `http_update.secure_urlopen` only covers manifest/file fetches.
- libtorrent is faked via `sys.modules["libtorrent"]`; the real library is
  never needed to run the suite (only the e2e tests use it).
- Tests redirect config to `tmp_path` via `config_store.configure(...)` and
  monkeypatch `CONFIG_FILE`/`CACHE_FILE` on both `core.constants` and
  `controllers.settings` (that module imports them by name).
- Qt tests share one `QApplication` via `create_qt_app()` (a second instance
  aborts Qt); widget assertions use `objectName`s set in the widgets.

## Known flaky

- `tests/test_addons_controller.py::test_apply_failure_records_error_and_posts_finished`
  times out intermittently under full-suite load but passes in isolation.
  Do not "fix" by disabling.
