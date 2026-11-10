"""Market context and relative volatility from Yahoo prices (a local addition).

A stock's volatility means little without the market it trades in. When the whole market is jumpy, a
volatile stock may only be following it; when the market is calm, the same volatility is the stock's
own. This tool gives the analysts the market's state (drawdowns of the broad indexes, the small-cap fund
and the stock's sector fund, and the VIX) and how this stock moves against it: its volatility compared
with the market's, its beta with the share of its daily moves the market explains (R-squared), its
stock-specific volatility, and how much of its recent move the market accounts for, worked out against the S&P 500 and,
when the stock has one, against its sector fund (a sector-wide selloff is the market's panic in one part of it, and
the S&P 500 alone would count it as the stock's own).

Everything is worked out here from dated daily closes, so a run dated in the past is served as of that
date with no look-ahead (the sector benchmark, which comes from today's Yahoo profile, is left out of
past runs). It describes the past; it is not a forecast and not a verdict on the stock.
"""

import re
from math import sqrt
from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

MARKET_INDEXES = (("S&P 500", "^GSPC"), ("Nasdaq Composite", "^IXIC"), ("Russell 2000 (IWM)", "IWM"))
BENCHMARK = "S&P 500"
VIX_SYMBOL = "^VIX"

YEAR_SESSIONS = 252
VOL_SESSIONS = 30
MONTH_SESSIONS = 21
QUARTER_SESSIONS = 63
BETA_SESSIONS = 250
MIN_BETA_SESSIONS = 60            # fewer aligned sessions than this: no beta
ROUGH_BELOW_SESSIONS = 200        # fewer than this: say the figures are rough

PULLBACK, CORRECTION, BEAR = 0.05, 0.10, 0.20           # drawdown conventions
BETA_PRECISE_T = 2.0              # a beta smaller than this many standard errors is called imprecise
MIN_SPLIT_MOVE = 0.03             # a move smaller than this (3%) is not split into market and residual
MOSTLY_SHARE = 2 / 3              # the market-implied share of a move at or above this: mostly market-driven
RESIDUAL_WORSE_POINTS = 15.0      # 3-month residual at or below minus this
RESIDUAL_BETTER_POINTS = 10.0     # 3-month residual at or above this
IDIOSYNCRATIC_VOL_HIGH = 0.75     # stock-specific volatility, annualized
VIX_HIGH_PERCENTILE = 0.90

_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,5}(-[A-Z])?$")

_SECTOR_FUND = {
    "technology": ("Technology (XLK)", "XLK"),
    "healthcare": ("Health care (XLV)", "XLV"),
    "financial-services": ("Financials (XLF)", "XLF"),
    "consumer-cyclical": ("Consumer discretionary (XLY)", "XLY"),
    "consumer-defensive": ("Consumer staples (XLP)", "XLP"),
    "energy": ("Energy (XLE)", "XLE"),
    "industrials": ("Industrials (XLI)", "XLI"),
    "basic-materials": ("Materials (XLB)", "XLB"),
    "communication-services": ("Communication services (XLC)", "XLC"),
    "real-estate": ("Real estate (XLRE)", "XLRE"),
    "utilities": ("Utilities (XLU)", "XLU"),
}
_INDUSTRY_FUND = {
    "semiconductors": ("Semiconductors (SMH)", "SMH"),
    "semiconductor-equipment-materials": ("Semiconductors (SMH)", "SMH"),
    "biotechnology": ("Biotechnology (XBI)", "XBI"),
}


def _close_series(symbol: str, as_of: str) -> pd.Series:
    """Daily closes (split and dividend adjusted) through ``as_of``, oldest first, one per date."""
    frame = load_ohlcv(symbol, as_of, fill_gaps=False)
    dates = pd.to_datetime(frame["Date"]).dt.normalize()
    series = pd.Series(frame["Close"].to_numpy(dtype=float), index=dates).dropna()
    return series[~series.index.duplicated(keep="last")].sort_index()


def _drawdown(series: pd.Series) -> float:
    return float(series.iloc[-1] / series.tail(YEAR_SESSIONS).max() - 1)


def _label(drawdown: float) -> str:
    depth = -drawdown
    if depth >= BEAR:
        return "bear market"
    if depth >= CORRECTION:
        return "correction"
    if depth >= PULLBACK:
        return "pullback"
    return "near its 52-week high"


