"""Tests for the Yahoo market-statistics vendor (a local addition): option-chain statistics, price-based figures, flags."""

import numpy as np
import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import market_stats as ms


def _chain(strikes, iv, bid=1.0, ask=1.2, oi=100, volume=10):
    return pd.DataFrame({"strike": strikes, "bid": bid, "ask": ask, "impliedVolatility": iv,
                         "openInterest": oi, "volume": volume})


def _prices(n=300, last_volume=1_000_000, seed=3):
    returns = np.random.default_rng(seed).normal(0.0005, 0.02, n)
    close = 50 * np.cumprod(1 + returns)
    return pd.DataFrame({"Date": pd.bdate_range(end="2026-11-06", periods=n), "Open": close, "High": close * 1.01,
                         "Low": close * 0.99, "Close": close, "Volume": [1_000_000] * (n - 1) + [last_volume]})


@pytest.mark.unit
class TestOptionStatistics:
    def test_the_quote_filter_drops_one_sided_quotes_and_nonsense_volatilities(self):
        frame = pd.DataFrame({"strike": [90, 95, 100, 105], "bid": [1, 0, 1, 1], "ask": [1.2, 1.1, 1.2, 1.2],
                              "impliedVolatility": [0.5, 0.5, 0.00001, 0.6], "openInterest": 1, "volume": 1})
        assert list(ms._quoted(frame)["strike"]) == [90, 105]

    def test_at_the_money_volatility_straddle_wings_and_open_interest(self):
        strikes = [90, 95, 100, 105, 110]
        calls = _chain(strikes, [0.40, 0.45, 0.50, 0.52, 0.55], bid=[11, 7, 4, 2, 1], ask=[11.4, 7.4, 4.4, 2.4, 1.4], oi=[10, 20, 30, 40, 50])
        puts = _chain(strikes, [0.70, 0.60, 0.54, 0.50, 0.48], bid=[1, 2, 4, 7, 11], ask=[1.4, 2.4, 4.4, 7.4, 11.4], oi=[5, 5, 5, 5, 5])
        stats = ms.expiry_stats(calls, puts, 100.0)
        assert stats["iv"] == pytest.approx(0.52)                      # the 100 strike: (0.50 + 0.54) / 2
        assert stats["straddle"] == pytest.approx((4.2 + 4.2) / 100)
        assert stats["put_wing_iv"] == pytest.approx(0.70) and stats["call_wing_iv"] == pytest.approx(0.55)
        assert stats["oi_calls"] == 150 and stats["oi_puts"] == 25

    def test_an_expiry_with_fewer_than_two_quotes_or_no_strike_near_the_price_is_dropped(self):
        assert ms.expiry_stats(_chain([100], [0.5]), _chain([], []), 100.0) is None
        far = _chain([150, 160], [0.5, 0.5])
        assert ms.expiry_stats(far, far, 100.0) is None

    def test_thirty_day_volatility_is_interpolated_in_total_variance(self):
        iv, how = ms.constant_maturity_iv([(14, 1.0), (45, 0.8)])
        t1, t2, t = 14 / 365, 45 / 365, 30 / 365
        w = (t - t1) / (t2 - t1)
        assert iv == pytest.approx((((1 - w) * 1.0 ** 2 * t1 + w * 0.8 ** 2 * t2) / t) ** 0.5)
        assert how == "interpolated between the 14- and 45-day expiries"

    def test_one_sided_expiries_use_the_nearest_and_say_so(self):
        assert ms.constant_maturity_iv([(45, 0.8), (60, 0.7)]) == (0.8, "nearest expiry (45 days)")
        assert ms.constant_maturity_iv([(30, 0.9)]) == (0.9, "nearest expiry (30 days)")
        assert ms.constant_maturity_iv([]) is None


ROWS = [
    {"date": "2026-11-13", "days": 7, "iv": 1.10, "straddle": 0.12, "put_wing_iv": 1.2, "call_wing_iv": 0.9,
     "oi_calls": 1000, "oi_puts": 2500, "vol_calls": 300, "vol_puts": 700},
    {"date": "2026-12-18", "days": 42, "iv": 0.80, "straddle": 0.2, "put_wing_iv": 0.9, "call_wing_iv": 0.8,
     "oi_calls": 500, "oi_puts": 500, "vol_calls": 10, "vol_puts": 10},
]


