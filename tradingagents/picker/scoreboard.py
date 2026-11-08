"""How the picker's recorded lists did (a local addition).

Every list the picker shows can be recorded in the picks log (``ledger``). This module reads that log and, for each stock,
compares its price on the day it was shown with its price 5, 21 and 63 trading sessions later (when that many have passed)
and today, against the S&P 500 over exactly the same days. Prices are Yahoo's adjusted closes, so splits and dividends do
not distort a move. The answer is facts and counts, never advice, and it says plainly when there is too little to judge:
stocks picked on the same day move together, so a few lists count for far less than their number of stocks.
"""

from __future__ import annotations

import csv
import io
import statistics
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from tradingagents.csv_safe import safe_cell
from tradingagents.picker.lists import clean_symbol
from tradingagents.picker.providers import Provider
from tradingagents.picker.quicklook import MARKET, PickerError

HORIZONS = (5, 21, 63)                      # trading sessions after the day a stock was shown
FIRST_READ = 21                             # the horizon the verdict looks at first
ENOUGH_STOCKS, ENOUGH_LISTS = 30, 4         # below either, the verdict says it is too early
RECIPE_NAMES = {"beaten-down": "Beaten-down quality", "list": "Your own lists"}
YARDSTICK = ("The yardstick is the S&P 500 over the same days. These stocks are usually more volatile than the index, so in a rising "
             "market they tend to beat it and in a falling market to trail it, whether or not the picker adds anything.")


@dataclass(frozen=True)
class Move:
    stock: float                            # the stock's return, as a fraction
    market: float                           # the S&P 500's return over the same days

    @property
    def excess(self) -> float:
        return self.stock - self.market


@dataclass
class Outcome:
    recipe: str
    shown: date                             # the day the stock was shown (the last close it was judged on)
    symbol: str
    name: str
    rank: int
    met: int
    price_then: float | None
    sessions: int = 0                       # trading sessions since it was shown
    to_date: Move | None = None
    horizons: dict[int, Move | None] = field(default_factory=dict)
    note: str = ""


@dataclass
class Stat:
    n: int
    avg_stock: float
    avg_market: float
    avg_excess: float
    median_excess: float
    beat: int                               # how many beat the market


@dataclass
class Batch:
    recipe: str
    shown: date
    recorded: int                           # stocks recorded in this list
    sessions: int                           # trading sessions since (the oldest stock in it)
    stats: dict                             # "to_date" and each horizon: Stat or None


@dataclass
class Scoreboard:
    as_of: date | None
    picks: list[Outcome]
    batches: list[Batch]
    recipes: dict                           # recipe -> {"stocks", "lists", "first", "last", "stats": {...}}
    buckets: dict                           # recipe -> {bucket label -> {"stats": {...}}}
    verdict: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- reading the record
def parse_rows(rows: list[dict]) -> tuple[list[Outcome], int]:
    """(one Outcome per recipe, day and stock - the first one, in record order; how many rows could not be read)."""
    picks: list[Outcome] = []
    seen: set[tuple] = set()
    bad = 0
    for row in rows:
        try:
            shown = date.fromisoformat((row.get("as_of") or "").strip())
            symbol = clean_symbol(row.get("symbol") or "")
            if symbol is None:
                raise ValueError("symbol")
            recipe = (row.get("recipe") or "").strip() or "unknown"
            rank = int(float(row.get("rank") or 0))
            met = int(float(row.get("conditions_met") or 0))
            price = float(row["price"]) if (row.get("price") or "").strip() else None
        except (ValueError, TypeError):
            bad += 1
            continue
        key = (recipe, shown, symbol)
        if key in seen:
            continue                        # the same list shown twice the same day counts once
        seen.add(key)
        picks.append(Outcome(recipe, shown, symbol, (row.get("name") or "").strip(), rank, met, price))
    return picks, bad


# ---------------------------------------------------------------- measuring
def _dates(series) -> list[date]:
    return [ts.date() for ts in series.index]


def _at(dates: list[date], day: date) -> int:
    """The position of the last close on or before ``day`` (-1 when there is none)."""
    return bisect_right(dates, day) - 1


