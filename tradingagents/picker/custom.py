"""The custom search: the filters you choose (a local addition).

Every filter is optional and a blank one means "no filter". Most are answered by Yahoo's free screener (size, valuation, growth,
margins, debt, short interest, ownership); the forward P/E and the analyst rating come back with each stock and are checked here;
the price filters (how far below the high, how much it moved) and "the market explains the fall" use the same price arithmetic as
the analysis. Stocks that pass are then checked against the analysis's five deal conditions like any other search.

This module is pure: it knows the filters, checks a request in plain words, and turns it into neutral terms (a tuple such as
``("gte", "intradaymarketcap", 2e9)``) that the data layer turns into Yahoo's query. No network, no yfinance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradingagents.picker import conditions as cond
from tradingagents.picker.lists import parse_symbols
from tradingagents.picker.settings import PickerSettings
from tradingagents.picker.stage_a import PriceMetrics

SECTORS = ("Technology", "Healthcare", "Financial Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
           "Industrials", "Basic Materials", "Communication Services", "Real Estate", "Utilities")
US_EXCHANGES = ("NMS", "NYQ", "ASE", "NGM", "NCM")      # Nasdaq, NYSE, NYSE American; left out: over-the-counter listings
MAX_EXCLUDE = 200
LIMITS = {"examine": (800, 10, 3000), "check": (60, 1, 200), "show": (15, 1, 500)}      # name: (usual, smallest, largest)
LIMIT_LABELS = {"examine": "How many stocks to look at", "check": "How many to check in depth", "show": "How many to show"}


@dataclass(frozen=True)
class Criterion:
    id: str
    group: str
    label: str
    unit: str
    help: str
    sides: tuple[str, ...] = ("min", "max")
    where: str = "yahoo"                    # yahoo (answered by the screener), quote (checked here on the screener's answer), price
    field: str = ""                         # Yahoo's name for it
    factor: float = 1.0                     # Yahoo's number = the number shown * factor
    low: float = -1e9                       # the smallest and largest number accepted (in the units shown)
    high: float = 1e9
    default_min: float | None = None        # what the page starts with (a blank filter is allowed)
    integer: bool = False


@dataclass(frozen=True)
class Flag:
    id: str
    group: str
    label: str
    help: str
    where: str = "yahoo"
    field: str = ""


GROUPS = (("which", "Which stocks"), ("size", "Size and trading"), ("price", "Where the price is"), ("value", "Valuation"),
          ("growth", "Growth"), ("quality", "Profits and strength"), ("holders", "Who owns it"), ("analysts", "Analysts"))

CRITERIA = (
    Criterion("market_cap", "size", "Market value", "billion dollars", "The size of the company. Small companies are riskier and thinner to trade.",
              field="intradaymarketcap", factor=1e9, low=0, high=20000, default_min=2),
    Criterion("price", "size", "Share price", "dollars", "The price of one share. It says little by itself: a low price often just means many shares.",
              field="intradayprice", low=0, high=1e6),
    Criterion("avg_volume", "size", "Trading volume", "thousand shares a day", "The 3-month average. Thinly traded stocks are hard to buy and sell.",
              sides=("min",), field="avgdailyvol3m", factor=1e3, low=0, high=1e7, default_min=500),
    Criterion("beta", "size", "Beta", "1 = moves like the market", "How much the stock tends to move when the market does. Over 1 swings more than the market.",
              field="beta", low=-5, high=10),
    Criterion("below_high", "price", "Below its 52-week high", "percent", "At least 20 means well off its high; at most 10 means near its high.",
              where="price", low=0, high=100),
    Criterion("move_63d", "price", "Price change over 63 trading days", "percent", "About three months. A fall is a negative number, for example at most -15.",
              where="price", low=-100, high=10000),
    Criterion("move_21d", "price", "Price change over 21 trading days", "percent", "About one month.", where="price", low=-100, high=10000),
    Criterion("forward_pe", "value", "Forward P/E", "times next year's earnings", "Stocks with no forward P/E (no expected profit) are left out when you set this.",
              where="quote", low=0, high=1000),
    Criterion("trailing_pe", "value", "Trailing P/E", "times the last 12 months' earnings", "Stocks with no P/E (no profit) are left out when you set this.",
              field="peratio.lasttwelvemonths", low=0, high=1000),
    Criterion("peg", "value", "PEG ratio", "P/E divided by growth", "Under 1 is often read as cheap for its growth, but it rests on a growth forecast.",
              sides=("max",), field="pegratio_5y", low=0, high=100),
    Criterion("price_book", "value", "Price to book value", "times", "Under 1 means the shares trade below the value of the company's net assets on paper.",
              sides=("max",), field="pricebookratio.quarterly", low=0, high=1000),
    Criterion("price_sales", "value", "Market value to sales", "times", "How much investors pay for each dollar of yearly sales.",
              sides=("max",), field="lastclosemarketcaptotalrevenue.lasttwelvemonths", low=0, high=1000),
    Criterion("dividend_yield", "value", "Dividend yield", "percent a year", "What the company pays out each year against its share price.",
              sides=("min",), field="dividendyield", low=0, high=100),
    Criterion("revenue_growth", "growth", "Sales growth", "percent, latest quarter against a year earlier", "Fast growers are often expensive; this only finds them.",
              sides=("min",), field="quarterlyrevenuegrowth.quarterly", low=-100, high=10000),
    Criterion("eps_growth", "growth", "Earnings per share growth", "percent over the last 12 months", "Growth in profit per share against a year earlier.",
              sides=("min",), field="epsgrowth.lasttwelvemonths", low=-10000, high=100000),
    Criterion("net_margin", "quality", "Net profit margin", "percent of sales", "How much of each sales dollar is left as profit.",
              sides=("min",), field="netincomemargin.lasttwelvemonths", low=-100000, high=100),
    Criterion("gross_margin", "quality", "Gross margin", "percent of sales", "Sales minus the direct cost of making the product.",
              sides=("min",), field="grossprofitmargin.lasttwelvemonths", low=-100000, high=100),
    Criterion("roe", "quality", "Return on equity", "percent a year", "Profit against the shareholders' money in the company.",
              sides=("min",), field="returnonequity.lasttwelvemonths", low=-100000, high=100000),
    Criterion("debt_equity", "quality", "Debt against equity", "percent", "100 means as much debt as shareholders' equity.",
              sides=("max",), field="totaldebtequity.lasttwelvemonths", low=0, high=1e6),
    Criterion("current_ratio", "quality", "Current ratio", "times", "Short-term assets against short-term bills. Over 1 means the bills are covered.",
              sides=("min",), field="currentratio.lasttwelvemonths", low=0, high=1000),
    Criterion("short_float", "holders", "Shares sold short", "percent of the shares available to trade", "A high number means many investors are betting against it.",
              field="short_percentage_of_float.value", low=0, high=100),
    Criterion("institutions", "holders", "Held by institutions", "percent of shares", "Funds and other big investors.", field="pctheldinst", low=0, high=100),
    Criterion("insiders", "holders", "Held by insiders", "percent of shares", "Executives and directors.", field="pctheldinsider", low=0, high=100),
    Criterion("analyst_rating", "analysts", "Average analyst rating", "1 = strong buy, 5 = sell", "Lower is more favorable. Analysts' past accuracy has not predicted their future accuracy, so treat it as a filter only.",
              sides=("max",), where="quote", low=1, high=5),
)
FLAGS = (
    Flag("market_explains", "price", "The market explains the fall",
         "Only stocks that fell by at least a few percent where the market's move explains most of it (the analysis's rule)", where="price"),
    Flag("profitable", "quality", "Profitable", "Positive net income over the last 12 months", field="netincomeis.lasttwelvemonths"),
    Flag("positive_fcf", "quality", "Positive free cash flow", "More cash coming in than going out over the last 12 months", field="leveredfreecashflow.lasttwelvemonths"),
)
SORTS = (
    ("conditions", "Most of the five deal conditions met first"),
    ("market_cap", "Largest company first"),
    ("below_high", "Furthest below its 52-week high first"),
    ("forward_pe", "Lowest forward P/E first"),
    ("move_63d", "Biggest gain over 63 days first"),
)
CRITERIA_BY_ID = {c.id: c for c in CRITERIA}
FLAGS_BY_ID = {f.id: f for f in FLAGS}
SORT_IDS = tuple(s[0] for s in SORTS)


# ---------------------------------------------------------------- a checked request
@dataclass(frozen=True)
class CustomQuery:
    ranges: dict = field(default_factory=dict)          # criterion id -> (smallest or None, largest or None), in the units shown
    flags: tuple = ()
    sectors: tuple = ()
    otc: bool = False                                   # include over-the-counter listings
    exclude: tuple = ()
    min_conditions: int | None = None
    sort: str = "conditions"
    examine: int = LIMITS["examine"][0]                 # how many matching stocks to look at (the largest first)
    check: int = LIMITS["check"][0]                     # how many of those to check in depth
    show: int = LIMITS["show"][0]

    def has(self, where: str) -> bool:
        return any(CRITERIA_BY_ID[i].where == where for i in self.ranges) or any(FLAGS_BY_ID[f].where == where for f in self.flags)


def _number(value, label: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise ValueError(f"{label}: type a number, or leave it blank.")
    return float(value)


def from_request(data: dict | None) -> CustomQuery:
    """A checked CustomQuery from what the page sent; ValueError in plain words for anything wrong."""
    data = data or {}
    ranges: dict = {}
    for cid, pair in (data.get("ranges") or {}).items():
        spec = CRITERIA_BY_ID.get(cid)
        if spec is None:
            raise ValueError(f"unknown filter: {str(cid)[:40]}")
        pair = pair or {}
        low, high = _number(pair.get("min"), spec.label), _number(pair.get("max"), spec.label)
        for side, value in (("min", low), ("max", high)):
            if value is None:
                continue
            if side not in spec.sides:
                raise ValueError(f"{spec.label} takes only {'a smallest' if spec.sides == ('min',) else 'a largest'} number.")
            if not spec.low <= value <= spec.high:
                raise ValueError(f"{spec.label}: type a number from {spec.low:g} to {spec.high:g}.")
            if spec.integer and value != int(value):
                raise ValueError(f"{spec.label}: type a whole number.")
        if low is not None and high is not None and low > high:
            raise ValueError(f"{spec.label}: the smallest number ({low:g}) is above the largest ({high:g}).")
        if low is not None or high is not None:
            ranges[cid] = (low, high)

    flags = tuple(dict.fromkeys(data.get("flags") or ()))
    unknown = [f for f in flags if f not in FLAGS_BY_ID]
    if unknown:
        raise ValueError(f"unknown filter: {str(unknown[0])[:40]}")
    sectors = tuple(dict.fromkeys(data.get("sectors") or ()))
    unknown = [s for s in sectors if s not in SECTORS]
    if unknown:
        raise ValueError(f"unknown sector: {str(unknown[0])[:40]}")

    exclude: tuple = ()
    text = data.get("exclude") or ""
    if text.strip():
        symbols, skipped = parse_symbols(text.replace(",", " ").replace(";", " "))
        if skipped:
            raise ValueError(f"Could not read these as stock symbols to leave out: {', '.join(skipped[:5])}")
        if len(symbols) > MAX_EXCLUDE:
            raise ValueError(f"Leave out at most {MAX_EXCLUDE} stocks.")
        exclude = tuple(symbols)

    minimum = data.get("min_conditions")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 5):
        raise ValueError("Deal conditions: choose a number from 1 to 5, or any.")
    sort = data.get("sort") or "conditions"
    if sort not in SORT_IDS:
        raise ValueError("Choose one of the ways to order the results.")
    counts = {}
    for name, (usual, low, high) in LIMITS.items():
        value = data.get(name)
        if value is None:
            counts[name] = usual
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{LIMIT_LABELS[name]}: type a whole number from {low} to {high}.")
        counts[name] = value
    return CustomQuery(ranges=ranges, flags=flags, sectors=sectors, otc=bool(data.get("otc")), exclude=exclude,
                       min_conditions=minimum, sort=sort, **counts)


# ---------------------------------------------------------------- what Yahoo is asked
def yahoo_terms(query: CustomQuery) -> list[tuple]:
    """Neutral terms for the screener: ("gte" | "lte" | "gt" | "btwn" | "is-in", field, value...). Region and exchange are included."""
    terms: list[tuple] = [("eq", "region", "us")]
    if not query.otc:
        terms.append(("is-in", "exchange", *US_EXCHANGES))
    for cid, (low, high) in query.ranges.items():
        spec = CRITERIA_BY_ID[cid]
        if spec.where != "yahoo":
            continue
        low = None if low is None else round(low * spec.factor, 6)
        high = None if high is None else round(high * spec.factor, 6)
        if low is not None and high is not None:
            terms.append(("btwn", spec.field, low, high))
        elif low is not None:
            terms.append(("gte", spec.field, low))
        else:
            terms.append(("lte", spec.field, high))
    for fid in query.flags:
        spec = FLAGS_BY_ID[fid]
        if spec.where == "yahoo":
            terms.append(("gt", spec.field, 0))
    return terms


def _inside(value: float | None, low: float | None, high: float | None) -> bool:
    if value is None:
        return False                        # a stock with no number for a filter that was set does not pass it
    return (low is None or value >= low) and (high is None or value <= high)


def passes_quote(record: dict, query: CustomQuery) -> bool:
    """The checks that use what the screener returned with each stock: leave-outs, forward P/E, analyst rating."""
    if record.get("symbol") in query.exclude:
        return False
    for cid, (low, high) in query.ranges.items():
        if CRITERIA_BY_ID[cid].where == "quote" and not _inside(record.get(cid), low, high):   # the record's keys are the criterion ids
            return False
    return True


def passes_price(metrics: PriceMetrics, query: CustomQuery, settings: PickerSettings) -> bool:
    """The price filters, from the same price numbers the analysis uses."""
    values = {"below_high": -metrics.off_high * 100,
              "move_63d": None if metrics.w63.stock is None else metrics.w63.stock * 100,
              "move_21d": None if metrics.w21.stock is None else metrics.w21.stock * 100}
    for cid, (low, high) in query.ranges.items():
        if CRITERIA_BY_ID[cid].where == "price" and not _inside(values[cid], low, high):
            return False
    return "market_explains" not in query.flags or cond.explained_condition(metrics, settings).met


# ---------------------------------------------------------------- order
def _last(value, reverse: bool):
    """A sort key that puts missing numbers last whichever way the numbers run."""
    return (value is None, 0 if value is None else (-value if reverse else value))


def stage_key(sort: str, record: dict, metrics: PriceMetrics):
    """Which stocks are checked in depth first (when more pass than ``check`` allows)."""
    if sort == "below_high":
        return _last(metrics.off_high, False)
    if sort == "forward_pe":
        pe = record.get("forward_pe")
        return _last(pe if pe and pe > 0 else None, False)
    if sort == "move_63d":
        return _last(metrics.w63.stock, True)
    return _last(record.get("market_cap"), True)                       # conditions and market_cap: the largest first


def final_key(sort: str, row):
    """The order of the results shown."""
    m = row.metrics
    if sort == "market_cap":
        return (_last(row.market_cap, True), -row.met)
    if sort == "below_high":
        return (_last(m.off_high if m else None, False), -row.met)
    if sort == "forward_pe":
        pe = row.facts.forward_pe if row.facts else None
        return (_last(pe if pe and pe > 0 else None, False), -row.met)
    if sort == "move_63d":
        return (_last(m.w63.stock if m else None, True), -row.met)
    return (-row.met, -row.score, m.off_high if m else 0)


# ---------------------------------------------------------------- saying what was asked
def _range_text(low, high) -> str:
    if low is not None and high is not None:
        return f"between {low:g} and {high:g}"
    return f"at least {low:g}" if low is not None else f"at most {high:g}"


def describe(query: CustomQuery) -> list[str]:
    """What the search asked for, in plain lines, for the top of the results and for the picks log."""
    lines = ["US stocks on Nasdaq, NYSE and NYSE American" + (" and over-the-counter" if query.otc else "")]
    if query.sectors:
        lines.append("Sectors: " + ", ".join(query.sectors))
    for cid, (low, high) in query.ranges.items():
        spec = CRITERIA_BY_ID[cid]
        lines.append(f"{spec.label}: {_range_text(low, high)} ({spec.unit})")
    lines += [FLAGS_BY_ID[f].label for f in query.flags]
    if query.exclude:
        lines.append("Left out: " + ", ".join(query.exclude[:12]) + (" and more" if len(query.exclude) > 12 else ""))
    if query.min_conditions:
        lines.append(f"At least {query.min_conditions} of the five deal conditions met")
    lines.append("Order: " + dict(SORTS)[query.sort])
    return lines


def summary(query: CustomQuery) -> dict:
    """A small plain dict of the search, written to the picks log so later results can be traced to the filters that found them."""
    return {"ranges": {k: list(v) for k, v in query.ranges.items()}, "flags": list(query.flags), "sectors": list(query.sectors),
            "otc": query.otc, "min_conditions": query.min_conditions, "sort": query.sort}


def spec_for_page() -> dict:
    """The catalog the page draws its form from (so the form and the checks can never disagree)."""
    return {
        "groups": [{"id": g, "label": label} for g, label in GROUPS],
        "criteria": [{"id": c.id, "group": c.group, "label": c.label, "unit": c.unit, "help": c.help, "sides": list(c.sides),
                      "low": c.low, "high": c.high, "default_min": c.default_min, "integer": c.integer} for c in CRITERIA],
        "flags": [{"id": f.id, "group": f.group, "label": f.label, "help": f.help} for f in FLAGS],
        "sectors": list(SECTORS), "sorts": [{"id": i, "label": label} for i, label in SORTS],
        "conditions": ["a", "b", "c", "d", "e"],
        "limits": {n: {"usual": u, "min": lo, "max": hi, "label": LIMIT_LABELS[n]} for n, (u, lo, hi) in LIMITS.items()},
    }
