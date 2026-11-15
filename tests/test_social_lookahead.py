"""Historical social sentiment must not leak current data into a backtest (#1220).

StockTwits pulls only recent items, so for a historical run it must be trimmed to the
analysis window (and yield a clear placeholder when nothing qualifies) rather than
showing today's chatter as if it were from the as-of date. Bluesky can be asked for a
past window, but a post belongs to it by the time Bluesky indexed it, never by the date
its author wrote on it. Both share dataflows.date_window.in_window.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.dataflows.date_window import in_window
from tradingagents.dataflows.vendors import bluesky, stocktwits


class _JsonResp:
    """Minimal urlopen() context-manager stub returning a JSON body."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


# --- shared window helper ---------------------------------------------------

@pytest.mark.unit
def test_in_window_bounds_and_exclusive_upper():
    start = datetime(2026, 5, 1)
    end = datetime(2026, 5, 9)
    assert in_window(datetime(2026, 5, 5, tzinfo=UTC), start, end) is True
    assert in_window(datetime(2026, 5, 9, 23, 59, tzinfo=UTC), start, end) is True
    # exactly midnight after end -> excluded (no leak)
    assert in_window(datetime(2026, 5, 10, 0, 0, tzinfo=UTC), start, end) is False
    # offset-aware converted, not truncated: 05-10T01:00+05:00 == 05-09T20:00Z
    assert in_window(datetime.fromisoformat("2026-05-10T01:00:00+05:00"), start, end) is True


@pytest.mark.unit
def test_in_window_undated_excluded_in_backtest_kept_live():
    old = datetime(2026, 5, 9)
    assert in_window(None, datetime(2026, 5, 1), old) is False       # historical
    now = datetime.now(UTC)
    assert in_window(None, now, now) is True                          # live


# --- StockTwits -------------------------------------------------------------

def _msg(created_iso, sentiment=None):
    return {
        "created_at": created_iso,
        "user": {"username": "u"},
        "entities": {"sentiment": {"basic": sentiment}},
        "body": "text",
    }


@pytest.mark.unit
def test_stocktwits_historical_window_excludes_recent(monkeypatch):
    # All messages are "today"; a run as-of a past week must show none of them.
    recent = [_msg("2026-08-30T12:00:00Z", "Bullish"), _msg("2026-08-29T09:00:00Z")]
    monkeypatch.setattr(stocktwits, "urlopen", lambda *a, **k: _JsonResp({"messages": recent}))
    out = stocktwits.fetch_stocktwits_messages("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert "2026-05-01..2026-05-08" in out
    assert "Bullish: 1" not in out  # the recent bullish message did not leak
    # Coverage starts after the window: unavailable, never a claim of silence.
    assert "unavailable" in out and "not an absence" in out


@pytest.mark.unit
def test_stocktwits_live_window_keeps_in_range(monkeypatch):
    msgs = [_msg("2026-05-05T12:00:00Z", "Bullish"), _msg("2026-05-07T09:00:00Z", "Bearish")]
    monkeypatch.setattr(stocktwits, "urlopen", lambda *a, **k: _JsonResp({"messages": msgs}))
    out = stocktwits.fetch_stocktwits_messages("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert "Total: 2" in out


@pytest.mark.unit
def test_stocktwits_no_window_is_unfiltered(monkeypatch):
    msgs = [_msg("2026-08-30T12:00:00Z", "Bullish")]
    monkeypatch.setattr(stocktwits, "urlopen", lambda *a, **k: _JsonResp({"messages": msgs}))
    out = stocktwits.fetch_stocktwits_messages("AAPL")  # live caller, no dates
    assert "Total: 1" in out


# --- Bluesky ----------------------------------------------------------------

def _post(indexed_iso, text="$AAPL is moving", created_iso=None):
    return {
        "indexedAt": indexed_iso,
        "author": {"handle": "u.bsky.social"},
        "record": {"text": text, "createdAt": created_iso or indexed_iso},
    }


def _serve(monkeypatch, posts):
    """Every day's search returns ``posts`` (the function trims them to the window itself)."""
    asked = []
    monkeypatch.setattr(bluesky, "_search_day", lambda symbol, day, timeout, name="": asked.append(day) or list(posts))
    return asked


@pytest.mark.unit
def test_bluesky_historical_window_excludes_a_post_indexed_after_it(monkeypatch):
    # The author backdated the post into the window; the server indexed it months later.
    _serve(monkeypatch, [_post("2026-08-30T12:00:00Z", created_iso="2026-05-04T12:00:00Z")])
    out = bluesky.fetch_bluesky_posts("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert "is moving" not in out
    assert out == "<no Bluesky posts with the cashtag $AAPL found within 2026-05-01..2026-05-08>"


@pytest.mark.unit
def test_bluesky_historical_window_keeps_what_was_indexed_inside_it(monkeypatch):
    _serve(monkeypatch, [_post("2026-05-05T12:00:00Z", "$AAPL INRANGE")])
    out = bluesky.fetch_bluesky_posts("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert "INRANGE" in out


@pytest.mark.unit
def test_bluesky_can_answer_for_a_past_window_so_empty_is_a_real_absence(monkeypatch):
    # Unlike StockTwits, Bluesky is asked for the window itself, so an empty answer is silence.
    _serve(monkeypatch, [])
    out = bluesky.fetch_bluesky_posts("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert out.startswith("<no Bluesky posts") and "unavailable" not in out


@pytest.mark.unit
def test_bluesky_a_post_with_no_server_time_or_date_is_dropped_from_a_past_window(monkeypatch):
    undated = {"author": {"handle": "u"}, "record": {"text": "$AAPL UNDATED"}}
    _serve(monkeypatch, [undated])
    out = bluesky.fetch_bluesky_posts("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert "UNDATED" not in out


@pytest.mark.unit
def test_bluesky_a_window_with_no_dates_reads_the_past_week(monkeypatch):
    asked = _serve(monkeypatch, [])
    bluesky.fetch_bluesky_posts("AAPL")
    today = datetime.now(UTC).date()
    assert asked == [today - timedelta(days=7 - i) for i in range(8)]


@pytest.mark.unit
def test_bluesky_shows_no_engagement_counts_that_would_grow_after_the_run_date(monkeypatch):
    post = _post("2026-05-05T12:00:00Z", "$AAPL INRANGE")
    post.update({"likeCount": 999, "repostCount": 888, "replyCount": 777})
    _serve(monkeypatch, [post])
    out = bluesky.fetch_bluesky_posts("AAPL", start_date="2026-05-01", end_date="2026-05-08")
    assert "999" not in out and "888" not in out and "777" not in out