def _move(series: pd.Series, sessions: int) -> float | None:
    return None if len(series) <= sessions else float(series.iloc[-1] / series.iloc[-1 - sessions] - 1)


def _volatility(series: pd.Series, sessions: int = VOL_SESSIONS) -> float | None:
    returns = series.pct_change().dropna().tail(sessions)
    return None if len(returns) < sessions // 2 else float(returns.std() * sqrt(YEAR_SESSIONS))


def _regression(stock: pd.Series, market: pd.Series) -> dict | None:
    """Beta, its uncertainty, R-squared and stock-specific volatility of daily returns over the last year of
    shared sessions."""
    both = pd.concat([stock, market], axis=1, keys=["s", "m"]).dropna()
    returns = both.pct_change().dropna().tail(BETA_SESSIONS)
    sessions = len(returns)
    if sessions < MIN_BETA_SESSIONS or not returns["m"].var():
        return None
    beta = returns["s"].cov(returns["m"]) / returns["m"].var()
    residual = returns["s"] - beta * returns["m"]
    # Standard error of the slope: the residual variance over the market's total variation.
    error = sqrt(residual.var(ddof=2) / (returns["m"].var() * (sessions - 1)))
    return {"beta": float(beta), "error": float(error), "r2": float(returns["s"].corr(returns["m"]) ** 2),
            "sessions": sessions, "specific_vol": float(residual.std() * sqrt(YEAR_SESSIONS))}


def _attribution(stock: pd.Series, market: pd.Series, beta: float, sessions: int) -> dict | None:
    """The stock's move over ``sessions``, what the market's move implies through beta, and the part left over."""
    both = pd.concat([stock, market], axis=1, keys=["s", "m"]).dropna()
    own, mkt = _move(both["s"], sessions), _move(both["m"], sessions)
    if own is None or mkt is None:
        return None
    return {"stock": own, "market": mkt, "implied": beta * mkt, "residual": own - beta * mkt}


# How the same split reads against each benchmark: the S&P 500 ("the market") and the stock's sector fund.
_WORDS = {
    "market": {"the": "the market", "adjective": "market-implied", "beta": "its beta", "driven": "market-driven"},
    "sector": {"the": "the sector fund", "adjective": "sector-implied", "beta": "its beta to the sector",
               "driven": "sector-driven"},
}


def _share(part: dict) -> float | None:
    """The share of a fall that the benchmark implies (above 1 when it implies more than the whole fall), or None when
    the stock did not fall by enough to need explaining or the benchmark implies the opposite direction."""
    stock, implied = part["stock"], part["implied"]
    if stock > -MIN_SPLIT_MOVE or implied * stock <= 0:
        return None
    return implied / stock


def _explains(share: float | None) -> str:
    """A share of a fall in words for a flag: a percentage, "more than all" when the benchmark implies more than the whole
    fall, "none" when it implies none."""
    return "none" if share is None else "more than all" if share > 1 else f"{share * 100:.0f}%"


def _reading(part: dict, source: str = "market") -> str:
    """What the split says in words, worked out here so the analyst does not have to infer it (a stock's
    stock-specific *volatility* can be large while the recent *move* is mostly what the market implies).
    ``source`` is "market" (the S&P 500, the default) or "sector" (the stock's sector fund)."""
    w = _WORDS[source]
    stock, implied, residual = part["stock"], part["implied"], part["residual"]
    if abs(stock) < MIN_SPLIT_MOVE and abs(implied) < MIN_SPLIT_MOVE:
        return f"Neither the stock's move nor the {w['adjective']} move is large enough to split."
    versus = f"{_pct(implied)} against {_pct(stock)}"
    outcome = f"the stock did {abs(residual) * 100:.0f} points {'better' if residual > 0 else 'worse'} than {w['beta']} implies."
    if implied * stock <= 0:
        return f"{w['the'].capitalize()} implies the opposite direction ({versus}): {outcome}"
    share = implied / stock
    if share > 1:
        return f"{w['the'].capitalize()} implies more than the stock's whole move ({versus}): {outcome}"
    verdict = (f"mostly {w['driven']}" if share >= MOSTLY_SHARE else
               "mostly specific to the stock" if share <= 1 - MOSTLY_SHARE else "a mix of the two")
    return (f"About {share * 100:.0f}% of the stock's move is what {w['the']} implies at this beta and "
            f"{(1 - share) * 100:.0f}% is not explained by it, so the move is {verdict}.")


