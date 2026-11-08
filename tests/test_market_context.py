"""Tests for the market-context vendor (a local addition): the market's state, and a stock's beta, relative
volatility and market-explained move, worked out from synthetic dated closes."""

import re

import numpy as np
import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import market_context as mc

N = 300
AS_OF = "2026-11-06"


def _prices(returns, base=100.0) -> pd.Series:
    returns = np.asarray(returns, dtype=float)
    values = base * np.concatenate([[1.0], np.cumprod(1 + returns)])
    return pd.Series(values, index=pd.bdate_range(end="2026-11-06", periods=len(values)))


def _world(seed=11, **kw):
    """A calm market (beta 1 by construction), a stock that tracks it at beta 1.5, and a VIX."""
    rng = np.random.default_rng(seed)
    market = rng.normal(0.0003, 0.009, N)
    stock = 1.5 * market + rng.normal(0, 0.003, N)
    return rng, market, stock


def _markets(market):
    rng = np.random.default_rng(5)
    return {"S&P 500": _prices(market), "Nasdaq Composite": _prices(1.2 * market + rng.normal(0, 0.003, N)),
            "Russell 2000 (IWM)": _prices(0.9 * market + rng.normal(0, 0.003, N))}


def _vix(rising: bool) -> pd.Series:
    path = np.linspace(14, 31, N) if rising else np.linspace(31, 14, N)
    return pd.Series(path, index=pd.bdate_range(end="2026-11-06", periods=N))


def _report(stock, market, vix=None, sector=None, markets=None):
    markets = markets or _markets(market)
    return mc.build_report("TEST", _prices(stock), markets, vix, sector, AS_OF)


def _flags(text: str) -> list[str]:
    section = text.split("## Flags to state in the report\n")[1].split("\n\n")[0]
    return [line[2:] for line in section.splitlines() if line.startswith("- ")]


@pytest.mark.unit
class TestMarketState:
    def test_the_table_lists_each_index_with_drawdown_moves_volatility_and_a_label(self):
        _, market, stock = _world()
        text = _report(stock, market)
        assert text.startswith("# Market context for TEST (Yahoo daily closes through 2026-11-06, analysis date 2026-11-06)")
        for name in ("S&P 500", "Nasdaq Composite", "Russell 2000 (IWM)"):
            assert re.search(rf"\| {re.escape(name)} \| -?[\d.]+% \| [+-][\d.]+% \| [+-][\d.]+% \| \d+\.\d% \|", text)

    def test_the_labels_follow_the_common_drawdown_conventions(self):
        assert mc._label(-0.02) == "near its 52-week high"
        assert mc._label(-0.057) == "pullback"
        assert mc._label(-0.109) == "correction"
        assert mc._label(-0.306) == "bear market"

    def test_a_bear_market_is_flagged_with_the_s_and_p_for_contrast(self):
        _, market, stock = _world()
        bear = np.concatenate([np.full(260, 0.0005), np.full(40, -0.008)])
        text = _report(stock, bear, markets={**_markets(market), "S&P 500": _prices(bear)})
        flag = _flags(text)[0]
        assert "S&P 500 is 27." in flag and "a bear market by the 20% convention" in flag

    def test_a_sector_fund_is_a_table_row_and_a_relative_strength_benchmark(self):
        _, market, stock = _world()
        sector = ("Semiconductors (SMH)", _prices(1.8 * market))
        text = _report(stock, market, sector=sector)
        assert "| Semiconductors (SMH) |" in text
        assert "points against Semiconductors (SMH)" in text
        assert "the sector fund's" in text and "No sector benchmark" not in text
        assert "No sector benchmark is shown" in _report(stock, market)

    def test_high_market_wide_volatility_is_flagged_and_a_calm_vix_is_not(self):
        _, market, stock = _world()
        high = _report(stock, market, vix=_vix(rising=True))
        assert any(f.startswith("Market-wide volatility is high (VIX 31.0") for f in _flags(high))
        assert "above 100% of that year's readings" in high or "above 99% of that year's readings" in high
        assert not any("Market-wide volatility" in f for f in _flags(_report(stock, market, vix=_vix(rising=False))))


