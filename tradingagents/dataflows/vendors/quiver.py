"""Quiver Quantitative vendor: Congress stock trades and SEC Form 4 insider transactions.

A local addition. Both datasets carry the date a trade became public, so a run dated in
the past is served only what had been disclosed by then.

* Congress trades: ``historical/congresstrading/<ticker>`` (every plan).
* Insider transactions: ``live/insiders`` (a higher plan); when the plan refuses it, the
  ticker endpoint ``mobile/stock/<ticker>/insiders`` is tried.

The key comes from ``QUIVER_API_KEY`` in the environment and is sent only as a header,
so it never appears in a URL or an error message.
"""

import logging
import os
import re
from statistics import median
from typing import Annotated, Any

import pandas as pd
import requests

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

API_BASE = "https://api.quiverquant.com/beta"
KEY_ENV = "QUIVER_API_KEY"

CONGRESS_LOOKBACK_DAYS = 180
INSIDER_LOOKBACK_DAYS = 120
CONGRESS_ROWS_SHOWN = 40
INSIDER_ROWS_SHOWN = 40
PAGE_SIZE = 200
MAX_PAGES = 10

# Quiver files Alphabet's Form 4s under GOOGL only, while Congress members report either
# class, so the two datasets need different symbols.
_INSIDER_SYMBOL = {"GOOG": "GOOGL", "BRK-B": "BRK.B", "BRK-A": "BRK.A"}
_CONGRESS_SYMBOLS = {
    "GOOG": ("GOOG", "GOOGL"),
    "GOOGL": ("GOOG", "GOOGL"),
    "BRK-B": ("BRK.B",),
    "BRK-A": ("BRK.A",),
}

_CODE_MEANING = {
    "S": "open-market sale",
    "P": "open-market purchase",
    "A": "award or grant",
    "F": "shares withheld for taxes",
    "M": "option or derivative exercise",
    "X": "option exercise",
    "G": "gift",
    "C": "conversion",
    "D": "disposition to the company",
    "J": "other",
}


class QuiverPlanError(VendorUnavailableError):
    """Quiver refused a dataset because the account's plan does not include it."""


def _key() -> str:
    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        raise VendorNotConfiguredError(f"{KEY_ENV} is not set; add your Quiver API token to .env")
    return key