def _pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    if abs(value * 100) < 0.05:
        value = 0.0                      # never print "-0.0%"
    return f"{value * 100:+.1f}%" if signed and value else f"{value * 100:.1f}%"


def _times(value: float | None, other: float | None) -> str:
    return "n/a" if not value or not other else f"{value / other:.1f}x"


def _sector_benchmark(symbol: str, as_of: str) -> tuple[str, pd.Series] | None:
    """The sector (or industry) fund for the stock, from today's Yahoo profile; None when there is none."""
    try:
        info = yf_retry(lambda: yf.Ticker(symbol).info) or {}
        chosen = _INDUSTRY_FUND.get(info.get("industryKey")) or _SECTOR_FUND.get(info.get("sectorKey"))
        return (chosen[0], _close_series(chosen[1], as_of)) if chosen else None
    except (NoMarketDataError, VendorUnavailableError):
        return None


def build_report(symbol: str, stock: pd.Series, markets: dict[str, pd.Series], vix: pd.Series | None,
                 sector: tuple[str, pd.Series] | None, as_of: str) -> str:
    """The report from daily closes; the S&P 500 must be among ``markets``."""
    spx = markets[BENCHMARK]
    last_day = stock.index[-1]
    flags: list[str] = []

    table = ["| Index or fund | Off its 52-week high | 1 month | 3 months | 30-day volatility | By the common labels |",
             "|---|---|---|---|---|---|"]
    benchmarks = dict(markets)
    if sector:
        benchmarks[sector[0]] = sector[1]
    depths = {}
    for name, series in benchmarks.items():
        drawdown = _drawdown(series)
        depths[name] = drawdown
        table.append(f"| {name} | {_pct(drawdown)} | {_pct(_move(series, MONTH_SESSIONS))} | "
                     f"{_pct(_move(series, QUARTER_SESSIONS))} | {_pct(_volatility(series), False)} | "
                     f"{_label(drawdown)} |")

    vix_line = ""
    if vix is not None and len(vix) >= 60:
        window = vix.tail(YEAR_SESSIONS)
        rank = float((window < vix.iloc[-1]).mean())
        vix_line = (f"- VIX {vix.iloc[-1]:.1f}; its range over the last year is {window.min():.1f} to {window.max():.1f}, "
                    f"and it is above {rank * 100:.0f}% of that year's readings.")
        if rank >= VIX_HIGH_PERCENTILE:
            flags.append(f"Market-wide volatility is high (VIX {vix.iloc[-1]:.1f}, above {rank * 100:.0f}% of the past "
                         "year's readings): volatility in a stock that simply follows the market says little about the stock.")
    bears = [n for n, d in depths.items() if -d >= BEAR]
    if bears:
        named = "; ".join(f"{n} is {-depths[n] * 100:.1f}% below its 52-week high" for n in bears)
        flags.insert(0, f"{named}, a bear market by the 20% convention; the S&P 500 is {-depths[BENCHMARK] * 100:.1f}% "
                        f"below its high ({_label(depths[BENCHMARK])}).")

    stock_lines = []
    own_vol, market_vol = _volatility(stock), _volatility(spx)
    if own_vol is not None:
        line = (f"- 30-day realized volatility, annualized: {own_vol * 100:.0f}%, {_times(own_vol, market_vol)} the "
                f"S&P 500's {_pct(market_vol, False)}")
        if sector:
            line += f" and {_times(own_vol, _volatility(sector[1]))} the sector fund's {_pct(_volatility(sector[1]), False)}"
        stock_lines.append(line + ".")
    fit = _regression(stock, spx)
    sessions_available = max(len(pd.concat([stock, spx], axis=1).dropna()) - 1, 0)    # daily returns they share
    if fit is None:
        stock_lines.append(f"- Beta cannot be worked out: only {sessions_available} sessions are shared with the S&P 500 "
                           f"(at least {MIN_BETA_SESSIONS} are needed).")
    else:
        precise = fit["error"] > 0 and abs(fit["beta"]) / fit["error"] >= BETA_PRECISE_T
        stock_lines.append(f"- Beta to the S&P 500 over the last {fit['sessions']} sessions: {fit['beta']:.2f} (uncertainty "
                           f"about ±{fit['error']:.2f}). The market explains {fit['r2'] * 100:.0f}% of the variation in the "
                           f"stock's daily moves (R-squared {fit['r2']:.2f}).")
        stock_lines.append(f"- Stock-specific volatility, the part the market does not explain: {fit['specific_vol'] * 100:.0f}% "
                           "a year.")
        if fit["sessions"] < ROUGH_BELOW_SESSIONS:
            flags.append(f"Only {fit['sessions']} sessions of shared history: the beta and volatility figures are rough.")
        if not precise:
            flags.append(f"The beta of {fit['beta']:.2f} is imprecise (uncertainty about ±{fit['error']:.2f}), so the "
                         "market-implied moves above are rough and no residual is flagged.")
        sector_fit = _regression(stock, sector[1]) if sector else None
        sector_parts = {}
        if sector_fit:
            sector_parts = {s: p for s in (QUARTER_SESSIONS, MONTH_SESSIONS)
                            if (p := _attribution(stock, sector[1], sector_fit["beta"], s)) is not None}
        for label, sessions in (("quarter", QUARTER_SESSIONS), ("month", MONTH_SESSIONS)):
            part = _attribution(stock, spx, fit["beta"], sessions)
            if part is None:
                continue
            stock_lines.append(
                f"- Last {sessions} sessions: the stock moved {_pct(part['stock'])} while the S&P 500 moved "
                f"{_pct(part['market'])}. That market move implies {_pct(part['implied'])} at this beta, so "
                f"{part['residual'] * 100:+.1f} points of the stock's move are not explained by the market. "
                f"Reading: {_reading(part)}")
            if label == "quarter" and precise:
                points = part["residual"] * 100
                sector_share = _share(sector_parts[sessions]) if sessions in sector_parts else None
                if points <= -RESIDUAL_WORSE_POINTS and sector_share is not None and sector_share >= MOSTLY_SHARE:
                    flags.append(f"The stock fell {abs(points):.0f} points more than the S&P 500 implies over {sessions} "
                                 f"sessions ({_pct(part['stock'])} against {_pct(part['implied'])} implied), but the "
                                 f"sector fund {sector[0]} explains {_explains(sector_share)} of that fall: it is mostly a sector move, "
                                 "not weakness specific to the stock.")
                elif points <= -RESIDUAL_WORSE_POINTS:
                    flags.append(f"The stock fell {abs(points):.0f} points more than the market implies over {sessions} "
                                 f"sessions ({_pct(part['stock'])} against {_pct(part['implied'])} implied): the fall is "
                                 "mostly specific to the stock.")
                elif points >= RESIDUAL_BETTER_POINTS:
                    flags.append(f"The stock has held up {points:.0f} points better than the market implies over "
                                 f"{sessions} sessions ({_pct(part['stock'])} against {_pct(part['implied'])} implied).")
        if sector_fit and sector_parts:
            stock_lines.append(
                f"- Beta to the sector fund ({sector[0]}) over the last {sector_fit['sessions']} sessions: "
                f"{sector_fit['beta']:.2f} (uncertainty about ±{sector_fit['error']:.2f}). The sector fund explains "
                f"{sector_fit['r2'] * 100:.0f}% of the variation in the stock's daily moves (R-squared "
                f"{sector_fit['r2']:.2f}), against {fit['r2'] * 100:.0f}% for the S&P 500.")
            for sessions in (QUARTER_SESSIONS, MONTH_SESSIONS):
                part = sector_parts.get(sessions)
                if part is None:
                    continue
                stock_lines.append(
                    f"- Against the sector fund, last {sessions} sessions: the stock moved {_pct(part['stock'])} while "
                    f"{sector[0]} moved {_pct(part['market'])}. That sector move implies {_pct(part['implied'])} at the "
                    f"stock's beta to the sector, so {part['residual'] * 100:+.1f} points of the stock's move are not "
                    f"explained by the sector. Sector reading: {_reading(part, 'sector')}")
                own = _attribution(stock, spx, fit["beta"], sessions)
                sector_share, market_share = _share(part), _share(own) if own else None
                if sector_share is not None and sector_share >= MOSTLY_SHARE and (market_share or 0) < MOSTLY_SHARE:
                    flags.append(f"The sector fund {sector[0]} explains {_explains(sector_share)} of the stock's {sessions}-session "
                                 f"fall ({_pct(part['stock'])}) where the S&P 500 explains {_explains(market_share)}: the part of the "
                                 "fall the S&P 500 does not explain is largely a sector-wide move, a shared fall and not "
                                 "weakness specific to the stock.")
        if fit["specific_vol"] >= IDIOSYNCRATIC_VOL_HIGH:
            flags.append(f"Stock-specific volatility is {fit['specific_vol'] * 100:.0f}% a year, {IDIOSYNCRATIC_VOL_HIGH * 100:.0f}% "
                         "or more: very high whatever the market is doing.")
    relative = []
    for label, series in [("S&P 500", spx)] + ([(sector[0], sector[1])] if sector else []):
        both = pd.concat([stock, series], axis=1, keys=["s", "b"]).dropna()
        for window, sessions in (("1 month", MONTH_SESSIONS), ("3 months", QUARTER_SESSIONS)):
            own, other = _move(both["s"], sessions), _move(both["b"], sessions)
            if own is not None and other is not None:
                relative.append(f"{window} {(own - other) * 100:+.1f} points against {label}")
    if relative:
        stock_lines.append("- Relative strength: " + "; ".join(relative) + ".")
    stock_lines.append(f"- The stock is {_pct(_drawdown(stock))} from its 52-week high, against {_pct(depths[BENCHMARK])} for "
                       "the S&P 500" + (f" and {_pct(_drawdown(sector[1]))} for the sector fund." if sector else "."))

    notes = ["Beta is how many percent the stock has tended to move for a 1% move in the S&P 500, measured here on daily "
             "returns over the last year (the beta on Yahoo's company profile is usually a five-year monthly figure, so "
             "the two differ); R-squared is the share of the variation in its daily moves the market explains; the "
             "residual is the part of a move the market does not explain. How much of the stock's volatility is its own "
             "and what drove its recent move are different questions: the Reading lines answer the second. All of it "
             "describes the past from price history; none of it is a forecast."]
    if not sector:
        notes.append("No sector benchmark is shown (a run dated in the past, or Yahoo gives the stock no sector).")
    elif any("Against the sector fund" in line for line in stock_lines):
        notes.append("The sector lines work the same way against the sector fund. A sector fund holds the stock itself, so "
                     "it tracks the stock more closely than the S&P 500 does, and a sector can fall on news that hurts "
                     "every company in it: a sector reading shows that a fall was shared, not that it was harmless.")
    return (f"# Market context for {symbol} (Yahoo daily closes through {last_day:%Y-%m-%d}, analysis date {as_of})\n\n"
            "## The market\n" + "\n".join(table) + ("\n" + vix_line if vix_line else "") + "\n\n"
            f"## {symbol} against the market\n" + "\n".join(stock_lines) + "\n\n"
            "## Flags to state in the report\n" + ("\n".join(f"- {f}" for f in flags) if flags else "No rule-based flags.")
            + "\n\n" + " ".join(notes))


