"""Worth versus price: the price set against the company's own history, its peers and the bond yield.

The analysts otherwise get the multiples (P/E, PEG, price to book) without anything to compare
them with, so "cheap" or "expensive" is the model's guess. This serves four yardsticks, each
worked out here so the analyst quotes figures and does not compute its own:

* the trailing P/E on reported earnings now against its readings at each of the last ten
  years' earnings reports,
* a table of the nearest-sized companies in the same Yahoo industry,
* the earnings yield against the 10-year Treasury yield, and the steady growth the price
  implies for a few required returns,
* the prices those multiples would give, as reference levels and not as forecasts.

Every input is a present-day figure with no historical vintage, so a run dated in the past is
not served this.
"""

from __future__ import annotations

import logging
import math
import statistics
from typing import Annotated

import pandas as pd
import requests
import yfinance as yf

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import fred
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry

logger = logging.getLogger(__name__)

HISTORY_YEARS = 10
RECENT_YEARS = 5
MIN_READINGS = 12              # fewer quarterly readings than this is not a range
MAX_PE = 150.0                 # a P/E above this means earnings near zero, not a price worth comparing
PEER_CANDIDATES = 10           # peers whose figures are fetched, picked by industry weight
PEERS_SHOWN = 5                # the ones nearest in size
MIN_PEERS_FOR_MEDIAN = 3
# A peer-based reference level uses only peers within this range of the company's own size.
SIMILAR_SIZE = (0.25, 4.0)
REQUIRED_RETURNS = (0.08, 0.09, 0.10)

# Trailing EPS this far above forward EPS usually means one-time items in the trailing year.
_TRAILING_ABOVE_FORWARD = 1.15