def measure(pick: Outcome, stock, market) -> Outcome:
    """Fill in how one stock did, from its adjusted closes and the S&P 500's."""
    if stock is None or len(stock) < 2:
        pick.note = "no prices from Yahoo (it may have been renamed or delisted)"
        return pick
    dates, mdates = _dates(stock), _dates(market)
    start = _at(dates, pick.shown)
    if start < 0:
        pick.note = f"prices do not reach back to {pick.shown.isoformat()}"
        return pick
    if (pick.shown - dates[start]).days > 10:
        pick.note = f"its prices end on {dates[start].isoformat()}, well before {pick.shown.isoformat()}"
        return pick
    values, mvalues = stock.to_numpy(dtype=float), market.to_numpy(dtype=float)
    entry_day = dates[start]
    m_start = _at(mdates, entry_day)
    if m_start < 0 or not values[start] > 0 or not mvalues[m_start] > 0:
        pick.note = "no usable starting price"
        return pick

    def move(end: int) -> Move | None:
        m_end = _at(mdates, dates[end])
        if end <= start or m_end < 0 or not values[end] > 0 or not mvalues[m_end] > 0:
            return None
        return Move(float(values[end] / values[start] - 1), float(mvalues[m_end] / mvalues[m_start] - 1))

    pick.sessions = len(values) - 1 - start
    pick.to_date = move(len(values) - 1)
    pick.horizons = {h: move(start + h) if start + h < len(values) else None for h in HORIZONS}
    return pick


def stat(moves: list[Move]) -> Stat | None:
    if not moves:
        return None
    excess = [m.excess for m in moves]
    return Stat(len(moves), statistics.fmean(m.stock for m in moves), statistics.fmean(m.market for m in moves),
                statistics.fmean(excess), statistics.median(excess), sum(1 for x in excess if x > 0))


def stats_for(picks: list[Outcome]) -> dict:
    """Stat (or None when nothing has reached it) for "to_date" and each horizon."""
    out = {"to_date": stat([p.to_date for p in picks if p.to_date])}
    for h in HORIZONS:
        out[h] = stat([p.horizons[h] for p in picks if p.horizons.get(h)])
    return out


def bucket_of(met: int) -> str:
    return "5 of 5 conditions" if met >= 5 else "4 of 5 conditions" if met == 4 else "3 or fewer conditions"


BUCKET_ORDER = ("5 of 5 conditions", "4 of 5 conditions", "3 or fewer conditions")


def _verdict(picks: list[Outcome], measured: list[Outcome]) -> str:
    if not picks:
        return "Nothing is recorded yet. Run a search with recording on, then check back in a few weeks."
    if not measured:
        return "None of the recorded stocks could be measured. See the notes below."
    oldest = max(p.sessions for p in measured)
    reached = [p for p in measured if p.horizons.get(FIRST_READ)]
    lists = {(p.recipe, p.shown) for p in reached}
    if not reached:
        return (f"Too early to say anything. The oldest recorded list is only {oldest} trading sessions old, and the first reading "
                f"here is at {FIRST_READ} sessions.")
    if len(reached) < ENOUGH_STOCKS or len(lists) < ENOUGH_LISTS:
        return (f"Too early to judge. Only {_n(len(reached), 'stock', 'stocks')} from {_n(len(lists), 'list', 'lists')} "
                f"{'has' if len(reached) == 1 else 'have'} reached {FIRST_READ} sessions. Stocks picked on the same day move together, "
                "so the number of lists matters more than the number of stocks, and few lists are few independent results.")
    s = stat([p.horizons[FIRST_READ] for p in reached])
    return (f"Worth a look, but not proof. {len(reached)} stocks from {len(lists)} lists have reached {FIRST_READ} sessions: on average "
            f"{s.avg_stock * 100:+.1f}% against {s.avg_market * 100:+.1f}% for the S&P 500, and {s.beat} of {s.n} beat it. A few strong or "
            "weak weeks in the market can explain a result like this.")


