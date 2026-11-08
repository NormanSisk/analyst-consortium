"""Analyst rating and price-target history, and daily prices, for the track-record study (a local addition).

Only the data layer imports yfinance, so the study's downloads live here and its statistics (which need no network) live
in ``tradingagents.research.track_record``.

Yahoo's rating history names the firm, not the analyst, and goes back to about 2012. Prices come back three ways: ``adj``
(adjusted for splits and dividends, for returns), and ``close``, ``high`` and ``low`` (adjusted for splits only, for
comparing with a price target, which is a price level).
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.vendors.yahoo.common import yf_retry

RATING_COLUMNS = ["Firm", "ToGrade", "FromGrade", "Action", "priceTargetAction", "currentPriceTarget", "priorPriceTarget"]
BATCH = 15


def fetch_rating_history(symbol: str) -> pd.DataFrame:
    """Every rating and target action Yahoo holds for ``symbol``: one row per action, with a ``date`` column.

    Returns an empty frame (with the columns) for a symbol Yahoo has no history for.
    """
    frame = yf_retry(lambda: yf.Ticker(symbol).upgrades_downgrades)
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["date", *RATING_COLUMNS])
    out = frame.copy()
    out = out.reindex(columns=RATING_COLUMNS)
    out.insert(0, "date", pd.to_datetime(out.index))
    return out.reset_index(drop=True)


def fetch_split_history(symbol: str) -> pd.Series:
    """Every stock split Yahoo records for ``symbol``: the ratio (2.0 for a 2-for-1), indexed by its date.

    Yahoo keeps an old price target in the share units of the day it was issued, while its price history is adjusted to
    today's shares, so the study needs the splits to put the two in the same units.
    """
    splits = yf_retry(lambda: yf.Ticker(symbol).splits)
    if splits is None or len(splits) == 0:
        return pd.Series(dtype=float)
    out = pd.Series(splits.to_numpy(dtype=float), index=pd.to_datetime(splits.index).tz_localize(None).normalize())
    return out[out > 0]


def fetch_price_history(symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
    """Daily prices from ``start`` for each symbol, as {symbol: frame with adj, close, high, low}.

    Symbols Yahoo returns nothing for are left out, so the caller can say how many it lost.
    """
    prices: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        raw = yf_retry(lambda chunk=chunk: yf.download(chunk, start=start, auto_adjust=False, progress=False,
                                                       group_by="ticker", threads=True))
        if raw is None or len(raw) == 0:
            continue
        for symbol in chunk:
            try:
                part = raw[symbol] if len(chunk) > 1 else raw
                frame = pd.DataFrame({"adj": part["Adj Close"], "close": part["Close"],
                                      "high": part["High"], "low": part["Low"]}).dropna(subset=["adj", "close"])
            except (KeyError, TypeError):
                continue
            if len(frame):
                frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
                prices[symbol] = frame
    return prices
