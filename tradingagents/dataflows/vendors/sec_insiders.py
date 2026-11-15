"""Insider trades read straight from SEC EDGAR Form 4 filings (a local addition).

Every officer, director and 10 percent owner reports each trade in the company's shares on a Form 4 within two business days. EDGAR
lists them, free and with no account, and dates every one by the day it was filed, so a run dated in the past is served only the
filings that were public by then (Yahoo and Alpha Vantage date a trade only by when it happened, so they must withhold them).

The report separates open-market purchases and sales, which are the insider's own choice, from the routine entries (awards, shares
withheld for taxes, option exercises, gifts), and marks sales made under a pre-arranged Rule 10b5-1 plan. It adds the net amount
over the last 60 days (the live window) and notes when several different insiders bought within 30 days and the run is recent
enough (it ended within 90 days) to count. Every line carries its age in days, counted from the trade date. It names the latest move
(the newest purchase or unplanned sale, since a planned sale was set long before) and the insiders who sold after buying, and a
purchase that the same insider later reversed by selling is left out of the cluster. The dollar total over the window ignores order.
Each purchase and sale shows how much it changed the insider's holding that day, and the live purchases outside batches and
reversals are graded token, modest or large by the share they added to the buyer's holding.
It describes filings; it does not say how they should be read beyond the caveats any reader of insider data should have.

Each filing is read once and kept in the cache folder (a filing never changes), and requests are spaced out the way the SEC asks.
Nothing needs a key. Set ``SEC_EDGAR_USER_AGENT`` to your name and address, as the SEC asks of everyone who reads EDGAR by program.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import sec_edgar

WINDOW_DAYS = 180                 # trades dated this far back are reported
LIVE_DAYS = 60                    # a trade this recent is the live signal; an older one in the window is context only
CLUSTER_DAYS = 30                 # this many days: different insiders buying within it make a cluster
CLUSTER_RECENT_DAYS = 90          # a cluster counts as current only when its last purchase is this recent
BATCH_INSIDERS = 3                # this many insiders buying at exactly the same price on the same day is a batch, not separate decisions
BATCH_SHARE = 0.8                 # a cluster is likely routine when this share of its purchases were made in such batches
TOKEN_PCT = 1.0                   # a purchase adding less than this percent to the buyer's holdings is a token one
LARGE_PCT = 10.0                  # a purchase adding at least this percent is a large one (judgment, from one practitioner study)
CLUSTER_INSIDERS = 3
MAX_FILINGS = 80                  # the newest Form 4s read in one report (each is one request the first time)
ROWS_SHOWN = 30
WORKERS = 4
MAX_FAILURES = 4                  # after this many filings EDGAR would not return, the rest are not tried
MIN_INTERVAL = 0.13               # seconds between request starts, the SEC's limit being ten a second
MAX_BYTES = 1_000_000             # a real Form 4 is a few thousand bytes; anything near this is not one
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
_FORMS = ("4", "4/A")

CODE_MEANING = {
    "P": "open-market purchase", "S": "open-market sale", "A": "award or grant", "F": "shares withheld for taxes",
    "M": "option or derivative exercise", "X": "option exercise", "G": "gift", "C": "conversion", "D": "disposition to the company",
    "I": "discretionary transaction", "W": "inheritance", "J": "other", "K": "equity swap", "L": "small acquisition",
    "O": "out-of-the-money exercise", "U": "tender of shares", "V": "voluntarily reported", "Z": "trust deposit or withdrawal",
}
_PLAN = re.compile(r"10b5-?1", re.IGNORECASE)

_limiter_lock = threading.Lock()
_next_start = [0.0]


def _wait_turn() -> None:
    """Space request starts across all threads by ``MIN_INTERVAL``."""
    with _limiter_lock:
        now = time.monotonic()
        delay = max(0.0, _next_start[0] - now)
        _next_start[0] = max(now, _next_start[0]) + MIN_INTERVAL
    if delay:
        time.sleep(delay)


def _fetch_text(url: str, retries: int = 2) -> str:
    """The text at ``url`` from EDGAR, identifying us as the SEC requires; retries a rate limit or server error."""
    last = None
    for attempt in range(retries + 1):
        _wait_turn()
        try:
            response = requests.get(url, headers={"User-Agent": sec_edgar._user_agent()}, timeout=30)
        except requests.RequestException as exc:
            last = exc
        else:
            if response.status_code == 200:
                if len(response.content) > MAX_BYTES:
                    raise VendorUnavailableError("a Form 4 from SEC EDGAR was larger than any real one; it was not read")
                return response.text
            last = RuntimeError(f"HTTP {response.status_code}")
            if response.status_code not in (429, 500, 502, 503, 504):
                break
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    raise VendorUnavailableError(f"SEC EDGAR did not return a Form 4 ({type(last).__name__}: {str(last)[:80]})")


# ---------------------------------------------------------------- reading one filing
def _text(node, path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _is_true(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "yes")


def parse_form4(xml_text: str) -> dict | None:
    """One Form 4 as plain data, or None when it is not an ownership document.

    Only the non-derivative table is read (shares, as opposed to options and other derivatives)."""
    # A Form 4 never declares entities; refusing a document that does keeps a hostile one from expanding into memory.
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return None
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument":
        return None
    owners, relation = [], {"director": False, "officer": False, "title": "", "ten_percent": False, "other": ""}
    for owner in root.findall("reportingOwner"):
        name = _text(owner, "reportingOwnerId/rptOwnerName")
        if name:
            owners.append(name)
        rel = owner.find("reportingOwnerRelationship")
        relation["director"] |= _is_true(_text(rel, "isDirector"))
        relation["officer"] |= _is_true(_text(rel, "isOfficer"))
        relation["ten_percent"] |= _is_true(_text(rel, "isTenPercentOwner"))
        relation["title"] = relation["title"] or _text(rel, "officerTitle")
        relation["other"] = relation["other"] or _text(rel, "otherText")
    footnotes = {n.get("id"): (n.text or "") for n in root.findall("footnotes/footnote")}
    plan_wide = _is_true(_text(root, "aff10b5One"))
    trades = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        refs = {f.get("id") for f in tx.iter("footnoteId")}
        planned = plan_wide or any(_PLAN.search(footnotes.get(ref, "")) for ref in refs)
        trades.append({
            "traded": _text(tx, "transactionDate/value")[:10], "code": _text(tx, "transactionCoding/transactionCode"),
            "shares": _number(_text(tx, "transactionAmounts/transactionShares/value")),
            "price": _number(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
            "acquired": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "after": _number(_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            "held": _text(tx, "ownershipNature/directOrIndirectOwnership/value"), "planned": planned,
            "security": _text(tx, "securityTitle/value")})
    issuer = _text(root, "issuer/issuerCik")
    return {"issuer_cik": int(issuer) if issuer.isdigit() else None, "owners": owners, "relation": relation,
            "period": _text(root, "periodOfReport"), "trades": trades}


# ---------------------------------------------------------------- finding and caching the filings
def _cache_path(accession: str) -> Path:
    return Path(get_config()["data_cache_dir"]) / "sec_edgar" / "form4" / f"{accession}.json"


def _read_cached(accession: str) -> dict | None:
    path = _cache_path(accession)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if "issuer_cik" in data else None          # an entry from before the issuer was kept is read again
    return None


def _save_cached(accession: str, data: dict) -> None:
    path = _cache_path(accession)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, lambda temp: Path(temp).write_text(json.dumps(data), encoding="utf-8"))
    except OSError:
        pass                                   # a cache that cannot be written only costs a second download


def _document(primary: str) -> str:
    """The raw XML file beside the styled page EDGAR lists (``xslF345X06/form4.xml`` is a view of ``form4.xml``)."""
    return re.sub(r"^xsl[^/]*/", "", primary or "")


def _filing(cik: str, entry: dict) -> dict | None:
    """One Form 4 (parsed), from the cache or EDGAR; None when it cannot be read."""
    accession = entry["accessionNumber"]
    cached = _read_cached(accession)
    if cached is not None:
        return cached
    document = _document(entry.get("primaryDocument") or "")
    if not document:
        return None
    url = _ARCHIVE_URL.format(cik=int(cik), accession=accession.replace("-", ""), document=document)
    parsed = parse_form4(_fetch_text(url))
    if parsed is None:
        return None
    parsed.update(accession=accession, filed=entry["filingDate"], amended=entry["form"] == "4/A")
    _save_cached(accession, parsed)
    return parsed


def _entries(submissions: dict, since: str, as_of: str) -> list[dict]:
    """Form 4 and 4/A entries of the issuer's filing list filed from ``since`` to ``as_of``, newest first."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "accessionNumber", "primaryDocument")
    columns = [recent.get(key) or [] for key in keys]
    rows = [dict(zip(keys, row, strict=False)) for row in zip(*columns, strict=False)]
    rows = [r for r in rows if r["form"] in _FORMS and since <= r["filingDate"] <= as_of]
    return sorted(rows, key=lambda r: (r["filingDate"], r["accessionNumber"]), reverse=True)


