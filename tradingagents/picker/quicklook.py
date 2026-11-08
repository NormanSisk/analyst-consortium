"""The quick look: screen, check and rank, with no model calls (a local addition).

``run`` takes a recipe ("beaten-down" screens the market; "list" checks stocks you name), a provider of data and the
settings, and returns the ranked stocks with the five deal conditions for each, the counts at every step of the funnel, and
the reasons a stock fell out. The paid analysis is left for the few the person picks.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tradingagents.dataflows.vendors.yahoo import market_context as mc
from tradingagents.picker import conditions as cond
from tradingagents.picker import custom
from tradingagents.picker.cache import FundamentalsCache
from tradingagents.picker.facts import Facts, build_facts
from tradingagents.picker.providers import Provider
from tradingagents.picker.settings import PickerSettings
from tradingagents.picker.stage_a import MarketState, PriceMetrics, Window, market_state, price_metrics, sector_windows

MARKET, VIX = "^GSPC", "^VIX"
RECIPES = ("beaten-down", "list", "custom")
EXTRA_FACTS = ("trailing_pe", "dividend_yield", "price_to_book", "analyst_rating")        # from the screener's answer, shown when it has them
MAX_LIST = 300                       # the most stocks a named list may hold


class PickerError(RuntimeError):
    """The quick look could not run; the message says why in plain words."""


@dataclass
class PickRow:
    symbol: str
    name: str
    sector: str
    industry: str
    price: float | None
    market_cap: float | None
    metrics: PriceMetrics | None
    facts: Facts | None
    conditions: dict[str, cond.Condition]
    sector_label: str = ""
    sector_windows: tuple[Window, Window] | None = None
    peers: str = ""
    low_pe_limit: float = 6.0
    met: int = 0
    score: float = 0.0
    note: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def failed(self) -> list[cond.Condition]:
        return [c for c in self.conditions.values() if not c.met]

    @property
    def low_pe(self) -> bool:
        """A forward P/E so low that the market probably expects earnings to fall (a cyclical peak, for example)."""
        pe = self.facts.forward_pe if self.facts else None
        return pe is not None and 0 < pe < self.low_pe_limit

    @property
    def sector_driven(self) -> bool:
        """The sector fund explains most of a fall that the market also explains: a sector selloff, not a market panic."""
        if not self.sector_windows:
            return False
        return any(w.fell and w.share is not None and w.share >= 0.6 for w in self.sector_windows)


@dataclass
class PickResult:
    recipe: str
    as_of: date | None
    market: MarketState
    funnel: list[tuple[str, int]]
    rows: list[PickRow]
    settings: PickerSettings
    treasury: float | None = None
    warnings: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)       # what a custom search asked for, in plain lines
    query: dict | None = None                              # the same, as a small dict for the picks log


def _sector_etf(facts: Facts) -> tuple[str, str] | None:
    return mc.industry_funds.get(facts.industry_key) or mc.sector_funds.get(facts.sector_key)


def _percentiles(values: dict[str, float | None]) -> dict[str, float]:
    """Each stock's rank among those that have a number, from 0 to 1; stocks without one get 0.5 (neither good nor bad)."""
    present = pd.Series({k: v for k, v in values.items() if v is not None and v == v}, dtype=float)
    if len(present) < 2:
        return {k: 0.5 for k in values}
    ranked = present.rank(pct=True)
    return {k: float(ranked[k]) if k in ranked.index else 0.5 for k in values}


def _score(rows: list[PickRow]) -> None:
    """A transparent tiebreak within the same number of conditions met: equal weight on five rankings.

    Quality (operating margin), growth (next year's expected EPS growth), revisions (analysts raising less cutting, next
    year), value (forward earnings yield) and how fully the market explains the fall.
    """
    def rev(r):
        f = r.facts.fy1 if r.facts else None
        return None if f is None or f.up is None else (f.up or 0) - (f.down or 0)

    parts = {
        "quality": _percentiles({r.symbol: r.facts.op_margin if r.facts and not r.facts.financial else None for r in rows}),
        "growth": _percentiles({r.symbol: r.facts.fy1.growth if r.facts else None for r in rows}),
        "revisions": _percentiles({r.symbol: rev(r) for r in rows}),
        "value": _percentiles({r.symbol: (1 / r.facts.forward_pe) if r.facts and r.facts.forward_pe and r.facts.forward_pe > 0 else None for r in rows}),
        "explained": _percentiles({r.symbol: min(r.metrics.w63.share, 1.5) if r.metrics and r.metrics.w63.share is not None else None for r in rows}),
    }
    for r in rows:
        r.score = sum(p[r.symbol] for p in parts.values()) / len(parts)


