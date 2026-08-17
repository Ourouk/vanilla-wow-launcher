"""Vanilla WoW Launcher Qt (PySide6) first-run auto-install prompt.

A QDialog shown once after the first-run Settings window closes: two
pre-checked checkboxes ("Install essential mods" / "Install recommended
addons") plus Install/Skip buttons. This is a one-shot, first-run-only choice
— there is no Settings entry for it afterwards; the checked installs run
directly from here.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .theme import Palette, theme_qss


class AutoInstallDialog(QDialog):
    """Ask whether to install the server's essential mods and recommended
    addons for the chosen game folder."""

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self._palette = palette
        p = palette
        self.setObjectName("autoInstallDialog")
        self.setWindowTitle("INSTALL ESSENTIAL CONTENT")
        self.setMinimumWidth(480)
        self.setStyleSheet(
            theme_qss(p)
            + f"\nQDialog {{ background-color: {p.panel.name()}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        title = QLabel("INSTALL ESSENTIAL CONTENT", self)
        title.setStyleSheet(
            f"color: {p.purple.name()}; font-weight: bold; font-size: 12pt;"
        )
        root.addWidget(title)

        hint = QLabel(
            "Your server provides essential mods and recommended addons for "
            "its client. Choose what to install now.",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {p.text_dim.name()}; font-size: 9pt;")
        root.addWidget(hint)
        root.addSpacing(4)

        self._mods_check = QCheckBox("Install essential mods", self)
        self._mods_check.setObjectName("autoInstallMods")
        self._mods_check.setChecked(True)
        self._mods_check.setCursor(Qt.PointingHandCursor)
        root.addWidget(self._mods_check)

        self._addons_check = QCheckBox("Install recommended addons", self)
        self._addons_check.setObjectName("autoInstallAddons")
        self._addons_check.setChecked(True)
        self._addons_check.setCursor(Qt.PointingHandCursor)
        root.addWidget(self._addons_check)
        root.addSpacing(4)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        skip = QPushButton("Skip", self)
        skip.setObjectName("autoInstallSkip")
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(self.reject)
        buttons.addWidget(skip)
        install = QPushButton("Install", self)
        install.setObjectName("autoInstallInstall")
        install.setCursor(Qt.PointingHandCursor)
        install.clicked.connect(self.accept)
        buttons.addWidget(install)
        root.addLayout(buttons)

    @property
    def mods_checked(self) -> bool:
        return self._mods_check.isChecked()

    @property
    def addons_checked(self) -> bool:
        return self._addons_check.isChecked()
