"""Interactive Brokers market statistics: volatility, trading volume and price range (a local addition).

Options-implied volatility, its percentile, 30-day realized volatility, 90-day dollar volume and the
13-, 26- and 52-week ranges come from IBKR's official MCP server, read-only. The app's own Python
environment has no MCP library, so a small helper in its own environment is run as a subprocess
(``fetch_stats.py`` in the folder named by ``IBKR_HELPER_DIR``); it prints one JSON status.

The helper can never open a browser from here. When its saved sign-in has lapsed it answers
``needs_sign_in`` and this vendor reports the data as unavailable, with the command that signs in again.
Figures are present-day only: IBKR serves no history, so a run dated in the past is withheld.

Percentages and the implied moves are worked out here from IBKR's raw figures, so the analyst quotes them
instead of computing its own, and rule-based flags say what deserves a sentence.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Annotated

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol

HELPER_DIR_ENV = "IBKR_HELPER_DIR"
DEFAULT_HELPER_DIR = os.path.join(os.path.expanduser("~"), ".tradingagents", "ibkr_helper")   # set IBKR_HELPER_DIR to use another folder
HELPER_SECONDS = 90
SIGN_IN_COMMAND = r"{python} {folder}\probe.py"

TRADING_DAYS_YEAR = 252
WEEK_DAYS = 5
MONTH_DAYS = 21

IV_HIGH = 1.0                  # annual implied volatility of 100% or more
IV_OVER_HV = 1.5               # implied at least this many times realized, or at most 1 / this
IV_PERCENTILE_EDGE = 0.9       # implied volatility at or above this percentile of its 52-week range, or at or below 1 - this
NEAR_EXTREME = 0.05            # last price within 5% of its 52-week high or low
VOLUME_RATIO_HIGH = 3.0        # latest session's dollar volume against the 90-day average
VOLUME_RATIO_LOW = 0.2

_PLAIN_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.-][A-Z])?$")
_STATUS = {
    "REALTIME": "a real-time quote",
    "DELAYED": "a delayed quote",
    "FROZEN": "the market was closed, so these are the last session's figures",
    "FROZEN_DELAYED": "the market was closed and the quote is delayed",
    "REJECT": "IBKR refused market data for this symbol",
}


def _helper_dir() -> Path:
    return Path(os.getenv(HELPER_DIR_ENV, DEFAULT_HELPER_DIR))


def _helper_python(folder: Path) -> Path:
    for sub in ("Scripts/python.exe", "bin/python"):
        if (folder / "venv" / sub).is_file():
            return folder / "venv" / sub
    return folder / "venv" / "Scripts" / "python.exe"


def _run_helper(symbol: str) -> dict:
    """The helper's JSON answer for one symbol; unreadable or failed runs are reported as unavailable."""
    folder = _helper_dir()
    python, script = _helper_python(folder), folder / "fetch_stats.py"
    if not python.is_file() or not script.is_file():
        raise VendorNotConfiguredError(
            f"the IBKR helper is not installed at {folder}; set {HELPER_DIR_ENV} to its folder")
    env = {k: v for k, v in os.environ.items() if k != "IBKR_TRACE"}
    if "SSL_CERT_FILE" not in env and os.getenv("REQUESTS_CA_BUNDLE"):
        env["SSL_CERT_FILE"] = os.environ["REQUESTS_CA_BUNDLE"]
    try:
        done = subprocess.run([str(python), str(script), symbol], cwd=str(folder), env=env,
                              capture_output=True, text=True, timeout=HELPER_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise VendorUnavailableError(f"the IBKR helper took longer than {HELPER_SECONDS} seconds") from exc
    except OSError as exc:
        raise VendorUnavailableError(f"the IBKR helper could not start ({type(exc).__name__})") from exc
    try:
        answer = json.loads(done.stdout)
    except ValueError as exc:
        raise VendorUnavailableError("the IBKR helper returned an unreadable answer") from exc
    if not isinstance(answer, dict):
        raise VendorUnavailableError("the IBKR helper returned an unreadable answer")
    return answer


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _field(snapshot: dict, block: str, name: str) -> float | None:
    inner = snapshot.get(block)
    return _num(inner.get(name)) if isinstance(inner, dict) else None


def _ratio_text(value: float) -> str:
    return f"{value:.1f}" if value >= 0.095 else f"{value:.2f}"


def _price_lines(snapshot: dict, last: float) -> tuple[list[str], list[str]]:
    lines, flags = [], []
    stamp = (snapshot.get("last") or {}).get("ts")
    when = ""
    if _num(stamp):
        when = f" at {datetime.fromtimestamp(float(stamp), tz=timezone.utc):%Y-%m-%d %H:%M} UTC"
    line = f"- Last price {last:g}{when}"
    change, change_pct = _field(snapshot, "change", "change"), _field(snapshot, "change", "change_pct")
    if change is not None and change_pct is not None:
        line += f"; {change:+.2f} ({change_pct:+.1f}%) from the prior close, as IBKR reports it"
    lines.append(line + ".")
    stats = snapshot.get("misc-statistics") or {}
    for label, key in (("13-week", "13w"), ("26-week", "26w"), ("52-week", "52w")):
        high, low = _num(stats.get(f"high_{key}")), _num(stats.get(f"low_{key}"))
        if high and low:
            lines.append(f"- {label} range {low:g} to {high:g}: the last price is {(1 - last / high) * 100:.0f}% "
                         f"below the high and {(last / low - 1) * 100:.0f}% above the low.")
    high52, low52 = _num(stats.get("high_52w")), _num(stats.get("low_52w"))
    if high52 and last >= high52 * (1 - NEAR_EXTREME):
        flags.append(f"The last price is within {NEAR_EXTREME * 100:.0f}% of its 52-week high ({high52:g}).")
    if low52 and last <= low52 * (1 + NEAR_EXTREME):
        flags.append(f"The last price is within {NEAR_EXTREME * 100:.0f}% of its 52-week low ({low52:g}).")
    year_ago = _num(stats.get("open_52w"))
    if year_ago:
        lines.append(f"- The open 52 weeks ago was {year_ago:g}, so the price is {(last / year_ago - 1) * 100:+.1f}% "
                     "over the year.")
    ytd = _field(snapshot, "year-to-date-change", "change_pct")
    if ytd is not None:
        lines.append(f"- Year to date: {ytd:+.1f}%, as IBKR reports it.")
    return lines, flags


def _volatility_lines(snapshot: dict) -> tuple[list[str], list[str]]:
    implied_block = snapshot.get("implied-vol-underlying") or {}
    implied = _num(implied_block.get("annual_iv")) if implied_block.get("is_valid", True) else None
    realized = _field(snapshot, "historical-vol", "annual_pct")
    lines, flags = [], []
    if implied is None:
        lines.append("- No valid options-implied volatility is available (the stock may have no listed options "
                     "or no current reading).")
    else:
        lines.append(f"- Options-implied volatility, annualized: {implied * 100:.0f}%.")
    if realized is not None:
        lines.append(f"- Realized volatility over the last 30 days, annualized: {realized * 100:.0f}%.")
    if implied is not None and realized:
        ratio = implied / realized
        lines.append(f"- Implied volatility is {_ratio_text(ratio)} times realized volatility.")
        if ratio >= IV_OVER_HV:
            flags.append(f"Implied volatility is {_ratio_text(ratio)} times realized volatility: the options market "
                         "prices more movement than the last 30 days showed.")
        elif ratio <= 1 / IV_OVER_HV:
            flags.append(f"Implied volatility is only {_ratio_text(ratio)} times realized volatility: the options market "
                         "prices less movement than the last 30 days showed.")
    percentile = snapshot.get("implied-volatility-percentile") or {}
    values = {label: _num(percentile.get(key)) for label, key in (("13 weeks", "high_13w"), ("26 weeks", "high_26w"),
                                                                 ("52 weeks", "high_52w"))}
    if any(v is not None for v in values.values()):
        shown = ", ".join(f"{label} {v * 100:.0f}%" for label, v in values.items() if v is not None)
        lines.append(f"- Implied-volatility percentile, as IBKR reports it: {shown} (a high figure means implied "
                     "volatility is high against its own recent range).")
        year = values["52 weeks"]
        if year is not None and year >= IV_PERCENTILE_EDGE:
            flags.append(f"Implied volatility is at the {year * 100:.0f}th percentile of its 52-week range: high "
                         "against its own history.")
        elif year is not None and year <= 1 - IV_PERCENTILE_EDGE:
            flags.append(f"Implied volatility is at the {year * 100:.0f}th percentile of its 52-week range: low "
                         "against its own history.")
    if implied is not None:
        week, month = implied * sqrt(WEEK_DAYS / TRADING_DAYS_YEAR), implied * sqrt(MONTH_DAYS / TRADING_DAYS_YEAR)
        beyond = (" A figure above 100% is only the arithmetic of very high volatility; a stock cannot fall by "
                  "more than 100%.") if max(week, month) >= 1.0 else ""
        lines.append(f"- One standard deviation of that implied volatility, as arithmetic and not a forecast, a "
                     f"target or a limit: about ±{week * 100:.0f}% over {WEEK_DAYS} trading days and "
                     f"±{month * 100:.0f}% over {MONTH_DAYS}.{beyond}")
        if implied >= IV_HIGH:
            flags.append(f"Implied volatility is {implied * 100:.0f}% a year, {IV_HIGH * 100:.0f}% or more: the options "
                         "market prices very large moves, and a level this high can come from a thin options market.")
    return lines, flags


def _volume_lines(snapshot: dict, last: float) -> tuple[list[str], list[str]]:
    volume = _field(snapshot, "volume", "volume")
    average = _field(snapshot, "avg-90d-usd-volume", "volume")
    lines, flags = [], []
    if average:
        lines.append(f"- 90-day average dollar volume: ${average:,.0f} a day.")
    if volume and average:
        dollars = volume * last
        ratio = dollars / average
        lines.append(f"- The latest session traded {volume:,.0f} shares, about ${dollars:,.0f}: {_ratio_text(ratio)} "
                     "times that average.")
        if ratio >= VOLUME_RATIO_HIGH or ratio <= VOLUME_RATIO_LOW:
            flags.append(f"The latest session's dollar volume is {_ratio_text(ratio)} times the 90-day average; "
                         "a few very large sessions can dominate that average.")
    return lines, flags


def build_report(symbol: str, contract: dict, snapshot: dict) -> str:
    """The report for one symbol from the helper's contract and snapshot; raises NoMarketDataError if empty."""
    last = _field(snapshot, "last", "price")
    if not last or last <= 0:
        raise NoMarketDataError(symbol, None, "IBKR returned no price for this symbol")
    status = str((snapshot.get("top-status") or {}).get("status", "")).upper()
    note = _STATUS.get(status, "")
    name = " ".join(part for part in (contract.get("description"), f"({contract.get('exchange')})"
                                      if contract.get("exchange") else "") if part)
    head = f"IBKR's figures for {name}." if name else "IBKR's figures."
    if note:
        head += f" Quote status: {note}."
    sections = []
    flags: list[str] = []
    for title, build in (("Price and range", lambda: _price_lines(snapshot, last)),
                         ("Volatility", lambda: _volatility_lines(snapshot)),
                         ("Trading volume", lambda: _volume_lines(snapshot, last))):
        lines, found = build()
        if lines:
            sections.append(f"## {title}\n" + "\n".join(lines))
        flags += found
    sections.append("## Flags to state in the report\n" + (
        "\n".join(f"- {flag}" for flag in flags) if flags else "No rule-based flags."))
    return f"# Market statistics for {symbol} (IBKR, present-day figures)\n\n{head}\n\n" + "\n\n".join(sections)


def get_market_statistics(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Volatility, volume and price-range statistics for ``ticker`` from IBKR (read-only).

    IBKR serves only the present day, so a run dated in the past is not served them.
    """
    canonical = normalize_symbol(ticker)
    if is_historical(as_of_date):
        return (
            f"# Market statistics for {canonical}\n# Point-in-time as of: {as_of_date}\n\n"
            f"Market statistics are withheld for this date. IBKR serves only present-day volatility, volume and "
            f"price ranges, with no history, so they cannot show what was known on {as_of_date}."
        )
    if not isinstance(canonical, str) or not _PLAIN_SYMBOL.match(canonical):
        raise NoMarketDataError(ticker, canonical, "IBKR statistics are fetched for plain US-style stock symbols only")
    answer = _run_helper(canonical)
    status = answer.get("status")
    if status == "ok":
        return build_report(canonical, answer.get("contract") or {}, answer.get("snapshot") or {})
    if status == "not_found":
        raise NoMarketDataError(ticker, canonical, "IBKR has no stock listing for this symbol")
    if status == "needs_sign_in":
        folder = _helper_dir()
        raise VendorUnavailableError(
            "the IBKR sign-in has lapsed; the person must sign in again with "
            + SIGN_IN_COMMAND.format(python=_helper_python(folder), folder=folder))
    raise VendorUnavailableError(f"the IBKR helper could not answer ({answer.get('reason') or 'unknown reason'})")
