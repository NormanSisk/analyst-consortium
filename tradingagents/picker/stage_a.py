"""The price-only pass: how far below its high, and how much of the fall the market explains (a local addition).

It uses the very same arithmetic as the analysis's market-context section (``market_context.regression``, ``attribution``,
``drawdown`` and ``move``), so a share the picker computes is the share the analysis will print. What the analysis leaves to a
model to read, this makes a fixed rule:

* a window in which the stock did not fall by at least 3% has no fall to explain, and is ignored;
* in a window with a fall, the share is the market's beta-implied move divided by the stock's move; more than 100% means the
  market implies more than the stock's whole fall, and passes;
* if the market did not fall in that window, it explains nothing (share 0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tradingagents.dataflows.vendors.yahoo import market_context as mc

WINDOWS = (63, 21)
MIN_SESSIONS = 120                     # fewer closes than this: too little history to work with


@dataclass
class Window:
    sessions: int
    stock: float | None = None         # the stock's move over the window
    implied: float | None = None       # what the market's move implies at the stock's beta
    fell: bool = False                 # the stock fell by at least the analysis's 3% "worth splitting" size
    share: float | None = None         # the share of the fall the market explains (None when there is no fall or no beta)


@dataclass
class PriceMetrics:
    price: float
    as_of: date
    off_high: float                    # 0 at the high, -0.25 for 25% below it
    beta: float | None
    r2: float | None
    w63: Window = field(default_factory=lambda: Window(63))
    w21: Window = field(default_factory=lambda: Window(21))

    def window(self, sessions: int) -> Window:
        return self.w63 if sessions == 63 else self.w21


@dataclass
class MarketState:
    drawdown: float                    # the S&P 500 against its 52-week high (negative)
    label: str                         # near its high, pullback, correction or bear market
    vix: float | None
    vix_rank: float | None             # the share of the past year's readings the VIX is above


def explained_share(stock: float | None, implied: float | None) -> tuple[bool, float | None]:
    """(did the stock fall by enough to need explaining, the share of the fall the market explains)."""
    if stock is None or implied is None or stock > -mc.MIN_SPLIT_MOVE:
        return False, None
    if implied >= 0:
        return True, 0.0               # the market did not fall, so it explains none of the stock's fall
    return True, implied / stock       # both negative: a positive ratio; above 1 means the market implies more than the fall


def window_for(stock: pd.Series, market: pd.Series, beta: float | None, sessions: int) -> Window:
    if beta is None:
        return Window(sessions)
    part = mc.attribution(stock, market, beta, sessions)
    if part is None:
        return Window(sessions)
    fell, share = explained_share(part["stock"], part["implied"])
    return Window(sessions, part["stock"], part["implied"], fell, share)


def price_metrics(close: pd.Series, market: pd.Series) -> PriceMetrics | None:
    """The price-only facts for one stock against the market, or None when there is too little history."""
    if close is None or len(close) < MIN_SESSIONS:
        return None
    regression = mc.regression(close, market)
    beta = regression["beta"] if regression else None
    metrics = PriceMetrics(price=float(close.iloc[-1]), as_of=close.index[-1].date(), off_high=mc.drawdown(close),
                           beta=beta, r2=regression["r2"] if regression else None)
    metrics.w63, metrics.w21 = (window_for(close, market, beta, s) for s in WINDOWS)
    return metrics


def sector_windows(close: pd.Series, sector: pd.Series) -> tuple[Window, Window] | None:
    """The same split against a sector fund instead of the market, to show how much of a fall is the sector's."""
    regression = mc.regression(close, sector)
    if not regression:
        return None
    return tuple(window_for(close, sector, regression["beta"], s) for s in WINDOWS)      # type: ignore[return-value]


def market_label(drawdown: float) -> str:
    depth = -drawdown
    if depth >= mc.BEAR:
        return "bear market"
    if depth >= mc.CORRECTION:
        return "correction"
    if depth >= mc.PULLBACK:
        return "pullback"
    return "near its 52-week high"


def market_state(market: pd.Series, vix: pd.Series | None) -> MarketState:
    drawdown = mc.drawdown(market)
    vix_now = rank = None
    if vix is not None and len(vix) >= 60:
        window = vix.tail(mc.YEAR_SESSIONS)
        vix_now, rank = float(vix.iloc[-1]), float((window < vix.iloc[-1]).mean())
    return MarketState(drawdown=drawdown, label=market_label(drawdown), vix=vix_now, vix_rank=rank)
