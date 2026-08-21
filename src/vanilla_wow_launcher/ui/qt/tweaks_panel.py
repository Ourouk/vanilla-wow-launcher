"""Vanilla WoW Launcher Qt (PySide6) tweaks panel.

Renders TWEAKS_ITEMS into a scrollable form — gold section headers, checkbox
rows and numeric entry rows with word-wrapped descriptions — plus an
Apply/Reset footer whose buttons follow the same dirty/custom rules. All
values, clamping and dirty/custom decisions come from the toolkit-agnostic
TweaksController; the panel only renders its events and forwards user actions
into it.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...controllers.tweaks import TweaksController
from ...services.tweaks import TWEAKS_DEFAULTS, TWEAKS_ITEMS, TWEAKS_LIMITS
from .list_panel import make_hairline
from .theme import Palette


class TweaksPanel(QWidget):
    """The TWEAKS tab: a scrollable tweak form with an Apply/Reset footer."""

    def __init__(
        self, tweaks: TweaksController, bridge, palette: Palette, parent=None
    ):
        super().__init__(parent)
        self._tweaks = tweaks
        self._palette = palette
        self.setObjectName("tweaksPanel")
        p = palette

        self.setStyleSheet(
            f"""
            #tweaksPanel {{ background-color: {p.panel.name()}; }}
            #tweaksContent {{ background-color: {p.panel.name()}; }}
            #tweaksSectionLabel {{ color: {p.gold.name()};
                                   font-weight: bold; font-size: 11pt; }}
            #tweaksRowLabel {{ color: {p.text.name()};
                               font-weight: bold; }}
            #tweaksRowLabel[dimm="true"] {{ color: {p.text_dim.name()}; }}
            #tweaksStatus {{ color: {p.text_dim.name()}; }}
            #tweaksStatus[dimm="true"] {{ color: {p.text_dim.name()}; }}
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("tweaksScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        root.addWidget(self._scroll, 1)

        self._content = QWidget()
        self._content.setObjectName("tweaksContent")
        self._form = QVBoxLayout(self._content)
        self._form.setContentsMargins(16, 8, 16, 8)
        self._form.setSpacing(0)
        self._form.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)

        self._checks: dict[str, QCheckBox] = {}
        self._entries: dict[str, QLineEdit] = {}
        self._rows: dict[str, QWidget] = {}

        self._build_rows()
        self._build_footer()

        bridge.logMessage.connect(self._on_log_message)
        bridge.operationFinished.connect(self._on_operation_finished)
        bridge.operationFailed.connect(self._on_operation_failed)

        self._refresh_from_config()

    # ── build ───────────────────────────────────────────────────────────────

    def _build_rows(self):
        p = self._palette
        values = self._tweaks.values()

        for (
            tid,
            label,
            kind,
            _recommended,
            _,
            desc,
            mn,
            _mx,
            _step,
        ) in TWEAKS_ITEMS:
            if kind == "section":
                hdr = QLabel(label, self._content)
                hdr.setObjectName(f"tweaksSection_{tid}")
                hdr.setProperty("role", "sectionTitle")
                self._form.addWidget(hdr)
                self._form.addSpacing(2)
                sep = make_hairline(self._content)
                sep.setObjectName(f"tweaksSection_{tid}_separator")
                self._form.addWidget(sep)
                self._form.addSpacing(4)
                continue

            row = QWidget(self._content)
            row.setObjectName(f"tweaksRow_{tid}")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 4, 0, 4)
            row_layout.setSpacing(10)

            name = QLabel(label, row)
            name.setObjectName(f"tweaksRow_{tid}_label")
            name.setFixedWidth(170)
            name.setStyleSheet(f"color: {p.text.name()}; font-weight: bold;")
            row_layout.addWidget(name, 0, Qt.AlignTop)

            if kind == "checkbox":
                check = QCheckBox(row)
                check.setObjectName(f"tweaksCheck_{tid}")
                check.setChecked(bool(values.get(tid, False)))
                check.setCursor(Qt.PointingHandCursor)
                check.toggled.connect(self._on_changed)
                row_layout.addWidget(check, 0, Qt.AlignTop)
                self._checks[tid] = check
            elif kind == "number":
                val = values.get(tid, mn or 0)
                entry = QLineEdit(row)
                entry.setObjectName(f"tweaksEntry_{tid}")
                entry.setText(str(int(val)))
                entry.setFixedWidth(80)
                entry.setAlignment(Qt.AlignCenter)
                entry.setValidator(QIntValidator(entry))
                entry.textChanged.connect(self._on_changed)
                entry.editingFinished.connect(lambda t=tid: self._clamp(t))
                entry.returnPressed.connect(lambda t=tid: self._clamp(t))
                row_layout.addWidget(entry, 0, Qt.AlignTop)
                self._entries[tid] = entry

            if desc:
                desc_label = QLabel(desc, row)
                desc_label.setObjectName(f"tweaksRow_{tid}_desc")
                desc_label.setWordWrap(True)
                desc_label.setStyleSheet(f"color: {p.text_dim.name()};")
                row_layout.addWidget(desc_label, 1)

            self._rows[tid] = row
            self._form.addWidget(row)

    def _build_footer(self):
        p = self._palette
        sep = make_hairline(self)
        self.layout().addWidget(sep)

        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 6, 16, 10)
        footer_layout.setSpacing(8)

        self._apply_button = QPushButton("Apply", footer)
        self._apply_button.setObjectName("tweaksApply")
        self._apply_button.clicked.connect(self._apply)
        footer_layout.addWidget(self._apply_button)

        self._reset_button = QPushButton("Reset", footer)
        self._reset_button.setObjectName("tweaksReset")
        self._reset_button.clicked.connect(self._reset)
        footer_layout.addWidget(self._reset_button)

        self._status_label = QLabel("", footer)
        self._status_label.setObjectName("tweaksStatus")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {p.text_dim.name()};")
        footer_layout.addWidget(self._status_label, 1)

        self.layout().addWidget(footer)

    # ── values ──────────────────────────────────────────────────────────────

    def _ui_values(self) -> dict:
        """Raw UI snapshot: checkbox states + entry text, unclamped."""
        values = {}
        for tid, check in self._checks.items():
            values[tid] = check.isChecked()
        for tid, entry in self._entries.items():
            values[tid] = entry.text()
        return values

    def _entry_bad(self, tid) -> bool:
        """True when a number entry holds an unparseable or out-of-range
        value."""
        lo, hi = TWEAKS_LIMITS.get(tid, (None, None))
        try:
            v = int(self._entries[tid].text())
            return (lo is not None and v < lo) or (hi is not None and v > hi)
        except ValueError:
            return True

    def _clamp(self, tid):
        """FocusOut/Return clamp: parse, clamp to limits, write back."""
        entry = self._entries[tid]
        lo, hi = TWEAKS_LIMITS.get(tid, (None, None))
        try:
            v = int(entry.text())
        except ValueError:
            v = TWEAKS_DEFAULTS.get(tid, lo or 0)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        entry.setText(str(v))
        self._on_changed()

    # ── rendering ───────────────────────────────────────────────────────────

    def _refresh_from_config(self):
        """Re-read the saved values into the form without re-emitting change
        signals (so the buttons settle against the saved config)."""
        values = self._tweaks.values()
        for tid, check in self._checks.items():
            v = values.get(tid, TWEAKS_DEFAULTS.get(tid))
            check.blockSignals(True)
            check.setChecked(bool(v))
            check.blockSignals(False)
        for tid, entry in self._entries.items():
            v = values.get(tid, TWEAKS_DEFAULTS.get(tid))
            entry.blockSignals(True)
            entry.setText(str(int(v)) if v is not None else "")
            entry.blockSignals(False)
        self._update_buttons()
        self._update_styles()

    def _on_changed(self, *_args):
        """Re-evaluate button visibility and red painting on every edit."""
        self._update_buttons()
        self._update_styles()

    def _update_buttons(self):
        """Apply is offered when the UI differs from the saved config, Reset
        when it differs from the defaults."""
        dirty, custom = self._tweaks.dirty_and_custom(self._ui_values())
        self._apply_button.setVisible(dirty)
        self._reset_button.setVisible(custom)

    def _update_styles(self):
        p = self._palette
        for tid, entry in self._entries.items():
            if self._entry_bad(tid):
                entry.setStyleSheet(f"QLineEdit {{ color: {p.err.name()}; }}")
            else:
                entry.setStyleSheet("")

    # ── actions ─────────────────────────────────────────────────────────────

    def _apply(self):
        """Gather the (clamped) UI values and hand them to the controller."""
        _, ui = self._tweaks.validate_entries(self._ui_values())
        if self._tweaks.apply(ui):
            self._set_running(True)

    def _reset(self):
        """Write the defaults and re-apply; then refresh the form from the
        config the controller just saved."""
        if self._tweaks.reset():
            self._set_running(True)
        self._refresh_from_config()

    def _set_running(self, running: bool):
        self._apply_button.setEnabled(not running)
        self._reset_button.setEnabled(not running)
        if running:
            self._status_label.setText("Applying tweaks…")

    # ── event wiring ────────────────────────────────────────────────────────

    def _on_log_message(self, text: str, tag: str = ""):
        """Keep the inline status label to the latest tweak log line."""
        line = text.strip()
        if line:
            self._status_label.setText(line)

    def _on_operation_finished(self, kind: str, ok: bool, message: str):
        if kind != "tweaks":
            return
        self._set_running(False)
        self._refresh_from_config()
        if ok:
            self._status_label.setText("Tweaks applied.")
        else:
            self._status_label.setText("Tweaks failed — check the log")

    def _on_operation_failed(self, kind: str, message: str):
        if kind != "tweaks":
            return
        self._set_running(False)
        self._refresh_from_config()
        self._status_label.setText("Tweaks failed — check the log")
