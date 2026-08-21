"""Vanilla WoW Launcher Qt (PySide6) addons panel.

Renders the controller's AddonsState into a searchable, collapsible sectioned
list of `AddonRow` widgets — recommendation star, colored title, word-wrapped
description, repo link, status text and a checkbox — plus
a check-for-updates / custom-addon footer and a nav-badge callback driven by
the out-of-date count. Rows are rebuilt from every AddonsLoaded snapshot the
bridge forwards; user actions are forwarded straight into the toolkit-agnostic
AddonsController. The list shell is shared with the mods panel via
`list_panel.ScrollListPanel`.
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)

from ...core.helpers import (
    parse_wow_colored,
    relative_age,
    strip_wow_colors,
)
from ...services import addons as addons_service
from .list_panel import (
    ClickableLabel,
    ScrollListPanel,
    add_row_divider,
    add_row_error,
    add_row_link,
    add_star,
    make_row_shell,
)
from .theme import Palette

_INTERFACE_VERSION = "11200"


class AddonRow(QWidget):
    """One addon row: install checkbox (checked when installed, unchecked
    when available), star badge, colored title, description, repo link and
    status text. Toggling the checkbox records a pending install/remove."""

    def __init__(
        self,
        rec,
        installed,
        recommended,
        installed_names,
        palette: Palette,
        on_toggle,
        on_retry=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName(f"addonsRow_{rec.folder}")
        p = palette
        toc = rec.toc or {}

        warnings = []
        if toc.get("Interface") and toc["Interface"] != _INTERFACE_VERSION:
            warnings.append(f"Made for client {toc['Interface']}")
        # pfUI bundles its own modules, so its .toc dependencies aren't real
        # missing addons — never warn about them.
        if installed and rec.folder != "pfUI":
            deps = [
                d.strip()
                for d in (toc.get("Dependencies") or "")
                .replace(";", ",")
                .split(",")
                if d.strip()
            ]
            missing = [d for d in deps if d not in installed_names]
            if missing:
                warnings.append("Missing deps: " + ", ".join(missing))

        root, top, top_layout = make_row_shell(self)

        # The install checkbox leads the row, before the title.
        self.checkbox = QCheckBox(top)
        self.checkbox.setObjectName(f"addonsCheck_{rec.folder}")
        self.checkbox.setChecked(installed)
        self.checkbox.setCursor(Qt.PointingHandCursor)
        self.checkbox.setToolTip(
            "Install or remove this addon on the next Apply"
        )
        self.checkbox.toggled.connect(
            lambda checked, f=rec.folder: on_toggle(f, checked)
        )
        top_layout.addWidget(self.checkbox, 0, Qt.AlignTop)

        # Fixed-width slot keeps titles aligned whether or not the star shows.
        self.star_label = add_star(
            top_layout,
            f"addonsStar_{rec.folder}",
            recommended,
            "Recommended addon",
            p,
        )

        # Title honouring WoW colour escapes — one label per colour segment.
        title = toc.get("Title") or rec.folder
        title_box = QWidget(top)
        title_layout = QHBoxLayout(title_box)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        for i, (seg, col) in enumerate(parse_wow_colored(title)[:6]):
            seg_label = QLabel(seg, title_box)
            if i == 0:
                seg_label.setObjectName(f"addonsName_{rec.folder}")
            seg_label.setStyleSheet(
                f"color: {col or p.text.name()}; font-weight: bold;"
            )
            title_layout.addWidget(seg_label)
        top_layout.addWidget(title_box, 0, Qt.AlignTop)

        top_layout.addStretch(1)

        # Right side pinned to the edge: status text and repo link.
        if rec.status == "downloading":
            status = QLabel("downloading…", top)
            status.setStyleSheet(f"color: {p.text_dim.name()};")
        elif rec.status == "unknown":
            if rec.git:
                # Remote couldn't be reached to compare SHAs — a retry, not
                # an error (the log holds the actual cause).
                status = ClickableLabel("⟳ Couldn't check", top)
                status.setStyleSheet(
                    f"color: {p.warn.name()}; font-weight: bold;"
                )
                status.setToolTip(
                    "Couldn't reach the remote to check for updates — "
                    "click to retry"
                )
                if on_retry is not None:
                    status.clicked.connect(lambda: on_retry(rec))
            else:
                # Installed but not in the catalog and never recorded — no
                # source to check against, so just note it isn't tracked.
                status = QLabel("Not tracked", top)
                status.setStyleSheet(f"color: {p.text_dim.name()};")
                status.setToolTip(
                    "This addon isn't in the launcher's catalog and wasn't "
                    "installed by the launcher — it isn't tracked for "
                    "updates."
                )
        elif rec.status == "invalid" or rec.error:
            status = QLabel("⛔ Addon error", top)
            status.setStyleSheet(f"color: {p.err.name()};")
        elif rec.status == "outOfDate" and installed:
            status = ClickableLabel("Update", top)
            status.setStyleSheet(f"color: {p.gold.name()}; font-weight: bold;")
            status.clicked.connect(lambda: on_toggle(rec.folder, True))
        elif warnings:
            status = QLabel(f"⚠ {warnings[0]}", top)
            status.setStyleSheet(f"color: {p.warn.name()};")
        elif rec.status == "upToDate":
            # The NEED UPDATE / INSTALLED categories say it all — a per-row
            # "Up to date" would just be noise.
            status = None
        else:
            status = QLabel("Not versioned", top)
            status.setStyleSheet(f"color: {p.text_dim.name()};")
        if status is not None:
            status.setObjectName(f"addonsStatus_{rec.folder}")
            top_layout.addWidget(status, 0, Qt.AlignTop)

        if rec.git:
            repo_url = rec.git[:-4] if rec.git.endswith(".git") else rec.git
            self.link_label = add_row_link(
                top_layout, f"addonsLink_{rec.folder}", repo_url, p
            )
        else:
            self.link_label = None

        root.addWidget(top)

        desc = strip_wow_colors(toc.get("Notes") or rec.description or "")
        self.desc_label = QLabel(desc, self)
        self.desc_label.setObjectName(f"addonsDesc_{rec.folder}")
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet(f"color: {p.text_dim.name()};")
        root.addWidget(self.desc_label)

        self.error_label = add_row_error(
            root,
            f"addonsError_{rec.folder}",
            None if rec.status == "unknown" else rec.error,
            p,
        )

        add_row_divider(root, p)


class AddonsPanel(ScrollListPanel):
    """The ADDONS tab: a searchable, collapsible addon list with a footer.

    Renders from the controller's state; re-renders on every AddonsLoaded and
    on filter/section changes. The optional `on_badge` callback receives the
    out-of-date count after each snapshot so the shell can paint a nav-tab
    badge. The custom-addon footer button only emits `customAddonRequested` —
    the dialog wiring lives in the main window.
    """

    customAddonRequested = Signal()

    def __init__(
        self, addons, bridge, palette: Palette, parent=None, on_badge=None
    ):
        super().__init__(
            "addons", bridge.addonsLoaded, palette, bridge, on_badge, parent
        )
        self._addons = addons
        self._op_kind = "addons"
        self._build_header()
        self._add_scroll_list()
        self._build_footer()
        self._render(self._addons.state)

    # ── shell ─────────────────────────────────────────────────────────────

    def _build_header(self):
        p = self._palette
        top = QWidget(self)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(16, 8, 16, 6)
        top_layout.setSpacing(8)

        legend = QWidget(top)
        legend_layout = QHBoxLayout(legend)
        legend_layout.setContentsMargins(0, 0, 0, 0)
        legend_layout.setSpacing(0)
        for text, color in (
            ("Addons marked with ", p.text_dim),
            ("★", p.gold),
            (" are recommended", p.text_dim),
        ):
            part = QLabel(text, legend)
            part.setStyleSheet(f"color: {color.name()};")
            legend_layout.addWidget(part)
        top_layout.addWidget(legend)

        top_layout.addStretch(1)

        self._age_label = QLabel("", top)
        self._age_label.setObjectName("addonsCatalogAge")
        self._age_label.setStyleSheet(f"color: {p.text_dim.name()};")
        self._age_label.hide()
        top_layout.addWidget(self._age_label)

        refresh = QToolButton(top)
        refresh.setObjectName("addonsCheck")
        refresh.setText("⟳  Check for updates")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setToolTip(
            "Re-check every addon against its repository "
            "(reloads the catalog too)"
        )
        refresh.setAccessibleName("Check addons for updates")
        refresh.clicked.connect(self._on_check)
        top_layout.addWidget(refresh)

        self._filter = QLineEdit(top)
        self._filter.setObjectName("addonsFilter")
        self._filter.setPlaceholderText("⌕  Search addons")
        self._filter.setClearButtonEnabled(True)
        self._filter.setFixedWidth(240)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._render)
        self._filter.textChanged.connect(lambda *_: self._debounce.start())
        top_layout.addWidget(self._filter)
        self._root_layout.addWidget(top)
        self._add_hsep()
        self._refresh_age_label()

    def _refresh_age_label(self):
        ts = addons_service.catalog_last_updated()
        if ts:
            self._age_label.setText(f"Catalog updated {relative_age(ts)}")
            self._age_label.show()
        else:
            self._age_label.hide()

    def _build_footer(self):
        p = self._palette
        self._add_hsep()
        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 6, 16, 10)

        self._recommended_button = QPushButton(
            "★  Install Recommended", footer
        )
        self._recommended_button.setObjectName("addonsInstallRecommended")
        self._recommended_button.setCursor(Qt.PointingHandCursor)
        self._recommended_button.setProperty("variant", "primary")
        self._recommended_button.clicked.connect(self._on_install_recommended)
        footer_layout.addWidget(self._recommended_button)

        footer_layout.addStretch(1)

        self._apply_button = QPushButton("Apply", footer)
        self._apply_button.setObjectName("addonsApply")
        self._apply_button.setCursor(Qt.PointingHandCursor)
        self._apply_button.setProperty("variant", "positive")
        self._apply_button.clicked.connect(self._apply)
        self._apply_button.setVisible(False)
        footer_layout.addWidget(self._apply_button)

        custom = QToolButton(footer)
        custom.setObjectName("addonsCustom")
        custom.setText("+  Add custom git addon")
        custom.setCursor(Qt.PointingHandCursor)
        custom.setStyleSheet(f"color: {p.pink.name()}; font-weight: bold;")
        custom.clicked.connect(self.customAddonRequested.emit)
        footer_layout.addWidget(custom)

        footer_layout.addStretch(1)

        self._footer_label = QToolButton(footer)
        self._footer_label.setObjectName("addonsFooter")
        self._footer_label.setCursor(Qt.PointingHandCursor)
        self._footer_label.clicked.connect(self._on_update_all)
        footer_layout.addWidget(self._footer_label)

        self._root_layout.addWidget(footer)

    # ── rendering ───────────────────────────────────────────────────────────

    def _matches(self, rec: dict) -> bool:
        """Space-insensitive filter both ways: "sell value" finds SellValue,
        "sellvalue" finds "Sell Value"."""
        flt = self._filter.text().strip().lower()
        if not flt:
            return True
        title = strip_wow_colors((rec.get("toc") or {}).get("Title") or "")
        hay = f"{rec['folder']} {title}".lower()
        return flt in hay or flt.replace(" ", "") in hay.replace(" ", "")

    def _render(self, state=None):
        state = state or self._addons.state
        self._clear_rows()

        installed = [
            r for r in state.addons.values() if self._matches(r.to_dict())
        ]
        installed.sort(key=lambda r: r.folder.lower())
        available = [
            a
            for a in state.available
            if a.folder not in state.addons and self._matches(a.to_dict())
        ]
        # Recommended addons sort first, then by folder name.
        available.sort(
            key=lambda a: (
                a.folder not in self._addons.recommended,
                a.folder.lower(),
            )
        )

        installed_names = set(state.addons)
        # Stale installs get their own category on top; everything else
        # installed stays under INSTALLED.
        need_update = [r for r in installed if r.status == "outOfDate"]
        up_to_date = [r for r in installed if r.status != "outOfDate"]
        sections = [("INSTALLED", up_to_date)]
        if need_update:
            sections.insert(0, ("NEED UPDATE", need_update))
        sections.append(("AVAILABLE", available))
        for title, rows in sections:
            self._add_section_header(title, rows, state)
            if state.sections_open.get(title, True):
                for rec in rows:
                    row = AddonRow(
                        rec,
                        installed=rec.folder in installed_names,
                        recommended=rec.folder in self._addons.recommended,
                        installed_names=installed_names,
                        palette=self._palette,
                        on_toggle=self._on_toggle,
                        on_retry=self._on_retry,
                        parent=self._content,
                    )
                    self._rows[rec.folder] = row
                    self._add_row(row)

        self._refresh_footer()
        self._refresh_recommended_visibility()

    def _add_section_header(self, title: str, rows: list, state):
        p = self._palette
        is_open = state.sections_open.get(title, True)

        hdr = QWidget(self._content)
        hdr.setObjectName(f"addonsSection_{title}")
        layout = QHBoxLayout(hdr)
        layout.setContentsMargins(0, 10, 0, 2)
        layout.setSpacing(4)

        toggle = QToolButton(hdr)
        toggle.setObjectName(f"addonsToggle_{title}")
        toggle.setText("▾" if is_open else "▸")
        toggle.setCursor(Qt.PointingHandCursor)
        toggle.setToolTip("Collapse or expand this section")
        toggle.setAccessibleName(f"{title} section")
        toggle.clicked.connect(lambda: self._toggle_section(title))
        layout.addWidget(toggle)

        label = ClickableLabel(title, hdr)
        label.setStyleSheet(
            f"color: {p.gold.name()}; font-weight: bold; font-size: 11pt;"
        )
        label.clicked.connect(lambda: self._toggle_section(title))
        layout.addWidget(label)

        count = QLabel(f"  {len(rows)}", hdr)
        count.setStyleSheet(f"color: {p.text_dim.name()};")
        layout.addWidget(count)

        layout.addStretch(1)
        self._rows_layout.addWidget(hdr)

        if is_open and not rows:
            msg = (
                "Verifying…" if state.state == "verifying" else "Nothing here."
            )
            empty = QLabel(msg, self._content)
            empty.setStyleSheet(f"color: {p.text_dim.name()};")
            self._rows_layout.addWidget(empty)

    def _toggle_section(self, title: str):
        self._addons.state.sections_open[
            title
        ] = not self._addons.state.sections_open.get(title, True)
        self._render()

    # ── actions ─────────────────────────────────────────────────────────────

    def _on_toggle(self, folder: str, checked: bool):
        is_installed = folder in self._addons.state.addons
        if checked == is_installed:
            self._addons.state.pending.pop(folder, None)
        else:
            self._addons.toggle(folder, checked)
        self._refresh_apply_visibility()

    def _apply(self):
        self._set_running(True)
        self._addons.apply_pending()

    def _on_check(self):
        if self._addons.verify(force=True):
            self._refresh_footer()
            self._refresh_age_label()

    def _on_retry(self, rec):
        """Re-verify after a "Couldn't check" — same force-verify as the
        header's Check for updates."""
        self._on_check()

    def _on_update_all(self):
        self._set_running(True)
        if self._addons.apply(self._addons.update_all()):
            self._render()

    def _on_install_recommended(self):
        self._set_running(True)
        if self._addons.apply_recommended_addons():
            self._render()

    def _set_running(self, running: bool):
        self._running = running
        self._apply_button.setEnabled(not running)
        self._refresh_recommended_visibility()

    def _refresh_apply_visibility(self):
        """Apply is offered only when there are pending checkbox changes."""
        self._apply_button.setVisible(bool(self._addons.state.pending))

    # ── footer ──────────────────────────────────────────────────────────────

    def _refresh_footer(self):
        text, fg, cursor = self._addons.footer_state()
        self._footer_label.setText(text)
        self._footer_label.setStyleSheet(f"color: {fg}; font-weight: bold;")
        clickable = cursor == "hand2"
        self._footer_label.setEnabled(clickable)
        self._footer_label.setCursor(
            Qt.PointingHandCursor if clickable else Qt.ArrowCursor
        )

    def _refresh_recommended_visibility(self):
        """The 'Install Recommended' button is enabled only while there is at
        least one recommended addon not yet installed and no install is
        running — otherwise it greys out."""
        installed = set(self._addons.state.addons)
        remaining = set(self._addons.recommended) - installed
        busy = bool(self._addons.state.busy)
        self._recommended_button.setEnabled(bool(remaining) and not busy)

    # ── event hooks ────────────────────────────────────────────────────────

    def _after_loaded(self):
        self._refresh_recommended_visibility()
        self._refresh_apply_visibility()
        self._refresh_age_label()

    def _after_operation(self):
        self._set_running(False)
        self._refresh_footer()
        self._refresh_recommended_visibility()
        self._refresh_apply_visibility()
