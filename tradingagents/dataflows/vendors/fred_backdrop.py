"""A lean macro backdrop from FRED: credit spreads, financial conditions, the next data releases (a local addition).

The macro tool already serves any FRED series on request; this one hands the news analyst the few that bear on risk
appetite without being asked, with the figures worked out, plus what is due in the next two weeks:

* the high-yield bond spread (ICE BofA US High Yield OAS) and the Chicago Fed financial conditions index, each with
  its change over a month and a quarter and where it sits in the past year;
* the dates of the next CPI, jobs, GDP, PCE and retail-sales releases (FRED does not carry FOMC dates);
* by the stock's profile (today's Yahoo profile, so not for past runs): yen per dollar for a Japanese company, and
  WTI crude and Henry Hub natural gas for energy and utilities.

Observations are read at the vintage known on the analysis date, as the macro tool does, so past runs see what was
published then. Macro context describes the backdrop; it is the noisiest input for a single stock and is labeled so.
"""

from datetime import date, timedelta
from typing import Annotated

import yfinance as yf

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import fred
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

ALWAYS = (("BAMLH0A0HYM2", "High-yield bond spread over Treasuries (ICE BofA US High Yield OAS)", "%"),
          ("NFCI", "Chicago Fed National Financial Conditions Index (negative is looser than average, positive tighter)", ""))
JAPAN = (("DEXJPUS", "Japanese yen per US dollar", ""),)
ENERGY = (("DCOILWTICO", "WTI crude oil, dollars per barrel", ""), ("DHHNGSP", "Henry Hub natural gas, dollars per million BTU", ""))
RELEASES = {10: "Consumer Price Index (CPI)", 50: "Employment Situation (jobs report)", 53: "Gross Domestic Product",
            54: "Personal Income and Outlays (PCE inflation)", 9: "Advance retail sales"}
CALENDAR_DAYS = 14
SOON_DAYS = 2
SPREAD_PERCENTILE_FLAG = 0.90
SPREAD_MONTH_RISE_FLAG = 0.5                  # percentage points of high-yield spread in a month


def _readings(series_id: str, as_of: str) -> list[tuple[str, float]]:
    """(date, value) pairs for the last 13 months at the vintage known on ``as_of``, oldest first."""
    pit = min(as_of, fred._fred_today())
    start = (date.fromisoformat(as_of) - timedelta(days=400)).isoformat()
    observations = fred._request("series/observations", {"series_id": series_id, "observation_start": start,
                                                         "observation_end": as_of, "realtime_start": pit, "realtime_end": pit})
    return [(o["date"], float(o["value"])) for o in observations.get("observations", []) if o.get("value") not in (".", "", None)]


def _release_dates(as_of: str) -> list[tuple[str, str]]:
    """(date, release name) for the releases due within the next ``CALENDAR_DAYS`` days, soonest first."""
    end = (date.fromisoformat(as_of) + timedelta(days=CALENDAR_DAYS)).isoformat()
    due = []
    for release_id, name in RELEASES.items():
        listed = fred._request("release/dates", {"release_id": release_id, "include_release_dates_with_no_data": "true",
                                                 "sort_order": "desc", "limit": 40, "realtime_start": "2025-01-01"})
        due += [(d["date"], name) for d in listed.get("release_dates", []) if as_of < d["date"] <= end]
    return sorted(due)


def _year_window(points: list[tuple[str, float]]) -> list[float]:
    day = date.fromisoformat(points[-1][0])
    return [v for d, v in points if date.fromisoformat(d) > day - timedelta(days=365)]


def standing(points: list[tuple[str, float]]) -> str:
    """Where the latest reading sits in the past year, in words ("above 100%" is never written)."""
    window, latest = _year_window(points), points[-1][1]
    if len(window) < 2 or max(window) == min(window):
        return "not different from the rest of the past year's readings"
    if latest >= max(window):
        return "the highest reading of the past year"
    if latest <= min(window):
        return "the lowest reading of the past year"
    return f"above {sum(v < latest for v in window) / len(window) * 100:.0f}% of the past year's readings"


