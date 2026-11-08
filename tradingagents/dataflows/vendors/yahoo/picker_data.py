"""Yahoo data for the stock picker (a local addition).

Only the data layer imports yfinance, so everything the picker downloads lives here and the picker's own logic
(``tradingagents.picker``) works on plain numbers and needs no network.

* ``screen_universe``: Yahoo's free screener, for a list of US stocks above a size and trading volume.
* ``fetch_closes``: adjusted daily closes for many symbols in a few batched calls (a few seconds for hundreds of stocks).
* ``fetch_fundamentals``: one stock's profile, quarterly statements and analyst estimates, cut down to the handful of
  lines the quick look uses and returned as plain dicts, so a result can be saved as JSON and read back.
* ``fetch_ten_year_yield``: the 10-year Treasury yield in percent, with no key.
"""

from __future__ import annotations

import re
import warnings

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.vendors.yahoo.common import yf_retry

PAGE = 250                              # the most Yahoo's screener returns in one answer
BATCH = 100                             # symbols per price download
_PLAIN_SYMBOL = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")

INFO_KEYS = ("shortName", "longName", "sector", "industry", "sectorKey", "industryKey", "quoteType", "currency",
             "financialCurrency", "forwardPE", "trailingPE", "marketCap", "numberOfAnalystOpinions", "revenueGrowth")
STATEMENT_LINES = {
    "quarterly_cashflow": ("Operating Cash Flow", "Free Cash Flow", "Capital Expenditure"),
    "quarterly_income_stmt": ("Total Revenue", "Operating Income", "Diluted Average Shares", "Net Income"),
    "quarterly_balance_sheet": ("Total Debt", "Long Term Debt", "Cash Cash Equivalents And Short Term Investments",
                                "Cash And Cash Equivalents"),
}
STATEMENT_KEYS = {"quarterly_cashflow": "cashflow", "quarterly_income_stmt": "income", "quarterly_balance_sheet": "balance"}


SECTORS = ("Technology", "Healthcare", "Financial Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
           "Industrials", "Basic Materials", "Communication Services", "Real Estate", "Utilities")


def screen_universe(min_cap: float, min_volume: float, limit: int, region: str = "us", sector: str | None = None) -> list[dict]:
    """US-listed ordinary stocks by market value, largest first: symbol, name, market cap, price, volume, P/Es.

    Yahoo's screener answers 250 at a time; ``limit`` caps the total. Funds, warrants and odd symbols are left out. With
    ``sector`` (one of ``SECTORS``) only that sector is returned, and each record carries it.
    """
    terms = [yf.EquityQuery("eq", ["region", region]), yf.EquityQuery("gt", ["intradaymarketcap", float(min_cap)]),
             yf.EquityQuery("gt", ["avgdailyvol3m", float(min_volume)])]
    if sector:
        terms.append(yf.EquityQuery("eq", ["sector", sector]))
    query = yf.EquityQuery("and", terms)
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        answer = yf_retry(lambda offset=offset: yf.screen(query, sortField="intradaymarketcap", sortAsc=False,
                                                          size=PAGE, offset=offset))
        quotes = (answer or {}).get("quotes") or []
        if not quotes:
            break
        for quote in quotes:
            symbol = str(quote.get("symbol") or "")
            if quote.get("quoteType", "EQUITY") == "EQUITY" and _PLAIN_SYMBOL.match(symbol):
                out.append({"symbol": symbol, "name": quote.get("shortName") or quote.get("longName") or "",
                            "market_cap": quote.get("marketCap"), "price": quote.get("regularMarketPrice"),
                            "avg_volume": quote.get("averageDailyVolume3Month"), "sector": sector or "",
                            "forward_pe": quote.get("forwardPE"), "trailing_pe": quote.get("trailingPE")})
        offset += len(quotes)
        if offset >= int(answer.get("total") or 0):
            break
    return out[:limit]


def screen_universe_with_sectors(min_cap: float, min_volume: float, limit: int, region: str = "us") -> list[dict]:
    """The same list as ``screen_universe`` but every stock carries its sector (one screener query per sector).

    Sectors are what the picker compares a stock's valuation against. The result is the ``limit`` largest stocks overall.
    """
    merged: dict[str, dict] = {}
    for sector in SECTORS:
        for record in screen_universe(min_cap, min_volume, limit, region, sector):
            merged.setdefault(record["symbol"], record)
    return sorted(merged.values(), key=lambda r: r.get("market_cap") or 0, reverse=True)[:limit]


def _close_column(raw, symbol: str):
    """The closes of ``symbol`` from a download, whichever way Yahoo laid the columns out (one symbol or many)."""
    for get in (lambda: raw[symbol]["Close"], lambda: raw["Close"][symbol], lambda: raw["Close"]):
        try:
            column = get()
        except (KeyError, TypeError):
            continue
        return column.iloc[:, 0] if isinstance(column, pd.DataFrame) else column
    return None


