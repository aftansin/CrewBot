"""Тесты загрузчика ленты: прокси, условные запросы, маскировка."""
from __future__ import annotations

from app.icalendar_feed.fetch import FeedFetcher, _mask, _mask_proxy


def test_empty_proxy_list_means_direct():
    f = FeedFetcher()
    assert f._proxy_order() == [None]


def test_single_proxy_is_the_only_step():
    f = FeedFetcher(proxies=["socks5://1.2.3.4:1080"])
    assert f._proxy_order() == ["socks5://1.2.3.4:1080"]


def test_proxy_order_covers_both_and_is_full():
    """Каждый обход содержит оба узла (это фолбэк), а порядок случаен."""
    f = FeedFetcher(proxies=["socks5://a:1080", "socks5://b:1081"])
    orders = {tuple(f._proxy_order()) for _ in range(50)}
    # оба порядка встречаются
    assert orders == {
        ("socks5://a:1080", "socks5://b:1081"),
        ("socks5://b:1081", "socks5://a:1080"),
    }
    # в каждом обходе присутствуют оба узла
    for order in orders:
        assert set(order) == {"socks5://a:1080", "socks5://b:1081"}


def test_blank_entries_dropped():
    f = FeedFetcher(proxies=["socks5://a:1080", "  ", ""])
    assert f._proxies == ["socks5://a:1080"]


def test_user_agent_is_named_not_disguised():
    """Честное самоназвание, без имитации айфона или человека."""
    f = FeedFetcher()
    assert "CrewApp" in f._user_agent
    assert "iPhone" not in f._user_agent
    assert "Mozilla" not in f._user_agent


def test_conditional_headers_empty_without_cache():
    f = FeedFetcher()
    assert f._conditional_headers("https://x/ics/abc") == {}


def test_conditional_headers_from_cache():
    f = FeedFetcher()
    f._validators["https://x/ics/abc"] = {"etag": '"v1"', "last_modified": "Mon, 01"}
    headers = f._conditional_headers("https://x/ics/abc")
    assert headers["If-None-Match"] == '"v1"'
    assert headers["If-Modified-Since"] == "Mon, 01"


def test_url_masking_hides_uuid():
    masked = _mask("https://crew.aeroflot.ru/api/calendar/ics/9161d714-c1e6-484f")
    assert "9161d714" not in masked
    assert "9161" in masked  # первые символы для узнавания


def test_proxy_masking_hides_credentials():
    masked = _mask_proxy("socks5://user:secret@1.2.3.4:1080")
    assert "secret" not in masked
    assert "user" not in masked
    assert "1.2.3.4:1080" in masked


def test_proxy_masking_without_credentials():
    assert _mask_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert _mask_proxy(None) == ""
