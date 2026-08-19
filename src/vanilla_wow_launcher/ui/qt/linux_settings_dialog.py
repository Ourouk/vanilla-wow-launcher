"""Vanilla WoW Launcher Qt (PySide6) Linux settings dialog.

A separate, non-modal window holding every Linux/umu-launcher play setting:
the Proton build, the renderer preset, the GameMode and Wayland-backend
toggles, the GAMEID and the umu-run binary override. It is opened from the
main Settings dialog via the "Linux (UMU) Settings…" button. Everything
forwards into the `SettingsController`; no GUI toolkit logic elsewhere.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...services.umu import RENDERER_CHOICES
from .theme import Palette, theme_qss

# Renderer presets surfaced in the combobox, keyed for the apply handler.
_RENDERER_LABELS = {value: label for value, label in RENDERER_CHOICES}
_RENDERER_BY_LABEL = {label: value for value, label in RENDERER_CHOICES}


class LinuxSettingsDialog(QDialog):
    """All Linux/umu-launcher play settings, in their own window."""

    def __init__(self, settings, palette: Palette, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._palette = palette
        p = palette
        self.setObjectName("linuxSettingsDialog")
        self.setWindowTitle("Linux (UMU) Settings")
        self.setMinimumSize(460, 520)
        self.setStyleSheet(
            theme_qss(p) + f"\nQDialog {{ background-color: {p.bg.name()}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addLayout(self._build_body(), 1)

    # ── build ────────────────────────────────────────────────────────────

    def _build_header(self):
        p = self._palette
        hdr = QWidget(self)
        hdr.setObjectName("linuxSettingsHeader")
        layout = QHBoxLayout(hdr)
        layout.setContentsMargins(18, 12, 12, 12)

        title = QLabel("LINUX (UMU) SETTINGS", hdr)
        title.setStyleSheet(
            f"color: {p.purple.name()}; font-weight: bold; font-size: 12pt;"
        )
        layout.addWidget(title)
        layout.addStretch(1)
        return hdr

    def _build_body(self) -> QVBoxLayout:
        p = self._palette
        body = QVBoxLayout()
        body.setContentsMargins(18, 12, 18, 12)
        body.setSpacing(8)

        launch = self._settings.launch

        title = QLabel("LINUX (UMU)", self)
        title.setObjectName("linuxSettingsTitle")
        title.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        body.addWidget(title)
        body.addSpacing(2)

        umu_bin = self._settings.resolve_umu_binary()
        hint = QLabel(
            f"umu-run detected at: {umu_bin}"
            if umu_bin
            else "umu-run not found on PATH — install umu-launcher "
            "(e.g. `pacman -S umu-launcher` / `apt install umu-launcher`) "
            "to enable PLAY on Linux.",
            self,
        )
        hint.setObjectName("settingsUmuHint")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {p.text_dim.name()}; font-size: 9pt;")
        body.addWidget(hint)
        body.addSpacing(6)

        proton_options = self._settings.available_protons()
        # Preserve a stored Proton (e.g. a custom path/code not currently
        # detected) so it stays selectable instead of being dropped.
        if launch.umu_proton and launch.umu_proton not in proton_options:
            proton_options = [launch.umu_proton] + proton_options
        self._add_launch_combo(
            layout=body,
            label="Proton",
            object_name="settingsProton",
            options=proton_options,
            current=launch.umu_proton or "UMU-Proton",
            on_apply=self._settings.set_umu_proton,
        )
        self._add_launch_combo(
            layout=body,
            label="Renderer",
            object_name="settingsRenderer",
            options=[label for _, label in RENDERER_CHOICES],
            current=_RENDERER_LABELS.get(
                launch.umu_renderer, "Auto (Proton default)"
            ),
            on_apply=self._on_renderer_apply,
        )

        features = self._settings.linux_features()
        self._add_feature_check(
            layout=body,
            text="GameMode",
            object_name="settingsGamemode",
            checked=launch.umu_gamemode,
            available=features["gamemode_available"],
            on_toggled=self._settings.set_umu_gamemode,
            unavailable_hint=(
                "gamemoderun not found — install GameMode to enable."
            ),
        )
        self._add_feature_check(
            layout=body,
            text="Wayland backend",
            object_name="settingsWayland",
            checked=launch.umu_wayland,
            available=features["wayland_session"],
            on_toggled=self._settings.set_umu_wayland,
            unavailable_hint="Not running on a Wayland session.",
        )

        self._add_launch_field(
            layout=body,
            label="GAMEID",
            object_name="settingsUmuGameId",
            value=launch.umu_game_id,
            on_apply=self._settings.set_umu_game_id,
            get_value=lambda: self._settings.launch.umu_game_id,
        )

        bin_row = QHBoxLayout()
        bin_name = QLabel("umu-run", self)
        bin_name.setStyleSheet(
            f"color: {p.text.name()}; font-weight: bold; font-size: 9pt;"
        )
        bin_name.setFixedWidth(64)
        bin_row.addWidget(bin_name)
        self._umu_bin_edit = QLineEdit(launch.umu_binary_path, self)
        self._umu_bin_edit.setObjectName("settingsUmuPath")
        self._umu_bin_edit.setPlaceholderText("auto-detect on PATH")
        bin_row.addWidget(self._umu_bin_edit, 1)
        browse = QPushButton("Browse…", self)
        browse.setObjectName("settingsUmuBrowse")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._on_umu_browse)
        bin_row.addWidget(browse)
        apply = QPushButton("Apply", self)
        apply.setObjectName("settingsUmuPathApply")
        apply.setCursor(Qt.PointingHandCursor)
        apply.clicked.connect(self._on_umu_path_apply)
        bin_row.addWidget(apply)
        body.addLayout(bin_row)

        body.addStretch(1)
        return body

    # ── field helpers ──────────────────────────────────────────────────────

    def _add_launch_field(
        self, layout, label, object_name, value, on_apply, get_value
    ):
        p = self._palette
        row = QHBoxLayout()
        name = QLabel(label, self)
        name.setStyleSheet(
            f"color: {p.text.name()}; font-weight: bold; font-size: 9pt;"
        )
        name.setFixedWidth(64)
        row.addWidget(name)
        edit = QLineEdit(value, self)
        edit.setObjectName(object_name)
        row.addWidget(edit, 1)
        apply = QPushButton("Apply", self)
        apply.setObjectName(f"{object_name}Apply")
        apply.setCursor(Qt.PointingHandCursor)
        apply.clicked.connect(
            lambda: self._on_apply_launch(edit, on_apply, get_value)
        )
        row.addWidget(apply)
        layout.addLayout(row)
        return edit

    def _add_launch_combo(
        self, layout, label, object_name, options, current, on_apply
    ):
        """A labeled dropdown (QComboBox) for a single launch setting."""
        p = self._palette
        row = QHBoxLayout()
        name = QLabel(label, self)
        name.setStyleSheet(
            f"color: {p.text.name()}; font-weight: bold; font-size: 9pt;"
        )
        name.setFixedWidth(64)
        row.addWidget(name)
        combo = QComboBox(self)
        combo.setObjectName(object_name)
        combo.addItems(options)
        if current in options:
            combo.setCurrentText(current)
        row.addWidget(combo, 1)
        apply = QPushButton("Apply", self)
        apply.setObjectName(f"{object_name}Apply")
        apply.setCursor(Qt.PointingHandCursor)
        apply.clicked.connect(lambda: on_apply(combo.currentText()))
        row.addWidget(apply)
        layout.addLayout(row)
        return combo

    def _add_feature_check(
        self,
        layout,
        text,
        object_name,
        checked,
        available,
        on_toggled,
        unavailable_hint,
    ):
        """A labeled checkbox for an optional Linux feature. When the feature
        isn't available on this system the box is disabled and a dim hint
        explains why (the stored value is left untouched)."""
        p = self._palette
        check = QCheckBox(text, self)
        check.setObjectName(object_name)
        check.setCursor(Qt.PointingHandCursor)
        check.blockSignals(True)
        check.setChecked(bool(checked))
        check.blockSignals(False)
        check.setEnabled(bool(available))
        check.toggled.connect(on_toggled)
        layout.addWidget(check)
        if not available:
            hint = QLabel(unavailable_hint, self)
            hint.setObjectName(f"{object_name}Hint")
            hint.setWordWrap(True)
            hint.setStyleSheet(
                f"color: {p.text_dim.name()}; font-size: 8pt; "
                f"margin-left: 24px;"
            )
            layout.addWidget(hint)
        return check

    def _on_apply_launch(self, edit, on_apply, get_value):
        on_apply(edit.text())
        edit.setText(get_value())

    def _on_renderer_apply(self, label):
        value = _RENDERER_BY_LABEL.get(label, "auto")
        self._settings.set_umu_renderer(value)

    def _on_umu_path_apply(self):
        self._settings.set_umu_binary_path(self._umu_bin_edit.text())
        self._umu_bin_edit.setText(self._settings.launch.umu_binary_path)

    def _on_umu_browse(self):
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select umu-run binary", os.path.expanduser("~")
        )
        if chosen:
            self._umu_bin_edit.setText(chosen)
            self._settings.set_umu_binary_path(chosen)
