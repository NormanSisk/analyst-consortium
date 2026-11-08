"""Tests for the local accuracy changes: the open-session guard, the insider window,
the live-price lines, the computed snapshot facts and the earnings tool."""

from datetime import datetime

import pandas as pd
import pytest
import pytz

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import earnings, fundamentals, news, ohlcv, snapshot

EASTERN = pytz.timezone("America/New_York")


class _Clock:
    """Stands in for ``datetime`` inside ohlcv so the guard sees a chosen time."""

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment


def _at(monkeypatch, year, month, day, hour, minute):
    moment = EASTERN.localize(datetime(year, month, day, hour, minute))
    monkeypatch.setattr(ohlcv, "datetime", _Clock(moment))


@pytest.mark.unit
class TestUnfinishedSession:
    BAR = pd.Timestamp("2026-11-03 00:00:00-05:00")   # a Tuesday, EST

    def test_a_bar_for_a_session_still_open_is_reported(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 3, 15, 37)
        assert ohlcv.unfinished_session_date(self.BAR) == pd.Timestamp("2026-11-03")

    def test_the_bar_is_final_after_the_settle_time(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 3, 16, 20)
        assert ohlcv.unfinished_session_date(self.BAR) is None

    def test_yesterdays_bar_is_final(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 4, 10, 0)
        assert ohlcv.unfinished_session_date(self.BAR) is None

    def test_a_weekend_has_no_open_session(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 7, 11, 0)
        assert ohlcv.unfinished_session_date(pd.Timestamp("2026-11-07 00:00:00-05:00")) is None

    def test_a_non_us_exchange_is_left_alone(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 3, 15, 37)
        assert ohlcv.unfinished_session_date(pd.Timestamp("2026-11-03 00:00:00+09:00")) is None
        assert ohlcv.unfinished_session_date(pd.Timestamp("2026-11-03 00:00:00+00:00")) is None

    def test_a_bar_with_no_offset_is_left_alone(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 3, 15, 37)
        assert ohlcv.unfinished_session_date(pd.Timestamp("2026-11-03")) is None

    def test_the_guard_can_be_switched_off(self, monkeypatch):
        _at(monkeypatch, 2026, 11, 3, 15, 37)
        monkeypatch.setattr(ohlcv, "get_config", lambda: {"drop_unfinished_session": False})
        assert ohlcv.unfinished_session_date(self.BAR) is None


@pytest.mark.unit
class TestInsiderWindow:
    TODAY = pd.Timestamp(get_current_date())

    def _frame(self):
        days = [self.TODAY - pd.Timedelta(days=d) for d in (450, 270, 10)]
        return pd.DataFrame({
            "Shares": [32500, 32500, 3669],
            "Text": ["Sale at price 195.02 - 196.96 per share.", "Sale at price 330.13 - 343.74 per share.", ""],
            "Insider": ["PICHAI SUNDAR"] * 3,
            "Start Date": pd.to_datetime(days),
        })

    def test_only_the_trailing_window_is_served_and_old_trades_are_counted(self, monkeypatch):
        frame = self._frame()
        monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: frame)
        out = fundamentals.get_insider_transactions("GOOG", str(self.TODAY.date()))
        assert f"{(self.TODAY - pd.Timedelta(days=10)).date()}" in out
        assert f"{(self.TODAY - pd.Timedelta(days=450)).date()}" not in out
        assert f"{(self.TODAY - pd.Timedelta(days=270)).date()}" not in out
        assert "2 older filings omitted" in out

    def test_a_window_with_no_trades_says_so(self, monkeypatch):
        old = self._frame().iloc[:1]
        monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: old)
        out = fundamentals.get_insider_transactions("GOOG", str(self.TODAY.date()))
        assert out.startswith("No insider transactions")
        assert "1 older filings were omitted" in out

    def test_a_direct_call_with_no_date_gets_everything(self, monkeypatch):
        frame = self._frame()
        monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: frame)
        out = fundamentals.get_insider_transactions("GOOG", None)
        assert f"{(self.TODAY - pd.Timedelta(days=450)).date()}" in out


@pytest.mark.unit
class TestNewsFallback:
    def _article(self):
        return {"title": "Alphabet falls", "publisher": "Wire", "relatedTickers": ["GOOG"],
                "providerPublishTime": int(pd.Timestamp.now(tz="UTC").timestamp())}

    def test_a_failing_quote_feed_falls_back_to_yahoo_search(self, monkeypatch):
        calls = []
        feed = news.yf_retry

        def retry(fn):
            calls.append(1)
            if len(calls) == 1:
                raise VendorUnavailableError("feed down")
            return feed(fn)

        search = type("S", (), {"news": [self._article()], "quotes": [{"symbol": "GOOG"}]})()
        monkeypatch.setattr(news, "yf_retry", retry)
        monkeypatch.setattr(news.yf, "Search", lambda *a, **k: search)
        monkeypatch.setattr(news, "_extract_article_data", lambda a: {
            "title": a["title"], "publisher": a["publisher"], "summary": "", "link": "",
            "pub_date": pd.Timestamp.now()})
        day = pd.Timestamp.now().strftime("%Y-%m-%d")
        out = news.get_news_yfinance("GOOG", day, day)
        assert "Alphabet falls" in out