def _get(path: str, params: dict | None = None) -> Any:
    """GET one Quiver endpoint and return its decoded JSON; an empty list for 404."""
    try:
        response = requests.get(
            f"{API_BASE}/{path}",
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {_key()}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        # Only the exception's type is kept: its text can quote the request.
        raise VendorUnavailableError(f"Quiver request failed ({type(exc).__name__})") from exc
    status = response.status_code
    if status == 401:
        raise VendorNotConfiguredError(f"Quiver rejected the token in {KEY_ENV} (HTTP 401)")
    if status == 403:
        raise QuiverPlanError("Quiver refused the request (HTTP 403): the plan may not include this dataset")
    if status == 404:
        return []
    if status == 429:
        raise VendorUnavailableError("Quiver rate limit reached (HTTP 429)")
    if status >= 400:
        raise VendorUnavailableError(f"Quiver answered HTTP {status}")
    try:
        return response.json()
    except ValueError as exc:
        raise VendorUnavailableError("Quiver returned an unreadable response") from exc


def _rows(payload: Any) -> list[dict]:
    """The list of records inside a response, whatever wrapper it comes in."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for name in ("insider_trades", "data", "results", "items", "trades", "transactions", "insiders"):
            if isinstance(payload.get(name), list):
                return [row for row in payload[name] if isinstance(row, dict)]
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [row for row in value if isinstance(row, dict)]
    return []


def _pick(row: dict, *names: str) -> Any:
    """A field by any of several names, ignoring case and underscores (ReportDate, report_date)."""
    flat = {str(k).lower().replace("_", ""): v for k, v in row.items()}
    for name in names:
        value = flat.get(name.lower().replace("_", ""))
        if value not in (None, ""):
            return value
    return None


def _date(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    stamp = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(stamp):
        return None
    return stamp.tz_localize(None).normalize() if stamp.tzinfo else stamp.normalize()


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _range_low(text: Any) -> float | None:
    """The lower bound of a disclosure range such as '$1,001 - $15,000'."""
    match = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", str(text or ""))
    return float(match.group(1).replace(",", "")) if match else None


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1e9:
        return f"${value / 1e9:.1f}B"
    if abs(value) >= 1e6:
        return f"${value / 1e6:.1f}M"
    if abs(value) >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:,.0f}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _chamber(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith(("rep", "house")):
        return "House"
    return "Senate" if lowered.startswith("sen") else text


def _party(text: str) -> str:
    return text[:1].upper() if text else ""


def _is_option(ticker_type: Any, description: Any) -> bool:
    """Whether a Congress record is an option contract rather than shares."""
    kind = str(ticker_type or "").lower()
    return kind == "op" or "option" in kind or "OPTIONS" in str(description or "").upper()


def _code(text: Any) -> str:
    """A Form 4 transaction as a one-letter code; Quiver's insider endpoint gives words."""
    word = str(text or "").strip().lower()
    if word == "s" or word.startswith("sale"):
        return "S"
    if word == "p" or word.startswith("purchase"):
        return "P"
    return word[:1].upper() or "?"


# Endpoints the plan has refused during this process, so each later call skips them.
_REFUSED: set[str] = set()


def _us_listed(symbol: str) -> bool:
    if not symbol or symbol.startswith("^") or symbol.endswith("=X") or symbol.endswith("=F"):
        return False
    if symbol.endswith("-USD"):
        return False
    return "." not in symbol or symbol in {"BRK.A", "BRK.B"}


def _not_covered(label: str, symbol: str) -> str:
    return (f"# {label} for {symbol}\n\nQuiver covers US-listed stocks and ETFs only, so there is "
            f"no {label.lower()} for '{symbol}'.")


def get_congress_trades(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Trades by members of Congress in ``ticker``, as disclosed in the last 180 days."""
    symbol = normalize_symbol(ticker)
    if not _us_listed(symbol):
        return _not_covered("Congress trades", symbol)
    symbols = _CONGRESS_SYMBOLS.get(symbol, (symbol,))
    records: list[dict] = []
    for name in symbols:
        records += _rows(_get(f"historical/congresstrading/{name}"))

    end = _date(as_of_date) if as_of_date else pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=CONGRESS_LOOKBACK_DAYS)
    trades = []
    for row in records:
        filed = _date(_pick(row, "ReportDate", "Filed", "DisclosureDate"))
        traded = _date(_pick(row, "TransactionDate", "Traded", "Date"))
        if filed is None and traded is None:
            continue
        known = filed or traded
        if known > end or known < start:      # not yet public on the analysis date, or too old
            continue
        label = str(_pick(row, "Transaction", "Type") or "").strip()
        low = _range_low(_pick(row, "Range")) or _number(_pick(row, "Amount"))
        description = str(_pick(row, "Description") or "").strip()
        option = _is_option(_pick(row, "TickerType"), description)
        trades.append({
            "filed": filed, "traded": traded, "who": str(_pick(row, "Representative", "Name") or "unknown").strip(),
            "party": _party(str(_pick(row, "Party") or "").strip()),
            "chamber": _chamber(str(_pick(row, "House", "Chamber") or "").strip()),
            "kind": label + (" (options)" if option else ""), "option": option, "description": description,
            "range": str(_pick(row, "Range") or _money(_number(_pick(row, "Amount")))),
            "low": low, "move": _number(_pick(row, "PriceChange")), "symbol": str(_pick(row, "Ticker") or symbol),
        })
    window = f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"
    title = " / ".join(symbols)
    if not trades:
        return (f"# Congress trades in {title}, disclosures filed {window}\n\n"
                "No disclosed trades by members of Congress in this window.")

    trades.sort(key=lambda t: (t["filed"] or t["traded"]), reverse=True)
    lags = [(t["filed"] - t["traded"]).days for t in trades if t["filed"] is not None and t["traded"] is not None]
    buys = [t for t in trades if t["kind"].lower().startswith("purchase")]
    sells = [t for t in trades if t["kind"].lower().startswith("sale")]
    newest = trades[0]["filed"] or trades[0]["traded"]
    live_run = not is_historical(as_of_date)     # price change since a trade would leak the future into a past run
    options = sum(1 for t in trades if t["option"])
    lines = [
        f"# Congress trades in {title}, disclosures filed {window}",
        "",
        "Members must disclose a trade within 45 days, so these trades are already weeks old when they appear. "
        "Amounts are ranges; dollar totals use each range's lower bound.",
        "",
        f"- Disclosures: {len(trades)}, of which {_plural(len(buys), 'purchase')} (at least {_money(sum(t['low'] or 0 for t in buys))}) "
        f"and {_plural(len(sells), 'sale')} (at least {_money(sum(t['low'] or 0 for t in sells))})."
        + (f" {_plural(options, 'row')} {'involves' if options == 1 else 'involve'} option contracts, not shares."
           if options else ""),
        f"- Typical delay between trade and filing: {median(lags):.0f} days (longest {max(lags)})." if lags else
        "- Delay between trade and filing: not available.",
        f"- Newest filing: {newest:%Y-%m-%d}, {(end - newest).days} days before {end:%Y-%m-%d}.",
        "",
        "| Filed | Traded | Days late | Member | Transaction | Amount range |" + (" Stock move since trade |" if live_run else ""),
        "|---|---|---:|---|---|---|" + ("---:|" if live_run else ""),
    ]
    for t in trades[:CONGRESS_ROWS_SHOWN]:
        filed, traded = t["filed"], t["traded"]
        late = (filed - traded).days if filed is not None and traded is not None else None
        member = t["who"] + (f" ({t['party']}-{t['chamber']})" if t["party"] else "")
        if len(symbols) > 1:                   # two share classes were merged; say which one traded
            member += f" [{t['symbol']}]"
        cells = [
            "n/a" if filed is None else filed.strftime("%Y-%m-%d"),
            "n/a" if traded is None else traded.strftime("%Y-%m-%d"),
            "n/a" if late is None else str(late),
            member, t["kind"] or "n/a", t["range"],
        ]
        if live_run:
            cells.append("n/a" if t["move"] is None else f"{t['move']:+.1f}%")
        lines.append("| " + " | ".join(cells) + " |")
    if len(trades) > CONGRESS_ROWS_SHOWN:
        lines.append(f"\n{len(trades) - CONGRESS_ROWS_SHOWN} older disclosures in the window are not listed.")
    notes = [
        f"- {t['who']}, filed {t['filed']:%Y-%m-%d}: {t['description'][:150]}"
        for t in trades[:CONGRESS_ROWS_SHOWN] if t["option"] and t["description"] and t["filed"] is not None
    ][:5]
    if notes:
        lines += ["", "Option contracts (not shares):"] + notes
    if live_run:
        lines += ["", "'Stock move since trade' is the stock's price change from the trade date to now, as Quiver "
                      "reports it. It is not what someone buying on the filing date would have earned."]
    return "\n".join(lines)


def _fetch_insider_rows(symbol: str, start: pd.Timestamp) -> list[dict]:
    """Insider records for ``symbol``, from the plan's best endpoint, paging as needed.

    The ticker endpoint lists open-market sales and purchases, newest filing first, so
    paging stops once a page reaches filings older than the window.
    """
    attempts = (
        ("live/insiders", lambda page: {"ticker": symbol, "page": page, "page_size": PAGE_SIZE}),
        (f"mobile/stock/{symbol}/insiders", lambda page: {"page": page, "page_size": PAGE_SIZE}),
    )
    refusal: VendorUnavailableError | None = None
    for path, params in attempts:
        if path in _REFUSED:
            continue
        rows: list[dict] = []
        try:
            for page in range(1, MAX_PAGES + 1):
                batch = _rows(_get(path, params(page)))
                rows += batch
                filed = [d for d in (_date(_pick(r, "FileDate", "Filed", "FilingDate", "Uploaded")) for r in batch)
                         if d is not None]
                if len(batch) < PAGE_SIZE or (filed and min(filed) < start - pd.Timedelta(days=7)):
                    break
        except QuiverPlanError as exc:
            _REFUSED.add(path)
            refusal = exc
            continue
        return rows
    raise refusal or QuiverPlanError("Quiver's insider endpoints are not part of the plan")


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date, yyyy-mm-dd"] = None,
):
    """SEC Form 4 insider transactions for ``ticker`` filed in the last 120 days."""
    canonical = normalize_symbol(ticker)
    if not _us_listed(canonical):
        return _not_covered("Insider transactions", canonical)
    symbol = _INSIDER_SYMBOL.get(canonical, canonical)
    end = _date(as_of_date) if as_of_date else pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=INSIDER_LOOKBACK_DAYS)
    records = _fetch_insider_rows(symbol, start)
    historical = is_historical(as_of_date)
    parsed, undated = [], 0
    for row in records:
        traded = _date(_pick(row, "TransactionDate", "Date", "TradeDate"))
        filed = _date(_pick(row, "FileDate", "Filed", "FilingDate", "Uploaded"))
        if traded is None:
            continue
        if filed is None and historical:
            undated += 1
            continue                           # cannot say it was public on the run date
        if (filed or traded) > end or traded < start:
            continue
        code = _code(_pick(row, "TransactionType", "TransactionCode", "Code"))
        shares = _number(_pick(row, "Shares", "TransactionShares"))
        price = _number(_pick(row, "PricePerShare", "Price", "TransactionPricePerShare"))
        parsed.append({
            "traded": traded, "filed": filed, "code": code, "shares": shares, "price": price,
            "value": shares * price if shares and price and price > 0 else None,
            "who": str(_pick(row, "OwnerName", "Name", "Owner", "Insider") or "unknown").strip().title(),
            "title": str(_pick(row, "Title", "OfficerTitle") or "").strip(),
            "director": bool(_pick(row, "IsDirector")),
            "after": _number(_pick(row, "SharesAfter", "SharesOwnedFollowing", "SharesOwnedAfter")),
        })
    window = f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"
    if historical and not parsed and undated:
        return (f"# Insider transactions for {canonical}\n# Point-in-time as of: {as_of_date}\n\n"
                "These records carry no filing date, so they cannot show which trades were public on that date.")
    if not parsed:
        return (f"# Insider transactions for {canonical}, trades dated {window}\n\n"
                "No Form 4 filings in this window.")

    parsed.sort(key=lambda t: t["traded"], reverse=True)
    sales = [t for t in parsed if t["code"] == "S"]
    buys = [t for t in parsed if t["code"] == "P"]
    others: dict[str, int] = {}
    for t in parsed:
        if t["code"] not in ("S", "P"):
            others[t["code"]] = others.get(t["code"], 0) + 1
    lines = [
        f"# Insider transactions for {canonical}, trades dated {window} (Quiver, SEC Form 4)",
        "",
        f"- Open-market purchases: {len(buys)}, about {_money(sum(t['value'] or 0 for t in buys))} "
        f"by {_plural(len({t['who'] for t in buys}), 'insider')}.",
        f"- Open-market sales: {len(sales)}, about {_money(sum(t['value'] or 0 for t in sales))} "
        f"by {_plural(len({t['who'] for t in sales}), 'insider')}.",
        ("- Other filings (not open-market): " + ", ".join(
            f"{n} x {_CODE_MEANING.get(c, 'code ' + c)}" for c, n in sorted(others.items(), key=lambda kv: -kv[1]))
         + ". Tax withholding, awards, gifts and option exercises are routine and not listed below.")
        if others else
        "- This feed lists open-market sales and purchases only; tax withholding, awards, gifts and option "
        "exercises are not included.",
        "- Sales are not automatically bearish: many follow pre-set plans, taxes or diversification.",
    ]
    shown = sorted(sales + buys, key=lambda t: t["traded"], reverse=True)
    if shown:
        lines += ["", "| Traded | Filed | Insider | Role | Type | Shares | Price | Value | Owned after |",
                  "|---|---|---|---|---|---:|---:|---:|---:|"]
        for t in shown[:INSIDER_ROWS_SHOWN]:
            role = t["title"] or ("Director" if t["director"] else "")
            filed_text = "n/a" if t["filed"] is None else t["filed"].strftime("%Y-%m-%d")
            shares_text = "n/a" if t["shares"] is None else format(t["shares"], ",.0f")
            price_text = "n/a" if t["price"] is None else format(t["price"], ",.2f")
            after_text = "n/a" if t["after"] is None else format(t["after"], ",.0f")
            lines.append(
                f"| {t['traded'].strftime('%Y-%m-%d')} | {filed_text} | {t['who']} | {role} "
                f"| {_CODE_MEANING[t['code']]} | {shares_text} | {price_text} | {_money(t['value'])} | {after_text} |"
            )
        if len(shown) > INSIDER_ROWS_SHOWN:
            lines.append(f"\n{len(shown) - INSIDER_ROWS_SHOWN} older open-market filings in the window are not listed.")
    return "\n".join(lines)