@pytest.mark.unit
class TestStockAgainstMarket:
    def _beta(self, text):
        match = re.search(r"Beta to the S&P 500 over the last (\d+) sessions: (-?\d\.\d\d)", text)
        assert match, text
        return int(match.group(1)), float(match.group(2))

    def test_a_stock_that_tracks_the_market_gets_its_beta_and_no_stock_specific_flag(self):
        _, market, stock = _world()
        text = _report(stock, market)
        sessions, beta = self._beta(text)
        assert sessions == 250 and 1.4 <= beta <= 1.6
        assert re.search(r"The market explains (9\d)% of the variation in the stock's daily moves \(R-squared 0\.9\d\)", text)
        assert re.search(r"\(uncertainty about ±0\.0\d\)", text)
        assert "the S&P 500's" in text and "x the" in text
        assert "Last 63 sessions:" in text and "Last 21 sessions:" in text
        assert not any("mostly specific" in f or "held up" in f or "imprecise" in f for f in _flags(text))

    def test_a_fall_the_market_does_not_explain_is_flagged_as_stock_specific(self):
        _, market, stock = _world()
        stock = stock.copy()
        stock[-63:] -= 0.01
        text = _report(stock, market)
        assert re.search(r"points of the stock's move are not explained by the market", text)
        flag = next(f for f in _flags(text) if "mostly specific to the stock" in f)
        assert flag.startswith("The stock fell ") and "more than the market implies over 63 sessions" in flag

    def test_a_stock_that_held_up_better_than_its_beta_implies_is_flagged(self):
        _, market, stock = _world()
        stock = stock.copy()
        stock[-63:] += 0.008
        flag = next(f for f in _flags(_report(stock, market)) if f.startswith("The stock has held up"))
        assert "better than the market implies over 63 sessions" in flag

    def test_a_beta_that_is_imprecise_is_said_to_be_and_no_residual_is_flagged(self):
        rng, market, _ = _world()
        noise = rng.normal(0, 0.02, N)                  # unrelated to the market: a beta near zero, with a wide error
        noise[-63:] -= 0.01
        text = _report(noise, market)
        flags = _flags(text)
        assert any(re.match(r"The beta of -?\d\.\d\d is imprecise \(uncertainty about ±\d\.\d\d\), so the market-implied "
                            r"moves above are rough and no residual is flagged\.", f) for f in flags)
        assert "Last 63 sessions:" in text
        assert not any("mostly specific" in f or "held up" in f for f in flags)

    def test_a_low_r_squared_does_not_stop_a_precise_beta_from_splitting_a_fall(self):
        rng, market, _ = _world()
        stock = 2.0 * market + rng.normal(0, 0.03, N)
        stock[-63:] -= 0.02
        text = _report(stock, market)
        r2 = float(re.search(r"\(R-squared (0\.\d\d)\)", text).group(1))
        assert r2 < 0.3
        assert any("mostly specific to the stock" in f for f in _flags(text))
        assert not any("imprecise" in f for f in _flags(text))

    def test_very_high_stock_specific_volatility_is_flagged_whatever_the_market_does(self):
        rng, market, _ = _world()
        text = _report(1.0 * market + rng.normal(0, 0.05, N), market)
        assert any(f.startswith("Stock-specific volatility is ") and "very high whatever the market is doing" in f
                   for f in _flags(text))

    def test_a_short_history_gives_a_rough_beta_and_a_very_short_one_gives_none(self):
        _, market, stock = _world()
        short = _report(stock[-120:], market)
        assert any(re.match(r"Only \d+ sessions of shared history: the beta and volatility figures are rough", f)
                   for f in _flags(short))
        very_short = _report(stock[-40:], market)
        assert "Beta cannot be worked out: only 40 sessions are shared with the S&P 500 (at least 60 are needed)." in very_short

    def test_relative_strength_and_the_distance_from_the_high_are_given_against_the_market(self):
        _, market, stock = _world()
        text = _report(stock, market)
        assert re.search(r"Relative strength: 1 month [+-][\d.]+ points against S&P 500; 3 months [+-][\d.]+ points against S&P 500\.", text)
        assert re.search(r"The stock is -?[\d.]+% from its 52-week high, against -?[\d.]+% for the S&P 500\.", text)


@pytest.mark.unit
class TestReading:
    """The split in words, so a stock with mostly its own volatility is not mistaken for one whose recent move
    was mostly its own (an earlier run called a move 'mostly stock-specific' when 75% of it was market-implied)."""

    def _part(self, stock, implied):
        return {"stock": stock, "market": -0.049, "implied": implied, "residual": stock - implied}

    def test_a_move_the_market_mostly_implies_is_called_market_driven(self):
        assert mc._reading(self._part(-0.278, -0.209)) == (
            "About 75% of the stock's move is what the market implies at this beta and 25% is not explained by it, "
            "so the move is mostly market-driven.")

    def test_a_move_the_market_barely_implies_is_called_specific_and_the_middle_is_a_mix(self):
        assert mc._reading(self._part(-0.60, -0.15)).endswith("so the move is mostly specific to the stock.")
        assert mc._reading(self._part(-0.40, -0.20)).endswith("so the move is a mix of the two.")

    def test_an_opposite_direction_says_who_did_better_and_by_how_much(self):
        assert mc._reading(self._part(0.10, -0.014)) == (
            "The market implies the opposite direction (-1.4% against +10.0%): the stock did 11 points better than its "
            "beta implies.")
        assert mc._reading(self._part(0.026, -0.102)).startswith("The market implies the opposite direction")

    def test_a_market_implied_move_larger_than_the_stocks_says_who_did_better(self):
        assert mc._reading(self._part(-0.044, -0.163)) == (
            "The market implies more than the stock's whole move (-16.3% against -4.4%): the stock did 12 points better "
            "than its beta implies.")
        assert "the stock did 7 points worse than its beta implies" in mc._reading(self._part(0.05, 0.12))

    def test_a_split_is_skipped_only_when_both_moves_are_small(self):
        assert mc._reading(self._part(-0.02, -0.01)) == (
            "Neither the stock's move nor the market-implied move is large enough to split.")
        assert not mc._reading(self._part(0.01, -0.10)).startswith("Neither")

    def test_the_report_carries_a_reading_for_each_window_and_the_beta_note(self):
        _, market, stock = _world()
        text = _report(stock, market)
        assert len(re.findall(r"Reading: ", text)) == 2
        assert "the beta on Yahoo's company profile is usually a five-year monthly figure" in text
        assert "what drove its recent move are different questions" in text


