"""A one-call Yahoo reachability probe for ``tradingagents doctor`` (a local addition).

It lives here because only the data layer imports yfinance. It raises whatever yfinance raises; the doctor reports
the exception's type and nothing else.
"""

import yfinance as yf


def recent_price_rows(symbol: str = "SPY", period: str = "5d") -> int:
    """How many daily prices Yahoo returns for ``symbol`` over ``period`` (0 when it answers with nothing)."""
    return len(yf.Ticker(symbol).history(period=period))
