# Agent guide: Qt UI conventions

Scope: `src/vanilla_wow_launcher/ui/qt/` — styling, dialogs, widgets.

## QSS f-string braces (breaks the suite)

The QSS in `ui/qt/main_window.py` is built from **f-strings that mix CSS
braces with `{p.*.name()}` interpolations**: an opening `{` must be `{{` and
a literal closing `}` must be `}}`. A single unescaped `}` is a hard
`SyntaxError` at import time that takes down every Qt test — it survives
`ruff format` too (which aborts on the unparseable file).

## Dialog close buttons

Qt settings dialogs are plain `QDialog`s (no frameless flag), so they
already get a native title-bar close button. Do NOT add a custom `✕` close
`QPushButton`/`QToolButton` — it renders a second close button beside the
native one. Close via the native title bar or `dialog.close()`; tests close
via `dialog.close()` (see `test_qt_settings_dialog.py` /
`test_qt_smoke.py`). The main `SettingsDialog` and `LinuxSettingsDialog`
follow this.

## Widget conventions

- Button language is all-caps for primary/global actions (`UPDATE`/`PLAY`,
  nav tabs) and Title Case for panel actions ("Apply", "Retry") — map
  controller machine strings to labels in the UI layer, never render raw
  `"retry"`/`"update"`.
- Recurring button looks come from QSS variants —
  `setProperty("variant", "primary"|"positive"|"outline"|"compact")` styled
  by `theme_qss` — not per-widget stylesheets.
- Dividers are `list_panel.make_hairline()`; section titles set
  `role="sectionTitle"`.
- Point sizes and paddings use the tokens in `ui/qt/metrics.py`
  (PT_*/PAD_*) — no ad hoc sizes. All palette colors (incl. pink/warn/
  btn_text) are themable slots in `core/themes.py`; never hardcode hex in
  widgets.
- Icon-only controls get a tooltip + `setAccessibleName`.
- The LinuxSettingsDialog uses the `linuxSettings*` objectName prefix (tests
  assert it). Footer pseudo-actions are real `QToolButton`s, not clickable
  labels.