def _num(value) -> float | None:
    """A finite float, or None: Yahoo mixes None, NaN and the text 'Infinity'."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _x(value) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number:.1f}x"


def _plain(value) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number:.2f}"


def _pct(value, signed: bool = True) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number * 100:{'+' if signed else ''}.1f}%"


def _median_positive(values) -> float | None:
    """Median of the positive, finite values: a negative multiple is a loss, not a cheap price."""
    good = [v for v in map(_num, values) if v is not None and v > 0]
    return statistics.median(good) if len(good) >= MIN_PEERS_FOR_MEDIAN else None


# --- The company's own history ------------------------------------------------------------

def pe_readings(eps: pd.Series, closes: pd.Series) -> pd.Series:
    """Trailing P/E at each earnings report: that day's close over the last four reported EPS.

    Quarters with a loss over the trailing four are left out, since a multiple of negative
    earnings is not a price to compare, and so are readings above ``MAX_PE``, where earnings were
    close to zero. ``eps`` is indexed by report date, ``closes`` by day.
    """
    trailing = eps.rolling(4).sum()
    prices = closes.reindex(trailing.index, method="ffill")
    readings = (prices / trailing).where(trailing > 0).dropna()
    return readings[readings <= MAX_PE]


def history_lines(readings: pd.Series, current: float, today: pd.Timestamp) -> list[str]:
    """Where the current P/E sits among the readings of the last ten years."""
    window = readings[readings.index >= today - pd.DateOffset(years=HISTORY_YEARS)]
    if len(window) < MIN_READINGS:
        return [f"Not enough history for a range: {len(window)} quarterly readings in the last "
                f"{HISTORY_YEARS} years where trailing earnings were positive (at least {MIN_READINGS} are needed)."]
    below = float((window < current).mean() * 100)
    recent = window[window.index >= today - pd.DateOffset(years=RECENT_YEARS)]
    lines = [
        f"Over the last {HISTORY_YEARS} years ({len(window)} quarterly readings, each taken at an earnings report): "
        f"low {window.min():.1f}x, median {window.median():.1f}x, high {window.max():.1f}x. "
        f"The current {current:.1f}x is higher than {below:.0f}% of those readings.",
    ]
    if len(recent) >= MIN_READINGS // 2:
        lines.append(f"Over the last {RECENT_YEARS} years: median {recent.median():.1f}x "
                     f"(low {recent.min():.1f}x, high {recent.max():.1f}x).")
    return lines


def _history_section(canonical: str, price: float, today: pd.Timestamp, info: dict) -> tuple[list[str], dict]:
    """(lines, reference figures) for the company's own P/E history."""
    dates = yf_retry(lambda: yf.Ticker(canonical).get_earnings_dates(limit=100))
    if dates is None or dates.empty or "Reported EPS" not in dates:
        return ["No reported-earnings history is available, so there is no history of the P/E."], {}
    reported = dates[dates["Reported EPS"].notna()].sort_index()
    eps = pd.Series(
        reported["Reported EPS"].astype(float).to_numpy(),
        index=pd.to_datetime(reported.index, utc=True).tz_convert("America/New_York").tz_localize(None).normalize(),
    )
    prices = yf_retry(lambda: yf.Ticker(canonical).history(period="max", auto_adjust=False))
    if prices is None or prices.empty:
        return ["No price history is available, so there is no history of the P/E."], {}
    closes = prices["Close"].copy()
    closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()

    if len(eps) < 4:
        return ["Fewer than four reported quarters, so no trailing earnings can be formed."], {}
    trailing_eps = float(eps.tail(4).sum())
    header = ("Trailing P/E on reported earnings, meaning EPS as analysts forecast it (it leaves out one-time items such "
              "as investment gains that the GAAP figure includes), split-adjusted.")
    if trailing_eps <= 0:
        return [header, f"Trailing reported EPS over the last four quarters is {trailing_eps:.2f}, a loss, so there "
                        "is no P/E to compare with its history."], {}
    current = price / trailing_eps
    readings = pe_readings(eps, closes)
    lines = [header, f"Now: {current:.1f}x (price {price:.2f} over the last four reported quarters' EPS of {trailing_eps:.2f})."]
    if current > MAX_PE:
        lines.append(f"That is above {MAX_PE:.0f}x, meaning earnings are close to zero; it is not compared with the past.")
        return lines, {}
    lines += history_lines(readings, current, today)
    gaap = _num(info.get("trailingPE"))
    if gaap and (gaap > current * 1.5 or gaap < current / 1.5):
        lines.append(f"The GAAP trailing P/E ({gaap:.1f}x) differs a lot from this one ({current:.1f}x): the reported "
                     "figure leaves out some costs or gains that GAAP includes (stock compensation, investment gains and "
                     "losses, one-time charges), so judge which fits the business.")
    reference = {}
    window = readings[readings.index >= today - pd.DateOffset(years=HISTORY_YEARS)]
    if len(window) >= MIN_READINGS:
        reference["own_median_pe"] = float(window.median())
        reference["trailing_eps"] = trailing_eps
    return lines, reference


# --- Peers ---------------------------------------------------------------------------------

def _candidates(top: pd.DataFrame, symbol: str, name: str | None, target_cap: float | None, info_of) -> list[str]:
    """The industry's listed companies worth fetching, those nearest the target's weight first.

    Yahoo lists each company's weight in the industry's total value, so weight stands in for
    size before any figures are fetched. A target absent from the list has its weight worked out
    from the top company's market cap, so a small company is compared with small ones.
    """
    others = top[(top.index.str.upper() != symbol.upper()) & (top["name"].fillna("") != (name or ""))]
    if others.empty:
        return []
    weights = others["market weight"].astype(float).clip(lower=1e-9)
    if symbol in top.index:
        target_weight = float(top.loc[symbol, "market weight"])
    else:
        leader = top.index[0]
        leader_cap = _num((info_of(leader) or {}).get("marketCap"))
        target_weight = (float(top.loc[leader, "market weight"]) * target_cap / leader_cap
                         if target_cap and leader_cap else None)
    if target_weight:
        order = (weights / target_weight).apply(lambda w: abs(math.log(w))).sort_values()
        return list(order.index[:PEER_CANDIDATES])
    return list(weights.sort_values(ascending=False).index[:PEER_CANDIDATES])


