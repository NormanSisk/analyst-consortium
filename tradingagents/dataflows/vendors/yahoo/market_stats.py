"""Volatility, options positioning, volume and price range from Yahoo (a local addition).

Replaces the Interactive Brokers statistics, which needed a browser sign-in that lapses. Two kinds of figure:

* From dated daily prices, so a run dated in the past is served as of that date: 30-day realized volatility, the
  13-, 26- and 52-week ranges, year to date, and the latest session's dollar volume against its 90-day average and median.
* From Yahoo's option chains, live only and so withheld from past runs: at-the-money implied volatility for a
  constant 30 days (interpolated in variance between the expiries around it), its term structure, put/call ratios,
  skew, and what the at-the-money straddle implies through the nearest expiry (and through the next earnings
  report when one falls inside it).

Yahoo has no history of implied volatility, so IBKR's implied-volatility percentile is replaced by a proxy that is
labelled as one: where today's implied volatility sits among the past year's 30-day realized readings.
"""

from datetime import timedelta
from math import sqrt
from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

YEAR_SESSIONS = 252
TARGET_DAYS = 30
MAX_EXPIRIES = 6
MAX_EXPIRY_DAYS = 120
FLOW_DAYS = 60                        # expiries up to this many days out count toward put/call ratios
MIN_QUOTED_OPTIONS = 2

IV_HIGH = 1.0
IV_OVER_HV = 1.5
PROXY_EDGE = 0.9
NEAR_EXTREME = 0.05
VOLUME_RATIO_HIGH, VOLUME_RATIO_LOW = 3.0, 0.2
MEAN_OVER_MEDIAN = 3.0                # a 90-day average dollar volume this many times the median is dominated by a few sessions
INVERTED_TERM = 1.25                  # front-month implied volatility this many times the back month's
PUT_CALL_HIGH = 2.0                   # open-interest put/call ratio
SKEW_RICH_PUTS = 0.15                 # out-of-the-money put IV above call IV by this much (15 volatility points)
FLOW_MIN_CONTRACTS = 200              # ignore put/call ratios built on fewer contracts


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _pct(value, digits=1, signed=False):
    if value is None:
        return "n/a"
    return f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"


def _times(value: float) -> str:
    return f"{value:.1f}" if value >= 0.095 else f"{value:.2f}"


# --- option chains -------------------------------------------------------------------------------------------