@pytest.mark.unit
class TestReport:
    @pytest.fixture(autouse=True)
    def _steady_inputs(self, monkeypatch):
        monkeypatch.setattr(ms, "_realized", lambda close, sessions=30: 0.60)
        monkeypatch.setattr(ms, "_realized_history", lambda close: pd.Series(np.linspace(0.3, 0.9, 250)))

    def _report(self, options, earnings=None, prices=None):
        return ms.build_report("TEST", _prices() if prices is None else prices, options, earnings, "2026-11-07")

    def _flags(self, text):
        return [l[2:] for l in text.split("## Flags to state in the report\n")[1].splitlines() if l.startswith("- ")]

    def test_the_option_lines_give_volatility_ratio_proxy_moves_term_straddle_flow_and_skew(self):
        text = self._report({"expiries": ROWS}, earnings=pd.Timestamp("2026-11-10"))
        assert "Implied volatility for 30 days, at the money:" in text and "interpolated between the 7- and 42-day expiries" in text
        assert "times realized volatility." in text
        assert "Proxy for an implied-volatility percentile (Yahoo has no implied-volatility history)" in text
        assert "as arithmetic and not a forecast, a target or a limit" in text
        assert "Term structure: 7-day expiry 110%, 42-day expiry 80%." in text
        assert "straddle implies a move of about ±12% through 2026-11-13 (7 days), which includes the earnings report expected 2026-11-10" in text
        assert "Put/call ratio, expiries up to 60 days: open interest 2.00 (3,000 puts, 1,500 calls); today's volume 2.29." in text
        assert "Skew at the 7-day expiry" in text

    def test_the_flags_follow_the_thresholds(self):
        flags = self._flags(self._report({"expiries": ROWS}))
        assert any(f.startswith("Near-term implied volatility (110%, 7 days) is well above the 42-day figure (80%)") for f in flags)
        assert any(f.startswith("Out-of-the-money puts are priced 30 volatility points above") for f in flags)
        assert any(f.startswith("Open-interest put/call ratio is 2.0") for f in flags)  # 3,000 puts to 1,500 calls
        quiet = [dict(ROWS[0], iv=0.6, put_wing_iv=0.62, call_wing_iv=0.6, oi_puts=900),
                 dict(ROWS[1], iv=0.6, put_wing_iv=0.62, call_wing_iv=0.6)]
        assert not any("Near-term" in f or "Out-of-the-money" in f for f in self._flags(self._report({"expiries": quiet})))

    def test_a_high_level_a_ratio_and_a_high_proxy_each_get_a_flag(self):
        text = self._report({"expiries": [dict(ROWS[0], days=30, iv=3.6, put_wing_iv=None, call_wing_iv=None)]})
        flags = self._flags(text)
        assert any(f.startswith("Implied volatility is 360% a year, 100% or more") for f in flags)
        assert any("times realized volatility: the options market prices more movement" in f for f in flags)
        assert any("above 100% of the past year's realized-volatility readings: high against" in f for f in flags)
        assert "A figure above 100% is only the arithmetic" in text

    def test_a_put_heavy_book_is_flagged_as_a_fact_that_can_be_hedging(self):
        heavy = [dict(ROWS[0], days=30, oi_calls=300, oi_puts=900)]
        flag = next(f for f in self._flags(self._report({"expiries": heavy})) if "put/call ratio is" in f)
        assert flag == "Open-interest put/call ratio is 3.0: puts far outnumber calls, which can reflect hedging as well as bearish positioning."
        thin = [dict(ROWS[0], days=30, oi_calls=50, oi_puts=100)]
        assert "too few open contracts to report one" in self._report({"expiries": thin})

    def test_withheld_missing_and_empty_options_are_said_not_filled_in(self):
        withheld = ms.build_report("TEST", _prices(), None, None, "2026-06-30", "Yahoo's option chains are live only.")
        assert "Option-based figures are not served: Yahoo's option chains are live only." in withheld
        assert "No usable listed-option quotes were found" in self._report({"expiries": []})
        assert "Implied volatility for 30 days" not in withheld

    def test_the_price_figures_give_ranges_year_volume_and_flags(self):
        text = self._report({"expiries": []}, prices=_prices(last_volume=9_000_000))
        assert "13-week range" in text and "26-week range" in text and "52-week range" in text
        assert "A year ago (252 sessions)" in text and "Year to date:" in text
        assert any("times the 90-day average; a few very large sessions" in f for f in self._flags(text))
        assert text.startswith("# Market statistics for TEST (Yahoo prices through 2026-11-06, analysis date 2026-11-07)")

    def _spike_prices(self, **kwargs):
        """A steady history with one enormous session about 30 sessions back (inside the 90-day window)."""
        prices = _prices(**kwargs)
        prices.loc[prices.index[-30], "Volume"] = 400_000_000
        return prices

    def test_a_steady_history_shows_the_median_and_no_domination_flag(self):
        text = self._report({"expiries": []})
        assert "- 90-day dollar volume a day: average $" in text and ", median $" in text and "times the median." in text
        assert not any("dominate the 90-day average" in f for f in self._flags(text))

    def test_one_huge_session_is_said_to_dominate_the_average_and_the_latest_session_is_judged_by_the_median(self):
        # the average is several times the median, so the latest ordinary session is NOT called 0.1x "the average"
        text = self._report({"expiries": []}, prices=self._spike_prices())
        flags = self._flags(text)
        assert any(f.startswith("A few very large sessions dominate the 90-day average dollar volume: the average is $") and
                   "Judge the latest session against the median, where it is 1." in f for f in flags)
        assert not any("times the 90-day average" in f for f in flags)
        assert "times the average and 1." in text

    def test_a_real_volume_surge_is_still_flagged_against_the_median_when_the_average_is_dominated(self):
        flags = self._flags(self._report({"expiries": []}, prices=self._spike_prices(last_volume=10_000_000)))
        assert any(f.startswith("The latest session's dollar volume is ") and "times the 90-day median." in f for f in flags)

    def test_a_stock_that_barely_trades_has_a_median_of_zero_and_says_so(self):
        prices = _prices(last_volume=1_000_000)
        prices.loc[prices.index[-60:-10], "Volume"] = 0
        text = self._report({"expiries": []}, prices=prices)
        assert ", median $0;" in text and "times the median" not in text
        assert any(f.endswith("but the median is $0. Judge the latest session against the median.") for f in self._flags(text))


