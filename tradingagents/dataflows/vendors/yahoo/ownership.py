"""Who owns a stock and how heavily it is sold short, from Yahoo (a local addition).

The fundamentals analyst otherwise has no figures for ownership or short interest. Yahoo serves
two filings-based data sets, and each carries a lag the report states beside it: short interest,
which the exchanges publish about twice a month, and the 13F holdings of institutions and funds,
which are filed up to 45 days after each quarter ends.

Percentages are worked out here from the raw share counts and name their denominator. Yahoo's own
"percent of float" can disagree with its own float figure (a stock whose float is smaller than the
shares sold short), so the report says so instead of leaving the analyst to quote either number.
"""

from datetime import date
from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import YAHOO_HOST, yf_retry

HOLDERS_SHOWN = 10
FUNDS_SHOWN = 5
FLAGGED_HOLDER_MOVES = 4          # at most this many individual position changes are flagged

SHORT_HEAVY_PERCENT = 10.0        # of shares outstanding
SHORT_MOVE_PERCENT = 20.0         # change in shares short between the two latest reports
DAYS_TO_COVER_FLAG = 5.0
IMPLIED_TURNOVER_FLAG = 0.5       # days to cover implying this share of all shares trades every day is not credible
FLOAT_MISMATCH_POINTS = 5.0       # Yahoo's percent of float against shares short over its float
SMALL_FLOAT_SHARE = 0.5           # float below this share of shares outstanding
HOLDER_MOVE_FRACTION = 0.25       # a top holder's position changed by this much in its latest report
HOLDER_FLAG_MIN_HELD = 0.01       # and holds at least this share of the shares outstanding (smaller stakes are noise)