# ---------------------------------------------------------------- summarizing
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


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _role(relation: dict) -> str:
    if relation.get("title"):
        return relation["title"]
    if relation.get("director"):
        return "Director"
    if relation.get("ten_percent"):
        return "10% owner"
    return relation.get("other") or ("Officer" if relation.get("officer") else "")


def _add_holdings_change(trades: list[dict]) -> None:
    """Give each purchase and sale ``change``: the percent the insider's holding of that security grew (a purchase) or shrank (a sale)
    over that day, or None when it cannot be worked out, or infinity for a first purchase (nothing was owned before).

    An insider fills a purchase in several lines at different prices, so the lines of one insider and one day are added up: the
    holding before is the holding after the last line less everything bought (or plus everything sold). Only shares the form lists
    as owned count, so options and other derivatives are not in the holding, and the figure is approximate.
    """
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        if t["code"] in ("P", "S"):
            groups.setdefault((t["who"], t["traded"], t["code"], t["security"], t["held"]), []).append(t)
    for group in groups.values():
        shares, owned = [t["shares"] for t in group], [t["after"] for t in group]
        change = None
        if all(s is not None and s > 0 for s in shares) and all(a is not None for a in owned):
            total = sum(shares)
            bought = group[0]["code"] == "P"
            before = max(owned) - total if bought else min(owned) + total
            if before > 0:
                change = total / before * 100
            elif before == 0 and bought:
                change = math.inf
        for t in group:
            t["change"] = change