def fetch_closes(symbols: list[str], period: str = "14mo") -> dict[str, pd.Series]:
    """Adjusted daily closes for each symbol (oldest first, one per date); symbols Yahoo has nothing for are left out."""
    closes: dict[str, pd.Series] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        with warnings.catch_warnings():
            # yfinance's own code triggers pandas "inplace keyword" deprecation notices, hundreds per download; they are not ours to fix
            warnings.filterwarnings("ignore", message=".*inplace keyword.*")
            raw = yf_retry(lambda chunk=chunk: yf.download(chunk, period=period, auto_adjust=True, progress=False,
                                                           group_by="ticker", threads=True))
        if raw is None or len(raw) == 0:
            continue
        for symbol in chunk:
            column = _close_column(raw, symbol)
            if column is None:
                continue
            series = column.dropna().astype(float)
            if len(series):
                series.index = pd.to_datetime(series.index).tz_localize(None).normalize()
                closes[symbol] = series[~series.index.duplicated(keep="last")].sort_index()
    return closes


def _lines(frame, labels) -> dict[str, dict[str, float]]:
    """{label: {quarter end date: value}} for the labels a statement has."""
    out: dict[str, dict[str, float]] = {}
    if frame is None or len(frame) == 0:
        return out
    for label in labels:
        if label in frame.index:
            row = frame.loc[label]
            out[label] = {str(pd.Timestamp(date).date()): float(value) for date, value in row.items() if value == value}
    return out


def _table(frame, columns) -> dict[str, dict[str, float]]:
    """{period: {column: value}} for an estimates table."""
    out: dict[str, dict[str, float]] = {}
    if frame is None or len(frame) == 0:
        return out
    for period, row in frame.iterrows():
        out[str(period)] = {c: float(row[c]) for c in columns if c in row.index and row[c] == row[c]}
    return out


def fetch_fundamentals(symbol: str) -> dict:
    """One stock's profile, quarterly statements and estimates as plain dicts, or ``{"error": ...}`` when Yahoo has none.

    Every piece is fetched on its own, so a missing statement leaves that piece empty instead of losing the others.
    """
    out: dict = {"symbol": symbol, "info": {}, "cashflow": {}, "income": {}, "balance": {}, "eps_trend": {},
                 "eps_revisions": {}, "estimate": {}, "errors": []}

    def piece(name, build):
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - a missing piece is reported, not fatal
            out["errors"].append(f"{name}: {type(exc).__name__}")
            return None

    info = piece("info", lambda: yf_retry(lambda: yf.Ticker(symbol).info))
    if isinstance(info, dict):
        out["info"] = {k: info.get(k) for k in INFO_KEYS if info.get(k) is not None}
    for attr, labels in STATEMENT_LINES.items():
        frame = piece(attr, lambda attr=attr: yf_retry(lambda: getattr(yf.Ticker(symbol), attr)))
        out[STATEMENT_KEYS[attr]] = _lines(frame, labels)
    out["eps_trend"] = _table(piece("eps_trend", lambda: yf_retry(lambda: yf.Ticker(symbol).eps_trend)),
                              ("current", "30daysAgo", "90daysAgo"))
    out["eps_revisions"] = _table(piece("eps_revisions", lambda: yf_retry(lambda: yf.Ticker(symbol).eps_revisions)),
                                  ("upLast30days", "downLast30days"))
    out["estimate"] = _table(piece("earnings_estimate", lambda: yf_retry(lambda: yf.Ticker(symbol).earnings_estimate)),
                             ("growth", "numberOfAnalysts"))
    if not out["info"] and not out["income"]:
        out["error"] = "Yahoo has no data for this symbol"
    return out


def fetch_ten_year_yield() -> float | None:
    """The 10-year Treasury yield in percent (4.84 means 4.84%), from Yahoo's ^TNX, or None."""
    frame = yf_retry(lambda: yf.Ticker("^TNX").history(period="5d"))
    if frame is None or len(frame) == 0:
        return None
    value = float(frame["Close"].dropna().iloc[-1])
    return value / 10 if value > 20 else value               # older quotes carried the yield times ten

def industry_forward_pes(industry: str, min_cap: float, min_volume: float, region: str = "us", limit: int = 250) -> list[float]:
    """The forward P/Es of US stocks in one of Yahoo's industries (the name ``info["industry"]`` gives), for a peer median.

    The screener spells an industry with a dash and no spaces ("Banks—Diversified") where a stock's profile has a hyphen and
    spaces ("Banks - Diversified"); an industry the screener does not know gives an empty list.
    """
    try:
        query = yf.EquityQuery("and", [yf.EquityQuery("eq", ["region", region]),
                                       yf.EquityQuery("eq", ["industry", industry.replace(" - ", "—")]),
                                       yf.EquityQuery("gt", ["intradaymarketcap", float(min_cap)]),
                                       yf.EquityQuery("gt", ["avgdailyvol3m", float(min_volume)])])
    except ValueError:
        return []
    answer = yf_retry(lambda: yf.screen(query, sortField="intradaymarketcap", sortAsc=False, size=min(PAGE, limit)))
    values = [q.get("forwardPE") for q in (answer or {}).get("quotes", [])]
    return [float(v) for v in values if isinstance(v, (int, float)) and v > 0]