def _fetch_all(provider: Provider, symbols: list[str], cache: FundamentalsCache, workers: int, refresh: bool,
               log: Callable[[str], None]) -> dict[str, dict]:
    raws: dict[str, dict] = {}
    todo = []
    for symbol in symbols:
        hit = None if refresh else cache.get(symbol)
        if hit is not None:
            raws[symbol] = hit
        else:
            todo.append(symbol)
    if raws:
        log(f"  {len(raws)} of {len(symbols)} read from today's saved copy")

    def one(symbol: str) -> tuple[str, dict]:
        try:
            raw = provider.fundamentals(symbol)
        except Exception as exc:  # noqa: BLE001 - one stock failing must not end the run
            raw = {"symbol": symbol, "error": f"{type(exc).__name__}"}
        raw.setdefault("symbol", symbol)
        cache.put(symbol, raw)
        return symbol, raw

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for symbol, raw in pool.map(one, todo):
                raws[symbol] = raw
    return raws


def run(recipe: str, provider: Provider, settings: PickerSettings, symbols: list[str] | None = None,
        cache: FundamentalsCache | None = None, workers: int = 6, refresh: bool = False,
        log: Callable[[str], None] = lambda message: None, query: custom.CustomQuery | None = None) -> PickResult:
    """Screen or check stocks and return them ranked. ``symbols`` is required for the "list" recipe, ``query`` for "custom"."""
    settings = settings.validated()
    if recipe not in RECIPES:
        raise PickerError(f"unknown recipe {recipe!r}; choose from {', '.join(RECIPES)}")
    if recipe == "custom" and query is None:
        raise PickerError("a custom search needs its filters")
    cache = cache or FundamentalsCache(None, date.today())
    first_steps: list[tuple[str, int]] = []
    described = {"filters": custom.describe(query), "query": custom.summary(query)} if recipe == "custom" else {}

    if recipe == "custom":
        log("Searching Yahoo's free screener with your filters...")
        found = provider.screen(custom.yahoo_terms(query), list(query.sectors or custom.SECTORS), query.examine)
        universe = [u for u in found if custom.passes_quote(u, query)]
        first_steps.append(("Stocks matching your filters at Yahoo", len(found)))
        if query.has("quote") or query.exclude:
            first_steps.append(("After your forward P/E, analyst rating and leave-out filters", len(universe)))
    elif recipe == "list":
        symbols = list(dict.fromkeys(symbols or []))
        if not symbols:
            raise PickerError("name at least one stock")
        if len(symbols) > MAX_LIST:
            raise PickerError(f"a list may hold at most {MAX_LIST} stocks; this one has {len(symbols)}")
        universe = [{"symbol": s, "name": "", "sector": "", "market_cap": None, "forward_pe": None} for s in symbols]
        log(f"Checking {len(universe)} stocks you named...")
    else:
        log("Screening the market with Yahoo's free screener...")
        universe = provider.universe(settings)
        if not universe:
            raise PickerError("the screener returned no stocks; try again, or lower the size and volume limits")
    record = {u["symbol"]: u for u in universe}
    funnel: list[tuple[str, int]] = first_steps or [("Stocks to look at", len(record))]

    if recipe != "custom" or record:
        log(f"Downloading prices for {len(record)} stocks...")
    closes = provider.closes([*record, MARKET, VIX])
    market = closes.get(MARKET)
    if market is None or len(market) < 120:
        raise PickerError("could not get S&P 500 prices from Yahoo, so nothing can be compared with the market; try again")
    state = market_state(market, closes.get(VIX))
    metrics = {s: price_metrics(closes[s], market) if s in closes else None for s in record}
    have = [s for s, m in metrics.items() if m is not None]
    funnel.append(("With enough price history", len(have)))
    as_of = max((m.as_of for m in metrics.values() if m), default=None)
    warnings: list[str] = []
    if len(have) < len(record):
        missing = len(record) - len(have)
        warnings.append(f"{missing} {'stock' if missing == 1 else 'stocks'} had too little price history and {'was' if missing == 1 else 'were'} skipped.")

    if recipe == "beaten-down":
        down = [s for s in have if metrics[s].off_high <= -settings.below_high]
        funnel.append((f"At least {settings.below_high * 100:.0f}% below the 52-week high", len(down)))
        explained = [s for s in down if cond.explained_condition(metrics[s], settings).met]
        funnel.append(("Market explains the fall (the analysis's thresholds)", len(explained)))
        survivors = sorted(explained, key=lambda s: metrics[s].off_high)[:settings.carry]
        if len(explained) > settings.carry:
            warnings.append(f"{len(explained)} stocks passed; only the {settings.carry} deepest below their highs were checked further.")
    elif recipe == "custom":
        kept = [s for s in have if custom.passes_price(metrics[s], query, settings)]
        if query.has("price"):
            funnel.append(("After your price filters", len(kept)))
        survivors = sorted(kept, key=lambda s: custom.stage_key(query.sort, record[s], metrics[s]))[:query.check]
        if len(kept) > query.check:
            warnings.append(f"{len(kept)} stocks passed your filters; only the {query.check} that come first in your chosen order were checked "
                            "in depth. Narrow your filters or raise \"How many to check in depth\" to see more.")
    else:
        survivors = have
    if not survivors:
        return PickResult(recipe, as_of, state, funnel, [], settings, None, warnings, **described)

    log(f"Checking the fundamentals of {len(survivors)} stocks...")
    raws = _fetch_all(provider, survivors, cache, workers, refresh, log)
    facts = {s: build_facts(raws[s]) for s in survivors}
    treasury = provider.ten_year_yield()
    if treasury is None:
        warnings.append("The 10-year Treasury yield could not be fetched, so the valuation check uses peers only.")

    # a sector fund's closes, for the "is this a sector selloff" column
    etfs = sorted({e[1] for f in facts.values() if (e := _sector_etf(f))})
    etf_closes = provider.closes(etfs) if etfs else {}

    industries: dict[str, list[float]] = {}
    sector_pes: dict[str, list[float]] = {}
    for item in universe:
        if item.get("sector") and item.get("forward_pe") and item["forward_pe"] > 0:
            sector_pes.setdefault(item["sector"], []).append(float(item["forward_pe"]))

    def peers(f: Facts) -> tuple[float | None, str]:
        if f.industry:
            if f.industry not in industries:
                try:
                    industries[f.industry] = provider.industry_forward_pes(f.industry, settings)
                except Exception:  # noqa: BLE001 - peers are a nicety; fall back to the sector
                    industries[f.industry] = []
            found = industries[f.industry]
            if len(found) >= settings.min_peers:
                return statistics.median(found), f"{f.industry} ({len(found)} peers)"
        found = sector_pes.get(f.sector or record.get(f.symbol, {}).get("sector", ""), [])
        if len(found) >= settings.min_peers:
            return statistics.median(found), f"{f.sector or record[f.symbol].get('sector')} sector ({len(found)} stocks)"
        return None, ""

    market_condition = cond.market_condition(state)
    rows: list[PickRow] = []
    for symbol in survivors:
        f, m = facts[symbol], metrics[symbol]
        median, label = peers(f)
        conditions = {"a": market_condition, "b": cond.explained_condition(m, settings),
                      "c": cond.deterioration_condition(f, settings), "d": cond.survival_condition(f, settings),
                      "e": cond.valuation_condition(f, median, label, treasury, settings)}
        etf = _sector_etf(f)
        windows = sector_windows(closes[symbol], etf_closes[etf[1]]) if etf and etf[1] in etf_closes else None
        error = "; ".join(raws[symbol].get("errors", []) + ([raws[symbol]["error"]] if raws[symbol].get("error") else []))
        rows.append(PickRow(symbol=symbol, name=f.name or record[symbol].get("name", ""), sector=f.sector or record[symbol].get("sector", ""),
                            industry=f.industry, price=m.price, market_cap=f.market_cap or record[symbol].get("market_cap"),
                            metrics=m, facts=f, conditions=conditions, sector_label=etf[0] if etf else "", sector_windows=windows,
                            peers=label, low_pe_limit=settings.low_pe_flag, met=sum(c.met for c in conditions.values()),
                            note=f"data trouble: {error}" if error else "",
                            extras={k: record[symbol][k] for k in EXTRA_FACTS if record[symbol].get(k) is not None}))
    _score(rows)
    funnel.append(("Fundamentals checked", len(rows)))
    if recipe == "custom":
        if query.min_conditions:
            rows = [r for r in rows if r.met >= query.min_conditions]
            funnel.append((f"Meeting at least {query.min_conditions} of 5 conditions", len(rows)))
        rows.sort(key=lambda r: custom.final_key(query.sort, r))
    else:
        rows.sort(key=lambda r: (-r.met, -r.score, r.metrics.off_high if r.metrics else 0))
    funnel.append(("Meeting at least 4 of 5 conditions", sum(r.met >= 4 for r in rows)))
    funnel.append(("Meeting all 5", sum(r.met == 5 for r in rows)))
    if not market_condition.met:
        warnings.append("The market is not in a drawdown today, so condition (a) fails for every stock: 4 of 5 is the most any can meet.")
    return PickResult(recipe, as_of, state, funnel, rows, settings, treasury, warnings, **described)
