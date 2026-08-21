"""Vanilla WoW Launcher Qt (PySide6) settings dialog.

A dark QDialog rendering the GAME FOLDER row (open-folder link, readonly path
entry, Change), the DOWNLOAD MIRRORS rows (one per configured server/mirror:
status dot + name + status label + a check button), the TROUBLESHOOTING and
SUPPORT THE DEVELOPER clickable rows and the GENERAL checkboxes. It renders
the SettingsController's state and forwards user actions straight into the
toolkit-agnostic controller; mirror results arrive as MirrorStatusChanged
events through the ControllerBridge and are rendered here.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...controllers.settings import SettingsController
from ...core import launcher, platform_support
from . import metrics
from .bridge import ControllerBridge
from .linux_settings_dialog import LinuxSettingsDialog
from .list_panel import ClickableLabel, make_hairline
from .theme import Palette, theme_qss

KO_FI_URL = "https://ko-fi.com/rebased"
BMC_URL = "https://buymeacoffee.com/rebased"


class _ClickableRow(QWidget):
    """A clickable icon+text row. Children are mouse-transparent so a click
    anywhere on the row fires clicked."""

    clicked = Signal()

    def __init__(
        self, icon: str, text: str, palette: Palette, icon_color, parent=None
    ):
        super().__init__(parent)
        p = palette
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        icon_label = QLabel(icon, self)
        icon_label.setStyleSheet(
            f"color: {icon_color.name()}; font-size: 11pt;"
        )
        icon_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        text_label = QLabel(text, self)
        text_label.setWordWrap(True)
        text_label.setStyleSheet(f"color: {p.text.name()}; font-size: 10pt;")
        text_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(icon_label, 0, Qt.AlignLeft)
        layout.addWidget(text_label, 0, Qt.AlignLeft)
        layout.addStretch(1)

        self._palette = palette
        self._text_label = text_label

    def click(self):
        self.clicked.emit()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._text_label.setStyleSheet(
            f"color: {self._palette.gold.name()}; font-size: 10pt;"
        )
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._text_label.setStyleSheet(
            f"color: {self._palette.text.name()}; font-size: 10pt;"
        )
        super().leaveEvent(event)


class SettingsDialog(QDialog):
    """The SETTINGS dialog.

    Constructible and closable headlessly: it reads the controller's state,
    renders the mirror status it already holds, and only starts work when the
    user clicks a row/button. `showLogsRequested` fires for the Show logs row.
    """

    showLogsRequested = Signal()

    def __init__(
        self,
        settings: SettingsController,
        bridge: ControllerBridge,
        palette: Palette,
        parent=None,
    ):
        super().__init__(parent)
        self._settings = settings
        self._palette = palette
        self._linuxDialog = None
        p = palette
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Settings")
        self.setMinimumSize(560, 660)
        self.setStyleSheet(
            theme_qss(p) + f"\nQDialog {{ background-color: {p.bg.name()}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(self._build_divider())
        root.addWidget(self._build_body(), 1)

        bridge.mirrorStatusChanged.connect(self._on_mirror_status)

    # ── build ───────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        p = self._palette
        hdr = QWidget(self)
        layout = QHBoxLayout(hdr)
        layout.setContentsMargins(18, 12, 12, 12)

        title = QLabel("SETTINGS", hdr)
        title.setStyleSheet(
            f"color: {p.purple.name()}; font-weight: bold;"
            f" font-size: {metrics.PT_DIALOG}pt;"
        )
        layout.addWidget(title)
        layout.addStretch(1)
        return hdr

    def _build_divider(self) -> QFrame:
        return make_hairline(self)

    def _build_body(self) -> QWidget:
        p = self._palette
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 16, 22, 12)
        body_layout.setSpacing(8)

        folder_row = QHBoxLayout()
        folder_label = QLabel("GAME FOLDER", body)
        folder_label.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        folder_row.addWidget(folder_label)
        folder_row.addStretch(1)
        open_link = ClickableLabel("Open folder", body)
        open_link.setObjectName("settingsOpenFolder")
        open_link.setCursor(Qt.PointingHandCursor)
        open_link.setStyleSheet(f"color: {p.text_dim.name()}; font-size: 9pt;")
        open_link.clicked.connect(self._settings.open_client_folder)
        folder_row.addWidget(open_link)
        body_layout.addLayout(folder_row)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit(self._settings.state.path, body)
        self._path_edit.setObjectName("settingsPath")
        self._path_edit.setReadOnly(True)
        self._path_edit.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        path_row.addWidget(self._path_edit, 1)
        change_btn = QPushButton("Change", body)
        change_btn.setObjectName("settingsChange")
        change_btn.clicked.connect(self._on_change_dir)
        path_row.addWidget(change_btn)
        body_layout.addLayout(path_row)

        mirror_title = QLabel("DOWNLOAD MIRRORS", body)
        mirror_title.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        body_layout.addWidget(mirror_title)
        body_layout.addSpacing(2)

        self._mirror_rows: dict[str, QLabel] = {}
        self._mirror_dots: dict[str, QLabel] = {}
        names = self._settings._http_mirror_names()
        if not names:
            cfg = launcher.config()
            text = (
                "No server configured (launcher configuration missing)."
                if cfg is None or not cfg.server_url
                else "No HTTP mirrors configured — update uses the server directly."
            )
            hint = QLabel(text, body)
            hint.setObjectName("settingsMirrorEmpty")
            hint.setStyleSheet(f"color: {p.text_dim.name()}; font-size: 9pt;")
            body_layout.addWidget(hint)
        else:
            for name in names:
                row = QHBoxLayout()
                dot = QLabel("●", body)
                dot.setStyleSheet(f"color: {p.text_dim.name()};")
                row.addWidget(dot)
                label = QLabel(name, body)
                label.setStyleSheet(
                    f"color: {p.text.name()}; font-weight: bold; font-size: 10pt;"
                )
                row.addWidget(label)
                status = QLabel("", body)
                status.setObjectName(f"settingsMirrorStatus_{name}")
                status.setStyleSheet(
                    f"color: {p.text_dim.name()}; font-size: 9pt;"
                )
                row.addWidget(status)
                row.addStretch(1)
                body_layout.addLayout(row)
                self._mirror_rows[name] = status
                self._mirror_dots[name] = dot
        refresh = QToolButton(body)
        refresh.setObjectName("settingsMirrorRefresh")
        refresh.setText("⟳  Check mirrors")
        refresh.setToolTip("Check server and mirror reachability")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setVisible(bool(names))
        refresh.setStyleSheet(
            f"QToolButton {{ color: {p.text_dim.name()}; font-size: 9pt; }}"
            f"QToolButton:hover {{ color: {p.gold.name()}; }}"
        )
        refresh.clicked.connect(self._on_refresh_mirror)
        body_layout.addWidget(refresh)

        self._render_mirror_statuses()

        body_layout.addSpacing(6)

        cols = QHBoxLayout()
        cols.setSpacing(24)
        lcol = QWidget(body)
        lcol_layout = QVBoxLayout(lcol)
        lcol_layout.setContentsMargins(0, 0, 0, 0)
        lcol_layout.setSpacing(0)
        lcol_layout.setAlignment(Qt.AlignTop)

        ts_title = QLabel("TROUBLESHOOTING", lcol)
        ts_title.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        lcol_layout.addWidget(ts_title)

        self._add_row(
            lcol_layout,
            "✓",
            "Verify game files",
            self._settings.verify_files,
            "settingsVerify",
            p.gold,
        )
        self._add_row(
            lcol_layout,
            "☰",
            "Show logs",
            self.showLogsRequested.emit,
            "settingsLogs",
            p.gold,
        )
        if platform_support.can_manage_antivirus():
            self._add_row(
                lcol_layout,
                "⛊",
                "Add game folder to Defender exclusions",
                self._settings.allow_through_antivirus,
                "settingsAv",
                p.gold,
            )

        support_title = QLabel("SUPPORT THE DEVELOPER", lcol)
        support_title.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        lcol_layout.addWidget(support_title)
        self._add_row(
            lcol_layout,
            "♥",
            "Ko-fi",
            lambda: self._settings.open_url(KO_FI_URL),
            "settingsKoFi",
            p.pink,
        )
        self._add_row(
            lcol_layout,
            "☕",
            "Buy Me a Coffee",
            lambda: self._settings.open_url(BMC_URL),
            "settingsBmc",
            p.warn,
        )

        rcol = QWidget(body)
        rcol_layout = QVBoxLayout(rcol)
        rcol_layout.setContentsMargins(0, 0, 0, 0)
        rcol_layout.setSpacing(4)
        rcol_layout.setAlignment(Qt.AlignTop)

        general_title = QLabel("GENERAL", rcol)
        general_title.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        rcol_layout.addWidget(general_title)

        cfg = self._settings.state.config
        self._clear_wdb_check = None
        self._close_on_launch_check = None
        if platform_support.can_launch_client():
            self._clear_wdb_check = self._add_check(
                rcol_layout,
                "Clear WDB on game launch",
                "settingsClearWdb",
                bool(cfg.get("clear_wdb_on_launch", False)),
                self._settings.set_clear_wdb,
            )
            self._close_on_launch_check = self._add_check(
                rcol_layout,
                "Close Vanilla WoW Launcher on game launch",
                "settingsCloseOnLaunch",
                bool(cfg.get("close_on_launch", False)),
                self._settings.set_close_on_launch,
            )
        self._client_update_check = self._add_check(
            rcol_layout,
            "Enable client updates",
            "settingsClientUpdate",
            self._settings.client_update_enabled,
            self._settings.set_client_update_enabled,
        )

        if platform_support.is_linux():
            self._build_linux_button(rcol_layout)

        cols.addWidget(lcol, 3)
        cols.addWidget(rcol, 2)
        body_layout.addLayout(cols, 1)

        self._build_registry_section(body_layout)
        return body

    def _add_row(self, layout, icon, text, command, object_name, color):
        row = _ClickableRow(icon, text, self._palette, color, self)
        row.setObjectName(object_name)
        row.clicked.connect(command)
        layout.addWidget(row)
        layout.addSpacing(8)
        return row

    # ── catalog registries ───────────────────────────────────────────────

    def _build_registry_section(self, layout):
        p = self._palette
        layout.addSpacing(10)

        title = QLabel("CATALOG REGISTRIES", self)
        title.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 10pt;"
        )
        layout.addWidget(title)

        hint = QLabel(
            "For advanced users: point the mod/addon catalogs at another "
            "HTTPS JSON registry, or add your own entries via the per-user "
            "custom JSON files.",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {p.text_dim.name()}; font-size: 9pt;")
        layout.addWidget(hint)
        layout.addSpacing(4)

        self._registry_status = QLabel("", self)
        self._registry_status.setObjectName("settingsRegistryStatus")
        self._registry_status.setWordWrap(True)
        self._registry_status.setStyleSheet(
            f"color: {p.err.name()}; font-size: 9pt;"
        )
        layout.addWidget(self._registry_status)
        layout.addSpacing(2)

        self._build_registry_row(
            layout,
            "ADDONS",
            "settingsAddon",
            self._settings.addons_registry_url,
            self._settings.set_addons_registry_url,
            self._settings.reset_addons_registry_url,
            self._settings.reload_addons_registry,
            self._settings.open_addons_custom_file,
            self._settings.clear_addons_custom,
        )
        self._build_registry_row(
            layout,
            "MODS",
            "settingsMod",
            self._settings.mods_registry_url,
            self._settings.set_mods_registry_url,
            self._settings.reset_mods_registry_url,
            self._settings.reload_mods_registry,
            self._settings.open_mods_custom_file,
            self._settings.clear_mods_custom,
        )

    def _build_registry_row(
        self,
        layout,
        label,
        prefix,
        get_url,
        on_apply,
        on_reset,
        on_reload,
        on_open_custom,
        on_clear_custom,
    ):
        p = self._palette
        row = QHBoxLayout()
        name = QLabel(label, self)
        name.setStyleSheet(
            f"color: {p.text.name()}; font-weight: bold; font-size: 9pt;"
        )
        name.setFixedWidth(64)
        row.addWidget(name)

        edit = QLineEdit(get_url(), self)
        edit.setObjectName(f"{prefix}RegistryUrl")
        edit.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        row.addWidget(edit, 1)

        apply_btn = QPushButton("Apply", self)
        apply_btn.setObjectName(f"{prefix}RegistryApply")
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(
            lambda: self._on_apply_registry(edit, get_url, on_apply)
        )
        row.addWidget(apply_btn)

        reset_btn = QToolButton(self)
        reset_btn.setObjectName(f"{prefix}RegistryReset")
        reset_btn.setText("Reset")
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.setToolTip("Use the default server catalog")
        reset_btn.clicked.connect(
            lambda: self._on_reset_registry(edit, get_url, on_reset)
        )
        row.addWidget(reset_btn)

        reload_btn = QToolButton(self)
        reload_btn.setObjectName(f"{prefix}RegistryReload")
        reload_btn.setText("Reload")
        reload_btn.setCursor(Qt.PointingHandCursor)
        reload_btn.setToolTip("Fetch the catalog now and refresh the tab")
        reload_btn.clicked.connect(on_reload)
        row.addWidget(reload_btn)
        layout.addLayout(row)

        links = QHBoxLayout()
        links.addSpacing(64)
        open_link = ClickableLabel("Open custom file", self)
        open_link.setObjectName(f"{prefix}RegistryOpenCustom")
        open_link.setCursor(Qt.PointingHandCursor)
        open_link.setStyleSheet(f"color: {p.gold.name()}; font-size: 9pt;")
        open_link.clicked.connect(on_open_custom)
        links.addWidget(open_link)
        clear_link = ClickableLabel("Clear custom entries", self)
        clear_link.setObjectName(f"{prefix}RegistryClearCustom")
        clear_link.setCursor(Qt.PointingHandCursor)
        clear_link.setStyleSheet(f"color: {p.err.name()}; font-size: 9pt;")
        clear_link.clicked.connect(on_clear_custom)
        links.addWidget(clear_link)
        links.addStretch(1)
        layout.addLayout(links)

    def _on_apply_registry(self, edit, get_url, on_apply):
        err = on_apply(edit.text())
        if err:
            self._registry_status.setText(f"✗ {err}")
        else:
            self._registry_status.setText("")
            edit.setText(get_url())

    def _on_reset_registry(self, edit, get_url, on_reset):
        on_reset()
        edit.setText(get_url())
        self._registry_status.setText("")

    def _add_check(self, layout, text, object_name, checked, on_toggled):
        check = QCheckBox(text, self)
        check.setObjectName(object_name)
        check.setCursor(Qt.PointingHandCursor)
        check.blockSignals(True)
        check.setChecked(bool(checked))
        check.blockSignals(False)
        check.toggled.connect(on_toggled)
        layout.addWidget(check)
        return check

    # ── Linux umu-launcher ────────────────────────────────────────────────

    def _build_linux_button(self, layout):
        """On Linux, a single button that opens the separate Linux (UMU)
        settings window holding every Linux play setting (Proton, renderer,
        GameMode, Wayland, GAMEID, umu-run path)."""
        p = self._palette
        btn = QPushButton("Linux (UMU) Settings…", self)
        btn.setObjectName("settingsLinuxButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {p.gold.name()}; font-size: 10pt; "
            f"text-align: left; padding: 6px 10px; }}"
            f"QPushButton:hover {{ color: {p.text.name()}; }}"
        )
        btn.clicked.connect(self._on_open_linux_settings)
        layout.addWidget(btn)

    def _on_open_linux_settings(self):
        if self._linuxDialog is None:
            self._linuxDialog = LinuxSettingsDialog(
                self._settings, self._palette, self
            )
            self._linuxDialog.finished.connect(self._on_linux_dialog_finished)
        self._linuxDialog.show()
        self._linuxDialog.raise_()
        self._linuxDialog.activateWindow()

    def _on_linux_dialog_finished(self):
        self._linuxDialog = None

    # ── actions ─────────────────────────────────────────────────────────────

    def _on_change_dir(self):
        cur = self._settings.state.path
        initial = cur if os.path.isdir(cur) else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(
            self, "Select game client folder", initial
        )
        if chosen:
            chosen = os.path.normpath(chosen)
            self._settings.set_path(chosen)
            self._path_edit.setText(chosen)

    def _on_refresh_mirror(self):
        p = self._palette
        for name in self._mirror_rows:
            self._mirror_rows[name].setText("checking…")
            self._mirror_rows[name].setStyleSheet(
                f"color: {p.text_dim.name()}; font-size: 9pt;"
            )
            self._mirror_dots[name].setStyleSheet(
                f"color: {p.text_dim.name()};"
            )
        self._settings.check_mirror()

    # ── mirror status rendering ─────────────────────────────────────────────

    def _render_mirror_statuses(self):
        p = self._palette
        statuses = self._settings.mirror_statuses
        for name, status in self._mirror_rows.items():
            text = statuses.get(name, "")
            color = (
                p.ok
                if text == "online"
                else (p.err if text == "offline" else p.text_dim)
            )
            status.setText(text)
            status.setStyleSheet(f"color: {color.name()}; font-size: 9pt;")
            self._mirror_dots[name].setStyleSheet(f"color: {color.name()};")

    def _on_mirror_status(self, ok: bool, text: str):
        self._render_mirror_statuses()