def get_market_context(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """The market's state and how ``ticker`` moves against it, from Yahoo prices as of ``as_of_date``."""
    canonical = normalize_symbol(ticker)
    if not isinstance(canonical, str) or not _US_SYMBOL.match(canonical):
        raise NoMarketDataError(ticker, canonical, "market context compares US-listed symbols with US indexes")
    as_of = as_of_date or get_current_date()
    stock = _close_series(canonical, as_of)
    markets = {}
    for name, symbol in MARKET_INDEXES:
        try:
            markets[name] = _close_series(symbol, as_of)
        except (NoMarketDataError, VendorUnavailableError):
            continue
    if BENCHMARK not in markets:
        raise VendorUnavailableError("the S&P 500 price history could not be retrieved")
    try:
        vix = _close_series(VIX_SYMBOL, as_of)
    except (NoMarketDataError, VendorUnavailableError):
        vix = None
    sector = None if is_historical(as_of_date) else _sector_benchmark(canonical, as_of)
    return build_report(canonical, stock, markets, vix, sector, as_of)

# PUBLIC NAMES FOR THE PICKER (a local addition): the stock picker's quick look uses the very same arithmetic as the
# analysis's market-context section, so what it says the market explains and what the analysis says cannot differ.
regression = _regression
attribution = _attribution
drawdown = _drawdown
move = _move
sector_funds = _SECTOR_FUND
industry_funds = _INDUSTRY_FUND
