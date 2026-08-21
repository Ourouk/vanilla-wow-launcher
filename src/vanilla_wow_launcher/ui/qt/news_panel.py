"""Vanilla WoW Launcher Qt (PySide6) news panel — featured post + announcements.

The left `FeaturedPanel` shows the featured forum post on the parchment
background, the right `AnnouncementsPanel` lists the dated announcements on
the dark panel. Both render the `NewsLoaded` events the ControllerBridge
forwards and keep their state so switching tabs preserves the content.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.helpers import format_news_date, strip_html
from ...state.events import NewsLoaded
from .list_panel import LinkLabel, clear_layout, make_hairline

_LOADING = "Loading…"
_ERROR = "Couldn't reach the news feed."
_EMPTY = "No news yet — check back later."
_BODY_LIMIT = 260


class FeaturedPanel(QWidget):
    """Parchment panel showing the featured forum post."""

    def __init__(self, news, palette, parent=None):
        super().__init__(parent)
        self._news = news
        p = palette
        self.setObjectName("featuredPanel")
        self.setStyleSheet(
            f"""
            #featuredPanel {{ background-color: {p.parch.name()};
                             border: 1px solid {p.parch_edge.name()}; }}
            #featuredBand {{ background-color: {p.parch_band.name()}; }}
            #featuredTitle {{ color: {p.parch_title.name()};
                             font-weight: bold; font-size: 13pt; }}
            #featuredByline {{ color: {p.parch_dim.name()};
                              font-style: italic; font-size: 10pt;
                              background-color: {p.parch_band.name()}; }}
            #featuredStatus {{ color: {p.parch_dim.name()};
                              font-size: 10pt; }}
            #featuredBody {{ background-color: {p.parch.name()};
                            color: {p.parch_text.name()};
                            border: none; font-size: 11pt; }}
            #featuredLink {{ color: {p.parch_link.name()};
                            font-size: 11pt; }}
            #featuredLink:hover {{ color: {p.parch_title.name()}; }}
            #featuredRefresh {{ color: {p.parch_dim.name()};
                               font-size: 14pt; }}
            #featuredRefresh:hover {{ color: {p.parch_link.name()}; }}
            """
        )

        band = QWidget(self)
        band.setObjectName("featuredBand")
        band_layout = QHBoxLayout(band)
        band_layout.setContentsMargins(20, 16, 12, 12)

        self.title_label = QLabel("NEWS", band)
        self.title_label.setObjectName("featuredTitle")
        self.title_label.setWordWrap(True)
        band_layout.addWidget(self.title_label, 1)

        self.refresh_button = QToolButton(band)
        self.refresh_button.setObjectName("featuredRefresh")
        self.refresh_button.setText("⟳")
        self.refresh_button.setToolTip("Refresh")
        self.refresh_button.clicked.connect(
            lambda: self._news.refresh_featured(force=True)
        )
        band_layout.addWidget(self.refresh_button, 0, Qt.AlignTop)

        self.byline_label = QLabel("", self)
        self.byline_label.setObjectName("featuredByline")

        self.separator = QFrame(self)
        self.separator.setObjectName("featuredSeparator")
        self.separator.setFrameShape(QFrame.HLine)
        self.separator.setStyleSheet(
            f"#featuredSeparator {{ background-color: {p.parch_line.name()};"
            f" border: none; max-height: 1px; }}"
        )

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("featuredStatus")

        self.body = QTextBrowser(self)
        self.body.setObjectName("featuredBody")
        self.body.setReadOnly(True)
        self.body.setWordWrapMode(QTextOption.WordWrap)
        self.body.setFrameShape(QFrame.NoFrame)

        self.link_label = LinkLabel("⧉  Read full post on the forum", "", self)
        self.link_label.setObjectName("featuredLink")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(band)
        layout.addWidget(self.byline_label)
        layout.addWidget(self.separator)
        layout.addWidget(self.status_label)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.link_label)

        self.render(None, loading=True)

    def render(self, post, loading=False, error=""):
        """Render the featured post snapshot (post dict, or None)."""
        title = (post or {}).get("title", "")
        self.title_label.setText(title.upper() if title else "NEWS")
        self.link_label._url = post.get("url", "") if post else ""

        if not post:
            self.status_label.setText(
                error or (_LOADING if loading else _EMPTY)
            )
            self.status_label.show()
            self.body.hide()
            self.link_label.hide()
            self.byline_label.hide()
            self.separator.hide()
            return

        byline = []
        if post.get("author"):
            byline.append(f"by {post['author']}")
        byline.append(format_news_date(post.get("date", "")))
        self.byline_label.setText(" · ".join(byline))
        self.byline_label.show()
        self.separator.show()
        self.status_label.hide()
        self.body.setPlainText(strip_html(post.get("html", "")))
        self.body.show()
        self.link_label.setVisible(bool(post.get("url")))


class AnnouncementsPanel(QWidget):
    """Dark panel listing the dated news announcements."""

    def __init__(self, news, palette, parent=None):
        super().__init__(parent)
        self._news = news
        self._palette = palette
        p = palette
        self.setObjectName("announcementsPanel")
        self.setStyleSheet(
            f"""
            #announcementsPanel {{ background-color: {p.panel.name()};
                                  border: 1px solid {p.panel_bdr.name()}; }}
            #announcementsHeader {{ color: {p.gold.name()};
                                   font-weight: bold; font-size: 12pt; }}
            #announcementsStatus {{ color: {p.text_dim.name()};
                                   font-size: 9pt; }}
            #announcementsList {{ background-color: {p.panel.name()}; }}
            #announcementDate {{ color: {p.text_dim.name()};
                                font-size: 9pt; }}
            #announcementTitle {{ color: {p.gold.name()};
                                 font-weight: bold; font-size: 11pt; }}
            #announcementAuthor {{ color: {p.text_dim.name()};
                                  font-style: italic; font-size: 10pt; }}
            #announcementBody {{ color: {p.text.name()};
                                font-size: 10pt; }}
            #announcementLink {{ color: {p.gold.name()};
                                font-size: 10pt; }}
            #announcementLink:hover {{ color: {p.gold_lt.name()}; }}
            """
        )

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 16, 12, 10)

        self.header_label = QLabel("ANNOUNCEMENTS", header)
        self.header_label.setObjectName("announcementsHeader")
        header_layout.addWidget(self.header_label, 1)

        self.refresh_button = QToolButton(header)
        self.refresh_button.setObjectName("announcementsRefresh")
        self.refresh_button.setText("⟳")
        self.refresh_button.setToolTip("Refresh")
        self.refresh_button.clicked.connect(
            lambda: self._news.refresh_announcements(force=True)
        )
        header_layout.addWidget(self.refresh_button)

        divider = make_hairline(self)
        divider.setObjectName("announcementsDivider")

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("announcementsStatus")

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("announcementsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {p.panel.name()};"
            f" border: none; }}"
        )

        self._list = QWidget()
        self._list.setObjectName("announcementsList")
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(14, 0, 8, 10)
        self._list_layout.setSpacing(0)
        self._list_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self._list)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(divider)
        layout.addWidget(self.status_label)
        layout.addWidget(self.scroll, 1)

        self.render(None, loading=True)

    def render(self, items, loading=False, error=""):
        """Render the announcements snapshot (items list, or None)."""
        if items is None or error:
            self.status_label.setText(
                error or (_LOADING if loading else _ERROR)
            )
            self.status_label.show()
            self.scroll.hide()
            return
        if not items:
            self.status_label.setText(_EMPTY)
            self.status_label.show()
            self.scroll.hide()
            return

        clear_layout(self._list_layout)
        for i, item in enumerate(items):
            row = QWidget(self._list)
            row.setObjectName(f"announcement_{i}")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 12, 0, 0)
            row_layout.setSpacing(4)

            top = QWidget(row)
            top_layout = QHBoxLayout(top)
            top_layout.setContentsMargins(0, 0, 0, 0)
            top_layout.setSpacing(8)

            title = QLabel(item.get("title", ""), top)
            title.setObjectName(f"announcement_{i}_title")
            title.setWordWrap(True)
            title.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            top_layout.addWidget(title, 1)

            date = QLabel(format_news_date(item.get("date", "")), top)
            date.setObjectName(f"announcement_{i}_date")
            date.setAlignment(Qt.AlignRight | Qt.AlignTop)
            top_layout.addWidget(date)

            row_layout.addWidget(top)

            if item.get("author"):
                author = QLabel(f"by {item['author']}", row)
                author.setObjectName(f"announcement_{i}_author")
                row_layout.addWidget(author)

            body = item.get("body", "").strip()
            if len(body) > _BODY_LIMIT:
                body = body[:_BODY_LIMIT].rstrip() + "…"
            if body:
                body_label = QLabel(body, row)
                body_label.setObjectName(f"announcement_{i}_body")
                body_label.setWordWrap(True)
                row_layout.addWidget(body_label)

            if item.get("url"):
                link = LinkLabel("⧉ Read more", item["url"], row)
                link.setObjectName(f"announcement_{i}_link")
                row_layout.addWidget(link)

            separator = make_hairline(row)
            separator.setObjectName(f"announcement_{i}_separator")
            row_layout.addWidget(separator)

            self._list_layout.addWidget(row)

        self.status_label.hide()
        self.scroll.show()


class NewsPanel(QWidget):
    """The NEWS tab: a splitter with the featured post and announcements."""

    def __init__(self, news, bridge, palette, parent=None):
        super().__init__(parent)
        self._news = news
        self._palette = palette
        self._featured = None
        self._items = None
        self.setObjectName("newsPanel")

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setObjectName("newsSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        self.featured_panel = FeaturedPanel(news, palette, splitter)
        self.announcements_panel = AnnouncementsPanel(news, palette, splitter)
        splitter.addWidget(self.featured_panel)
        splitter.addWidget(self.announcements_panel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        bridge.newsLoaded.connect(self._on_news_loaded)

    def _on_news_loaded(self, event):
        if not isinstance(event, NewsLoaded):
            return
        if event.kind == "featured":
            self._featured = event.data
            self.featured_panel.render(
                event.data.data,
                loading=event.data.loading,
                error=event.data.error,
            )
        else:
            self._items = event.data
            self.announcements_panel.render(
                event.data.data,
                loading=event.data.loading,
                error=event.data.error,
            )
