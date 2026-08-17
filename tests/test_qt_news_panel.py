"""Headless Qt tests for the news panel (qt_news_panel).

QT_QPA_PLATFORM=offscreen is set before PySide6 is imported so the module
runs without a display. News results are posted straight onto the shared
EventDispatcher (bypassing the network fetchers) and the bridge QTimer
delivers them to the panel via QTest.qWait.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from vanilla_wow_launcher.controllers.news import NewsResult
from vanilla_wow_launcher.core.helpers import format_news_date
from vanilla_wow_launcher.state.events import NewsLoaded
from vanilla_wow_launcher.ui.qt.app import create_qt_app
from vanilla_wow_launcher.ui.qt.bridge import ControllerHub
from vanilla_wow_launcher.ui.qt.main_window import MainWindow
from vanilla_wow_launcher.ui.qt.news_panel import NewsPanel

SAMPLE_POST = {
    "id": 1,
    "title": "Patch 1.17 Lands",
    "author": "Octo Team",
    "date": "2026-07-04T10:00:00+01:00",
    "url": "https://forum.example/post/1",
    "html": "<p>Welcome to the new patch.</p><p>Enjoy the fresh content.</p>",
}

SAMPLE_ITEMS = [
    {
        "id": 1,
        "title": "Server maintenance tonight",
        "author": "GM Willow",
        "date": "2026-07-10T20:00:00+01:00",
        "url": "https://forum.example/post/10",
        "body": "The realm goes offline at 22:00 CET for maintenance.",
    },
    {
        "id": 2,
        "title": "New rewards event",
        "author": "Octo Team",
        "date": "2026-07-12T12:00:00+01:00",
        "url": "https://forum.example/post/11",
        "body": ("word " * 80).strip(),
    },
]


@pytest.fixture(autouse=True)
def _offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    return create_qt_app()


@pytest.fixture()
def hub(qapp):
    hub = ControllerHub()
    yield hub
    hub.close()


@pytest.fixture()
def window(hub):
    win = MainWindow(hub)
    win.show()
    yield win
    win.close()


def _post(hub, kind, result):
    hub.dispatcher.post(NewsLoaded(kind, result))
    QTest.qWait(200)


def _news_panel(window):
    panel = window._stack.widget(window._pages["NEWS"])
    assert isinstance(panel, NewsPanel)
    return panel


# ── integration ────────────────────────────────────────────────────────────


def test_news_panel_replaces_the_news_placeholder(qapp, window):
    assert window._stack.count() == 5
    assert window._pages["UPDATE"] == MainWindow.TABS.index("UPDATE")
    panel = window._stack.widget(window._pages["NEWS"])
    assert isinstance(panel, NewsPanel)
    assert panel.objectName() == "newsPanel"
    assert panel.featured_panel.objectName() == "featuredPanel"
    assert panel.announcements_panel.objectName() == "announcementsPanel"


def test_tab_switching_still_works_and_keeps_panel(qapp, window):
    window.switch_tab("MODS")
    assert window._stack.currentIndex() == window._pages["MODS"]
    window.switch_tab("NEWS")
    assert window._stack.currentIndex() == window._pages["NEWS"]
    assert isinstance(window._stack.widget(window._pages["NEWS"]), NewsPanel)


# ── featured ───────────────────────────────────────────────────────────────


def test_featured_renders_title_byline_and_body(qapp, window, hub):
    _news_panel(window)
    _post(
        hub, "featured", NewsResult(data=SAMPLE_POST, loading=False, error="")
    )

    panel = _news_panel(window)
    feat = panel.featured_panel
    assert feat.title_label.text() == "PATCH 1.17 LANDS"
    assert feat.byline_label.text() == (
        f"by Octo Team · {format_news_date(SAMPLE_POST['date'])}"
    )
    body = feat.body.toPlainText()
    assert "Welcome to the new patch." in body
    assert "Enjoy the fresh content." in body


def test_featured_loading_then_data(qapp, window, hub):
    _news_panel(window)
    _post(hub, "featured", NewsResult(data=None, loading=True, error=""))
    feat = _news_panel(window).featured_panel
    assert feat.status_label.isVisible()
    assert feat.status_label.text() == "Loading…"
    assert not feat.body.isVisible()

    _post(
        hub, "featured", NewsResult(data=SAMPLE_POST, loading=False, error="")
    )
    feat = _news_panel(window).featured_panel
    assert not feat.status_label.isVisible()
    assert feat.body.isVisible()
    assert feat.title_label.text() == "PATCH 1.17 LANDS"


def test_featured_error_state(qapp, window, hub):
    _news_panel(window)
    _post(
        hub,
        "featured",
        NewsResult(
            data=None, loading=False, error="Couldn't reach the news feed."
        ),
    )
    feat = _news_panel(window).featured_panel
    assert feat.status_label.isVisible()
    assert feat.status_label.text() == "Couldn't reach the news feed."


def test_featured_empty_state(qapp, window, hub):
    _news_panel(window)
    _post(hub, "featured", NewsResult(data=None, loading=False, error=""))
    feat = _news_panel(window).featured_panel
    assert feat.status_label.text() == "No news yet — check back later."


# ── announcements ──────────────────────────────────────────────────────────


def test_announcements_render_titles_dates_authors_and_links(
    qapp, window, hub
):
    _news_panel(window)
    _post(hub, "items", NewsResult(data=SAMPLE_ITEMS, loading=False, error=""))

    ann = _news_panel(window).announcements_panel
    title0 = ann.findChild(QLabel, "announcement_0_title")
    title1 = ann.findChild(QLabel, "announcement_1_title")
    assert title0.text() == "Server maintenance tonight"
    assert title1.text() == "New rewards event"
    assert ann.findChild(QLabel, "announcement_0_date").text() == (
        format_news_date(SAMPLE_ITEMS[0]["date"])
    )
    assert (
        ann.findChild(QLabel, "announcement_0_author").text() == "by GM Willow"
    )
    assert ann.findChild(QLabel, "announcement_0_link").text() == "⧉ Read more"
    assert ann.scroll.isVisible()
    assert not ann.status_label.isVisible()


def test_announcements_truncate_long_body_to_260_chars(qapp, window, hub):
    _news_panel(window)
    _post(hub, "items", NewsResult(data=SAMPLE_ITEMS, loading=False, error=""))

    ann = _news_panel(window).announcements_panel
    body1 = ann.findChild(QLabel, "announcement_1_body")
    long_body = SAMPLE_ITEMS[1]["body"].strip()
    expected = long_body[:260].rstrip() + "…"
    assert body1.text() == expected
    assert len(body1.text()) <= 261
    assert body1.text().endswith("…")


def test_announcements_loading_state(qapp, window, hub):
    _news_panel(window)
    _post(hub, "items", NewsResult(data=None, loading=True, error=""))
    ann = _news_panel(window).announcements_panel
    assert ann.status_label.isVisible()
    assert ann.status_label.text() == "Loading…"
    assert not ann.scroll.isVisible()


def test_announcements_error_state(qapp, window, hub):
    _news_panel(window)
    _post(
        hub,
        "items",
        NewsResult(
            data=None, loading=False, error="Couldn't reach the news feed."
        ),
    )
    ann = _news_panel(window).announcements_panel
    assert ann.status_label.isVisible()
    assert ann.status_label.text() == "Couldn't reach the news feed."


def test_announcements_empty_state(qapp, window, hub):
    _news_panel(window)
    _post(hub, "items", NewsResult(data=[], loading=False, error=""))
    ann = _news_panel(window).announcements_panel
    assert ann.status_label.text() == "No news yet — check back later."


def test_rerender_replaces_previous_announcements(qapp, window, hub):
    _news_panel(window)
    _post(hub, "items", NewsResult(data=SAMPLE_ITEMS, loading=False, error=""))
    _post(
        hub,
        "items",
        NewsResult(data=[SAMPLE_ITEMS[0]], loading=False, error=""),
    )

    ann = _news_panel(window).announcements_panel
    assert ann.findChild(QLabel, "announcement_0_title").text() == (
        "Server maintenance tonight"
    )
    assert ann.findChild(QLabel, "announcement_1_title") is None


# ── refresh buttons ────────────────────────────────────────────────────────


def test_refresh_buttons_call_the_controller(qapp, window, hub, monkeypatch):
    panel = _news_panel(window)
    feat_spy = []
    ann_spy = []
    monkeypatch.setattr(
        hub.news,
        "refresh_featured",
        lambda force=False: feat_spy.append(force),
    )
    monkeypatch.setattr(
        hub.news,
        "refresh_announcements",
        lambda force=False: ann_spy.append(force),
    )

    panel.featured_panel.refresh_button.click()
    panel.announcements_panel.refresh_button.click()
    assert feat_spy == [True]
    assert ann_spy == [True]


# ── persistence ────────────────────────────────────────────────────────────


def test_content_survives_tab_switch(qapp, window, hub):
    _news_panel(window)
    _post(
        hub, "featured", NewsResult(data=SAMPLE_POST, loading=False, error="")
    )
    _post(hub, "items", NewsResult(data=SAMPLE_ITEMS, loading=False, error=""))

    window.switch_tab("MODS")
    window.switch_tab("NEWS")
    panel = _news_panel(window)
    assert panel.featured_panel.title_label.text() == "PATCH 1.17 LANDS"
    assert (
        panel.announcements_panel.findChild(
            QLabel, "announcement_0_title"
        ).text()
        == "Server maintenance tonight"
    )