def build(rows: list[dict], provider: Provider, log: Callable[[str], None] = lambda message: None) -> Scoreboard:
    """Measure every recorded stock against the S&P 500 (needs one download of prices, nothing else)."""
    picks, bad = parse_rows(rows)
    picks.sort(key=lambda p: (p.shown, p.recipe, p.rank))                   # by day, whatever order the log was written in
    warnings = [f"{_n(bad, 'row', 'rows')} of the picks log could not be read and {'was' if bad == 1 else 'were'} skipped."] if bad else []
    if not picks:
        return Scoreboard(None, [], [], {}, {}, _verdict([], []), warnings)
    symbols = sorted({p.symbol for p in picks})
    log(f"Downloading prices for {len(symbols)} recorded stocks...")
    closes = provider.closes([*symbols, MARKET])
    market = closes.get(MARKET)
    if market is None or len(market) < 2:
        raise PickerError("could not get S&P 500 prices from Yahoo, so nothing can be compared with the market; try again")
    for p in picks:
        measure(p, closes.get(p.symbol), market)
    measured = [p for p in picks if p.to_date is not None]
    missing = [p for p in picks if p.to_date is None and p.note]
    if missing:
        warnings.append(f"{_n(len(missing), 'recorded stock', 'recorded stocks')} could not be measured, for example because Yahoo has no prices "
                        f"for {'it' if len(missing) == 1 else 'them'}. {'It is' if len(missing) == 1 else 'They are'} left out of every average; "
                        "the CSV file says why for each one.")
    waiting = [p for p in picks if p.to_date is None and not p.note]
    if waiting:
        warnings.append(f"{_n(len(waiting), 'stock was', 'stocks were')} shown on the latest day that has prices, so there is nothing to compare yet.")

    batches: list[Batch] = []
    groups: dict[tuple, list[Outcome]] = {}
    for p in picks:
        groups.setdefault((p.recipe, p.shown), []).append(p)
    for (recipe, shown), members in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0]), reverse=True):
        batches.append(Batch(recipe, shown, len(members), max((m.sessions for m in members), default=0), stats_for(members)))

    recipes: dict = {}
    buckets: dict = {}
    for recipe in sorted({p.recipe for p in picks}):
        mine = [p for p in picks if p.recipe == recipe]
        days = sorted({p.shown for p in mine})
        recipes[recipe] = {"stocks": len(mine), "lists": len(days), "first": days[0], "last": days[-1], "stats": stats_for(mine)}
        buckets[recipe] = {b: {"stats": stats_for([p for p in mine if bucket_of(p.met) == b]),
                               "stocks": sum(bucket_of(p.met) == b for p in mine)} for b in BUCKET_ORDER}
    as_of = max((_dates(closes[p.symbol])[-1] for p in picks if p.symbol in closes and len(closes[p.symbol])), default=None)
    return Scoreboard(as_of, picks, batches, recipes, buckets, _verdict(picks, measured), warnings)


# ---------------------------------------------------------------- showing it
def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def _points(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f} points"


def _line(label: str, s: Stat | None) -> str:
    if s is None:
        return f"  {label:<24}nothing has reached this yet"
    return (f"  {label:<24}{_n(s.n, 'stock', 'stocks'):<10}{_pct(s.avg_stock):>7} on average against {_pct(s.avg_market):>7} for the S&P 500 "
            f"({_points(s.avg_excess)}; {s.beat} of {s.n} beat it)")


def _horizon_lines(stats: dict) -> list[str]:
    return [_line("Today", stats["to_date"])] + [_line(f"After {h} sessions", stats[h]) for h in HORIZONS]