def series_lines(label: str, unit: str, points: list[tuple[str, float]]) -> tuple[str, float | None, float | None]:
    """One line for a series, with its percentile in the past year and its change over a month."""
    latest_day, latest = points[-1]
    day = date.fromisoformat(latest_day)

    def earlier(days):
        old = [v for d, v in points if date.fromisoformat(d) <= day - timedelta(days=days)]
        return old[-1] if old else None

    window = _year_window(points)
    rank = sum(v < latest for v in window) / len(window) if window else None
    month, quarter = earlier(30), earlier(91)
    parts = []
    if month is not None:
        parts.append(f"{latest - month:+.2f} in a month")
    if quarter is not None:
        parts.append(f"{latest - quarter:+.2f} in a quarter")
    if window:
        parts.append(f"past-year range {min(window):.2f} to {max(window):.2f}, the latest is {standing(points)}")
    suffix = unit if unit else ""
    return (f"- {label}: {latest:.2f}{suffix} on {latest_day}; " + "; ".join(parts) + ".",
            rank, None if month is None else latest - month)


def build_report(as_of: str, groups: list[tuple[str, str, str, list]], releases: list[tuple[str, str]], note: str) -> str:
    """The report; ``groups`` are (series id, label, unit, points); a series that failed has no points."""
    lines, flags = [], []
    for series_id, label, unit, points in groups:
        if not points:
            lines.append(f"- {label}: not available this time.")
            continue
        line, rank, month_change = series_lines(label, unit, points)
        if series_id == "NFCI":
            # the index is near its own year's high while still loose in absolute terms; say which, so it is not misread
            line += f" Reading: conditions are {'tighter' if points[-1][1] > 0 else 'looser'} than the long-run average."
        lines.append(line)
        if series_id == "BAMLH0A0HYM2" and rank is not None and (rank >= SPREAD_PERCENTILE_FLAG or
                                                                  (month_change or 0) >= SPREAD_MONTH_RISE_FLAG):
            flags.append(f"High-yield spreads are {points[-1][1]:.2f}%, {standing(points)}"
                         + (f" and {month_change:+.2f} points in a month" if month_change is not None else "")
                         + ": credit markets are demanding more for risk.")
        if series_id == "NFCI" and points[-1][1] > 0:
            flags.append(f"The Chicago Fed financial conditions index is {points[-1][1]:+.2f}: conditions are tighter than average.")
    if releases:
        lines.append(f"- Data releases due in the next {CALENDAR_DAYS} days: " + "; ".join(f"{d} {n}" for d, n in releases) + ".")
        for release_date, name in releases:
            if (date.fromisoformat(release_date) - date.fromisoformat(as_of)).days <= SOON_DAYS:
                flags.append(f"{name} is due on {release_date}, within {SOON_DAYS} days.")
    else:
        lines.append(f"- No CPI, jobs, GDP, PCE or retail-sales release is due in the next {CALENDAR_DAYS} days.")
    notes = [note] if note else []
    notes.append("FOMC meeting dates are not in FRED's calendar. This is backdrop for the whole market; for a single stock it is "
                 "the noisiest input, so weigh it accordingly.")
    return (f"# Macro backdrop (FRED, as known on {as_of})\n\n" + "\n".join(lines) + "\n\n## Flags to state in the report\n"
            + ("\n".join(f"- {f}" for f in flags) if flags else "No rule-based flags.") + "\n\n" + " ".join(notes))


def get_macro_backdrop(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Credit spreads, financial conditions, the next data releases and sector-linked series, from FRED."""
    canonical = normalize_symbol(ticker)
    as_of = as_of_date or get_current_date()
    series = list(ALWAYS)
    note = ""
    if is_historical(as_of_date):
        note = "Series linked to the stock's country or sector are not shown for a past run (they come from today's profile)."
    else:
        try:
            info = yf_retry(lambda: yf.Ticker(canonical).info) or {}
        except VendorUnavailableError:
            info = {}
        if info.get("country") == "Japan":
            series += JAPAN
        if info.get("sectorKey") in ("energy", "utilities"):
            series += ENERGY
    groups = []
    for series_id, label, unit in series:
        try:
            groups.append((series_id, label, unit, _readings(series_id, as_of)))
        except (ValueError, VendorUnavailableError):
            groups.append((series_id, label, unit, []))
    try:
        releases = _release_dates(as_of)
    except (ValueError, VendorUnavailableError):
        releases = []
    return build_report(as_of, groups, releases, note)
