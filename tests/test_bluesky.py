"""Bluesky public search, the sentiment analyst's replacement for Reddit (which ended its feeds on 2026-11-13).

No test here touches the network: ``_search_day`` or ``urlopen`` is always stubbed.
"""
from __future__ import annotations

import http.client
import json
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from tradingagents.dataflows.vendors import bluesky

DAY = date(2026, 5, 8)


def _post(indexed_iso, text="$AAPL is moving", handle="u.bsky.social", created_iso=None):
    return {
        "indexedAt": indexed_iso,
        "author": {"handle": handle},
        "record": {"text": text, "createdAt": created_iso or indexed_iso},
    }


def _stamp(day, hour=12, minute=0):
    return f"{day:%Y-%m-%d}T{hour:02d}:{minute:02d}:00.000Z"


def _serve(monkeypatch, by_day=None, default=None):
    """``_search_day`` answers from ``by_day`` (a date -> list or an exception); every other day gets ``default``."""
    asked = []

    def fake(symbol, day, timeout, name=""):
        asked.append((symbol, day))
        answer = (by_day or {}).get(day, default if default is not None else [])
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    monkeypatch.setattr(bluesky, "_search_day", fake)
    return asked


def _fetch(**kwargs):
    kwargs.setdefault("start_date", "2026-05-01")
    kwargs.setdefault("end_date", "2026-05-08")
    return bluesky.fetch_bluesky_posts("AAPL", **kwargs)


@pytest.mark.unit
class TestTheRequest:
    class _Resp:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self._body if n < 0 else self._body[:n]

    def _capture(self, monkeypatch, payload=None):
        seen = []
        monkeypatch.setattr(bluesky, "urlopen", lambda req, timeout=None: seen.append(req) or self._Resp(payload or {"posts": []}))
        return seen

    def test_it_asks_for_one_utc_day_of_the_newest_cashtag_posts(self, monkeypatch):
        seen = self._capture(monkeypatch)
        bluesky._search_day("NVDA", date(2026, 5, 8), 10.0)
        url = urlparse(seen[0].full_url)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
        assert query == {"q": "$NVDA", "sort": "latest", "limit": "100",
                         "since": "2026-05-08T00:00:00Z", "until": "2026-05-09T00:00:00Z"}

    def test_a_company_name_is_added_to_the_search_when_given(self, monkeypatch):
        seen = self._capture(monkeypatch)
        bluesky._search_day("BE", DAY, 10.0, "Bloom Energy")
        assert parse_qs(urlparse(seen[0].full_url).query)["q"] == ["Bloom Energy $BE"]

    def test_it_says_who_is_calling_and_needs_no_key(self, monkeypatch):
        seen = self._capture(monkeypatch)
        bluesky._search_day("NVDA", DAY, 10.0)
        headers = {k.lower(): v for k, v in seen[0].header_items()}
        assert headers["user-agent"] == bluesky.USER_AGENT and "authorization" not in headers

    def test_a_crypto_pair_is_searched_by_its_base(self, monkeypatch):
        asked = _serve(monkeypatch)
        bluesky.fetch_bluesky_posts("BTC-USD", start_date="2026-05-08", end_date="2026-05-08")
        assert asked == [("BTC", DAY)]

    def test_an_oversized_answer_is_refused(self, monkeypatch):
        monkeypatch.setattr(bluesky, "_MAX_BYTES", 10)
        self._capture(monkeypatch, {"posts": [{"x": "y" * 50}]})
        with pytest.raises(http.client.HTTPException, match="exceeded"):
            bluesky._search_day("NVDA", DAY, 10.0)

    def test_an_unexpected_shape_is_no_posts_not_a_crash(self, monkeypatch):
        self._capture(monkeypatch, {"posts": "nope"})
        assert bluesky._search_day("NVDA", DAY, 10.0) == []
        self._capture(monkeypatch, ["not", "a", "dict"])
        assert bluesky._search_day("NVDA", DAY, 10.0) == []