def render_text(board: Scoreboard) -> str:
    through = f"  (prices through {board.as_of.isoformat()})" if board.as_of else ""
    lines = ["", f"HOW THE RECORDED PICKS DID{through}", "", board.verdict, "", YARDSTICK]
    if board.warnings:
        lines += ["", "NOTES"] + [f"  - {w}" for w in board.warnings]
    for recipe, info in board.recipes.items():
        lines += ["", f"{RECIPE_NAMES.get(recipe, recipe).upper()}: {_n(info['stocks'], 'stock', 'stocks')} in {_n(info['lists'], 'list', 'lists')}, "
                      f"{info['first'].isoformat()} to {info['last'].isoformat()}"] + _horizon_lines(info["stats"])
        shown = [b for b in BUCKET_ORDER if board.buckets[recipe][b]["stocks"]]
        if len(shown) > 1:
            lines += ["", f"  By how many conditions were met ({RECIPE_NAMES.get(recipe, recipe)}), today:"]
            lines += [_line(b, board.buckets[recipe][b]["stats"]["to_date"]) for b in shown]
    if board.batches:
        lines += ["", "EACH LIST, NEWEST FIRST"]
        for b in board.batches:
            s = b.stats["to_date"]
            lines.append(f"  {b.shown.isoformat()}  {RECIPE_NAMES.get(b.recipe, b.recipe):<20}{_n(b.recorded, 'stock', 'stocks'):<10}"
                         f"{_n(b.sessions, 'session', 'sessions')} ago: "
                         + ("not measured yet" if s is None else f"{_pct(s.avg_stock)} against {_pct(s.avg_market)} ({_points(s.avg_excess)})"))
    lines += ["", "This compares past prices with the S&P 500. It is not a forecast and not advice.", ""]
    return "\n".join(lines)


def _stat_json(s: Stat | None) -> dict | None:
    return None if s is None else {"n": s.n, "avg_stock": s.avg_stock, "avg_market": s.avg_market, "avg_excess": s.avg_excess,
                                   "median_excess": s.median_excess, "beat": s.beat}


def _stats_json(stats: dict) -> dict:
    return {str(k): _stat_json(v) for k, v in stats.items()}


def _move_json(m: Move | None) -> dict | None:
    return None if m is None else {"stock": m.stock, "market": m.market, "excess": m.excess}


def to_json(board: Scoreboard) -> dict:
    return {
        "as_of": board.as_of.isoformat() if board.as_of else None, "verdict": board.verdict, "yardstick": YARDSTICK,
        "warnings": board.warnings, "horizons": list(HORIZONS),
        "recipes": [{"id": r, "label": RECIPE_NAMES.get(r, r), "stocks": i["stocks"], "lists": i["lists"], "first": i["first"].isoformat(),
                     "last": i["last"].isoformat(), "stats": _stats_json(i["stats"]),
                     "buckets": [{"label": b, "stocks": board.buckets[r][b]["stocks"], "stats": _stats_json(board.buckets[r][b]["stats"])}
                                 for b in BUCKET_ORDER if board.buckets[r][b]["stocks"]]} for r, i in board.recipes.items()],
        "batches": [{"recipe": b.recipe, "label": RECIPE_NAMES.get(b.recipe, b.recipe), "shown": b.shown.isoformat(), "recorded": b.recorded,
                     "sessions": b.sessions, "stats": _stats_json(b.stats)} for b in board.batches],
        "picks": [{"recipe": p.recipe, "shown": p.shown.isoformat(), "symbol": p.symbol, "name": p.name, "rank": p.rank, "met": p.met,
                   "sessions": p.sessions, "to_date": _move_json(p.to_date), "note": p.note,
                   "horizons": {str(h): _move_json(m) for h, m in p.horizons.items()}} for p in board.picks],
    }


def csv_columns() -> list[str]:
    cols = ["recipe", "shown_on", "symbol", "name", "rank", "conditions_met", "price_then", "sessions_since",
            "return_to_date_pct", "market_to_date_pct", "excess_to_date_pct"]
    for h in HORIZONS:
        cols += [f"return_{h}d_pct", f"market_{h}d_pct", f"excess_{h}d_pct"]
    return cols + ["note"]


def _cells(m: Move | None) -> list:
    return ["", "", ""] if m is None else [round(m.stock * 100, 2), round(m.market * 100, 2), round(m.excess * 100, 2)]


def to_csv(board: Scoreboard) -> bytes:
    """Every recorded stock with its outcomes (UTF-8 with a byte-order mark, text defused for spreadsheets)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(csv_columns())
    for p in board.picks:
        row = [safe_cell(p.recipe), p.shown.isoformat(), safe_cell(p.symbol), safe_cell(p.name), p.rank, p.met,
               "" if p.price_then is None else p.price_then, p.sessions] + _cells(p.to_date)
        for h in HORIZONS:
            row += _cells(p.horizons.get(h))
        writer.writerow(row + [safe_cell(p.note)])
    return ("﻿" + out.getvalue()).encode("utf-8")
