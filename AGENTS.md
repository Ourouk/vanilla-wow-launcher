# AGENTS.md

Vanilla WoW Launcher — a PySide6 desktop app (updater + mod manager for the
Vanilla WoW client). Runtime deps: PySide6 (GUI) and libtorrent (the
BitTorrent backend for client updates, imported lazily — the client update
path degrades to per-file HTTP downloads when it isn't installed, so tests
never need it); business logic is otherwise pure stdlib.

Thematic guides (**read the relevant one before touching that area**):

| File | Read before working on |
|------|------------------------|
| `docs/agents-architecture.md` | launcher config, catalogs, update backends, torrent, umu launch, marker protocol |
| `docs/agents-testing.md` | writing/running tests, fixtures, monkeypatch seams |
| `docs/agents-packaging.md` | PyInstaller specs, AppImage/DMG, CI/CD, version bumps |
| `docs/agents-ui.md` | anything in `ui/qt/` — QSS, dialogs, widget conventions |
| `docs/BITTORRENT_UPDATER_NOTES.md` | libtorrent-specific pitfalls |
| `docs/CODEBASE_REVIEW.md` | deep map of the codebase (verified claims w/ file:line) |

## Commands

```bash
uv sync                          # installs the package editable + PySide6
uv run vanilla-wow-launcher      # run the app
uv run python -m vanilla_wow_launcher # equivalent
uv run pytest                    # full suite; the 6 e2e tests skip unless RUN_E2E=1
uv run ruff format .             # pep8-style 79-col wrapping ([tool.ruff])
uv run ruff check .              # lint gate: E4/E7/E9/F/I/W/UP/B — run after edits
```

- **Ruff is the only lint/format gate — there is no type checker** (no mypy/
  pyright in the toolchain). `ruff check` selects E4/E7/E9/F/I/W/UP/B with
  `line-length = 79` and `target-version = "py310"` (see `pyproject.toml`).
- Manual run against a real server config (the only example in the repo):
  `uv run vanilla-wow-launcher --launcher-config examples/octowow.json`

## Non-negotiable conventions

- Inside the package use **relative** imports; tests import via
  `vanilla_wow_launcher.*` absolute paths (e.g.
  `from vanilla_wow_launcher.services.mods import ...`).
- Tests monkeypatch by dotted path with the FULL package name (e.g.
  `"vanilla_wow_launcher.ui.qt.addons_panel.QMessageBox.question"`), not the
  bare module name.
- `context/` holds third-party reference sources (Deluge, OctoLauncher) plus
  the real `client/` + `wow-client.torrent` used by e2e — all git-ignored,
  not part of the package, never executed. Leave it alone; don't lint/format/
  refactor anything under it.
- Never write raw `"__…__"` marker strings outside
  `services/update_backend/markers.py` — use its constants; see
  `docs/agents-architecture.md`.
- **Testing discipline**: don't overtest. Run only the test(s) that cover the
  code you changed while iterating; run the full suite once, right before
  committing. Details: `docs/agents-testing.md`.