@pytest.mark.unit
class TestLastPrice:
    INFO = {
        "longName": "Alphabet", "currency": "USD", "currentPrice": 278.27,
        "regularMarketPreviousClose": 278.04, "regularMarketTime": 1793739600,
        "fiftyDayAverage": 334.0, "twoHundredDayAverage": 338.0,
        "fiftyTwoWeekHigh": 404.0, "fiftyTwoWeekLow": 271.0,
        "trailingEps": 19.15, "forwardEps": 15.44,
    }

    def test_the_live_price_and_distances_are_stated(self, monkeypatch):
        monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: dict(self.INFO))
        out = fundamentals.get_fundamentals("GOOG", None)
        assert "Last Price (live quote): 278.27 USD" in out
        assert "Last Price vs 50 Day Average: -16.7%" in out
        assert "Last Price vs 52 Week Low: +2.7%" in out

    def test_trailing_earnings_well_above_forward_earnings_are_flagged(self, monkeypatch):
        monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: dict(self.INFO))
        assert "one-time gains" in fundamentals.get_fundamentals("GOOG", None)

    def test_growth_that_puts_forward_above_trailing_is_not_flagged(self, monkeypatch):
        info = dict(self.INFO, trailingEps=10.0, forwardEps=12.0)
        monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: info)
        assert "one-time gains" not in fundamentals.get_fundamentals("GOOG", None)


@pytest.mark.unit
class TestComputedFacts:
    def _frame(self, closes):
        dates = pd.bdate_range(end="2026-11-02", periods=len(closes))
        close = pd.Series(closes, dtype=float)
        return pd.DataFrame({"Date": dates, "Open": close, "High": close + 1, "Low": close - 1,
                             "Close": close, "Volume": 1_000_000.0})

    def test_a_decline_after_a_peak_reads_as_a_death_cross_regime(self, monkeypatch):
        closes = [100.0] * 150 + [120.0] * 60 + [120.0 - i for i in range(1, 61)]
        frame = self._frame(closes)
        monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: frame.copy())
        out = snapshot.build_verified_market_snapshot("TEST", "2026-11-02", 30)
        assert "### Computed facts" in out
        assert "BELOW the 200-day SMA (death-cross regime)" in out
        assert "death cross on" in out
        assert "Latest close is below the 50-day SMA" in out
        assert "20 sessions before it" in out

    def test_an_open_session_is_named_in_the_header(self, monkeypatch):
        frame = self._frame([100.0 + i * 0.1 for i in range(260)])
        frame.attrs["open_session"] = "2026-11-03"
        monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: frame.copy())
        out = snapshot.build_verified_market_snapshot("TEST", "2026-11-03", 30)
        assert "The 2026-11-03 session was still open" in out


@pytest.mark.unit
class TestAnalystsShareTheEarningsDate:
    def test_the_news_and_fundamentals_analysts_both_get_the_earnings_tool(self):
        from tradingagents.agents.analysts import fundamentals_analyst, news_analyst

        assert "get_earnings_facts" in [t.name for t in news_analyst.TOOLS]
        assert "get_earnings_facts" in [t.name for t in fundamentals_analyst.TOOLS]


@pytest.mark.unit
class TestEarningsFacts:
    HISTORY = pd.DataFrame(
        {"EPS Estimate": [3.27, 2.98, 2.91], "Reported EPS": [float("nan"), 2.67, 2.85],
         "Surprise(%)": [float("nan"), -10.53, -2.15]},
        index=pd.DatetimeIndex(["2027-02-03 16:00", "2026-10-28 16:00", "2026-07-22 16:00"], tz="America/New_York"),
    )

    def test_misses_are_stated_with_their_figures(self, monkeypatch):
        monkeypatch.setattr(earnings, "yf_retry", lambda fn: self.HISTORY)
        monkeypatch.setattr(earnings, "is_historical", lambda d: False)
        monkeypatch.setattr(earnings, "_analyst_section", lambda c: [])
        monkeypatch.setattr(earnings, "_estimates_section", lambda c: [])
        out = earnings.get_earnings_facts("GOOG", None)
        assert "| 2026-10-28 | 2.98 | 2.67 | -10.5% |" in out
        assert "0 beat the estimate and 2 missed" in out
        assert "Next scheduled report: 2027-02-03 (Wednesday), " in out and " days from " in out
        assert "EPS estimate 3.27" in out and "before the open or after the close" in out

    def test_a_past_run_gets_no_live_targets_and_no_later_reports(self, monkeypatch):
        monkeypatch.setattr(earnings, "yf_retry", lambda fn: self.HISTORY)
        monkeypatch.setattr(earnings, "is_historical", lambda d: True)
        out = earnings.get_earnings_facts("GOOG", "2026-08-01")
        assert "2026-10-28" not in out
        assert "| 2026-07-22 |" in out
        assert "withheld" in out