def _quoted(frame: pd.DataFrame) -> pd.DataFrame:
    """Contracts with a two-sided quote and a sane implied volatility; Yahoo's figure for the rest is noise."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    bid, ask, iv = (pd.to_numeric(frame.get(c), errors="coerce") for c in ("bid", "ask", "impliedVolatility"))
    keep = (bid > 0) & (ask >= bid) & (iv > 0.01) & (iv < 10)
    return frame[keep.fillna(False)].copy()


def _nearest(frame: pd.DataFrame, strike: float, tolerance: float) -> pd.Series | None:
    """The quoted contract whose strike is closest to ``strike``, within ``tolerance`` of it (a fraction)."""
    if frame.empty:
        return None
    distance = (frame["strike"] - strike).abs()
    best = distance.idxmin()
    return frame.loc[best] if distance[best] <= strike * tolerance else None


def _mid(row) -> float:
    return (float(row["bid"]) + float(row["ask"])) / 2


def expiry_stats(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> dict | None:
    """At-the-money implied volatility and straddle, and the skew, for one expiry; None without usable quotes."""
    quoted_calls, quoted_puts = _quoted(calls), _quoted(puts)
    if len(quoted_calls) + len(quoted_puts) < MIN_QUOTED_OPTIONS:
        return None
    call, put = _nearest(quoted_calls, spot, 0.07), _nearest(quoted_puts, spot, 0.07)
    if call is None and put is None:
        return None
    ivs = [float(row["impliedVolatility"]) for row in (call, put) if row is not None]
    straddle = None
    if call is not None and put is not None and call["strike"] == put["strike"]:
        straddle = (_mid(call) + _mid(put)) / spot
    wing_put, wing_call = _nearest(quoted_puts, spot * 0.9, 0.04), _nearest(quoted_calls, spot * 1.1, 0.04)
    count = lambda frame, column: float(pd.to_numeric(frame.get(column), errors="coerce").fillna(0).sum()) if frame is not None and not frame.empty else 0.0  # noqa: E731
    return {"iv": sum(ivs) / len(ivs), "straddle": straddle,
            "put_wing_iv": None if wing_put is None else float(wing_put["impliedVolatility"]),
            "call_wing_iv": None if wing_call is None else float(wing_call["impliedVolatility"]),
            "oi_calls": count(calls, "openInterest"), "oi_puts": count(puts, "openInterest"),
            "vol_calls": count(calls, "volume"), "vol_puts": count(puts, "volume")}


def constant_maturity_iv(points: list[tuple[int, float]], target_days: int = TARGET_DAYS) -> tuple[float, str] | None:
    """Implied volatility for ``target_days``, interpolated linearly in total variance between the two expiries around it.

    ``points`` are (days to expiry, implied volatility). With expiries on one side only, the nearest is used and
    the note says so.
    """
    points = sorted(p for p in points if p[0] > 0 and p[1] > 0)
    if not points:
        return None
    before = [p for p in points if p[0] <= target_days]
    after = [p for p in points if p[0] >= target_days]
    if before and after and before[-1][0] != after[0][0]:
        (d1, v1), (d2, v2) = before[-1], after[0]
        t1, t2, target = d1 / 365, d2 / 365, target_days / 365
        weight = (target - t1) / (t2 - t1)
        variance = (1 - weight) * v1 ** 2 * t1 + weight * v2 ** 2 * t2
        return sqrt(variance / target), f"interpolated between the {d1}- and {d2}-day expiries"
    day, vol = min(points, key=lambda p: abs(p[0] - target_days))
    return vol, f"nearest expiry ({day} days)"


def _option_summary(symbol: str, spot: float, as_of: pd.Timestamp) -> dict:
    """Per-expiry statistics for the first expiries within ``MAX_EXPIRY_DAYS``; ``{'expiries': []}`` without options."""
    ticker_expiries = yf_retry(lambda: list(yf.Ticker(symbol).options)) or []
    rows = []
    for expiry in ticker_expiries:
        days = (pd.Timestamp(expiry) - as_of).days
        if days < 3 or days > MAX_EXPIRY_DAYS:
            continue
        if len(rows) >= MAX_EXPIRIES:
            break
        chain = yf_retry(lambda e=expiry: yf.Ticker(symbol).option_chain(e))
        stats = None if chain is None else expiry_stats(chain.calls, chain.puts, spot)
        if stats:
            rows.append({"date": expiry, "days": days, **stats})
    return {"expiries": rows}


def _next_earnings(symbol: str, as_of: pd.Timestamp) -> pd.Timestamp | None:
    try:
        calendar = yf_retry(lambda: yf.Ticker(symbol).calendar)
        dates = (calendar or {}).get("Earnings Date") or []
        upcoming = sorted(pd.Timestamp(d) for d in dates if pd.Timestamp(d) >= as_of)
        return upcoming[0] if upcoming else None
    except (VendorUnavailableError, TypeError, ValueError, AttributeError):
        return None


# --- price-based figures -------------------------------------------------------------------------------------

def _realized(close: pd.Series, sessions: int = 30) -> float | None:
    returns = close.pct_change().dropna().tail(sessions)
    return None if len(returns) < sessions // 2 else float(returns.std() * sqrt(YEAR_SESSIONS))


def _realized_history(close: pd.Series) -> pd.Series:
    return (close.pct_change().rolling(30).std() * sqrt(YEAR_SESSIONS)).dropna().tail(YEAR_SESSIONS)


def price_lines(prices: pd.DataFrame) -> tuple[list[str], list[str], float]:
    """Range, year-to-date and volume lines with their flags, from daily bars (Date, High, Low, Close, Volume)."""
    close = prices["Close"].astype(float)
    last = float(close.iloc[-1])
    lines, flags = [], []
    stamp = pd.Timestamp(prices["Date"].iloc[-1])
    change = close.iloc[-1] / close.iloc[-2] - 1 if len(close) > 1 else None
    lines.append(f"- Last close {last:g} on {stamp:%Y-%m-%d}" + ("" if change is None else f", {_pct(change, 1, True)} on the day") + ".")
    for label, sessions in (("13-week", 63), ("26-week", 126), ("52-week", 252)):
        window = prices.tail(sessions)
        high, low = float(window["High"].max()), float(window["Low"].min())
        lines.append(f"- {label} range {low:g} to {high:g}: the last close is {(1 - last / high) * 100:.0f}% below the high "
                     f"and {(last / low - 1) * 100:.0f}% above the low.")
        if sessions == 252:
            if last >= high * (1 - NEAR_EXTREME):
                flags.append(f"The last close is within {NEAR_EXTREME * 100:.0f}% of its 52-week high ({high:g}).")
            if last <= low * (1 + NEAR_EXTREME):
                flags.append(f"The last close is within {NEAR_EXTREME * 100:.0f}% of its 52-week low ({low:g}).")
    if len(close) > 252:
        lines.append(f"- A year ago (252 sessions) the close was {close.iloc[-253]:g}: {_pct(last / close.iloc[-253] - 1, 1, True)} since.")
    this_year = close[pd.to_datetime(prices["Date"]).dt.year.to_numpy() == stamp.year]
    if len(this_year) > 1 and pd.Timestamp(prices["Date"].iloc[len(close) - len(this_year)]).month <= 2:
        lines.append(f"- Year to date: {_pct(last / this_year.iloc[0] - 1, 1, True)} (from the first close of {stamp.year}).")
    dollars = (prices["Close"].astype(float) * prices["Volume"].astype(float))
    recent = prices[pd.to_datetime(prices["Date"]) > stamp - timedelta(days=90)]
    recent_dollars = recent["Close"].astype(float) * recent["Volume"].astype(float)
    average = float(recent_dollars.mean()) if len(recent) > 20 else None
    if average:
        latest = float(dollars.iloc[-1])
        median = float(recent_dollars.median())
        # one huge session can lift the average far above a normal day; then the median is the fair yardstick
        dominated = median <= 0 or average >= MEAN_OVER_MEDIAN * median
        ratio = latest / average
        median_ratio = latest / median if median > 0 else None
        lines.append(f"- 90-day dollar volume a day: average ${average:,.0f}, median ${median:,.0f}; the latest session traded "
                     f"{prices['Volume'].iloc[-1]:,.0f} shares, about ${latest:,.0f}: {_times(ratio)} times the average"
                     + (f" and {_times(median_ratio)} times the median." if median_ratio is not None else "."))
        if dominated:
            flags.append(f"A few very large sessions dominate the 90-day average dollar volume: the average is ${average:,.0f} a day "
                         f"but the median is ${median:,.0f}. Judge the latest session against the median"
                         + (f", where it is {_times(median_ratio)} times." if median_ratio is not None else "."))
        basis, reference = ("median", median_ratio) if dominated and median_ratio is not None else ("average", ratio)
        if reference >= VOLUME_RATIO_HIGH or reference <= VOLUME_RATIO_LOW:
            flags.append(f"The latest session's dollar volume is {_times(reference)} times the 90-day {basis}"
                         + ("; a few very large sessions can dominate that average." if basis == "average" else "."))
    return lines, flags, last


# --- the report ----------------------------------------------------------------------------------------------

def build_report(symbol: str, prices: pd.DataFrame, options: dict | None, earnings: pd.Timestamp | None, as_of: str,
                 options_note: str = "") -> str:
    """The report; ``options`` is None when the option figures are withheld (a past run) or unavailable."""
    price_part, flags, last = price_lines(prices)
    close = prices["Close"].astype(float)
    realized = _realized(close)
    lines = []
    if realized is not None:
        lines.append(f"- Realized volatility over the last 30 sessions, annualized: {realized * 100:.0f}%.")
    option_lines: list[str] = []
    rows = (options or {}).get("expiries") or []
    cmi = constant_maturity_iv([(r["days"], r["iv"]) for r in rows])
    if options is None:
        option_lines.append(f"- Option-based figures are not served: {options_note}")
    elif not rows or cmi is None:
        option_lines.append("- No usable listed-option quotes were found for this stock, so no implied volatility, "
                            "put/call or skew figures are available.")
    else:
        iv, how = cmi
        option_lines.append(f"- Implied volatility for 30 days, at the money: {iv * 100:.0f}% ({how}).")
        if realized:
            ratio = iv / realized
            option_lines.append(f"- That is {_times(ratio)} times realized volatility.")
            if ratio >= IV_OVER_HV:
                flags.append(f"Implied volatility is {_times(ratio)} times realized volatility: the options market prices more "
                             "movement than the last 30 sessions showed.")
            elif ratio <= 1 / IV_OVER_HV:
                flags.append(f"Implied volatility is only {_times(ratio)} times realized volatility: the options market prices "
                             "less movement than the last 30 sessions showed.")
        history = _realized_history(close)
        if len(history) >= 120:
            rank = float((history < iv).mean())
            option_lines.append(f"- Proxy for an implied-volatility percentile (Yahoo has no implied-volatility history): today's "
                                f"implied volatility is above {rank * 100:.0f}% of the past year's 30-session realized readings.")
            if rank >= PROXY_EDGE:
                flags.append(f"Implied volatility is above {rank * 100:.0f}% of the past year's realized-volatility readings: high "
                             "against the stock's own recent range (a proxy for an implied-volatility percentile).")
            elif rank <= 1 - PROXY_EDGE:
                flags.append(f"Implied volatility is above only {rank * 100:.0f}% of the past year's realized-volatility readings: low "
                             "against the stock's own recent range (a proxy).")
        week, month = iv * sqrt(5 / 252), iv * sqrt(21 / 252)
        beyond = (" A figure above 100% is only the arithmetic of very high volatility; a stock cannot fall by more than 100%."
                  if max(week, month) >= 1 else "")
        option_lines.append(f"- One standard deviation of that implied volatility, as arithmetic and not a forecast, a target or a "
                            f"limit: about ±{week * 100:.0f}% over 5 trading days and ±{month * 100:.0f}% over 21.{beyond}")
        if iv >= IV_HIGH:
            flags.append(f"Implied volatility is {iv * 100:.0f}% a year, {IV_HIGH * 100:.0f}% or more: the options market prices very "
                         "large moves, and a level this high can come from a thin options market.")
        if len(rows) >= 2:
            front, back = rows[0], rows[-1]
            option_lines.append(f"- Term structure: {front['days']}-day expiry {front['iv'] * 100:.0f}%, {back['days']}-day expiry "
                                f"{back['iv'] * 100:.0f}%.")
            if front["iv"] >= INVERTED_TERM * back["iv"]:
                flags.append(f"Near-term implied volatility ({front['iv'] * 100:.0f}%, {front['days']} days) is well above the "
                             f"{back['days']}-day figure ({back['iv'] * 100:.0f}%): the options market is pricing a near-term event "
                             "or stress.")
        first = rows[0]
        if first.get("straddle"):
            through = f"through {first['date']} ({first['days']} days)"
            if earnings is not None and pd.Timestamp(first["date"]) >= earnings:
                through += f", which includes the earnings report expected {earnings:%Y-%m-%d}"
            option_lines.append(f"- The at-the-money straddle implies a move of about ±{first['straddle'] * 100:.0f}% {through}.")
        near = [r for r in rows if r["days"] <= FLOW_DAYS] or rows[:1]
        calls_oi, puts_oi = sum(r["oi_calls"] for r in near), sum(r["oi_puts"] for r in near)
        calls_v, puts_v = sum(r["vol_calls"] for r in near), sum(r["vol_puts"] for r in near)
        if calls_oi + puts_oi >= FLOW_MIN_CONTRACTS and calls_oi > 0:
            option_lines.append(f"- Put/call ratio, expiries up to {FLOW_DAYS} days: open interest {puts_oi / calls_oi:.2f} "
                                f"({puts_oi:,.0f} puts, {calls_oi:,.0f} calls)" + (f"; today's volume {puts_v / calls_v:.2f}"
                                if calls_v > 0 and calls_v + puts_v >= FLOW_MIN_CONTRACTS else "") + ".")
            if puts_oi / calls_oi >= PUT_CALL_HIGH:
                flags.append(f"Open-interest put/call ratio is {puts_oi / calls_oi:.1f}: puts far outnumber calls, which can reflect "
                             "hedging as well as bearish positioning.")
        else:
            option_lines.append("- Put/call ratio: too few open contracts to report one.")
        pw, cw = first.get("put_wing_iv"), first.get("call_wing_iv")
        if pw and cw:
            option_lines.append(f"- Skew at the {first['days']}-day expiry: out-of-the-money puts (about 90% of the price) at "
                                f"{pw * 100:.0f}% implied volatility against calls (about 110%) at {cw * 100:.0f}%.")
            if pw - cw >= SKEW_RICH_PUTS:
                flags.append(f"Out-of-the-money puts are priced {(pw - cw) * 100:.0f} volatility points above out-of-the-money "
                             "calls: downside protection is expensive.")
    body = ["## Price and range\n" + "\n".join(price_part), "## Volatility\n" + "\n".join(lines + option_lines),
            "## Flags to state in the report\n" + ("\n".join(f"- {f}" for f in flags) if flags else "No rule-based flags.")]
    return (f"# Market statistics for {symbol} (Yahoo prices through {pd.Timestamp(prices['Date'].iloc[-1]):%Y-%m-%d}, "
            f"analysis date {as_of})\n\n" + "\n\n".join(body))


def get_market_statistics(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Volatility, options positioning, volume and range for ``ticker`` from Yahoo as of ``as_of_date``."""
    canonical = normalize_symbol(ticker)
    as_of = as_of_date or get_current_date()
    prices = load_ohlcv(canonical, as_of, fill_gaps=False)
    if prices is None or len(prices) < 30:
        raise NoMarketDataError(ticker, canonical, "fewer than 30 sessions of prices")
    prices = prices.dropna(subset=["Close"]).reset_index(drop=True)
    if is_historical(as_of_date):
        return build_report(canonical, prices, None, None, as_of,
                            "Yahoo's option chains are live only, so a run dated in the past gets the price-based figures only.")
    spot = float(prices["Close"].iloc[-1])
    stamp = pd.Timestamp(as_of).normalize()
    try:
        options = _option_summary(canonical, spot, stamp)
        earnings = _next_earnings(canonical, stamp)
        return build_report(canonical, prices, options, earnings, as_of)
    except VendorUnavailableError:
        return build_report(canonical, prices, None, None, as_of,
                            "Yahoo's option chains could not be retrieved this time.")
