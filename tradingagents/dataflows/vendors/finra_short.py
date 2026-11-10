"""FINRA short-sale data: official short interest history, daily short-sale volume, and the threshold list.

A local addition, free and keyless, from FINRA's public files:

* Short interest, published twice a month about seven business days after each settlement date (the 15th or the
  business day before it, and the last business day), with the previous report, the average daily volume and
  FINRA's own days to cover. Yahoo shows only the latest report and the one a month before; this gives the path
  between and a days-to-cover that does not depend on Yahoo's volume figure.
* Daily short-sale volume: the share of a stock's volume reported to FINRA that was sold short. It includes
  market makers' hedging sales, so it is not short interest and its level means little; its change against the
  stock's own recent average is the information.
* The Nasdaq Reg SHO threshold list: Nasdaq-listed stocks with persistent failures to deliver.

Files are cached by name; a published file never changes.
"""

import csv
import io
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorUnavailableError

_INTEREST_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{day}.csv"
_DAILY_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day}.txt"
_THRESHOLD_URL = "https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{day}.txt"
_HEADERS = {"User-Agent": "Mozilla/5.0 (TradingAgents research)"}

PUBLICATION_LAG_DAYS = 11             # a settlement date's file is out by then (about seven business days)
INTEREST_REPORTS = 5
DAILY_SESSIONS = 20
RECENT_SESSIONS = 5
RATIO_RISE_POINTS = 0.10              # recent short-volume share above the 20-session average by this much
RISING_REPORTS = 3                    # consecutive increases
RISING_TOTAL = 0.5                    # and a rise of at least this much over them


def _cache_path(name: str) -> Path:
    return Path(get_config()["data_cache_dir"]) / "finra" / name


def _text(url: str, name: str) -> str | None:
    """The file's text, cached; None when it is not published (403 or 404); an outage raises."""
    path = _cache_path(name)
    if path.exists() and path.stat().st_size > 0:
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        response = requests.get(url, headers=_HEADERS, timeout=60)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"FINRA request failed ({type(exc).__name__})") from exc
    if response.status_code in (403, 404):
        return None
    if response.status_code != 200:
        raise VendorUnavailableError(f"FINRA answered HTTP {response.status_code}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")
    return response.text


def settlement_dates(as_of: date, count: int = INTEREST_REPORTS) -> list[date]:
    """The most recent settlement dates whose files should be published by ``as_of``, newest first."""
    horizon = as_of - timedelta(days=PUBLICATION_LAG_DAYS)
    found: list[date] = []
    month = date(horizon.year, horizon.month, 1)
    while len(found) < count + 2 and month.year >= horizon.year - 1:
        last = (pd.Timestamp(month) + pd.offsets.MonthEnd(0)).date()
        for target in (last, date(month.year, month.month, 15)):
            day = pd.Timestamp(target)
            while day.weekday() >= 5:
                day -= pd.Timedelta(days=1)
            if day.date() <= horizon:
                found.append(day.date())
        month = (pd.Timestamp(month) - pd.Timedelta(days=1)).date().replace(day=1)
    return sorted(set(found), reverse=True)[: count + 2]


def parse_interest(text: str, symbol: str) -> dict | None:
    """The row for ``symbol`` in a short-interest file, or None."""
    marker = f'|"{symbol}"|'
    start = text.find(marker)
    if start < 0:
        return None
    begin = text.rfind("\n", 0, start) + 1
    end = text.find("\n", start)
    row = next(csv.reader(io.StringIO(text[begin:end if end > 0 else None]), delimiter="|", quotechar='"'))
    if len(row) < 14:
        return None

    def number(value):
        try:
            return float(value)
        except ValueError:
            return None

    return {"settled": row[13], "short": number(row[5]), "previous": number(row[6]), "avg_volume": number(row[8]),
            "days_to_cover": number(row[9]), "change_percent": number(row[11])}


def parse_daily(text: str, symbol: str) -> tuple[float, float] | None:
    """(short volume, total volume) for ``symbol`` in a daily short-sale volume file, or None."""
    marker = f"|{symbol}|"
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("\n", start)
    fields = text[start + 1:end if end > 0 else None].split("|")
    try:
        return float(fields[1]), float(fields[3])
    except (IndexError, ValueError):
        return None


