"""Shared scaffolding for the scrollable list panels (MODS, ADDONS).

Both panels render a scrollable list of rows between a header and a footer,
and both wire the same three bridge signals (the panel's XLoaded snapshot,
plus operationFinished/operationFailed). This module holds that common shell
and the row chrome both panels reuse, so each panel only implements its own
rows, header and footer.
"""

import webbrowser

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .metrics import PT_LINK_ICON


def clear_layout(layout):
    """Drop every widget a layout owns so a re-render can rebuild the list."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class ClickableLabel(QLabel):
    """A QLabel that emits clicked on a left mouse release."""

    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class LinkLabel(ClickableLabel):
    """A QLabel that opens a URL on left-click."""

    def __init__(self, text, url, parent=None):
        super().__init__(text, parent)
        self._url = url
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._url:
            webbrowser.open(self._url)
        super().mouseReleaseEvent(event)


def make_hairline(parent):
    """A 1px horizontal divider, themed by the global stylesheet's
    ``QFrame[role="hairline"]`` rule."""
    line = QFrame(parent)
    line.setFrameShape(QFrame.HLine)
    line.setProperty("role", "hairline")
    return line


# ── row chrome ──────────────────────────────────────────────────────────────


def make_row_shell(parent):
    """→ (root, top, top_layout) with the standard list-row layout."""
    root = QVBoxLayout(parent)
    root.setContentsMargins(0, 6, 4, 0)
    root.setSpacing(3)
    top = QWidget(parent)
    top_layout = QHBoxLayout(top)
    top_layout.setContentsMargins(0, 0, 0, 0)
    top_layout.setSpacing(8)
    return root, top, top_layout


def add_star(top_layout, object_name, show, tooltip, p):
    """The fixed-width star/badge slot at the left of a row's title."""
    label = QLabel("★" if show else "", top_layout.parentWidget())
    label.setObjectName(object_name)
    label.setFixedWidth(20)
    label.setStyleSheet(f"color: {p.gold.name()};")
    if show:
        label.setToolTip(tooltip)
    top_layout.addWidget(label, 0, Qt.AlignTop)
    return label


def add_row_link(top_layout, object_name, url, p):
    """The '⧉' repo-link label pinned to a row's right edge."""
    label = LinkLabel("⧉", url, top_layout.parentWidget())
    label.setObjectName(object_name)
    label.setStyleSheet(f"color: {p.text_dim.name()};")
    font = label.font()
    font.setPointSize(PT_LINK_ICON)
    label.setFont(font)
    label.setFixedWidth(24)
    label.setToolTip(url)
    top_layout.addWidget(label, 0, Qt.AlignTop)
    return label


def add_row_error(root, object_name, error, p):
    """The ⚠ error line under a row; hidden unless there is an error."""
    label = QLabel("", root.parentWidget())
    label.setObjectName(object_name)
    label.setStyleSheet(f"color: {p.err.name()};")
    if error:
        label.setText(f"  \u26a0  {error}")
    label.setVisible(bool(error))
    root.addWidget(label)
    return label


def add_row_divider(root, p):
    """The hairline between consecutive rows."""
    divider = make_hairline(root.parentWidget())
    root.addWidget(divider)
    return divider


class ScrollListPanel(QWidget):
    """Base for the scrollable list tabs (MODS, ADDONS).

    Provides the shared shell: the panel/content/scroll styling and
    objectNames, the rows layout, the XLoaded re-render, the
    operationFinished/Failed wiring (filtered by `_op_kind`) and the
    header/footer separators. Subclasses build their header and footer,
    implement `_render()` and the `_after_loaded` / `_after_operation` hooks.
    """

    def __init__(
        self,
        prefix,
        loaded_signal,
        palette,
        bridge,
        on_badge=None,
        parent=None,
    ):
        super().__init__(parent)
        self._prefix = prefix
        self._palette = palette
        self._on_badge = on_badge or (lambda count: None)
        self._rows: dict = {}
        self.setObjectName(f"{prefix}Panel")
        p = palette
        self.setStyleSheet(
            f"""
            #{prefix}Panel {{ background-color: {p.panel.name()}; }}
            #{prefix}Content {{ background-color: {p.panel.name()}; }}
            #{prefix}Scroll {{ background-color: {p.panel.name()}; border: none; }}
            """
        )
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

        loaded_signal.connect(self._on_loaded)
        bridge.operationFinished.connect(self._on_operation_finished)
        bridge.operationFailed.connect(self._on_operation_failed)

    # ── shell builders ───────────────────────────────────────────────────

    def _add_hsep(self):
        """A hairline divider in the panel's root layout."""
        sep = make_hairline(self)
        self._root_layout.addWidget(sep)
        return sep

    def _add_scroll_list(self):
        """The scroll area + content widget holding the rows."""
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName(f"{self._prefix}Scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self._root_layout.addWidget(self.scroll, 1)
        self._content = QWidget()
        self._content.setObjectName(f"{self._prefix}Content")
        self._rows_layout = QVBoxLayout(self._content)
        self._rows_layout.setContentsMargins(16, 0, 16, 6)
        self._rows_layout.setSpacing(0)
        self._rows_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self._content)
        return self.scroll

    def _clear_rows(self):
        clear_layout(self._rows_layout)
        self._rows = {}

    def _add_row(self, widget):
        self._rows_layout.addWidget(widget)

    # ── event wiring ─────────────────────────────────────────────────────

    def _on_loaded(self, event):
        if event.state is None:
            return
        self._render(event.state)
        self._on_badge(event.state.updates_count)
        self._after_loaded()

    def _on_operation_finished(self, kind, ok, message):
        if kind == self._op_kind:
            self._after_operation()

    def _on_operation_failed(self, kind, message):
        if kind == self._op_kind:
            self._after_operation()

    # ── subclass hooks ───────────────────────────────────────────────────

    def _render(self, state):
        raise NotImplementedError

    def _after_loaded(self):
        pass

    def _after_operation(self):
        pass