def flatten(filings: list[dict], start: str, end: str) -> list[dict]:
    """Every non-derivative trade dated ``start`` to ``end`` in the filings, one dict each, with who made it and when it was filed."""
    unique: dict[tuple, dict] = {}
    # oldest filing first, so an amendment (Form 4/A) that restates a trade replaces the original instead of counting it twice
    for f in sorted(filings, key=lambda f: (f["filed"], f.get("accession", ""))):
        who = " and ".join(f["owners"]) or "unknown"
        for t in f["trades"]:
            if not t["traded"] or not start <= t["traded"] <= end or not t["code"]:
                continue
            value = t["shares"] * t["price"] if t["shares"] and t["price"] and t["price"] > 0 else None
            key = (who, t["traded"], t["code"], t["shares"], t["price"], t["after"], t["security"], t["held"])
            unique[key] = {**t, "who": who.title(), "role": _role(f["relation"]), "filed": f["filed"], "value": value,
                           "amended": f.get("amended", False)}
    trades = sorted(unique.values(), key=lambda t: (t["traded"], t["filed"]), reverse=True)
    _add_holdings_change(trades)
    return trades


def _age(day: str, as_of: str) -> int:
    """Days from a trade date to the run date."""
    return (date.fromisoformat(as_of) - date.fromisoformat(day)).days


def _ago(days: int) -> str:
    return "today" if days <= 0 else f"{days} day{'' if days == 1 else 's'} ago"


