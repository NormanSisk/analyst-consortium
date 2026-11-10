from datetime import UTC, datetime
from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import (
    withhold_live_profile,
    withhold_undated_statements,
    withhold_undisclosed_trades,
)
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import (
    YAHOO_HOST,
    raise_for_empty,
    yf_retry,
)

# Quote currencies Yahoo gives in a minor unit, and the main unit of each.
_MAIN_UNIT = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}

# Yahoo returns every insider filing it holds, often more than a year of them. An
# analyst shown all of it can attach the wrong year to an old trade, so only filings
# in this trailing window are served.
INSIDER_LOOKBACK_DAYS = 120

# Trailing EPS this far above forward EPS usually means one-time items in the trailing year.
_TRAILING_ABOVE_FORWARD = 1.15


def _percent_vs(value, reference) -> str | None:
    """``value`` as a signed percent distance from ``reference``, or None."""
    try:
        return f"{(float(value) / float(reference) - 1) * 100:+.1f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None
):
    """Get company fundamentals overview from yfinance.

    ``Ticker.info`` is a present-day snapshot with no historical vintage, so a
    past ``as_of_date`` withholds it through the shared point-in-time guard
    (``date_window.withhold_live_profile``, #1300).
    """
    canonical = normalize_symbol(ticker)

    # Guard before the request: the response would only be discarded, and the
    # answer does not depend on it.
    withheld = withhold_live_profile(as_of_date, canonical)
    if withheld:
        return withheld

    info = yf_retry(lambda: yf.Ticker(canonical).info)
    if not info:
        raise_for_empty(ticker, canonical, "fundamentals")

    # Yahoo gives these two in percent (dividendYield 0.41 is 0.41%; debtToEquity
    # 78.4 is 78.4%, a ratio of 0.78) but the margins and returns as fractions,
    # so each carries its unit.
    dividend_yield, debt_to_equity = info.get("dividendYield"), info.get("debtToEquity")
    # Money figures come in three currencies (#1456): price levels in the quote
    # currency, which can be a minor unit (London in pence, GBp); market cap and
    # per-share figures in that currency's main unit (GBP), as Yahoo's own P/E
    # and P/B are computed against them; income figures in the currency the
    # company reports in (an ADR quotes in USD, reports in TWD), or bare when
    # Yahoo does not say which.
    quote = info.get("currency")
    main = _MAIN_UNIT.get(quote, quote)
    reported = info.get("financialCurrency")

    def money(key, currency):
        value = info.get(key)
        return None if value is None else f"{value} {currency}" if currency else value

    # The live quote, so the analyst reads the price instead of inferring it from the
    # moving averages below.
    last_price = info.get("currentPrice") or info.get("regularMarketPrice")
    previous_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
    quote_time = info.get("regularMarketTime")
    quote_stamp = (
        datetime.fromtimestamp(quote_time, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        if isinstance(quote_time, (int, float)) else None
    )

    def priced(value):
        return None if value is None else f"{value} {quote}" if quote else value

    fields = [
        ("Name", info.get("longName")),
        ("Sector", info.get("sector")),
        ("Industry", info.get("industry")),
        ("Last Price (live quote)", priced(last_price)),
        ("Previous Close", priced(previous_close)),
        ("Quote Time", quote_stamp),
        ("Market Cap", money("marketCap", main)),
        ("PE Ratio (TTM)", info.get("trailingPE")),
        ("Forward PE (on Yahoo's forward EPS, normally the next fiscal year's estimate)", info.get("forwardPE")),
        ("PEG Ratio", info.get("pegRatio")),
        ("Price to Book", info.get("priceToBook")),
        ("EPS (TTM)", money("trailingEps", main)),
        ("Forward EPS (normally the next fiscal year's estimate)", money("forwardEps", main)),
        ("Dividend Yield", None if dividend_yield is None else f"{dividend_yield}%"),
        ("Beta", info.get("beta")),
        ("52 Week High", money("fiftyTwoWeekHigh", quote)),
        ("52 Week Low", money("fiftyTwoWeekLow", quote)),
        ("50 Day Average", money("fiftyDayAverage", quote)),
        ("200 Day Average", money("twoHundredDayAverage", quote)),
        ("Revenue (TTM)", money("totalRevenue", reported)),
        ("Gross Profit", money("grossProfits", reported)),
        ("EBITDA", money("ebitda", reported)),
        ("Net Income", money("netIncomeToCommon", reported)),
        ("Profit Margin", info.get("profitMargins")),
        ("Operating Margin", info.get("operatingMargins")),
        ("Return on Equity", info.get("returnOnEquity")),
        ("Return on Assets", info.get("returnOnAssets")),
        ("Debt to Equity", None if debt_to_equity is None
         else f"{debt_to_equity}% ({debt_to_equity / 100:.2f}x)"),
        ("Current Ratio", info.get("currentRatio")),
        ("Book Value", money("bookValue", main)),
        ("Free Cash Flow", money("freeCashflow", reported)),
    ]

    lines = [f"{label}: {v}" for label, v in fields if v is not None]

    # yfinance returns a stub dict (e.g. {"trailingPegRatio": None}) for
    # unknown symbols, so `info` is truthy but every field is empty. Treat
    # "no usable fields" as no data rather than emitting a bare header the
    # agent might fabricate around.
    if not lines:
        raise_for_empty(ticker, canonical, "fundamental fields")

    # Distances worked out here, so the analyst quotes them instead of computing its own.
    derived = [
        ("Last Price vs 50 Day Average", _percent_vs(last_price, info.get("fiftyDayAverage"))),
        ("Last Price vs 200 Day Average", _percent_vs(last_price, info.get("twoHundredDayAverage"))),
        ("Last Price vs 52 Week High", _percent_vs(last_price, info.get("fiftyTwoWeekHigh"))),
        ("Last Price vs 52 Week Low", _percent_vs(last_price, info.get("fiftyTwoWeekLow"))),
    ]
    lines += [f"{label}: {value}" for label, value in derived if value is not None]

    trailing_eps, forward_eps = info.get("trailingEps"), info.get("forwardEps")
    if (
        isinstance(trailing_eps, (int, float)) and isinstance(forward_eps, (int, float))
        and forward_eps > 0 and trailing_eps > forward_eps * _TRAILING_ABOVE_FORWARD
    ):
        lines.append(
            f"Note: trailing EPS ({trailing_eps}) is well above forward EPS ({forward_eps}), so "
            "the trailing year probably includes one-time gains. Trailing P/E, net income and "
            "profit margin then overstate normal earnings; prefer forward figures for valuation."
        )

    return f"# Company Fundamentals for {canonical}\n\n" + "\n".join(lines)


def _statement(ticker, freq, as_of_date, title, quarterly_attr, annual_attr) -> str:
    """One financial statement as CSV, for a run dated today."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undated_statements(as_of_date, canonical, title)
    if withheld:
        return withheld
    what = title.lower()
    attr = quarterly_attr if freq.lower() == "quarterly" else annual_attr
    data = yf_retry(lambda: getattr(yf.Ticker(canonical), attr))
    if data is None or data.empty:
        raise_for_empty(ticker, canonical, f"{what} data")
    return f"# {title} data for {canonical} ({freq})\n" + data.to_csv()


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get balance sheet data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Balance Sheet", "quarterly_balance_sheet", "balance_sheet")


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get cash flow data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Cash Flow", "quarterly_cashflow", "cashflow")


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get income statement data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Income Statement", "quarterly_income_stmt", "income_stmt")


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date, yyyy-mm-dd"] = None,
):
    """Get insider transactions data from yfinance, for a run dated today."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undisclosed_trades(as_of_date, canonical)
    if withheld:
        return withheld
    data = yf_retry(lambda: yf.Ticker(canonical).insider_transactions)

    # Empty is normal here (many valid symbols have no insider filings),
    # so report it plainly rather than treating the symbol as invalid.
    if data is None or data.empty:
        if not vendor_reachable(YAHOO_HOST):
            raise VendorUnavailableError("Yahoo Finance is unreachable; insider filings were not retrieved")
        return f"No insider transactions reported for symbol '{canonical}'"

    # A dated run is served only the trailing window, with its dates stated, so an old
    # trade cannot be read as a recent one. A direct call with no date gets everything.
    if not as_of_date:
        return f"# Insider Transactions data for {canonical}\n" + data.to_csv()
    end = pd.Timestamp(as_of_date)
    start = end - pd.Timedelta(days=INSIDER_LOOKBACK_DAYS)
    filed = pd.to_datetime(data["Start Date"], errors="coerce")
    if getattr(filed.dt, "tz", None) is not None:
        filed = filed.dt.tz_localize(None)
    recent = data[(filed >= start) & (filed <= end + pd.Timedelta(days=1))]
    window = f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"
    omitted = len(data) - len(recent)
    if recent.empty:
        return (
            f"No insider transactions for '{canonical}' dated {window}. "
            f"{omitted} older filings were omitted."
        )
    return (
        f"# Insider Transactions data for {canonical}, trades dated {window}\n"
        f"# {len(recent)} filings shown; {omitted} older filings omitted. "
        "Read the Start Date column in full, year included.\n"
        + recent.to_csv()
    )


def get_company_profile(ticker: str) -> dict:
    """Yahoo's current profile for ``ticker``: name, sector, industry and the like."""
    return yf_retry(lambda: yf.Ticker(normalize_symbol(ticker)).info) or {}