@pytest.mark.unit
class TestWhatCountsAsAPostAboutTheStock:
    @pytest.mark.parametrize("text, counts", [
        ("$BE is up", True), ("$be lower", True), ("Buying #BE today", True), ("watch $BE, $AAPL", True),
        ("to be or not to be", False), ("$BEAM is another company", False), ("$BE2", False),
        ("see price$BE", False), ("", False),
    ])
    def test_the_cashtag_or_hashtag_must_stand_alone(self, text, counts):
        assert bluesky._mentions(text, "BE") is counts

    def test_a_symbol_with_a_dot_is_matched_literally(self):
        assert bluesky._mentions("$BRK.B rallies", "BRK.B") and not bluesky._mentions("$BRKxB rallies", "BRK.B")

    def test_the_name_alone_is_not_a_cashtag(self, monkeypatch):
        _serve(monkeypatch, {DAY: [_post(_stamp(DAY), "Nvidia reports tonight"), _post(_stamp(DAY, 13), "$AAPL keeps going")]})
        out = _fetch(start_date="2026-05-08")
        assert "keeps going" in out and "Nvidia reports" not in out

    def test_identical_posts_from_reposting_accounts_are_shown_once(self, monkeypatch):
        bots = [_post(_stamp(DAY, h), "$AAPL  BREAKING: record  quarter", handle=f"bot{h}") for h in range(1, 6)]
        _serve(monkeypatch, {DAY: bots})
        out = _fetch(start_date="2026-05-08")
        assert out.count("record quarter") == 1 and "08: 1" in out


@pytest.mark.unit
class TestATickerThatIsAlsoACommonWord:
    """``$BE``: the search drops the dollar sign, so a full page is mostly posts that merely say "be"."""

    @staticmethod
    def _word_page(day):
        return [_post(_stamp(day, 0, m % 60), f"to be honest {m}", handle=f"w{m}") for m in range(bluesky._PAGE)]

    def _serve_swamped(self, monkeypatch, with_name_posts):
        calls = []

        def fake(symbol, day, timeout, name=""):
            calls.append((day, name))
            return list(with_name_posts if name else self._word_page(day))

        monkeypatch.setattr(bluesky, "_search_day", fake)
        return calls

    def test_a_swamped_day_is_asked_again_with_the_company_name_and_that_answer_is_used(self, monkeypatch):
        real = [_post(_stamp(DAY, 9), "$BE: Bloom jumps on a new order")]
        calls = self._serve_swamped(monkeypatch, real)
        out = bluesky.fetch_bluesky_posts("BE", start_date="2026-05-08", end_date="2026-05-08",
                                          name_of=lambda: "Bloom Energy Corporation")
        assert calls == [(DAY, ""), (DAY, "Bloom Energy")]
        assert "Bloom jumps on a new order" in out and "to be honest" not in out
        assert "had to include the company name: posts that carry only the cashtag are missing" in out

    def test_an_empty_answer_to_the_name_search_is_not_called_silence(self, monkeypatch):
        self._serve_swamped(monkeypatch, [])
        out = bluesky.fetch_bluesky_posts("BE", start_date="2026-05-08", end_date="2026-05-08", name_of=lambda: "Bloom Energy")
        assert out.startswith("<Bluesky unavailable:") and "carry only the cashtag cannot be searched for" in out
        assert "not an absence" in out

    def test_the_name_is_looked_up_once_for_all_the_days(self, monkeypatch):
        self._serve_swamped(monkeypatch, [_post(_stamp(DAY, 9), "$BE: Bloom jumps")])
        looked_up = []
        bluesky.fetch_bluesky_posts("BE", start_date="2026-05-06", end_date="2026-05-08",
                                    name_of=lambda: looked_up.append(1) or "Bloom Energy Corporation")
        assert looked_up == [1]

    def test_without_a_name_a_swamped_stock_is_unavailable_never_silence(self, monkeypatch):
        calls = self._serve_swamped(monkeypatch, [])
        out = bluesky.fetch_bluesky_posts("BE", start_date="2026-05-08", end_date="2026-05-08")
        assert out.startswith("<Bluesky unavailable:") and "common word" in out and "cannot isolate" in out and "not an absence" in out
        assert calls == [(DAY, "")]

    def test_a_name_that_still_leaves_the_page_swamped_is_unavailable(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bluesky, "_search_day",
                            lambda symbol, day, timeout, name="": calls.append(name) or self._word_page(day))
        out = bluesky.fetch_bluesky_posts("BE", start_date="2026-05-08", end_date="2026-05-08", name_of=lambda: "Bloom Energy")
        assert "cannot isolate" in out and calls == ["", "Bloom Energy"]

    def test_a_name_lookup_that_fails_does_not_fail_the_fetch(self, monkeypatch):
        self._serve_swamped(monkeypatch, [])

        def broken():
            raise RuntimeError("yahoo is down")

        out = bluesky.fetch_bluesky_posts("BE", start_date="2026-05-08", end_date="2026-05-08", name_of=broken)
        assert "cannot isolate" in out

    def test_an_ordinary_full_page_never_asks_for_the_name(self, monkeypatch):
        page = [_post(_stamp(DAY, 0, m % 60), f"$AAPL n{m}") for m in range(bluesky._PAGE)]
        _serve(monkeypatch, {DAY: page})
        out = bluesky.fetch_bluesky_posts("AAPL", start_date="2026-05-08", end_date="2026-05-08",
                                          name_of=lambda: pytest.fail("the name was not needed"))
        assert "05-08: 100+" in out

    @pytest.mark.parametrize("name, short", [
        ("Bloom Energy Corporation", "Bloom Energy"), ("Apple Inc.", "Apple"), ("Advanced Micro Devices, Inc.", "Advanced Micro"),
        ("Taiwan Semiconductor Manufacturing Company Limited", "Taiwan Semiconductor"), ("", ""), ("Inc", ""),
    ])
    def test_the_name_is_cut_to_what_people_actually_write(self, name, short):
        assert bluesky._short_name(name) == short