def cluster(buys: list[dict]) -> list[dict] | None:
    """The newest run of purchases within ``CLUSTER_DAYS`` by at least ``CLUSTER_INSIDERS`` different insiders (the run with the
    most insiders among those ending on the same day), or None."""
    ordered = sorted(buys, key=lambda t: t["traded"])
    best: list[dict] | None = None
    best_key: tuple[str, int] | None = None
    for i, first in enumerate(ordered):
        end = (date.fromisoformat(first["traded"]) + timedelta(days=CLUSTER_DAYS)).isoformat()
        window = [t for t in ordered[i:] if t["traded"] <= end]
        count = len({t["who"] for t in window})
        if count < CLUSTER_INSIDERS:
            continue
        key = (window[-1]["traded"], count)
        if best_key is None or key > best_key:
            best, best_key = window, key
    return best


def reversals(buys: list[dict], sales: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Split the purchases into those still standing and those the same insider reversed by selling afterwards.

    A purchase is reversed when the same insider made an unplanned sale dated after it (a planned sale was set long before and says
    nothing about a change of mind). Returns (standing purchases, {insider: what was reversed}), where the second value gives the
    reversed purchases' total and last date and the sales that followed: their total and the first date.
    """
    sale_days: dict[str, list[dict]] = {}
    for t in sales:
        if not t["planned"]:
            sale_days.setdefault(t["who"], []).append(t)
    standing: list[dict] = []
    reversed_by: dict[str, list[dict]] = {}
    for t in buys:
        if any(s["traded"] > t["traded"] for s in sale_days.get(t["who"], ())):
            reversed_by.setdefault(t["who"], []).append(t)
        else:
            standing.append(t)
    found: dict[str, dict] = {}
    for who, bought in reversed_by.items():
        first_bought = min(t["traded"] for t in bought)
        after = [s for s in sale_days[who] if s["traded"] > first_bought]
        found[who] = {"bought_value": sum(t["value"] or 0 for t in bought), "bought_last": max(t["traded"] for t in bought),
                      "sold_value": sum(s["value"] or 0 for s in after), "sold_first": min(s["traded"] for s in after)}
    return standing, found


def _batch_days(purchases: list[dict]) -> dict[tuple[str, float], list[dict]]:
    """The (day, price) pairs at which ``BATCH_INSIDERS`` or more different insiders bought at exactly the same price, with their trades."""
    days: dict[tuple[str, float], list[dict]] = {}
    for t in purchases:
        if t["price"] is not None:
            days.setdefault((t["traded"], round(t["price"], 2)), []).append(t)
    return {key: trades for key, trades in days.items() if len({t["who"] for t in trades}) >= BATCH_INSIDERS}


def purchase_sizes(standing: list[dict], buys: list[dict], as_of: str) -> dict:
    """The live purchases (the last ``LIVE_DAYS`` days) that count, graded by the share they added to the buyer's holding.

    Left out: purchases made in a batch (a scheduled program, see ``routine_batches``) and purchases the same insider later reversed
    by selling (``standing`` is what ``reversals`` kept). An insider's fills on one day count once. A grade is token (under
    ``TOKEN_PCT`` percent of the holding), modest, or large (``LARGE_PCT`` percent or more, or a first purchase).
    """
    cut = (date.fromisoformat(as_of) - timedelta(days=LIVE_DAYS)).isoformat()
    batches = _batch_days(buys)
    days: dict[tuple[str, str], float | None] = {}
    for t in standing:
        if t["traded"] < cut or (t["price"] is not None and (t["traded"], round(t["price"], 2)) in batches):
            continue
        known = [c for c in (days.get((t["who"], t["traded"])), t.get("change")) if c is not None]
        days[(t["who"], t["traded"])] = max(known) if known else None
    grades = {"large": 0, "modest": 0, "token": 0, "unknown": 0}
    for change in days.values():
        if change is None:
            grades["unknown"] += 1
        elif change >= LARGE_PCT:
            grades["large"] += 1
        elif change >= TOKEN_PCT:
            grades["modest"] += 1
        else:
            grades["token"] += 1
    sized = [(c, day, who) for (who, day), c in days.items() if c is not None]
    largest = max(sized, key=lambda s: (s[0], s[1])) if sized else None
    return {**grades, "count": len(days), "largest": largest}


def _pct(change: float) -> str:
    return "a new position (nothing owned before)" if math.isinf(change) else f"{change:,.1f}% of the holding"


def _size_line(sizes: dict, as_of: str) -> str:
    if not sizes["count"]:
        return f"- Purchase size: none. No purchase in the last {LIVE_DAYS} days stands outside a batch or a reversal."
    parts = [f"{sizes['large']} large ({LARGE_PCT:.0f}% or more of the buyer's holding)",
             f"{sizes['modest']} modest ({TOKEN_PCT:.0f}% to {LARGE_PCT:.0f}%)", f"{sizes['token']} token (under {TOKEN_PCT:.0f}%)"]
    if sizes["unknown"]:
        parts.append(f"{sizes['unknown']} with the holding unknown")
    text = (f"- Purchase size: {_count(sizes['count'], 'purchase', 'purchases')} in the last {LIVE_DAYS} days outside batches and "
            f"reversals (an insider's fills on one day count once), graded by the share added to the buyer's holding: {', '.join(parts)}.")
    if sizes["largest"]:
        change, day, who = sizes["largest"]
        text += f" Largest: {who} added {_pct(change)} on {day} ({_ago(_age(day, as_of))})."
    return text


def routine_batches(group: list[dict]) -> dict | None:
    """Whether a cluster looks like a scheduled purchase program instead of separate decisions, or None.

    Open-market buyers fill at different prices through the day. When ``BATCH_INSIDERS`` or more different insiders all bought at
    exactly the same price on the same day, one program (a company plan that buys for its executives on a set date) filled them. The
    cluster is called likely routine when at least ``BATCH_SHARE`` of its purchases were made in such batches.
    """
    batches = _batch_days(group)
    in_batches = sum(len(trades) for trades in batches.values())
    if not group or in_batches / len(group) < BATCH_SHARE:
        return None
    return {"in_batches": in_batches, "total": len(group),
            "days": [(day, price, len({t["who"] for t in trades})) for (day, price), trades in sorted(batches.items(), reverse=True)]}


def _latest_move(buys: list[dict], sales: list[dict], as_of: str) -> str:
    """The newest purchase or unplanned sale (a planned sale does not count: it was decided long before it was made)."""
    moves = [("purchase", "purchases", t) for t in buys] + [("unplanned sale", "unplanned sales", t) for t in sales if not t["planned"]]
    if not moves:
        return "- Latest move: none, no purchase or unplanned sale in this window."
    newest = max(t["traded"] for _, _, t in moves)
    parts = []
    for one, many in (("purchase", "purchases"), ("unplanned sale", "unplanned sales")):
        mine = [t for kind, _, t in moves if kind == one and t["traded"] == newest]
        if mine:
            names = sorted({t["who"] for t in mine})
            parts.append(f"{_count(len(mine), one, many)} by {', '.join(names[:3])}{' and others' if len(names) > 3 else ''}")
    return f"- Latest move: {' and '.join(parts)} on {newest} ({_ago(_age(newest, as_of))})."


def build_report(symbol: str, filings: list[dict], as_of: str, unread: int, total_entries: int, truncated: bool,
                elsewhere: int = 0) -> str:
    start = (date.fromisoformat(as_of) - timedelta(days=WINDOW_DAYS)).isoformat()
    trades = flatten(filings, start, as_of)
    window = f"{start} to {as_of}"
    head = f"# Insider transactions for {symbol}, trades dated {window} (SEC EDGAR Form 4, filed by {as_of})"
    sales = [t for t in trades if t["code"] == "S"]
    buys = [t for t in trades if t["code"] == "P"]
    others: dict[str, int] = {}
    for t in trades:
        if t["code"] not in ("S", "P"):
            others[t["code"]] = others.get(t["code"], 0) + 1
    if not trades:
        read = f" ({unread} of {total_entries} filings could not be read.)" if unread else ""
        return f"{head}\n\nNo Form 4 trades in this window.{read}"
    cut = (date.fromisoformat(as_of) - timedelta(days=LIVE_DAYS)).isoformat()
    live_buys, live_sales = [t for t in buys if t["traded"] >= cut], [t for t in sales if t["traded"] >= cut]
    net = sum(t["value"] or 0 for t in live_buys) - sum(t["value"] or 0 for t in live_sales)
    planned = [t for t in sales if t["planned"]]
    latest_buy = f", the latest {_ago(_age(max(t['traded'] for t in buys), as_of))}" if buys else ""
    latest_sale = f", the latest {_ago(_age(max(t['traded'] for t in sales), as_of))}" if sales else ""
    lines = [
        head, "",
        f"- Open-market purchases: {len(buys)}, about {_money(sum(t['value'] or 0 for t in buys))} "
        f"by {_plural(len({t['who'] for t in buys}), 'insider')}{latest_buy}.",
        f"- Open-market sales: {len(sales)}, about {_money(sum(t['value'] or 0 for t in sales))} "
        f"by {_plural(len({t['who'] for t in sales}), 'insider')}{latest_sale}.",
        f"- Last {LIVE_DAYS} days (the live window): {_count(len(live_buys), 'purchase', 'purchases')} about "
        f"{_money(sum(t['value'] or 0 for t in live_buys))} and {_count(len(live_sales), 'sale', 'sales')} about "
        f"{_money(sum(t['value'] or 0 for t in live_sales))}, net "
        f"{'buying' if net > 0 else 'selling' if net < 0 else 'flat'} of {_money(abs(net))} in dollars (order ignored; "
        "the Latest move line gives the direction).",
    ]
    if sales:
        lines.append(f"- Planned sales: {len(planned)} of {_count(len(sales), 'sale', 'sales')}, about "
                     f"{_money(sum(t['value'] or 0 for t in planned))}, {'was' if len(planned) == 1 else 'were'} made under a "
                     "pre-arranged Rule 10b5-1 plan, which sets the date and size ahead of time.")
    lines.append(_latest_move(buys, sales, as_of))
    standing, flipped = reversals(buys, sales)
    if flipped:
        items = [f"{who} (bought about {_money(r['bought_value'])}, last on {r['bought_last']}; sold about {_money(r['sold_value'])} "
                 f"after it, first on {r['sold_first']}, {_ago(_age(r['sold_first'], as_of))})"
                 for who, r in sorted(flipped.items(), key=lambda kv: kv[1]["sold_first"], reverse=True)]
        more = f"; and {len(items) - 5} more" if len(items) > 5 else ""
        lines.append(f"- Sold after buying: {'; '.join(items[:5])}{more}. Each such insider's purchases before the sale are left out "
                     "of the Cluster line.")
    group = cluster(standing)
    if group:
        names = sorted({t["who"] for t in group})
        ended = _age(group[-1]["traded"], as_of)
        if ended <= CLUSTER_RECENT_DAYS:
            lines.append(f"- Cluster: {len(names)} different insiders bought on the open market within {CLUSTER_DAYS} days "
                         f"({group[0]['traded']} to {group[-1]['traded']}, the latest {_ago(ended)}): {', '.join(names[:5])}.")
            routine = routine_batches(group)
            if routine:
                shown = "; ".join(f"{n} insiders at {price:,.2f} on {day}" for day, price, n in routine["days"][:3])
                lines.append(f"- Cluster note: likely routine. {routine['in_batches']} of {routine['total']} purchases in this cluster were "
                             f"made on days when {BATCH_INSIDERS} or more insiders bought at exactly the same price ({shown}), which is how "
                             "a scheduled purchase program fills, not how separate decisions look.")
        else:
            lines.append(f"- Cluster: none now. An older run of {len(names)} different insiders buying within {CLUSTER_DAYS} days "
                         f"ended {_ago(ended)}, too old to count as a current signal.")
    else:
        lines.append(f"- Cluster: no run of {CLUSTER_INSIDERS} or more different insiders buying within {CLUSTER_DAYS} days.")
    if buys:
        lines.append(_size_line(purchase_sizes(standing, buys, as_of), as_of))
    if others:
        lines.append("- Other entries (not the insider's own market decision): " + ", ".join(
            f"{n} x {CODE_MEANING.get(c, 'code ' + c)}" for c, n in sorted(others.items(), key=lambda kv: -kv[1]))
            + ". Awards, tax withholding, option exercises and gifts are routine and are not listed below.")
    lines.append("- Insiders sell for many reasons (taxes, diversification, planned programs) and buy mainly for one, so a sale is "
                 "not automatically bearish and a purchase is the rarer event.")
    lines.append(f"- Age matters: days are counted from the trade date. A trade in the last {LIVE_DAYS} days is the live signal; an older "
                 f"one in this window is context only, and a cluster counts only if it ended within {CLUSTER_RECENT_DAYS} days. The newest "
                 "purchase or unplanned sale sets the direction, and a sale after the same insider's own purchase reverses that purchase.")
    shown = [t for t in trades if t["code"] in ("S", "P")]
    if shown:
        lines += ["", "| Traded | Days ago | Filed | Insider | Role | Type | Shares | Price | Value | Owned after | Change that day |",
                  "|---|---:|---|---|---|---|---:|---:|---:|---:|---:|"]
        for t in shown[:ROWS_SHOWN]:
            kind = CODE_MEANING[t["code"]] + (" (10b5-1 plan)" if t["planned"] else "") + (" (amendment)" if t["amended"] else "")
            shares = "n/a" if t["shares"] is None else format(t["shares"], ",.0f")
            price = "n/a" if t["price"] is None else format(t["price"], ",.2f")
            after = "n/a" if t["after"] is None else format(t["after"], ",.0f")
            change = t.get("change")
            moved = "n/a" if change is None else "new position" if math.isinf(change) else f"{'+' if t['code'] == 'P' else '-'}{change:,.1f}%"
            lines.append(f"| {t['traded']} | {_age(t['traded'], as_of)} | {t['filed']} | {t['who']} | {t['role']} | {kind} | {shares} | "
                         f"{price} | {_money(t['value'])} | {after} | {moved} |")
        if len(shown) > ROWS_SHOWN:
            lines.append(f"\n{len(shown) - ROWS_SHOWN} older open-market trades in the window are not listed.")
    notes = []
    if truncated:
        notes.append(f"Only the newest {MAX_FILINGS} of {total_entries} Form 4 filings in the window were read, so the oldest trades "
                     "may be missing.")
    if unread:
        notes.append(f"{_plural(unread, 'filing')} could not be read and {'is' if unread == 1 else 'are'} left out.")
    if elsewhere:
        notes.append(f"{_plural(elsewhere, 'Form 4 filing')} in this company's filing list {'is' if elsewhere == 1 else 'are'} about "
                     "other companies (it filed them as an owner of their shares) and left out.")
    if notes:
        lines += ["", *(f"Note: {n}" for n in notes)]
    return "\n".join(lines)


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date, yyyy-mm-dd"] = None,
):
    """Insider trades for ``ticker`` from SEC Form 4 filings filed by ``as_of_date`` (the last 180 days of trades)."""
    canonical = normalize_symbol(ticker)
    as_of = as_of_date or get_current_date()
    cik = sec_edgar.cik_for(canonical)
    if cik is None:
        raise NoMarketDataError(ticker, canonical, "not a US SEC filer (no EDGAR record)")
    # a trade is filed up to a few business days after it, so the filings searched start a little before the window
    since = (date.fromisoformat(as_of) - timedelta(days=WINDOW_DAYS + 10)).isoformat()
    submissions = sec_edgar._cached_json(_SUBMISSIONS_URL.format(cik=cik), f"submissions-CIK{cik}.json")
    entries = _entries(submissions, since, as_of)
    chosen = entries[:MAX_FILINGS]
    filings: list[dict] = []
    unread = 0
    failures, lock = [0], threading.Lock()

    def read(entry: dict) -> dict | None:
        if failures[0] >= MAX_FAILURES:              # EDGAR is not answering: stop asking, the report says how many were read
            return None
        try:
            return _filing(cik, entry)
        except VendorUnavailableError:
            with lock:
                failures[0] += 1
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(read, chosen):
            if result is None:
                unread += 1
            else:
                filings.append(result)
    if chosen and unread == len(chosen):
        raise VendorUnavailableError("SEC EDGAR returned none of the Form 4 filings; insider trades were not retrieved")
    # An owner's own filing list (Berkshire's, for one) also holds the Form 4s it filed about other companies' shares.
    about = [f for f in filings if f.get("issuer_cik") == int(cik)]
    return build_report(canonical, about, as_of, unread, len(entries), len(entries) > len(chosen), len(filings) - len(about))
