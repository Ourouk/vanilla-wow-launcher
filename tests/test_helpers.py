"""Unit tests for the pure helpers module."""

import pytest

from vanilla_wow_launcher.core.helpers import (
    fmt_size,
    fmt_speed,
    format_news_date,
    parse_version,
    parse_wow_colored,
    same_git_repo,
    strip_html,
    strip_wow_colors,
)


@pytest.mark.parametrize(
    "num,expected",
    [
        (0, "0 KB"),
        (1024, "1 KB"),
        (1024 * 1024, "1.0 MB"),
        (5 * 1024 * 1024, "5.0 MB"),
    ],
)
def test_fmt_size(num, expected):
    assert fmt_size(num) == expected


@pytest.mark.parametrize(
    "bps,expected",
    [
        (0, "0 KB/s"),
        (1024 * 512, "512 KB/s"),
        (2 * 1024 * 1024, "2.0 MB/s"),
    ],
)
def test_fmt_speed(bps, expected):
    assert fmt_speed(bps) == expected


@pytest.mark.parametrize(
    "v,expected",
    [
        ("v1.2.0", (1, 2, 0)),
        ("1.2.0", (1, 2, 0)),
        ("2.2", (2, 2)),
        ("v3", (3,)),
        ("", (0,)),
        (None, (0,)),
        ("1.0-beta", (1, 0)),
    ],
)
def test_parse_version(v, expected):
    assert parse_version(v) == expected


def test_parse_wow_colored_splits_escapes():
    segs = parse_wow_colored("hi |cff00ff00green|r bye")
    assert segs[0] == ("hi ", None)
    assert segs[1] == ("green", "#00ff00")
    assert segs[2] == (" bye", None)


def test_parse_wow_colored_empty():
    assert parse_wow_colored("") == []
    assert parse_wow_colored("plain") == [("plain", None)]


def test_strip_wow_colors():
    assert (
        strip_wow_colors("|cff00ff00green|r and |cff0000ffblue|r")
        == "green and blue"
    )


def test_same_git_repo():
    assert same_git_repo("https://github.com/a/b", "https://github.com/a/b")
    assert same_git_repo(
        "https://github.com/a/b.git", "https://github.com/A/B/"
    )
    assert same_git_repo(None, None)
    assert not same_git_repo(
        "https://github.com/a/b", "https://github.com/a/c"
    )
    assert not same_git_repo(
        "https://github.com/a/b", "https://gitlab.com/a/b"
    )


def test_strip_html_removes_tags_and_scripts():
    raw = (
        "<p>Hello <b>world</b></p><br>"
        "<script>alert(1)</script><style>body{}</style>"
    )
    out = strip_html(raw)
    assert "<" not in out
    assert "Hello world" in out
    assert "alert" not in out


def test_strip_html_unescapes_entities():
    assert strip_html("<p>a &amp; b</p>") == "a & b"


def test_format_news_date():
    assert format_news_date("2026-08-13T10:00:00+02:00") == "13 Aug 2026"


def test_format_news_date_invalid_returns_input():
    assert format_news_date("not-a-date") == "not-a-date"


def test_relative_age_buckets():
    from vanilla_wow_launcher.core.helpers import relative_age

    now = 1_000_000_000
    assert relative_age(None) == ""
    assert relative_age(0) == ""
    assert relative_age(now - 30, now=now) == "just now"
    assert relative_age(now - 5 * 60, now=now) == "5m ago"
    assert relative_age(now - 3 * 3600, now=now) == "3h ago"
    assert relative_age(now - 2 * 86400, now=now) == "2d ago"