def _today() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _day(value) -> pd.Timestamp | None:
    """A Yahoo date (epoch seconds, a date or a string) as a day, or None when unreadable."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = pd.Timestamp(int(value), unit="s")
        else:
            stamp = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    if pd.isna(stamp):
        return None
    return (stamp.tz_convert(None) if stamp.tzinfo else stamp).normalize()


def _shares(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _percent(fraction: float | None, digits: int = 2) -> str:
    return "n/a" if fraction is None else f"{fraction * 100:.{digits}f}%"


def _signed_percent(fraction: float | None) -> str:
    return "n/a" if fraction is None else f"{fraction * 100:+.1f}%"


def _days_before(day: pd.Timestamp | None, today: pd.Timestamp) -> str:
    return "" if day is None else f" ({(today - day).days} days before today)"


def _short_section(info: dict, today: pd.Timestamp) -> tuple[list[str], list[str]]:
    """The short-interest lines and the flags worked out from them; both empty when Yahoo has none."""
    short = _number(info.get("sharesShort"))
    if short is None:
        return [], []
    outstanding = _number(info.get("sharesOutstanding"))
    float_shares = _number(info.get("floatShares"))
    yahoo_percent = _number(info.get("shortPercentOfFloat"))
    prior = _number(info.get("sharesShortPriorMonth"))
    days_to_cover = _number(info.get("shortRatio"))
    as_of = _day(info.get("dateShortInterest"))
    prior_date = _day(info.get("sharesShortPreviousMonthDate"))

    percent_outstanding = short / outstanding * 100 if outstanding else None
    percent_float = short / float_shares * 100 if float_shares else None
    yahoo_float_percent = None if yahoo_percent is None else yahoo_percent * 100

    stated = f"Shares short: {_shares(short)}"
    if as_of is not None:
        stated += f" as of {as_of:%Y-%m-%d}{_days_before(as_of, today)}"
    lines = [f"- {stated}."]
    if percent_outstanding is not None:
        basis = f"- That is {percent_outstanding:.1f}% of the {_shares(outstanding)} shares outstanding"
        if percent_float is not None and percent_float <= 100:
            basis += f", and {percent_float:.1f}% of the float of {_shares(float_shares)} shares"
        lines.append(basis + ".")
    change = None
    if prior is not None and prior > 0:
        change = (short / prior - 1) * 100
        when = f" ({prior_date:%Y-%m-%d})" if prior_date is not None else ""
        lines.append(f"- The report before that{when}: {_shares(prior)} shares short, a change of {change:+.0f}%.")
    # Days to cover is shares short over average daily volume, so it implies that volume. One that
    # would turn over half of all shares every day means Yahoo's volume or share count is distorted.
    implied_volume = short / days_to_cover if days_to_cover else None
    turnover = implied_volume / outstanding if implied_volume is not None and outstanding else None
    days_unreliable = turnover is not None and turnover >= IMPLIED_TURNOVER_FLAG
    if days_to_cover is not None:
        meaning = "shares short divided by average daily volume"
        if days_unreliable:
            meaning += "; this figure looks unreliable, see the flags"
        lines.append(f"- Days to cover: {days_to_cover:.1f} ({meaning}).")
    lines.append("- Short interest is published about twice a month and lags; it does not show today's shorting.")

    flags = []
    if percent_outstanding is not None and percent_outstanding >= SHORT_HEAVY_PERCENT:
        flags.append(f"{percent_outstanding:.1f}% of the shares outstanding are sold short, "
                     f"{SHORT_HEAVY_PERCENT:.0f}% or more.")
    if change is not None and abs(change) >= SHORT_MOVE_PERCENT:
        span = f" from {prior_date:%Y-%m-%d} to {as_of:%Y-%m-%d}" if prior_date is not None and as_of is not None else ""
        flags.append(f"Shares short {'rose' if change > 0 else 'fell'} {abs(change):.0f}%{span}.")
    if days_unreliable:
        flags.append(f"Days to cover ({days_to_cover:.1f}) implies an average daily volume of {implied_volume:,.0f} "
                     f"shares, {turnover * 100:.0f}% of all shares outstanding every day, which is not credible, so "
                     "Yahoo's volume or share count is distorted (for example by a few huge sessions or an unadjusted "
                     "share-count change); do not quote days to cover for this stock.")
    elif days_to_cover is not None and days_to_cover >= DAYS_TO_COVER_FLAG:
        flags.append(f"Days to cover is {days_to_cover:.1f}, {DAYS_TO_COVER_FLAG:.0f} or more: the shorted shares "
                     "would take that many days of average volume to buy back.")
    steadier = f"{percent_outstanding:.1f}% of shares outstanding" if percent_outstanding is not None else "the share count"
    if yahoo_float_percent is not None and percent_float is not None and (
            percent_float > 100 or abs(yahoo_float_percent - percent_float) >= FLOAT_MISMATCH_POINTS):
        flags.append(f"Yahoo's own percent of float ({yahoo_float_percent:.1f}%) does not match shares short divided "
                     f"by its float figure ({percent_float:.0f}%), so its float figure is unreliable for this stock; "
                     f"quote {steadier} and not a percent of float.")
    elif percent_float is not None and percent_float > 100:
        flags.append(f"Shares short exceed Yahoo's float figure, so that figure cannot be right; quote {steadier}.")
    elif outstanding and float_shares and float_shares / outstanding < SMALL_FLOAT_SHARE:
        flags.append(f"The float ({_shares(float_shares)}) is {float_shares / outstanding * 100:.0f}% of the shares "
                     "outstanding, so most shares are not freely traded and percentages of the float run much "
                     "higher than percentages of shares outstanding.")
    return lines, flags


def _holder_rows(frame, limit: int) -> list[dict]:
    """The largest holders in a Yahoo holders table, as plain dicts; empty when there is none."""
    if frame is None or len(frame) == 0:
        return []
    if "Shares" in frame.columns:
        frame = frame.sort_values("Shares", ascending=False)
    rows = []
    for _, row in frame.head(limit).iterrows():
        name = str(row.get("Holder", "")).replace("|", "/").strip() or "unnamed"
        rows.append({"name": name, "shares": _number(row.get("Shares")), "percent": _number(row.get("pctHeld")),
                     "change": _number(row.get("pctChange")), "date": _day(row.get("Date Reported"))})
    return rows


def _holder_table(rows: list[dict]) -> list[str]:
    table = ["| Holder | Shares | % of shares outstanding | Change since its previous report | Reported |",
             "|---|---|---|---|---|"]
    for row in rows:
        date = "n/a" if row["date"] is None else f"{row['date']:%Y-%m-%d}"
        table.append(f"| {row['name']} | {_shares(row['shares'])} | {_percent(row['percent'])} | "
                     f"{_signed_percent(row['change'])} | {date} |")
    return table


def _owner_section(info: dict, institutions: list[dict], funds: list[dict],
                   today: pd.Timestamp) -> tuple[list[str], list[str]]:
    """The ownership lines and flags; both empty when Yahoo has neither totals nor holders."""
    held_institutions = _number(info.get("heldPercentInstitutions"))
    held_insiders = _number(info.get("heldPercentInsiders"))
    if held_institutions is None and held_insiders is None and not institutions and not funds:
        return [], []
    lines = []
    totals = []
    if held_institutions is not None:
        totals.append(f"institutions hold {_percent(held_institutions, 1)} of the shares outstanding")
    if held_insiders is not None:
        totals.append(f"insiders hold {_percent(held_insiders, 1)}")
    if totals:
        lines.append("- Yahoo's totals: " + " and ".join(totals) + ".")
    flags = []
    if institutions:
        dates = [row["date"] for row in institutions if row["date"] is not None]
        if dates:
            newest = max(dates)
            lines.append(f"- The newest institutional report is dated {newest:%Y-%m-%d}{_days_before(newest, today)}. "
                         "Holdings are filed up to 45 days after each quarter ends, so this is not today's ownership.")
        moves = [row["change"] for row in institutions if row["change"] is not None]
        if moves:
            lines.append(f"- Of the {len(moves)} largest institutions with a stated change, "
                         f"{sum(m > 0 for m in moves)} show an increase and {sum(m < 0 for m in moves)} a decrease "
                         "since their previous reports (Yahoo does not say whether a +100% is a new position).")
        lines += ["", f"Largest institutions (top {len(institutions)}):"] + _holder_table(institutions)
        big = sorted((r for r in institutions
                      if r["change"] is not None and abs(r["change"]) >= HOLDER_MOVE_FRACTION
                      and (r["percent"] or 0) >= HOLDER_FLAG_MIN_HELD),
                     key=lambda r: -abs(r["change"]))[:FLAGGED_HOLDER_MOVES]
        for row in big:
            when = f" ({row['date']:%Y-%m-%d})" if row["date"] is not None else ""
            flags.append(f"{row['name']}'s position changed by {row['change'] * 100:+.0f}% in its latest "
                         f"report{when}; it holds {_percent(row['percent'], 1)} of the shares outstanding.")
    if funds:
        lines += ["", f"Largest funds (top {len(funds)}; already part of the institutional figures, so do not add them):"]
        lines += _holder_table(funds)
    lines += ["", "A parent company can appear under more than one name, so a family's total can be larger than "
                  "any single line."]
    return lines, flags


def _finra_section(symbol: str, as_of: str, prefer_finra_days_to_cover: bool = False) -> tuple[list[str], list[str]]:
    """FINRA's official short-interest history, daily short-sale volume and threshold list; never raises.

    When Yahoo's days to cover was called unreliable, FINRA's own figure (its average daily volume is not Yahoo's)
    is added as the one to use.
    """
    try:
        from tradingagents.dataflows.vendors import finra_short

        data = finra_short.fetch(symbol, date.fromisoformat(as_of))
        lines, flags = finra_short.build_section(symbol, data)
        rows = data.get("interest") or []
        if prefer_finra_days_to_cover and rows and rows[0].get("days_to_cover") is not None:
            lines.append(f"FINRA's own days to cover for {rows[0]['settled']} is {rows[0]['days_to_cover']:.2f} (its average "
                         f"daily volume is {rows[0]['avg_volume']:,.0f} shares): use this in place of Yahoo's figure, which "
                         "was called unreliable above.")
        return lines, flags
    except (VendorUnavailableError, ValueError, OSError):
        return ["FINRA's files could not be reached this time."], []


def get_ownership_and_short_interest(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Short interest and the largest institutional holders of ``ticker`` from Yahoo.

    Yahoo serves only the latest short-interest report and the latest holder filings, with no
    history, so a run dated in the past is not served them.
    """
    canonical = normalize_symbol(ticker)
    if is_historical(as_of_date):
        return (
            f"# Ownership and short interest for {canonical}\n"
            f"# Point-in-time as of: {as_of_date}\n\n"
            f"Ownership and short interest are withheld for this date. Yahoo serves only the latest "
            f"short-interest report and the latest holder filings, with no history, so they cannot show what "
            f"was public on {as_of_date}."
        )
    today = _today()
    info = yf_retry(lambda: yf.Ticker(canonical).info)
    info = info if isinstance(info, dict) else {}

    def holders(attribute: str, limit: int) -> list[dict] | None:
        try:
            return _holder_rows(yf_retry(lambda: getattr(yf.Ticker(canonical), attribute)), limit)
        except VendorUnavailableError:
            return None

    institutions = holders("institutional_holders", HOLDERS_SHOWN)
    funds = holders("mutualfund_holders", FUNDS_SHOWN)
    unretrieved = [label for label, rows in (("institutional", institutions), ("fund", funds)) if rows is None]

    short_lines, short_flags = _short_section(info, today)
    owner_lines, owner_flags = _owner_section(info, institutions or [], funds or [], today)
    if not short_lines and not owner_lines:
        if unretrieved or not vendor_reachable(YAHOO_HOST):
            raise VendorUnavailableError("Yahoo Finance ownership and short-interest data could not be retrieved")
        return (f"No ownership or short-interest data is reported for '{canonical}' "
                "(funds and some listings have none).")

    sections = []
    if short_lines:
        sections.append("## Short interest\n" + "\n".join(short_lines))
    if owner_lines:
        sections.append("## Who owns the shares\n" + "\n".join(owner_lines))
    yahoo_days_unreliable = any(f.startswith("Days to cover (") and "implies an average daily volume" in f for f in short_flags)
    finra_lines, finra_flags = _finra_section(canonical, today.strftime("%Y-%m-%d"), yahoo_days_unreliable)
    if finra_lines:
        sections.append("## Short-sale activity (FINRA)\n" + "\n".join(finra_lines))
    if unretrieved:
        sections.append(f"Note: the {' and '.join(unretrieved)} holder list could not be retrieved this time.")
    flags = short_flags + finra_flags + owner_flags
    sections.append("## Flags to state in the report\n" + (
        "\n".join(f"- {flag}" for flag in flags) if flags else "No rule-based flags."))
    return (f"# Ownership and short interest for {canonical} (present-day figures)\n\n"
            "Yahoo's figures. Each is a filing with a lag, stated beside it; none says what is happening today.\n\n"
            + "\n\n".join(sections))