@pytest.mark.unit
class TestTheBlock:
    def test_it_heads_with_posts_per_day_and_lists_newest_first(self, monkeypatch):
        d1, d2 = date(2026, 5, 6), date(2026, 5, 7)
        _serve(monkeypatch, {d1: [_post(_stamp(d1, 9), "$AAPL first day")],
                             d2: [_post(_stamp(d2, 9), "$AAPL second morning"), _post(_stamp(d2, 15), "$AAPL second evening")]})
        out = _fetch(start_date="2026-05-06", end_date="2026-05-07")
        assert "Bluesky posts with the cashtag $AAPL, 2026-05-06..2026-05-07" in out
        assert "Posts per day: 05-06: 1 · 05-07: 2." in out and "no engagement counts or stance tags" in out
        body = out.split("\n\n")[-1].splitlines()
        assert [line.split("] ")[1] for line in body] == ["$AAPL second evening", "$AAPL second morning", "$AAPL first day"]
        assert body[0] == "[2026-05-07T15:00Z · @u.bsky.social] $AAPL second evening"

    def test_the_shown_posts_are_an_even_share_per_day(self, monkeypatch):
        # Eight days at a limit of 30: four per day, whichever day is busy.
        busy = [_post(_stamp(DAY, 0, m), f"$AAPL post {m}") for m in range(40)]
        _serve(monkeypatch, {DAY: busy, date(2026, 5, 1): [_post(_stamp(date(2026, 5, 1)), "$AAPL the quiet day")]})
        out = _fetch(limit=30)
        shown = out.split("\n\n")[-1].splitlines()
        assert len(shown) == 5 and sum("post " in line for line in shown) == 4 and "the quiet day" in out
        assert "05-08: 40" in out and "05-01: 1" in out

    def test_a_day_that_filled_the_page_is_marked_with_a_plus(self, monkeypatch):
        page = [_post(_stamp(DAY, 0, m % 60), f"$AAPL n{m}") for m in range(bluesky._PAGE)]
        _serve(monkeypatch, {DAY: page})
        out = _fetch(start_date="2026-05-08")
        assert "05-08: 100+" in out

    def test_a_long_post_is_cut_and_its_lines_joined(self, monkeypatch):
        _serve(monkeypatch, {DAY: [_post(_stamp(DAY), "$AAPL " + "word\nnext " * 100)]})
        line = _fetch(start_date="2026-05-08").split("\n\n")[-1]
        assert "\n" not in line and line.endswith("…") and len(line) < 330

    def test_the_timestamp_shown_is_when_the_author_wrote_it(self, monkeypatch):
        _serve(monkeypatch, {DAY: [_post(_stamp(DAY, 13), "$AAPL late to the index", created_iso=_stamp(DAY, 12, 30))]})
        assert "[2026-05-08T12:30Z · @u.bsky.social]" in _fetch(start_date="2026-05-08")

    def test_the_screen_sees_only_what_would_be_shown_and_its_note_heads_the_block(self, monkeypatch):
        _serve(monkeypatch, {DAY: [_post(_stamp(DAY, 9), "$AAPL on topic"), _post(_stamp(DAY, 10), "$AAPL spam spam")]})
        seen = []

        def screen(texts):
            seen.append(texts)
            return [("spam" not in t) for t in texts], "Screened by Jev: 1 of the 2 posts fetched are about Apple."

        out = _fetch(start_date="2026-05-08", screen=screen)
        assert seen == [["$AAPL spam spam", "$AAPL on topic"]]
        assert out.startswith("Screened by Jev") and "on topic" in out and "spam spam" not in out

    def test_a_screen_that_drops_everything_says_so(self, monkeypatch):
        _serve(monkeypatch, {DAY: [_post(_stamp(DAY), "$AAPL promo")]})
        out = _fetch(start_date="2026-05-08", screen=lambda texts: ([False] * len(texts), "Screened by Jev: 0 of 1."))
        assert out.endswith("<none of the 1 Bluesky posts is about $AAPL>")