@pytest.mark.unit
class TestEntryPoint:
    def test_a_run_dated_today_reads_the_options_and_a_past_run_does_not(self, monkeypatch):
        monkeypatch.setattr(ms, "load_ohlcv", lambda *a, **k: _prices())
        monkeypatch.setattr(ms, "_option_summary", lambda *a: {"expiries": ROWS})
        monkeypatch.setattr(ms, "_next_earnings", lambda *a: None)
        assert "Implied volatility for 30 days" in ms.get_market_statistics("test", None)
        monkeypatch.setattr(ms, "_option_summary", lambda *a: pytest.fail("a past run must not read option chains"))
        past = ms.get_market_statistics("TEST", "2025-06-30")
        assert "Option-based figures are not served: Yahoo's option chains are live only" in past and "52-week range" in past

    def test_option_chains_that_cannot_be_read_do_not_stop_the_price_figures(self, monkeypatch):
        def broken(*args):
            raise VendorUnavailableError("down")

        monkeypatch.setattr(ms, "load_ohlcv", lambda *a, **k: _prices())
        monkeypatch.setattr(ms, "_option_summary", broken)
        text = ms.get_market_statistics("TEST", None)
        assert "could not be retrieved this time" in text and "52-week range" in text

    def test_a_very_short_price_history_is_no_data(self, monkeypatch):
        monkeypatch.setattr(ms, "load_ohlcv", lambda *a, **k: _prices(n=20))
        with pytest.raises(NoMarketDataError):
            ms.get_market_statistics("TEST", None)

    def test_the_market_analyst_is_offered_the_tool_with_yahoo_as_the_default_vendor(self):
        from tradingagents.agents.analysts import fundamentals_analyst, market_analyst
        from tradingagents.dataflows import router
        from tradingagents.default_config import DEFAULT_CONFIG

        assert "get_market_statistics" in [t.name for t in market_analyst.TOOLS]
        assert "get_market_statistics" not in [t.name for t in fundamentals_analyst.TOOLS]
        assert set(router.VENDOR_METHODS["get_market_statistics"]) == {"yfinance", "ibkr"}
        assert DEFAULT_CONFIG["data_vendors"]["market_statistics"] == "yfinance"