@pytest.mark.unit
class TestEntryPoint:
    @pytest.fixture
    def world(self, monkeypatch):
        _, market, stock = _world()
        closes = {"TEST": _prices(stock), "^GSPC": _prices(market), "^IXIC": _prices(1.2 * market),
                  "IWM": _prices(0.9 * market), "^VIX": _vix(rising=False), "SMH": _prices(1.8 * market),
                  "XLK": _prices(1.1 * market)}
        asked = []

        def fake_close(symbol, as_of):
            asked.append(symbol)
            if symbol not in closes:
                raise NoMarketDataError(symbol, symbol, "no rows")
            return closes[symbol]

        class FakeTicker:
            info = {"sectorKey": "technology", "industryKey": "semiconductors"}

        monkeypatch.setattr(mc, "_close_series", fake_close)
        monkeypatch.setattr(mc, "yf", type("FakeYF", (), {"Ticker": lambda symbol: FakeTicker()}))
        return closes, asked, FakeTicker

    def test_the_industry_fund_beats_the_sector_fund_and_the_report_is_assembled(self, world):
        _, asked, _ = world
        out = mc.get_market_context("test", None)
        assert "| Semiconductors (SMH) |" in out and "SMH" in asked and "XLK" not in asked
        assert "## The market" in out and "## TEST against the market" in out and "## Flags to state in the report" in out

    def test_the_sector_fund_is_used_when_the_industry_has_none(self, world):
        _, asked, ticker = world
        ticker.info = {"sectorKey": "technology", "industryKey": "software-application"}
        assert "| Technology (XLK) |" in mc.get_market_context("TEST", None)

    def test_a_run_dated_in_the_past_has_no_sector_fund_and_never_reads_todays_profile(self, world, monkeypatch):
        monkeypatch.setattr(mc, "yf", type("NoYF", (), {"Ticker": lambda symbol: pytest.fail("profile must not be read")}))
        out = mc.get_market_context("TEST", "2025-06-30")
        assert "No sector benchmark is shown" in out and "analysis date 2025-06-30" in out

    def test_a_failed_vix_or_sector_does_not_stop_the_report_but_a_missing_s_and_p_does(self, world, monkeypatch):
        closes, _, _ = world
        del closes["^VIX"], closes["SMH"]
        out = mc.get_market_context("TEST", None)
        assert "- VIX" not in out and "## TEST against the market" in out
        del closes["^GSPC"]
        with pytest.raises(VendorUnavailableError, match="S&P 500"):
            mc.get_market_context("TEST", None)

    def test_symbols_that_are_not_us_listed_stocks_never_reach_yahoo(self, monkeypatch):
        monkeypatch.setattr(mc, "_close_series", lambda *a: pytest.fail("no prices should be fetched"))
        for symbol in ("7203.T", "^GSPC", "GC=F", "BTC-USD", "VOD.L"):
            with pytest.raises(NoMarketDataError):
                mc.get_market_context(symbol, None)

    def test_an_instrument_with_no_prices_is_no_data(self, world):
        with pytest.raises(NoMarketDataError):
            mc.get_market_context("NOPE", None)

    def test_the_market_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import market_analyst
        from tradingagents.dataflows import router
        from tradingagents.default_config import DEFAULT_CONFIG

        assert "get_market_context" in [t.name for t in market_analyst.TOOLS]
        assert router.get_category_for_method("get_market_context") == "market_context"
        assert "market_context" in router.OPTIONAL_CATEGORIES
        assert "yfinance" in router.VENDOR_METHODS["get_market_context"]
        assert DEFAULT_CONFIG["data_vendors"]["market_context"] == "yfinance"