@pytest.mark.unit
class TestWhenBlueskyCannotAnswer:
    def test_a_refusal_is_unavailable_never_silence_and_stops_further_requests(self, monkeypatch):
        refused = HTTPError("https://api.bsky.app/x", 403, "Forbidden", {}, None)
        asked = _serve(monkeypatch, default=refused)
        out = _fetch()
        assert out == "<Bluesky unavailable: search failed (HTTP 403); this is not an absence of discussion>"
        assert len(asked) == 1            # no point asking seven more times

    def test_a_network_failure_on_every_day_is_unavailable(self, monkeypatch):
        asked = _serve(monkeypatch, default=URLError("no route"))
        out = _fetch()
        assert "unavailable" in out and "URLError" in out and "not an absence" in out
        assert len(asked) == 8

    def test_one_failed_day_is_named_and_the_others_still_count(self, monkeypatch):
        _serve(monkeypatch, {date(2026, 5, 5): TimeoutError("slow"), DAY: [_post(_stamp(DAY), "$AAPL fine")]})
        out = _fetch()
        assert "<1 of 8 days could not be fetched (TimeoutError): their counts are missing, not zero>" in out
        assert "fine" in out

    def test_failed_days_and_no_posts_elsewhere_is_not_reported_as_silence(self, monkeypatch):
        _serve(monkeypatch, {date(2026, 5, 5): TimeoutError("slow")})
        out = _fetch()
        assert out.startswith("<Bluesky unavailable:") and "1 of 8 days failed" in out and "no matching posts" in out

    def test_a_malformed_answer_counts_as_a_failure(self, monkeypatch):
        _serve(monkeypatch, default=ValueError("bad json"))
        assert "ValueError" in _fetch()

    def test_a_window_before_bluesky_opened_is_unavailable(self, monkeypatch):
        asked = _serve(monkeypatch)
        out = _fetch(start_date="2023-05-01", end_date="2023-05-08")
        assert "not open to the public before 2024-02-06" in out and "not an absence" in out and asked == []

    def test_a_window_after_today_is_unavailable(self, monkeypatch):
        asked = _serve(monkeypatch)
        tomorrow = datetime.now(UTC).date() + timedelta(days=3)
        out = _fetch(start_date=str(tomorrow), end_date=str(tomorrow + timedelta(days=2)))
        assert "after today" in out and asked == []

    def test_a_long_window_is_cut_to_its_last_two_weeks(self, monkeypatch):
        asked = _serve(monkeypatch)
        _fetch(start_date="2026-03-01", end_date="2026-05-08")
        assert len(asked) == bluesky._MAX_DAYS and asked[-1][1] == DAY and asked[0][1] == DAY - timedelta(days=13)

    def test_a_window_reaching_past_today_asks_only_up_to_today(self, monkeypatch):
        asked = _serve(monkeypatch)
        today = datetime.now(UTC).date()
        _fetch(start_date=str(today - timedelta(days=2)), end_date=str(today + timedelta(days=5)))
        assert [d for _, d in asked] == [today - timedelta(days=2), today - timedelta(days=1), today]

    def test_the_fetcher_never_raises_whatever_a_day_throws_at_it(self, monkeypatch):
        _serve(monkeypatch, default=http.client.IncompleteRead(b"x"))
        assert "unavailable" in _fetch()