def fetch(symbol: str, as_of: date) -> dict:
    """The official short-interest rows, daily short volumes and threshold-list flag, newest first; parts may be empty."""
    interest = []
    for day in settlement_dates(as_of):
        text = _text(_INTEREST_URL.format(day=f"{day:%Y%m%d}"), f"shrt{day:%Y%m%d}.csv")
        row = parse_interest(text, symbol) if text else None
        if row:
            interest.append(row)
        if len(interest) >= INTEREST_REPORTS:
            break
    daily = []
    day = as_of - timedelta(days=1)                 # the newest file may not be out yet: start the day before
    for _ in range(45):
        if len(daily) >= DAILY_SESSIONS:
            break
        if day.weekday() < 5:
            text = _text(_DAILY_URL.format(day=f"{day:%Y%m%d}"), f"CNMSshvol{day:%Y%m%d}.txt")
            parsed = parse_daily(text, symbol) if text else None
            if parsed and parsed[1] > 0:
                daily.append((day.isoformat(), parsed[0], parsed[1]))
        day -= timedelta(days=1)
    threshold = None
    for offset in range(0, 5):
        probe = as_of - timedelta(days=offset)
        if probe.weekday() >= 5:
            continue
        text = _text(_THRESHOLD_URL.format(day=f"{probe:%Y%m%d}"), f"nasdaqth{probe:%Y%m%d}.txt")
        if text:
            threshold = (probe.isoformat(), any(line.startswith(f"{symbol}|") for line in text.splitlines()))
            break
    return {"interest": interest, "daily": daily, "threshold": threshold}


def build_section(symbol: str, data: dict) -> tuple[list[str], list[str]]:
    """The report lines and flags from ``fetch`` output."""
    lines, flags = [], []
    rows = data.get("interest") or []
    if rows:
        lines += ["Official short interest (FINRA, by settlement date):", "| Settlement | Shares short | Change on the prior report | "
                  "Average daily volume | Days to cover |", "|---|---|---|---|---|"]
        for row in rows:
            change = "n/a" if row["change_percent"] is None else f"{row['change_percent']:+.1f}%"
            avg = "n/a" if row["avg_volume"] is None else f"{row['avg_volume']:,.0f}"
            dtc = "n/a" if row["days_to_cover"] is None else f"{row['days_to_cover']:.2f}"
            lines.append(f"| {row['settled']} | {row['short']:,.0f} | {change} | {avg} | {dtc} |")
        oldest_first = [r["short"] for r in reversed(rows) if r["short"] is not None]
        rises = 0
        for earlier, later in zip(oldest_first, oldest_first[1:], strict=False):
            rises = rises + 1 if later > earlier else 0
        if rises >= RISING_REPORTS and oldest_first[-1 - rises] > 0 and oldest_first[-1] / oldest_first[-1 - rises] - 1 >= RISING_TOTAL:
            start = oldest_first[-1 - rises]
            flags.append(f"Short interest rose in each of the last {rises} reports ({start:,.0f} to {oldest_first[-1]:,.0f}, "
                         f"{(oldest_first[-1] / start - 1) * 100:+.0f}%).")
        lines.append("FINRA publishes each report about seven business days after its settlement date, so the newest is "
                     "already weeks old when it appears.")
    else:
        lines.append("No FINRA short-interest report was found for this symbol.")
    daily = data.get("daily") or []
    if len(daily) >= RECENT_SESSIONS + 5:
        ratios = [short / total for _, short, total in daily]                   # newest first
        recent, base = sum(ratios[:RECENT_SESSIONS]) / RECENT_SESSIONS, sum(ratios) / len(ratios)
        lines.append(f"Daily short-sale volume (FINRA, off-exchange trades; it includes market makers' hedging sales, so it is not "
                     f"short interest): {ratios[0] * 100:.0f}% of the volume on {daily[0][0]}; {recent * 100:.0f}% over the last "
                     f"{RECENT_SESSIONS} sessions against {base * 100:.0f}% over the last {len(ratios)}.")
        if recent - base >= RATIO_RISE_POINTS:
            flags.append(f"The short-sale share of volume has risen: {recent * 100:.0f}% over the last {RECENT_SESSIONS} sessions "
                         f"against {base * 100:.0f}% over the last {len(ratios)}.")
    else:
        lines.append("Too few daily short-sale volume files were found to report a trend.")
    threshold = data.get("threshold")
    if threshold:
        lines.append(f"Nasdaq Reg SHO threshold list on {threshold[0]}: {'on the list' if threshold[1] else 'not on the list'} "
                     "(the list covers Nasdaq-listed stocks only).")
        if threshold[1]:
            flags.append(f"{symbol} is on the Nasdaq Reg SHO threshold list: it has had persistent failures to deliver shares.")
    return lines, flags
