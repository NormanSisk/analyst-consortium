"""The data tools the analysts call.

Each dated tool takes the run's ``trade_date`` from graph state (``InjectedState``)
and never serves data past it, whatever date the model asks for.
"""

from typing import Annotated, Literal

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tradingagents.dataflows.date_window import as_of, as_of_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.router import no_data_available, route_to_vendor, vendor_unavailable
from tradingagents.dataflows.vendors.yahoo.snapshot import build_verified_market_snapshot


@tool
def get_stock_data(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve stock price data (OHLCV) for the instrument under analysis.
    Uses the configured core_stock_apis vendor.
    Args:
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the price data for the instrument over the date range.
    """
    try:
        start_date, end_date = as_of_window(start_date, end_date, trade_date)
    except ValueError as e:
        return str(e)
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)


@tool
def get_indicators(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve a single technical indicator for the instrument under analysis.
    Uses the configured technical_indicators vendor.
    Args:
        indicator (str): A single technical indicator name, e.g. 'rsi', 'macd'. Call this tool once per indicator.
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the instrument and indicator.
    """
    # LLMs sometimes pass multiple indicators as a comma-separated string;
    # split and process each individually.
    curr_date = as_of(curr_date, trade_date)
    indicators = [i.strip().lower() for i in indicator.split(",") if i.strip()]
    results = []
    for ind in indicators:
        try:
            results.append(route_to_vendor("get_indicators", symbol, ind, curr_date, look_back_days))
        except ValueError as e:
            results.append(str(e))
    return "\n\n".join(results)


@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[
        int, "number of recent trading rows to include for sanity-checking"
    ] = 30,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest OHLCV row on or before curr_date, common technical
    indicators, and recent closes. Call this before making exact claims about
    price levels, Bollinger bands, RSI, MACD, moving averages, support /
    resistance, or historical comparisons, and treat it as the source of truth.
    """
    # An exception out of a tool would end the run.
    try:
        return build_verified_market_snapshot(symbol, as_of(curr_date, trade_date), look_back_days)
    except VendorUnavailableError as exc:
        return vendor_unavailable("get_verified_market_snapshot", exc)
    except NoMarketDataError as exc:
        return no_data_available(exc)


@tool
def get_fundamentals(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve comprehensive fundamental data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    return route_to_vendor("get_fundamentals", ticker, as_of(curr_date, trade_date))


@tool
def get_balance_sheet(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    freq: Annotated[Literal["annual", "quarterly"], "reporting frequency"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve balance sheet data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing balance sheet data
    """
    return route_to_vendor("get_balance_sheet", ticker, freq, as_of(curr_date, trade_date))


@tool
def get_cashflow(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    freq: Annotated[Literal["annual", "quarterly"], "reporting frequency"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve cash flow statement data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing cash flow statement data
    """
    return route_to_vendor("get_cashflow", ticker, freq, as_of(curr_date, trade_date))


@tool
def get_income_statement(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    freq: Annotated[Literal["annual", "quarterly"], "reporting frequency"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve income statement data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing income statement data
    """
    return route_to_vendor("get_income_statement", ticker, freq, as_of(curr_date, trade_date))


@tool
def get_earnings_facts(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve earnings history and analyst expectations for the instrument under analysis:
    reported versus estimated EPS for recent quarters (beat or miss, with the surprise
    percent), the next scheduled report, sell-side price targets and ratings, the consensus EPS
    and revenue estimates by period with how they have moved over 30 and 90 days and how many
    analysts raised or cut them, and the recent analyst actions (target changes, upgrades, downgrades).
    Uses the configured fundamental_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report of earnings results and analyst expectations
    """
    return route_to_vendor("get_earnings_facts", ticker, as_of(curr_date, trade_date))


@tool
def get_trend_facts(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve the last five quarters of the instrument's revenue, operating income and margin,
    cash flow, capital spending, share count, debt and buybacks, with each trend worked out two
    ways (against the same quarter a year earlier and against the quarters just before it) and
    rule-based flags for what deserves a sentence. Uses the configured fundamental_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A table of five quarters, both views of the latest quarter, and flags
    """
    return route_to_vendor("get_trend_facts", ticker, as_of(curr_date, trade_date))


@tool
def get_valuation_context(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve yardsticks for whether the instrument's price is high or low: its trailing P/E
    against its own readings over the last ten years, a table of similar companies in the same
    industry, the earnings yield against the 10-year Treasury yield with the steady growth the
    price implies, and reference price levels worked out from those multiples. These are
    present-day figures and arithmetic, not forecasts. Uses the configured fundamental_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A report of valuation yardsticks with the figures already worked out
    """
    return route_to_vendor("get_valuation_context", ticker, as_of(curr_date, trade_date))


@tool
def get_ownership_and_short_interest(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve how heavily the instrument is sold short and who owns it: shares short with the
    report date, the change from the report before, days to cover, the percentages worked out
    against shares outstanding, the totals held by institutions and insiders, and the largest
    institutional and fund holders with how each changed its position in its latest filing.
    These are filings with a lag, stated beside each figure, not today's positions. Uses the
    configured fundamental_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: Short-interest and ownership figures with rule-based flags
    """
    return route_to_vendor("get_ownership_and_short_interest", ticker, as_of(curr_date, trade_date))


@tool
def get_market_statistics(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve how much the instrument moves and how it trades: 30-day realized volatility, the 13-, 26- and
    52-week ranges with where the last close sits in each, year to date, and the latest session's dollar
    volume against its 90-day average and median (all as of the analysis date); and, for a run dated today, the
    options-implied volatility for 30 days with its ratio to realized, a proxy percentile, the
    one-standard-deviation ranges and the straddle-implied move (arithmetic, not a forecast), the term
    structure, put/call ratios and skew, with rule-based flags. Uses the configured market_statistics vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: Volatility, range and volume statistics with flags
    """
    return route_to_vendor("get_market_statistics", ticker, as_of(curr_date, trade_date))


@tool
def get_market_context(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve the state of the market and how the instrument moves against it: the S&P 500, Nasdaq,
    small-cap and sector-fund drawdowns from their 52-week highs with the common labels (pullback,
    correction, bear market), the VIX against its past year, and for the instrument its volatility
    compared with the market's, its beta with R-squared, its stock-specific volatility, how much of its
    last quarter's move the market explains, relative strength and drawdown, with rule-based flags.
    Worked out from dated daily closes, so a past date is served as of that date. Uses the configured
    market_context vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: Market state and relative-volatility figures with flags
    """
    return route_to_vendor("get_market_context", ticker, as_of(curr_date, trade_date))


@tool
def get_filing_events(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve what the company has filed with the SEC that its financial statements do not show yet, as filed
    by the analysis date: Form 8-K events by item for the last 180 days (delisting notices, auditor changes,
    sales of unregistered equity, material agreements, officer changes, shareholder votes), registration and
    prospectus filings, late-filing notices, amended periodic reports, Schedule 13D and 13G holders of 5% or
    more filed in the last year, and the cover-page share count with its change over a year, with
    rule-based flags. Uses the configured filing_events vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: Filing events with flags
    """
    return route_to_vendor("get_filing_events", ticker, as_of(curr_date, trade_date))


@tool
def get_public_records(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve public government records about the company: federal contract awards in the last 12 months
    (USAspending, with the largest listed), and for health-care companies the studies it sponsors that are
    recruiting by phase (ClinicalTrials.gov) and FDA drug recalls naming it (openFDA). Matching is by the
    company's registered name, so absence is not absence of a business. Uses the configured public_records
    vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: Awards, studies and recalls with flags
    """
    return route_to_vendor("get_public_records", ticker, as_of(curr_date, trade_date))


@tool
def get_macro_backdrop(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve a lean macro backdrop from FRED as known on the analysis date: the high-yield bond spread and
    the Chicago Fed financial conditions index with their changes over a month and a quarter and their place
    in the past year, the dates of the next CPI, jobs, GDP, PCE and retail-sales releases, and for a
    Japanese company the yen, or for an energy or utilities company crude oil and natural gas, with
    rule-based flags. It is backdrop for the whole market, not evidence about one company. Uses the
    configured macro_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: Credit, conditions and release dates with flags
    """
    return route_to_vendor("get_macro_backdrop", ticker, as_of(curr_date, trade_date))


@tool
def get_news(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve news data for the instrument under analysis.
    Uses the configured news_data vendor.
    Args:
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    try:
        start_date, end_date = as_of_window(start_date, end_date, trade_date)
    except ValueError as e:
        return str(e)
    return route_to_vendor("get_news", ticker, start_date, end_date)


@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit to use the configured default"] = None,
    limit: Annotated[int | None, "Max articles to return; omit to use the configured default"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor. Defaults for look_back_days and
    limit come from DEFAULT_CONFIG (global_news_lookback_days,
    global_news_article_limit); pass explicit values to override.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back; omit to inherit config
        limit (int): Maximum number of articles to return; omit to inherit config

    Returns:
        str: A formatted string containing global news data
    """
    return route_to_vendor("get_global_news", as_of(curr_date, trade_date), look_back_days, limit)


@tool
def get_insider_transactions(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor.
    Returns:
        str: A report of insider transaction data
    """
    return route_to_vendor("get_insider_transactions", ticker, trade_date or None)


@tool
def get_congress_trades(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve trades in the instrument made by members of the U.S. Congress, as they
    disclosed them over the last 180 days: who, buy or sell, the amount range, the trade
    date, the filing date and how many days late it was filed. Disclosures arrive up to
    45 days after a trade, so this is slow background context. Uses the configured
    congress_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A report of recent Congress trades in the instrument
    """
    return route_to_vendor("get_congress_trades", ticker, as_of(curr_date, trade_date))


@tool
def get_company_statements(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve what the company itself said about its latest quarter: the earnings call
    transcript (prepared remarks and the analysts' questions), the earnings release with
    its guidance, and the management's discussion and analysis from the latest 10-Q or 10-K.
    These are the company's claims, not verified facts. Uses the configured
    company_statements vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: The three documents, trimmed, each under its own heading
    """
    return route_to_vendor("get_company_statements", ticker, as_of(curr_date, trade_date))


@tool
def get_macro_indicators(
    indicator: Annotated[
        str,
        "Macro indicator: a friendly alias such as 'cpi', 'core_pce', "
        "'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve', "
        "'real_gdp', 'vix', euro-area aliases such as 'ecb_deposit_rate', "
        "'euro_hicp', 'euro_core_hicp', 'euro_real_gdp', 'germany_10y', "
        "'france_10y', 'eur_usd', or a raw FRED series ID such as 'CPIAUCSL'.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 1-year window"
    ] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve a macroeconomic indicator time series from FRED (Federal Reserve
    Economic Data): policy rates, Treasury yields, inflation, labor, and growth,
    for the US and, through FRED's mirrored series, the euro area.
    Returns the series title, units, frequency, the latest value, the change
    over the window, and a recent observation table. Uses the configured
    macro_data vendor.

    Args:
        indicator (str): Friendly alias or raw FRED series ID
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window length; omit for a 1-year window

    Returns:
        str: A formatted markdown report of the macro series
    """
    return route_to_vendor("get_macro_indicators", indicator, as_of(curr_date, trade_date), look_back_days)


@tool
def get_prediction_markets(
    topic: Annotated[
        str,
        "Event topic/keyword, e.g. 'Fed rate cut', 'recession 2026', "
        "'US election', or a sector/company event.",
    ],
    limit: Annotated[int | None, "Max markets to return; omit for a default of 6"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve live, market-implied probabilities for forward-looking events from
    prediction markets (Polymarket): Fed decisions, recession, elections,
    geopolitics, crypto. Returns the most-traded open markets matching the
    topic, each with its implied probability, traded volume, resolution date,
    and recent move. Uses the configured prediction_markets vendor.

    Args:
        topic (str): Event keyword(s) to search
        limit (int): Max markets to return; omit for a default of 6

    Returns:
        str: A formatted markdown report of matching prediction markets
    """
    return route_to_vendor("get_prediction_markets", topic, limit, trade_date or None)
