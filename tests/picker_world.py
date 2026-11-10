"""Made-up markets and companies for the picker's tests (no network, fully deterministic)."""

from __future__ import annotations

import copy
from datetime import date

import numpy as np
import pandas as pd

DAYS = 300
INDEX = pd.bdate_range(end="2026-11-06", periods=DAYS)
AS_OF = INDEX[-1].date()


def levels(returns, start: float = 100.0) -> pd.Series:
    """A price series from daily returns, ending on the last day of INDEX."""
    returns = np.asarray(returns, dtype=float)
    return pd.Series(start * np.cumprod(1 + returns), index=INDEX[-len(returns):])


def base_returns(seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0004, 0.006, DAYS - 63)


def market_returns(last: float = -0.002) -> np.ndarray:
    """A year of noise, then 63 sessions of steady falling (a market pullback or worse)."""
    return np.concatenate([base_returns(), np.full(63, last)])


def rising_market_returns() -> np.ndarray:
    """Every day up a little, never the same twice: no drawdown, but a real variance to take a beta from."""
    return 0.0005 + 0.0003 * np.sin(np.arange(DAYS))


def vix(high: bool = True) -> pd.Series:
    values = np.linspace(14, 16, DAYS)
    if high:
        values[-1] = 31.0
    else:
        values[:] = 15.0
    return pd.Series(values, index=INDEX)


def raw(symbol: str = "AAA", **info) -> dict:
    """A healthy company's raw fundamentals, in the shape the data layer returns."""
    quarters = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]

    def lines(*values):
        return dict(zip(quarters, values, strict=False))

    data = {
        "symbol": symbol,
        "info": {"shortName": f"{symbol} Corp", "sector": "Technology", "industry": "Semiconductors", "sectorKey": "technology",
                 "industryKey": "semiconductors", "quoteType": "EQUITY", "currency": "USD", "financialCurrency": "USD",
                 "forwardPE": 12.0, "trailingPE": 15.0, "marketCap": 5e10, "numberOfAnalystOpinions": 20, "revenueGrowth": 0.2},
        "income": {"Total Revenue": lines(100, 95, 90, 88, 80), "Operating Income": lines(25, 24, 22, 21, 20),
                   "Diluted Average Shares": lines(1000, 1000, 1000, 1000, 1000), "Net Income": lines(20, 19, 18, 17, 16)},
        "cashflow": {"Operating Cash Flow": lines(30, 29, 28, 27, 26), "Free Cash Flow": lines(20, 18, 17, 16, 15),
                     "Capital Expenditure": lines(-10, -11, -11, -11, -11)},
        "balance": {"Total Debt": lines(100, 100, 100, 100, 100),
                    "Cash Cash Equivalents And Short Term Investments": lines(500, 480, 460, 450, 440)},
        "eps_trend": {"0y": {"current": 10.0, "30daysAgo": 9.8, "90daysAgo": 9.5},
                      "+1y": {"current": 12.0, "30daysAgo": 11.5, "90daysAgo": 11.0}},
        "eps_revisions": {"0y": {"upLast30days": 5.0, "downLast30days": 1.0}, "+1y": {"upLast30days": 6.0, "downLast30days": 1.0}},
        "estimate": {"+1y": {"growth": 0.2, "numberOfAnalysts": 20.0}},
        "errors": [],
    }
    data["info"].update(info)
    return data


class FakeProvider:
    """A provider that hands the picker made-up data and records what it was asked for."""

    def __init__(self, universe=None, closes=None, raws=None, treasury: float | None = 4.8, industry_pes=None, fail=(), matches: int = 0):
        self._universe = universe or []
        self._matches = matches
        self.screen_calls: list[tuple] = []
        self.count_calls: list[tuple] = []
        self._closes = closes or {}
        self._raws = raws or {}
        self._treasury = treasury
        self._industry_pes = industry_pes if industry_pes is not None else {"Semiconductors": [10, 11, 12, 13, 14, 15]}
        self._fail = set(fail)
        self.fundamentals_calls: list[str] = []
        self.closes_calls: list[list[str]] = []

    def universe(self, settings):
        return list(self._universe)

    def closes(self, symbols):
        self.closes_calls.append(list(symbols))
        return {s: self._closes[s] for s in symbols if s in self._closes}

    def fundamentals(self, symbol):
        self.fundamentals_calls.append(symbol)
        if symbol in self._fail:
            raise OSError("Yahoo is down")
        return copy.deepcopy(self._raws.get(symbol) or {"symbol": symbol, "error": "Yahoo has no data for this symbol"})

    def ten_year_yield(self):
        return self._treasury

    def industry_forward_pes(self, industry, settings):
        return list(self._industry_pes.get(industry, []))

    def screen(self, terms, sectors, limit):
        """What a custom search asks Yahoo for is recorded; the answer is the made-up universe, the largest first."""
        self.screen_calls.append((list(terms), list(sectors), limit))
        return sorted(copy.deepcopy(self._universe), key=lambda r: r.get("market_cap") or 0, reverse=True)[:limit]

    def count(self, terms, sectors):
        self.count_calls.append((list(terms), list(sectors)))
        return self._matches


def record(symbol: str, forward_pe: float = 12.0) -> dict:
    return {"symbol": symbol, "name": f"{symbol} Corp", "sector": "Technology", "market_cap": 5e10, "forward_pe": forward_pe}


def pullback_world() -> FakeProvider:
    """A falling market and five stocks: GOOD (the market explains its fall), OWN (it fell on its own), FLAT (near its
    high), SHORT (too little history) and BAD (the market explains its fall, but the company is deteriorating)."""
    m = market_returns()
    own = np.concatenate([base_returns(), np.full(63, -0.012)])
    flat = np.concatenate([base_returns(), np.full(63, 0.001)])
    bad = raw("BAD")
    bad["cashflow"]["Operating Cash Flow"] = dict(zip(["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"], (12, 20, 24, 25, 26), strict=True))
    bad["balance"]["Total Debt"] = dict(zip(["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"], (300, 200, 150, 120, 100), strict=True))
    closes = {"^GSPC": levels(m), "^VIX": vix(True), "GOOD": levels(2 * m), "BAD": levels(2 * m), "OWN": levels(own),
              "FLAT": levels(flat), "SHORT": levels(2 * m[-50:]), "SMH": levels(1.8 * m)}
    return FakeProvider(universe=[record(s) for s in ("GOOD", "OWN", "FLAT", "SHORT", "BAD")], closes=closes,
                        raws={"GOOD": raw("GOOD"), "BAD": bad, "OWN": raw("OWN"), "FLAT": raw("FLAT")})


def calm_world() -> FakeProvider:
    """A market that is not falling at all, with GOOD still a stock that fell hard on its own."""
    m = rising_market_returns()
    own = np.concatenate([base_returns(), np.full(63, -0.012)])
    closes = {"^GSPC": levels(m), "^VIX": vix(False), "GOOD": levels(own)}
    return FakeProvider(universe=[record("GOOD")], closes=closes, raws={"GOOD": raw("GOOD")})


TODAY = date(2026, 11, 8)