def _peer_row(symbol: str, info: dict) -> dict:
    return {
        "symbol": symbol,
        "name": info.get("shortName") or info.get("longName") or symbol,
        "cap": _num(info.get("marketCap")),
        "forward_pe": _num(info.get("forwardPE")),
        "trailing_pe": _num(info.get("trailingPE")),
        "price_sales": _num(info.get("priceToSalesTrailing12Months")),
        "ev_ebitda": _num(info.get("enterpriseToEbitda")),
        "growth": _num(info.get("revenueGrowth")),
        "margin": _num(info.get("profitMargins")),
    }


def _peer_section(canonical: str, info: dict) -> tuple[list[str], dict]:
    key = info.get("industryKey")
    if not key:
        return ["Yahoo lists no industry for this security (funds and some listings have none), so there are no peers."], {}
    top = yf_retry(lambda: yf.Industry(key).top_companies)
    if top is None or top.empty:
        return [f"Yahoo lists no companies for the '{key}' industry."], {}

    cache: dict[str, dict | None] = {}

    def info_of(symbol: str) -> dict | None:
        if symbol not in cache:
            try:
                cache[symbol] = yf_retry(lambda: yf.Ticker(symbol).info) or None
            except VendorUnavailableError:
                cache[symbol] = None
        return cache[symbol]

    target_cap = _num(info.get("marketCap"))
    name = info.get("shortName") or info.get("longName")
    rows = []
    for symbol in _candidates(top, canonical, name, target_cap, info_of):
        peer_info = info_of(symbol)
        if peer_info and _num(peer_info.get("marketCap")):
            rows.append(_peer_row(symbol, peer_info))
    if target_cap:
        rows.sort(key=lambda r: abs(math.log(r["cap"] / target_cap)))
    rows = rows[:PEERS_SHOWN]
    if not rows:
        return [f"No peer figures could be fetched for the '{key}' industry."], {}

    me = _peer_row(canonical, info)
    table = ["| Company | Market cap | Forward P/E | Trailing P/E (GAAP) | Price/sales | EV/EBITDA | Revenue growth | Profit margin |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]

    def line(row: dict, label: str, versus: float | None = None) -> str:
        cap = "n/a" if row["cap"] is None else f"{row['cap'] / 1e9:,.1f}B"
        if versus is not None:
            cap += f" ({versus:.2f}x)"
        return (f"| {label} | {cap} | {_x(row['forward_pe'])} | {_x(row['trailing_pe'])} | {_x(row['price_sales'])} | "
                f"{_x(row['ev_ebitda'])} | {_pct(row['growth'])} | {_pct(row['margin'], signed=False)} |")

    table.append(line(me, f"{canonical} (this stock)"))
    table += [line(r, f"{r['symbol']} {r['name']}", r["cap"] / target_cap if target_cap else None) for r in rows]
    similar = [r for r in rows if target_cap and SIMILAR_SIZE[0] <= r["cap"] / target_cap <= SIMILAR_SIZE[1]]
    medians = {k: _median_positive(r[k] for r in similar) for k in ("forward_pe", "trailing_pe", "price_sales", "ev_ebitda")}
    if len(similar) >= MIN_PEERS_FOR_MEDIAN:
        table.append("| Median of the similar-size peers (positive values only) | | "
                     + " | ".join(_x(medians[k]) for k in ("forward_pe", "trailing_pe", "price_sales", "ev_ebitda")) + " | | |")
    note = (f"Peers are the {len(rows)} companies nearest in size from Yahoo's '{key}' industry list (the bracket is each "
            "peer's market cap as a multiple of this stock's). Yahoo's industry groups are loose: these businesses can "
            "differ a lot in growth, margins and risk. A negative multiple means losses. "
            f"{len(similar)} of the {len(rows)} are within {SIMILAR_SIZE[0]:g} to {SIMILAR_SIZE[1]:g} times this stock's "
            f"size. A median and a peer-based reference level are worked out only from peers in that range, and only "
            f"when at least {MIN_PEERS_FOR_MEDIAN} qualify; "
            + ("" if len(similar) >= MIN_PEERS_FOR_MEDIAN else
               "here too few do, so the rows are only context for how larger companies in the sector are priced, "
               "and a large company usually earns a higher multiple than a small one."))
    return [*table, "", note], {"peer_forward_pe": medians["forward_pe"], "similar_peers": len(similar)}


# --- What the price implies --------------------------------------------------------------

def _ten_year_yield() -> tuple[float, str] | None:
    """The latest 10-year Treasury yield in percent and its date, or None when FRED cannot say."""
    try:
        observations = fred._request("series/observations", {"series_id": "DGS10", "sort_order": "desc", "limit": 10})
    except (VendorNotConfiguredError, VendorUnavailableError, ValueError, requests.RequestException) as exc:
        logger.warning("Valuation: the 10-year yield could not be fetched: %s", exc)
        return None
    for observation in observations.get("observations", []):
        value = _num(observation.get("value"))
        if value is not None:
            return value, observation["date"]
    return None


def implied_growth_lines(earnings: float, price: float, basis: str, bond: tuple[float, str] | None) -> list[str]:
    """The earnings yield, its gap to the bond yield, and the growth the price implies."""
    yield_now = earnings / price
    lines = [f"Earnings yield on {basis}: {yield_now * 100:.1f}% (EPS {earnings:.2f} over price {price:.2f})."]
    if bond is not None:
        gap = yield_now * 100 - bond[0]
        lines.append(f"10-year Treasury yield: {bond[0]:.2f}% ({bond[1]}). The earnings yield is "
                     f"{abs(gap):.1f} percentage points {'above' if gap >= 0 else 'below'} it.")
    lines.append(_steady_growth_line(yield_now))
    if yield_now > REQUIRED_RETURNS[-1]:
        lines.append(_NEGATIVE_GROWTH_NOTE)
    return lines


_NEGATIVE_GROWTH_NOTE = ("A negative figure means the price already assumes earnings will fall, or that buyers doubt the "
                         "estimate (a debt-laden or shrinking business can trade at a high earnings yield for good reason).")


def _steady_growth_line(yield_now: float, worked_from: str = "") -> str:
    """The steady growth that a given required return and earnings yield imply, with the caution that goes with it."""
    steady = "; ".join(f"needing {r * 100:.0f}% a year -> {(r - yield_now) * 100:.1f}%" for r in REQUIRED_RETURNS)
    return (f"Steady yearly earnings growth the price implies{worked_from}, for a buyer who needs a given return: {steady}. "
            "This is a rough yardstick: it treats earnings as cash available to owners and ignores capital "
            "spending, dilution and debt, so read it against the cash flow report.")


def _fiscal_labels(info: dict) -> tuple[str, str]:
    """('this fiscal year (ending Jan 2027)', 'next fiscal year (ending Jan 2028)'), the year-end from Yahoo when it has one."""
    end = _num(info.get("nextFiscalYearEnd"))
    if end is None:
        return "this fiscal year", "next fiscal year"
    stamp = pd.Timestamp(end, unit="s")
    return (f"this fiscal year (ending {stamp:%b %Y})",
            f"next fiscal year (ending {stamp + pd.DateOffset(years=1):%b %Y})")


def _estimates(canonical: str) -> dict | None:
    """Analyst consensus EPS for the current and the next fiscal year, or None when Yahoo has none."""
    try:
        frame = yf_retry(lambda: yf.Ticker(canonical).earnings_estimate)
    except VendorUnavailableError:
        return None
    if frame is None or getattr(frame, "empty", True):
        return None
    found = {}
    for key, name in (("0y", "this"), ("+1y", "next")):
        if key in frame.index and _num(frame.loc[key, "avg"]) is not None:
            row = frame.loc[key]
            found[name] = {"eps": float(row["avg"]), "low": _num(row.get("low")), "high": _num(row.get("high")),
                           "analysts": _num(row.get("numberOfAnalysts"))}
    return found or None


def estimate_lines(price: float, estimates: dict, labels: tuple[str, str], bond: tuple[float, str] | None) -> list[str]:
    """Earnings yield on this year's and next year's consensus EPS, both against the Treasury, and the growth implied.

    Yahoo's 'forward EPS' is the next fiscal year's estimate, 12 to 15 months out for a company early in its
    year, so a single 'forward' figure can make a stock look much cheaper than this year's estimate does.
    """
    lines = ["Analyst consensus EPS for two fiscal years. Yahoo's 'forward EPS' and forward P/E are the NEXT fiscal "
             "year's estimate, so the two years can tell different stories:"]
    yields: dict[str, float] = {}
    for key, label in zip(("this", "next"), labels, strict=True):
        estimate = estimates.get(key)
        if not estimate:
            continue
        analysts = f" from {int(estimate['analysts'])} analysts" if estimate["analysts"] else ""
        spread = (f" (range {estimate['low']:.2f} to {estimate['high']:.2f})"
                  if estimate["low"] is not None and estimate["high"] is not None else "")
        if estimate["eps"] > 0:
            yields[key] = estimate["eps"] / price
            lines.append(f"- {label}: EPS {estimate['eps']:.2f}{analysts}{spread}; P/E {price / estimate['eps']:.1f}x; "
                         f"earnings yield {yields[key] * 100:.1f}%.")
        else:
            lines.append(f"- {label}: EPS {estimate['eps']:.2f}{analysts}{spread}; a loss, so no P/E or earnings yield.")
    if not yields:
        return [*lines, "Earnings are not positive in either year, so an earnings yield and the growth it implies are "
                        "not defined."]
    names = {"this": "this year's", "next": "next year's"}
    if bond is not None:
        parts = []
        for key, value in yields.items():
            gap = value * 100 - bond[0]
            parts.append(f"{abs(gap):.1f} percentage points {'above' if gap >= 0 else 'below'} it on {names[key]} estimate")
        lines.append(f"10-year Treasury yield: {bond[0]:.2f}% ({bond[1]}). The earnings yield is " + " and ".join(parts) + ".")
    base = "this" if "this" in yields else "next"
    worked = f" (worked from {labels[0 if base == 'this' else 1]})"
    lines.append(_steady_growth_line(yields[base], worked))
    if len(yields) == 2:
        other = "next" if base == "this" else "this"
        diff = (yields[other] - yields[base]) * 100
        if abs(diff) >= 0.1:
            lines.append(f"Worked from {labels[0 if other == 'this' else 1]} instead, those growth figures would be about "
                         f"{abs(diff):.1f} points {'lower' if diff > 0 else 'higher'}.")
    if yields[base] > REQUIRED_RETURNS[-1]:
        lines.append(_NEGATIVE_GROWTH_NOTE)
    return lines


def _implied_section(canonical: str, price: float, info: dict) -> list[str]:
    bond = _ten_year_yield() if info.get("currency") in (None, "USD") else None
    estimates = _estimates(canonical)
    if estimates:
        return estimate_lines(price, estimates, _fiscal_labels(info), bond)
    forward, trailing = _num(info.get("forwardEps")), _num(info.get("trailingEps"))
    if forward and forward > 0:
        earnings, basis = forward, "Yahoo's forward earnings (normally the next fiscal year's estimate)"
    elif trailing and trailing > 0:
        earnings, basis = trailing, "trailing earnings"
    else:
        return ["Earnings are not positive, so an earnings yield and the growth it implies are not defined."]
    return implied_growth_lines(earnings, price, basis, bond)


# --- Reference levels --------------------------------------------------------------------

def reference_lines(price: float, info: dict, history: dict, peers: dict) -> list[str]:
    """The prices the company's own median multiple and the peers' would give, as arithmetic."""
    lines = []
    if history.get("own_median_pe") and history.get("trailing_eps"):
        level = history["own_median_pe"] * history["trailing_eps"]
        lines.append(f"- Its own {HISTORY_YEARS}-year median trailing P/E of {history['own_median_pe']:.1f}x on trailing reported "
                     f"EPS of {history['trailing_eps']:.2f}: {level:.2f} ({(level / price - 1) * 100:+.0f}% from the price).")
    forward = _num(info.get("forwardEps"))
    if peers.get("peer_forward_pe") and forward and forward > 0:
        level = peers["peer_forward_pe"] * forward
        lines.append(f"- The peers' median forward P/E of {peers['peer_forward_pe']:.1f}x on its next-fiscal-year EPS "
                     f"estimate of {forward:.2f}: {level:.2f} ({(level / price - 1) * 100:+.0f}% from the price).")
    if not peers.get("peer_forward_pe") and peers.get("similar_peers") is not None:
        lines.append(f"- A peer-based level is not worked out: {peers['similar_peers']} peer(s) of similar size had a "
                     f"positive forward P/E, and at least {MIN_PEERS_FOR_MEDIAN} are needed.")
    if not any(line.startswith("- Its own") or line.startswith("- The peers") for line in lines):
        return []
    return ["Reference price levels. These are arithmetic from the multiples above, not forecasts or targets: a company "
            "can deserve a higher or lower multiple than its history or its peers.", *lines]


# --- Entry point ---------------------------------------------------------------------------

def get_valuation_context(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """The price against the company's history, its peers and the bond yield, as present-day figures.

    A section that cannot be built says so and the others are still served; the call fails only
    when none could be.
    """
    canonical = normalize_symbol(ticker)
    if is_historical(as_of_date):
        return (f"# Valuation context for {canonical}\n\nWithheld for {as_of_date}: the multiples, peer figures and bond "
                "yield here are present-day values with no historical vintage, so serving them would put "
                "post-decision information into that analysis.")

    info = yf_retry(lambda: yf.Ticker(canonical).info)
    if not info:
        raise_for_empty(ticker, canonical, "valuation fields")
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    if not price:
        raise_for_empty(ticker, canonical, "quote")

    today = pd.Timestamp.now().normalize()
    history_reference: dict = {}
    peer_reference: dict = {}

    def snapshot() -> list[str]:
        trailing_eps, forward_eps = _num(info.get("trailingEps")), _num(info.get("forwardEps"))
        lines = [f"Price {price:.2f}. Trailing P/E (GAAP) {_x(info.get('trailingPE'))}; forward P/E {_x(info.get('forwardPE'))} (on the next fiscal year's estimate); "
                 f"PEG {_plain(info.get('pegRatio'))}; price/sales {_x(info.get('priceToSalesTrailing12Months'))}; "
                 f"EV/EBITDA {_x(info.get('enterpriseToEbitda'))}; revenue growth {_pct(info.get('revenueGrowth'))}; "
                 f"profit margin {_pct(info.get('profitMargins'), signed=False)}."]
        if trailing_eps and forward_eps and forward_eps > 0 and trailing_eps > forward_eps * _TRAILING_ABOVE_FORWARD:
            lines.append("Trailing earnings are well above forward earnings, which usually means one-time gains in the "
                         "trailing year: the GAAP trailing P/E then flatters the stock, and the forward and reported-earnings "
                         "figures below are the fairer guides.")
        return lines

    # The history and peer builders return their reference figures alongside their lines.
    outputs: dict[str, list[str]] = {}
    failures = 0
    plan = (
        ("Where the price stands", snapshot),
        ("Against its own history", lambda: _record(_history_section(canonical, price, today, info), history_reference)),
        ("Against its peers", lambda: _record(_peer_section(canonical, info), peer_reference)),
        ("What the price implies", lambda: _implied_section(canonical, price, info)),
    )
    for title, build in plan:
        try:
            outputs[title] = build()
        except (VendorUnavailableError, NoMarketDataError, KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
            logger.warning("Valuation: the '%s' section for %s could not be built: %s", title, canonical, exc)
            failures += 1
            outputs[title] = [f"Not available ({type(exc).__name__}: {exc})."]
    if failures >= 3:
        raise VendorUnavailableError("Yahoo Finance could not supply the data for a valuation comparison")

    body = []
    for title, lines in outputs.items():
        body.append(f"## {title}\n\n" + "\n".join(lines))
    levels = reference_lines(price, info, history_reference, peer_reference)
    if levels:
        body.append("## Reference price levels\n\n" + "\n".join(levels))
    return (f"# Valuation context for {canonical} (present-day figures)\n\n"
            "These are yardsticks for whether the price is high or low relative to the company's own past, its peers and "
            "bond yields. Multiples depend on growth, risk and interest rates, so none of them is a fair-value "
            "estimate.\n\n" + "\n\n".join(body))


def _record(result: tuple[list[str], dict], into: dict) -> list[str]:
    """Keep a section's reference figures and return its lines."""
    lines, figures = result
    into.update(figures)
    return lines
