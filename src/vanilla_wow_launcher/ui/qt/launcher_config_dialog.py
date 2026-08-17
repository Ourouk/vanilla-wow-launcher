"""Vanilla WoW Launcher Qt (PySide6) first-launch launcher config dialog.

A QDialog shown on first launch to let the user pick a server before any
MainWindow exists. The server list comes from this repo's ``servers.json``
index (fetched by the caller and passed in); selecting a server fetches and
validates that server's ``vanilla_wow_launcher.json`` over HTTPS. A local-file
"Browse…" option is kept for custom/private servers not in the list. The
chosen selection is exposed via ``selection()`` as
``{"kind": "file", "path": ...}`` or ``{"kind": "remote", "config_url": ...}``.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ...core import launcher
from ...services import server_index
from .theme import Palette, theme_qss

_ROLE_SERVER = Qt.UserRole


class LauncherConfigDialog(QDialog):
    """First-launch wizard: pick a server (fetched index) or a local file."""

    def __init__(
        self,
        palette: Palette | None = None,
        parent=None,
        initial_path: str = "",
        servers: list[dict] | None = None,
    ):
        super().__init__(parent)
        p = palette or Palette()
        self._palette = p
        self._servers = list(servers or [])
        self._selected_path = ""
        self._selection = None
        self.setObjectName("launcherConfigDialog")
        self.setWindowTitle("FIRST LAUNCH — CHOOSE A SERVER")
        self.setMinimumWidth(560)
        self.setStyleSheet(
            theme_qss(p)
            + f"\nQDialog {{ background-color: {p.panel.name()}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        title = QLabel("FIRST LAUNCH — CHOOSE A SERVER", self)
        title.setObjectName("launcherConfigTitle")
        title.setStyleSheet(
            f"color: {p.purple.name()}; font-weight: bold; font-size: 12pt;"
        )
        root.addWidget(title)

        intro = QLabel(
            "Pick the private server you want to play on. Its configuration is "
            "fetched automatically; the game client will be installed into "
            "your Games folder for that server. You can also choose a local "
            "configuration file for a server not listed here.",
            self,
        )
        intro.setObjectName("launcherConfigIntro")
        intro.setStyleSheet(f"color: {p.text_dim.name()};")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._status = QLabel("", self)
        self._status.setObjectName("launcherConfigStatus")
        self._status.setStyleSheet(f"color: {p.text_dim.name()};")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._list = QListWidget(self)
        self._list.setObjectName("launcherConfigServers")
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self._list)

        path_row = QHBoxLayout()
        self._path = QLineEdit(self)
        self._path.setObjectName("launcherConfigPath")
        self._path.setReadOnly(True)
        self._path.setPlaceholderText("No configuration selected yet")
        self._path.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self._path)
        browse = QPushButton("Browse…", self)
        browse.setObjectName("launcherConfigBrowse")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self.browse)
        path_row.addWidget(browse)
        root.addLayout(path_row)

        self._error = QLabel("", self)
        self._error.setObjectName("launcherConfigError")
        self._error.setStyleSheet(f"color: {p.err.name()};")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.setObjectName("launcherConfigCancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("Continue", self)
        ok.setObjectName("launcherConfigOk")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._submit)
        buttons.addWidget(ok)
        root.addLayout(buttons)

        self._populate_servers()
        if initial_path and os.path.isfile(initial_path):
            self._path.setText(initial_path)
        self._refresh_ok()

    # ── population ───────────────────────────────────────────────────────

    def _populate_servers(self):
        self._list.clear()
        if not self._servers:
            self._status.setText(
                "Couldn't load the server list (offline?). You can still "
                "choose a local configuration file below."
            )
            self._list.setEnabled(False)
            return
        self._status.setText(
            "Select a server, or choose a local configuration file below."
        )
        self._list.setEnabled(True)
        for srv in self._servers:
            item = QListWidgetItem(srv["name"])
            item.setData(_ROLE_SERVER, srv)
            desc = (srv.get("description") or "").strip()
            if desc:
                item.setToolTip(desc)
            self._list.addItem(item)

    # ── interaction ──────────────────────────────────────────────────────

    def browse(self):
        current = self._path.text()
        start_dir = os.path.dirname(current) if current else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select launcher configuration",
            start_dir,
            "Launcher configuration (*.json)",
        )
        if path:
            self._path.setText(path)
            self._list.clearSelection()

    def _on_selection_changed(self):
        if self._list.selectedItems():
            # Selecting a server deselects the local-file choice.
            self._path.clear()
        self._refresh_ok()

    def _on_path_changed(self, _text):
        self._error.clear()
        self._error.hide()
        if self._path.text().strip():
            self._list.clearSelection()
        self._refresh_ok()

    def _refresh_ok(self):
        ready = bool(self._list.selectedItems()) or bool(
            self._path.text().strip()
        )
        ok = self.findChild(QPushButton, "launcherConfigOk")
        if ok is not None:
            ok.setEnabled(ready)

    def _clear_error(self):
        self._error.clear()
        self._error.hide()

    def _show_error(self, message: str):
        self._error.setText(message)
        self._error.show()

    # ── submission ───────────────────────────────────────────────────────

    def _submit(self):
        item = self._list.currentItem()
        if item is not None and item.isSelected():
            self._submit_remote(item.data(_ROLE_SERVER))
            return
        path = self._path.text().strip()
        if not path:
            self._show_error(
                "Select a server from the list, or choose a local "
                "configuration file."
            )
            return
        config, err = launcher.validate_path(path)
        if config is None:
            self._show_error(
                str(err) or "Please choose a valid vanilla_wow_launcher.json."
            )
            return
        self._selected_path = path
        self._selection = {"kind": "file", "path": path}
        self.accept()

    def _submit_remote(self, server: dict):
        self._clear_error()
        from PySide6.QtWidgets import QApplication

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            data, raw, err = server_index.fetch_server_config(
                server["config_url"]
            )
            if err:
                self._show_error(err)
                return
            config, verr = launcher.validate_dict(data)
            if config is None:
                self._show_error(
                    str(verr) or "The server configuration is invalid."
                )
                return
        finally:
            QApplication.restoreOverrideCursor()
        self._selected_path = ""
        self._selection = {
            "kind": "remote",
            "config_url": server["config_url"],
            "name": server.get("name", ""),
            "raw": raw,
        }
        self.accept()

    # ── results ──────────────────────────────────────────────────────────

    def selected_path(self) -> str:
        """The chosen local file path (file selection) or "" (remote)."""
        return self._selected_path

    def selection(self) -> dict | None:
        """The chosen selection: {"kind": "file", "path"} or
        {"kind": "remote", "config_url", "name"}, or None if cancelled."""
        return self._selection
