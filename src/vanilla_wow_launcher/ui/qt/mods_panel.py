"""Vanilla WoW Launcher Qt (PySide6) mods panel.

Renders the mod registry into a scrollable list of `ModRow` widgets —
install checkbox, essential star badge, name/version, repo link,
retry/update action and error line — plus an Apply footer and a nav-badge
callback driven by the updates count. Rows are rebuilt from every ModsLoaded
snapshot the bridge forwards; user actions are forwarded straight into the
toolkit-agnostic ModsController. The list shell is shared with the addons
panel via `list_panel.ScrollListPanel`.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

from ...core.helpers import relative_age
from ...services import mods as mods_service
from .list_panel import (
    ScrollListPanel,
    add_row_divider,
    add_row_error,
    add_row_link,
    add_star,
    make_row_shell,
)
from .theme import Palette


class ModRow(QWidget):
    """One mod row: install checkbox, star badge, name/version, repo link,
    retry/update action, word-wrapped description and an error line under
    the row."""

    def __init__(
        self, mod, rec, pend, latest_versions, action, palette, parent=None
    ):
        super().__init__(parent)
        self.mod_id = mod["id"]
        p = palette
        mid = mod["id"]
        self.setObjectName(f"modsRow_{mid}")

        installed_version = rec.installed_version if rec else None
        has_error = rec.error if rec else None
        installed = rec.present if rec is not None else False
        enabled = (
            pend.enabled
            if pend is not None and pend.enabled is not None
            else (rec.enabled if rec else False)
        )
        essential = mod.get("essential", False)

        name_col = p.err if has_error else (p.mod_hl if installed else p.text)
        desc_col = p.text if enabled else p.text_dim
        version = installed_version or latest_versions.get(mid) or "unknown"

        root, top, top_layout = make_row_shell(self)

        # The install/enable checkbox leads the row, before the name.
        self.enabled_check = QCheckBox(top)
        self.enabled_check.setObjectName(f"modsCheck_{mid}")
        self.enabled_check.setCursor(Qt.PointingHandCursor)
        self.enabled_check.setChecked(enabled)
        self.enabled_check.setToolTip(
            "Enable or disable this mod for the next launch"
        )
        top_layout.addWidget(self.enabled_check, 0, Qt.AlignTop)

        # Fixed-width slot keeps names aligned whether or not the star shows.
        self.star_label = add_star(
            top_layout, f"modsStar_{mid}", essential, "Essential mod", p
        )

        self.name_label = QLabel(mod["name"], top)
        self.name_label.setObjectName(f"modsName_{mid}")
        self.name_label.setStyleSheet(
            f"color: {name_col.name()}; font-weight: bold;"
        )
        top_layout.addWidget(self.name_label, 0, Qt.AlignTop)

        self.version_label = QLabel(f"  {version}", top)
        self.version_label.setObjectName(f"modsVer_{mid}")
        self.version_label.setStyleSheet(f"color: {p.text_dim.name()};")
        top_layout.addWidget(self.version_label, 0, Qt.AlignTop)

        top_layout.addStretch(1)

        self.action_button = None
        if action in ("retry", "update"):
            # Display labels are Title Case per panel-action convention;
            # the action *kind* itself ("retry"/"update") is machine-facing.
            self.action_button = QPushButton(action.capitalize(), top)
            self.action_button.setObjectName(f"modsAction_{mid}")
            self.action_button.setCursor(Qt.PointingHandCursor)
            self.action_button.setProperty("variant", "compact")
            self.action_button.setToolTip(
                "Retry the last failed action"
                if action == "retry"
                else "Update this mod to the latest version"
            )
            top_layout.addWidget(self.action_button)

        if mod.get("repo_url"):
            self.link_label = add_row_link(
                top_layout, f"modsLink_{mid}", mod["repo_url"], p
            )
        else:
            self.link_label = None

        root.addWidget(top)

        self.desc_label = QLabel(mod["description"], self)
        self.desc_label.setObjectName(f"modsDesc_{mid}")
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet(f"color: {desc_col.name()};")
        root.addWidget(self.desc_label)

        self.error_label = add_row_error(
            root, f"modsError_{mid}", has_error, p
        )

        add_row_divider(root, p)


class ModsPanel(ScrollListPanel):
    """The MODS tab: a scrollable mod list with an Apply footer.

    Renders from the controller's registry and state; re-renders on every
    ModsLoaded and forwards checkbox/action clicks into the controller. The
    optional `on_badge` callback receives the updates count after each
    snapshot so the shell can paint a nav-tab badge.
    """

    def __init__(
        self, mods, bridge, palette: Palette, parent=None, on_badge=None
    ):
        super().__init__(
            "mods", bridge.modsLoaded, palette, bridge, on_badge, parent
        )
        self._mods = mods
        self._op_kind = "mods"
        self._running = False
        self._build_header()
        self._add_scroll_list()
        self._build_footer()
        self._render(self._mods.state)
        self._refresh_apply_visibility()

    # ── shell ─────────────────────────────────────────────────────────────

    def _build_header(self):
        p = self._palette
        banner = QWidget(self)
        banner.setObjectName("modsBanner")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(16, 12, 16, 8)
        banner_layout.setSpacing(6)
        for text, color in (
            ("Mods marked with ", p.text_dim),
            ("★", p.gold),
            (" are essential", p.text_dim),
        ):
            part = QLabel(text, banner)
            part.setStyleSheet(f"color: {color.name()};")
            banner_layout.addWidget(part)
        banner_layout.addStretch(1)

        self._age_label = QLabel("", banner)
        self._age_label.setObjectName("modsCatalogAge")
        self._age_label.setStyleSheet(f"color: {p.text_dim.name()};")
        self._age_label.hide()
        banner_layout.addWidget(self._age_label)

        refresh = QToolButton(banner)
        refresh.setObjectName("modsCatalogRefresh")
        refresh.setText("⟳")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setToolTip("Reload the mod catalog from the server")
        refresh.setAccessibleName("Reload mod catalog")
        refresh.clicked.connect(self._on_refresh_catalog)
        banner_layout.addWidget(refresh)

        self._root_layout.addWidget(banner)
        self._add_hsep()
        self._refresh_age_label()

    def _refresh_age_label(self):
        ts = mods_service.catalog_timestamp()
        if ts:
            self._age_label.setText(f"Catalog updated {relative_age(ts)}")
            self._age_label.show()
        else:
            self._age_label.hide()

    def _on_refresh_catalog(self):
        self._mods.reload_catalog()

    def _after_loaded(self):
        self._refresh_apply_visibility()
        self._refresh_recommended_visibility()
        self._refresh_age_label()

    def _build_footer(self):
        self._add_hsep()
        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 6, 16, 10)
        self._recommended_button = QPushButton("★  Install Essential", footer)
        self._recommended_button.setObjectName("modsInstallRecommended")
        self._recommended_button.setCursor(Qt.PointingHandCursor)
        self._recommended_button.setProperty("variant", "primary")
        self._recommended_button.clicked.connect(self._on_install_recommended)
        footer_layout.addWidget(self._recommended_button)

        self._apply_button = QPushButton("Apply", footer)
        self._apply_button.setObjectName("modsApply")
        self._apply_button.setCursor(Qt.PointingHandCursor)
        self._apply_button.setProperty("variant", "primary")
        self._apply_button.clicked.connect(self._apply)
        self._apply_button.setVisible(False)
        footer_layout.addWidget(self._apply_button)
        footer_layout.addStretch(1)
        self._root_layout.addWidget(footer)

    # ── rendering ───────────────────────────────────────────────────────────

    def _render(self, state):
        if state is None:
            return
        self._clear_rows()
        if not self._mods.registry:
            p = self._palette
            empty = QLabel(
                "No mods catalog available.\n"
                "Configure a catalog URL under Settings → Catalog "
                "registries, then use Reload.",
                self._content,
            )
            empty.setObjectName("modsEmptyState")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {p.text_dim.name()};")
            self._add_row(empty)
        for mod in sorted(
            self._mods.registry, key=lambda m: m["name"].lower()
        ):
            mid = mod["id"]
            row = ModRow(
                mod,
                state.records.get(mid),
                state.pending.get(mid),
                state.latest_versions,
                self._mods.action_for(mid),
                self._palette,
                self._content,
            )
            row.enabled_check.toggled.connect(
                lambda checked, m=mid: self._on_enabled_toggled(m, checked)
            )
            if row.action_button is not None:
                row.action_button.clicked.connect(
                    lambda checked=False, m=mid: self._on_action(m)
                )
            self._rows[mid] = row
            self._add_row(row)
        self._render_unknown(state)
        self._refresh_recommended_visibility()

    def _render_unknown(self, state):
        """Rows for dlls.txt entries no catalog mod claims — mods the client
        loads that the launcher doesn't track — each with a filesystem-first
        Remove button."""
        unknown = getattr(state, "unknown", None)
        if not unknown:
            return
        p = self._palette
        head = QLabel("Detected (not in catalog)", self._content)
        head.setObjectName("modsUnknownHeader")
        head.setStyleSheet(f"color: {p.text_dim.name()}; font-weight: bold;")
        head.setContentsMargins(0, 10, 0, 2)
        self._add_row(head)
        for name in unknown:
            shell = QWidget(self._content)
            shell.setObjectName(f"modsUnknownRow_{name}")
            root, top, top_layout = make_row_shell(shell)
            label = QLabel(name, shell)
            label.setObjectName(f"modsUnknownName_{name}")
            label.setStyleSheet(f"color: {p.text_dim.name()};")
            top_layout.addWidget(label, 0, Qt.AlignTop)
            top_layout.addStretch(1)
            remove = QPushButton("Remove", shell)
            remove.setObjectName(f"modsUnknownRemove_{name}")
            remove.setCursor(Qt.PointingHandCursor)
            remove.setToolTip("Delete this file and its dlls.txt entry")
            remove.setProperty("variant", "outline")
            remove.clicked.connect(
                lambda checked=False, n=name: self._on_remove_unknown(n)
            )
            top_layout.addWidget(remove, 0, Qt.AlignTop)
            root.addWidget(top)
            add_row_divider(root, p)
            self._add_row(shell)

    def _on_remove_unknown(self, name):
        self._mods.remove_unknown(name)

    def _refresh_apply_visibility(self):
        """Apply is offered only when there is something to apply: pending
        checkbox changes, or a failed mod the user may want to retry."""
        st = self._mods.state
        self._apply_button.setVisible(
            bool(st.has_pending_changes or st.has_errors)
        )

    def _essential_remaining(self) -> list:
        """Essential mods (registry flag) not yet present on disk."""
        remaining = []
        for mod in self._mods.registry:
            if not mod.get("essential", False):
                continue
            rec = self._mods.state.records.get(mod["id"])
            if rec is not None and rec.present:
                continue
            remaining.append(mod["id"])
        return remaining

    def _refresh_recommended_visibility(self):
        """The 'Install Essential' button is enabled only while there is at
        least one essential mod not yet installed and no install is running —
        otherwise it greys out."""
        self._recommended_button.setEnabled(
            bool(self._essential_remaining())
            and not self._running
            and not self._mods.busy
        )

    # ── actions ─────────────────────────────────────────────────────────────

    def _on_enabled_toggled(self, mid, checked):
        self._mods.toggle(mid, checked)
        self._refresh_apply_visibility()

    def _on_action(self, mid):
        self._set_running(True)
        self._mods.apply(only_mod_id=mid)

    def _apply(self):
        self._set_running(True)
        self._mods.apply()

    def _on_install_recommended(self):
        if self._mods.apply_essential_mods():
            self._set_running(True)
        else:
            self._refresh_recommended_visibility()

    def _set_running(self, running: bool):
        self._running = running
        self._apply_button.setEnabled(not running)
        if running:
            self._apply_button.setText("Applying…")
        else:
            self._apply_button.setText("Apply")
        for row in self._rows.values():
            if row.action_button is not None:
                row.action_button.setEnabled(not running)
        self._refresh_recommended_visibility()

    # ── event hooks ────────────────────────────────────────────────────────

    def _after_operation(self):
        self._set_running(False)
        self._refresh_apply_visibility()
