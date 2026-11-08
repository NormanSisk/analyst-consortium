"""Do some analysts do better than others, and does it last? (a local addition)

A study of Yahoo's rating and price-target history. Yahoo names the firm, not the analyst, so everything here is about
firms. It needs no network: the downloads are in ``tradingagents.dataflows.vendors.yahoo.analyst_history`` and this
module only does arithmetic on frames it is given, so it can be tested on made-up data.

Two kinds of call are scored.

* **Rating changes** (upgrade, downgrade, a first rating that is bullish or bearish). The score of a call is its
  direction times the stock's return minus the market's over the next ``horizon`` trading days, entered at the close of
  the first trading day AFTER the call (so a follower who reads the news the next day could have acted). A second score,
  "timing only", subtracts the stock's own average over that calendar year, so a firm gets no credit for simply covering
  stocks that rose that year.
* **Price targets**. A target is compared with what the stock did over the next 252 trading days: was it reached at any
  time, was it reached at the end, how far off was it, and how optimistic. The error is measured relative to what the
  other firms' targets on the same stock in the same quarter missed by, so a firm is not blamed for covering a volatile
  stock.

The question that matters is **persistence**: rank firms using only calls whose outcomes were fully known before a year
began, then see whether the top third did better than the bottom third in that year. If past accuracy does not predict
future accuracy, weighting analysts by it would add nothing. Every year's ranking uses only the past.

Known limits, also printed in the report: the stocks are today's large companies (survivors, so buy calls look better
than they were), Yahoo's history can be incomplete, firm names change, calls overlap in time and stocks move together
(so the real uncertainty is larger than the statistics shown), and with dozens of firms a few will look good by luck.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BULLISH = {"buy", "strong buy", "outperform", "overweight", "positive", "market outperform", "sector outperform",
           "accumulate", "add", "top pick", "long-term buy", "speculative buy", "moderate buy", "trading buy"}
NEUTRAL = {"hold", "neutral", "equal-weight", "equal weight", "market perform", "peer perform", "perform",
           "sector perform", "sector weight", "in-line", "inline", "mixed", "fair value", "market weight"}
BEARISH = {"sell", "strong sell", "underperform", "underweight", "negative", "reduce", "market underperform",
           "sector underperform", "moderate sell"}

# Names Yahoo spells several ways, or that changed. Matched after the cleaning in firm_key.
FIRM_ALIASES = {
    "b of a": "bank of america", "bofa": "bank of america", "bofa securities": "bank of america",
    "bank of america": "bank of america", "merrill lynch": "bank of america", "bank of america merrill lynch": "bank of america",
    "jp morgan": "jpmorgan", "jpmorgan chase": "jpmorgan", "jp morgan chase": "jpmorgan", "td cowen": "cowen",
    "cowen": "cowen", "sanford c bernstein": "bernstein", "wells fargo securities": "wells fargo",
    "stifel nicolaus": "stifel", "deutsche bank": "deutsche bank", "credit suisse": "credit suisse",
}
_GENERIC_TAIL = {"securities", "capital", "markets", "co", "group", "research", "llc", "inc", "partners", "isi"}


def grade_direction(grade) -> int | None:
    """+1 for a bullish grade, -1 for a bearish one, 0 for a neutral one, None for anything unrecognized."""
    text = re.sub(r"\s+", " ", str(grade or "").strip().lower())
    if text in BULLISH:
        return 1
    if text in BEARISH:
        return -1
    if text in NEUTRAL:
        return 0
    return None


def firm_key(name) -> str:
    """A firm's name cleaned so spellings of one firm match ("JP Morgan" and "JPMorgan", "Cowen & Co." and "TD Cowen")."""
    text = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower().replace("&", " "))
    words = text.split()
    while len(words) > 1 and words[-1] in _GENERIC_TAIL:
        words.pop()
    cleaned = " ".join(words)
    return FIRM_ALIASES.get(cleaned, cleaned)


def _tstat(values) -> float | None:
    values = [v for v in values if v == v]
    if len(values) < 3:
        return None
    sd = float(np.std(values, ddof=1))
    return None if sd < 1e-12 else float(np.mean(values) / (sd / math.sqrt(len(values))))     # identical values: no spread to test


# --------------------------------------------------------------------------- rating changes

def build_rating_calls(histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per rating call: symbol, firm, date, direction (+1 or -1), kind (upgrade, downgrade or initiate).

    A "maintains" or "reiterates" is not a call: it says nothing new. A first rating counts only when bullish or bearish.
    """
    rows = []
    for symbol, history in histories.items():
        if history is None or len(history) == 0:
            continue
        for row in history.itertuples(index=False):
            action = str(getattr(row, "Action", "") or "").lower()
            if action == "up":
                direction, kind = 1, "upgrade"
            elif action == "down":
                direction, kind = -1, "downgrade"
            elif action == "init":
                direction, kind = grade_direction(getattr(row, "ToGrade", "")), "initiate"
                if not direction:
                    continue
            else:
                continue
            rows.append((symbol, firm_key(getattr(row, "Firm", "")), pd.Timestamp(row.date).normalize(), direction, kind))
    calls = pd.DataFrame(rows, columns=["symbol", "firm", "date", "direction", "kind"])
    return calls.drop_duplicates(["symbol", "firm", "date", "direction"]).reset_index(drop=True)


def attach_outcomes(calls: pd.DataFrame, prices: dict[str, pd.DataFrame], bench: pd.Series, horizon: int) -> pd.DataFrame:
    """Each call with what followed: entry day, exit day, excess return, timing-only excess, and both signed scores.

    ``prices`` maps a symbol to a frame with an ``adj`` column; ``bench`` is the market's adjusted close. A call is left
    out when the stock has no price history, or its window has not finished.
    """
    parts = []
    for symbol, group in calls.groupby("symbol"):
        if symbol not in prices:
            continue
        adj = prices[symbol]["adj"].dropna()
        if len(adj) <= horizon + 1:
            continue
        market = bench.reindex(adj.index).ffill()
        excess = (adj.shift(-horizon) / adj - 1) - (market.shift(-horizon) / market - 1)
        exits = pd.Series(adj.index, index=adj.index).shift(-horizon)
        year_mean = excess.groupby(excess.index.year).transform("mean")
        positions = adj.index.searchsorted(pd.DatetimeIndex(group["date"]), side="right")     # the first day AFTER the call
        keep = positions < len(adj)
        group, positions = group[keep], positions[keep]
        out = group.copy()
        out["entry"] = adj.index[positions]
        out["exit"] = exits.iloc[positions].values
        out["excess"] = excess.iloc[positions].values
        out["timing"] = out["excess"] - year_mean.iloc[positions].values
        parts.append(out)
    if not parts:
        return pd.DataFrame(columns=[*calls.columns, "entry", "exit", "excess", "timing", "signed", "signed_timing"])
    result = pd.concat(parts, ignore_index=True).dropna(subset=["excess", "exit"])
    result["signed"] = result["direction"] * result["excess"]
    result["signed_timing"] = result["direction"] * result["timing"]
    return result


def information_spread(outcomes: pd.DataFrame, min_side: int = 15) -> dict:
    """Do bullish calls beat bearish ones? The average excess return after each, and the spread year by year."""
    bull, bear = outcomes[outcomes["direction"] > 0], outcomes[outcomes["direction"] < 0]
    yearly = []
    for year in sorted(outcomes["entry"].dt.year.unique()):
        b, s = bull[bull["entry"].dt.year == year]["excess"], bear[bear["entry"].dt.year == year]["excess"]
        if len(b) >= min_side and len(s) >= min_side:
            yearly.append({"year": int(year), "bull_n": len(b), "bear_n": len(s), "spread": float(b.mean() - s.mean())})
    spreads = [row["spread"] for row in yearly]
    return {"bull_n": len(bull), "bear_n": len(bear),
            "bull_mean": float(bull["excess"].mean()) if len(bull) else None,
            "bear_mean": float(bear["excess"].mean()) if len(bear) else None,
            "years": yearly, "positive_years": sum(1 for v in spreads if v > 0),
            "mean_spread": float(np.mean(spreads)) if spreads else None, "t_stat": _tstat(spreads)}


def firm_skill(outcomes: pd.DataFrame, min_calls: int = 20) -> pd.DataFrame:
    """Per firm: number of calls, average scores, and the share of calls that did better than the market in their direction."""
    grouped = outcomes.groupby("firm")
    table = pd.DataFrame({
        "calls": grouped.size(), "mean_signed": grouped["signed"].mean(), "mean_timing": grouped["signed_timing"].mean(),
        "hit_rate": grouped["signed"].apply(lambda s: float((s > 0).mean())),
    })
    return table[table["calls"] >= min_calls].sort_values("mean_timing", ascending=False)


def walk_forward(outcomes: pd.DataFrame, min_calls: int = 10, metric: str = "signed_timing", first_year: int = 2016) -> dict:
    """Does past accuracy predict future accuracy? Rank firms on outcomes known before each year, then score that year.

    For each year, firms with at least ``min_calls`` finished calls before it are split into thirds by their average
    ``metric``. The year's calls by the top third are compared with the year's calls by the bottom third.
    """
    rows = []
    for year in sorted(outcomes["entry"].dt.year.unique()):
        if year < first_year:
            continue
        start = pd.Timestamp(year=int(year), month=1, day=1)
        prior = outcomes[outcomes["exit"] < start]                      # outcomes completely known before this year began
        stats = prior.groupby("firm")[metric].agg(["mean", "count"])
        stats = stats[stats["count"] >= min_calls]
        if len(stats) < 6:
            continue
        third = len(stats) // 3
        ranked = stats["mean"].sort_values(ascending=False)
        top, bottom = set(ranked.index[:third]), set(ranked.index[-third:])
        now = outcomes[outcomes["entry"].dt.year == year]
        top_calls, bottom_calls = now[now["firm"].isin(top)][metric], now[now["firm"].isin(bottom)][metric]
        if len(top_calls) < 10 or len(bottom_calls) < 10:
            continue
        rows.append({"year": int(year), "firms_ranked": len(stats), "top_calls": len(top_calls), "bottom_calls": len(bottom_calls),
                     "top_mean": float(top_calls.mean()), "bottom_mean": float(bottom_calls.mean()),
                     "spread": float(top_calls.mean() - bottom_calls.mean()), "all_mean": float(now[metric].mean())})
    spreads = [r["spread"] for r in rows]
    return {"metric": metric, "years": rows, "positive_years": sum(1 for v in spreads if v > 0),
            "mean_spread": float(np.mean(spreads)) if spreads else None, "t_stat": _tstat(spreads)}


# --------------------------------------------------------------------------- price targets

def build_target_events(histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per new or changed price target: symbol, firm, date, target. A target merely maintained is not new."""
    rows = []
    for symbol, history in histories.items():
        if history is None or len(history) == 0:
            continue
        for row in history.itertuples(index=False):
            target = pd.to_numeric(getattr(row, "currentPriceTarget", None), errors="coerce")
            if target != target or target <= 0:
                continue
            if str(getattr(row, "priceTargetAction", "") or "") not in ("Raises", "Lowers", "Announces"):
                continue
            rows.append((symbol, firm_key(getattr(row, "Firm", "")), pd.Timestamp(row.date).normalize(), float(target)))
    events = pd.DataFrame(rows, columns=["symbol", "firm", "date", "target"])
    return events.drop_duplicates(["symbol", "firm", "date"], keep="last").reset_index(drop=True)


def split_factor(splits: pd.Series | None, date: pd.Timestamp) -> float:
    """How many of today's shares one share on ``date`` became: the product of every split after that date."""
    if splits is None or len(splits) == 0:
        return 1.0
    later = splits[splits.index > date]
    return float(later.prod()) if len(later) else 1.0


def target_outcomes(events: pd.DataFrame, prices: dict[str, pd.DataFrame], horizon: int = 252,
                    min_implied: float = -0.8, max_implied: float = 3.0,
                    splits: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    """Each price target against the next ``horizon`` trading days.

    The starting price is the close on the target's date (or the last close before it). Yahoo's prices are adjusted to
    today's shares but a target is in the shares of its day, so each target is first divided by the splits that came
    after it (``splits`` maps a symbol to its split ratios by date). A target whose implied move is still beyond
    ``min_implied`` to ``max_implied`` is dropped as a probable units mismatch; the number dropped is in
    ``result.attrs["excluded"]`` and the number that needed a split adjustment in ``result.attrs["split_adjusted"]``.
    Targets whose window has not finished are left out.
    """
    rows, excluded, adjusted = [], 0, 0
    for symbol, group in events.groupby("symbol"):
        if symbol not in prices:
            continue
        frame = prices[symbol]
        index, close = frame.index, frame["close"].to_numpy()
        high, low = frame["high"].to_numpy(), frame["low"].to_numpy()
        symbol_splits = (splits or {}).get(symbol)
        for date, firm, target in group[["date", "firm", "target"]].itertuples(index=False):
            p0 = index.searchsorted(date, side="right") - 1          # the last trading day on or before the target's date
            if p0 < 0 or p0 + horizon >= len(index):
                continue
            factor = split_factor(symbol_splits, date)
            if factor != 1.0:
                target, adjusted = target / factor, adjusted + 1
            start = float(close[p0])
            implied = target / start - 1
            if not (min_implied <= implied <= max_implied):
                excluded += 1
                continue
            window = slice(p0 + 1, p0 + horizon + 1)
            end = float(close[p0 + horizon])
            up = target >= start
            rows.append({
                "symbol": symbol, "firm": firm, "date": date, "target": target, "start": start, "implied": implied,
                "realized": end / start - 1,
                "hit_any": bool(np.nanmax(high[window]) >= target) if up else bool(np.nanmin(low[window]) <= target),
                "hit_end": end >= target if up else end <= target,
            })
    result = pd.DataFrame(rows, columns=["symbol", "firm", "date", "target", "start", "implied", "realized", "hit_any", "hit_end"])
    result["date"] = pd.to_datetime(result["date"])
    result["abs_err"] = (result["realized"].astype(float) - result["implied"].astype(float)).abs()
    result["optimism"] = result["implied"] - result["realized"]
    result["quarter"] = result["date"].dt.to_period("Q")
    for col in ("abs_err", "optimism"):                                # relative to the other firms on the same stock that quarter
        group = result.groupby(["symbol", "quarter"])[col]
        result["rel_" + col] = (result[col] - group.transform("mean")).where(group.transform("count") >= 3)
    result.attrs["excluded"], result.attrs["split_adjusted"] = excluded, adjusted
    return result


def target_summary(targets: pd.DataFrame) -> dict:
    if targets.empty:
        return {"n": 0}
    return {"n": len(targets), "mean_implied": float(targets["implied"].mean()), "mean_realized": float(targets["realized"].mean()),
            "mean_optimism": float(targets["optimism"].mean()), "median_abs_err": float(targets["abs_err"].median()),
            "hit_end": float(targets["hit_end"].mean()), "hit_any": float(targets["hit_any"].mean()),
            "excluded": int(targets.attrs.get("excluded", 0)), "split_adjusted": int(targets.attrs.get("split_adjusted", 0))}


def firm_target_skill(targets: pd.DataFrame, min_events: int = 25) -> pd.DataFrame:
    grouped = targets.groupby("firm")
    table = pd.DataFrame({"targets": grouped.size(), "rel_abs_err": grouped["rel_abs_err"].mean(),
                          "optimism": grouped["optimism"].mean(), "hit_end": grouped["hit_end"].mean(),
                          "hit_any": grouped["hit_any"].mean()})
    return table[table["targets"] >= min_events].sort_values("rel_abs_err")


def target_persistence(targets: pd.DataFrame, split: str = "2019-01-01", min_each: int = 10) -> dict:
    """Do firms whose targets missed least before ``split`` also miss least after it?"""
    cut = pd.Timestamp(split)
    early, late = targets[targets["date"] < cut], targets[targets["date"] >= cut]
    a = early.groupby("firm")["rel_abs_err"].agg(["mean", "count"])
    b = late.groupby("firm")["rel_abs_err"].agg(["mean", "count"])
    both = a[a["count"] >= min_each].join(b[b["count"] >= min_each], lsuffix="_early", rsuffix="_late", how="inner")
    if len(both) < 6:
        return {"firms": len(both), "spearman": None, "best_third_later": None, "worst_third_later": None}
    third = len(both) // 3
    ranked = both.sort_values("mean_early")
    return {"firms": len(both), "split": split,
            # Spearman's rank correlation is the ordinary correlation of the ranks (no extra package needed)
            "spearman": float(both["mean_early"].rank().corr(both["mean_late"].rank())),
            "best_third_later": float(ranked["mean_late"].iloc[:third].mean()),
            "worst_third_later": float(ranked["mean_late"].iloc[-third:].mean())}


# --------------------------------------------------------------------------- the whole study and its report

@dataclass
class StudyResult:
    horizons: tuple
    stocks: int = 0
    actions: int = 0
    rating_calls: int = 0
    firms_seen: int = 0
    date_range: tuple = (None, None)
    information: dict = field(default_factory=dict)       # horizon -> information_spread
    persistence: dict = field(default_factory=dict)       # (horizon, metric) -> walk_forward
    firm_table: dict = field(default_factory=dict)        # horizon -> firm_skill
    outcomes: dict = field(default_factory=dict)          # horizon -> outcomes frame
    targets: pd.DataFrame | None = None
    target_overall: dict = field(default_factory=dict)
    target_firms: pd.DataFrame | None = None
    target_persist: dict = field(default_factory=dict)


def run_study(histories: dict[str, pd.DataFrame], prices: dict[str, pd.DataFrame], bench_symbol: str = "SPY",
              horizons: tuple = (21, 63), splits: dict[str, pd.Series] | None = None) -> StudyResult:
    """Everything: rating-change information and persistence at each horizon, and the price-target study."""
    bench = prices[bench_symbol]["adj"]
    result = StudyResult(horizons=tuple(horizons))
    result.stocks = sum(1 for h in histories.values() if h is not None and len(h))
    all_dates = [pd.to_datetime(h["date"]) for h in histories.values() if h is not None and len(h)]
    result.actions = sum(len(h) for h in histories.values() if h is not None)
    if all_dates:
        joined = pd.concat(all_dates)
        result.date_range = (joined.min().date(), joined.max().date())
    calls = build_rating_calls(histories)
    result.rating_calls, result.firms_seen = len(calls), calls["firm"].nunique() if len(calls) else 0
    for horizon in horizons:
        outcomes = attach_outcomes(calls, prices, bench, horizon)
        result.outcomes[horizon] = outcomes
        result.information[horizon] = information_spread(outcomes)
        result.firm_table[horizon] = firm_skill(outcomes)
        for metric in ("signed_timing", "signed"):
            result.persistence[(horizon, metric)] = walk_forward(outcomes, metric=metric)
    events = build_target_events(histories)
    result.targets = target_outcomes(events, prices, splits=splits)
    result.target_overall = target_summary(result.targets)
    result.target_firms = firm_target_skill(result.targets)
    result.target_persist = target_persistence(result.targets)
    return result


def _pct(value, digits: int = 1) -> str:
    return "n/a" if value is None or value != value else f"{value * 100:+.{digits}f}%"


def _num(value, digits: int = 2) -> str:
    return "n/a" if value is None or value != value else f"{value:.{digits}f}"


def render_report(result: StudyResult, universe: int) -> str:
    """The study as Markdown, with the limits written out and no conclusion drawn for the reader."""
    lines = ["# Do some analysts do better than others, and does it last?", "",
             f"Stocks asked for: {universe}. Stocks with rating history: {result.stocks}. Rating and target actions: {result.actions:,} "
             f"({result.date_range[0]} to {result.date_range[1]}). Rating calls scored: {result.rating_calls:,} by {result.firms_seen} firms.", ""]
    lines += ["## 1. Do rating changes carry any information?", "",
              "Excess return = the stock's return minus SPY's, from the close of the first trading day after the call. "
              "If analysts' changes mean something, bullish calls should be followed by better returns than bearish ones.", "",
              "| Window | Bullish calls | Avg excess | Bearish calls | Avg excess | Spread by year (mean) | Years positive | t-stat of yearly spread |",
              "|---|---|---|---|---|---|---|---|"]
    for horizon in result.horizons:
        info = result.information[horizon]
        lines.append(f"| {horizon} trading days | {info['bull_n']:,} | {_pct(info['bull_mean'])} | {info['bear_n']:,} | {_pct(info['bear_mean'])} "
                     f"| {_pct(info['mean_spread'])} | {info['positive_years']} of {len(info['years'])} | {_num(info['t_stat'])} |")
    lines += ["", "## 2. Does skill last? (each year uses only outcomes known before it began)", "",
              "Firms are ranked on past calls; the table compares the year's calls by the top third with those by the bottom third. "
              "'Timing only' removes credit for covering stocks that happened to rise that year. A reliable skill would show a positive "
              "spread in most years.", ""]
    for horizon in result.horizons:
        for metric, label in (("signed_timing", "timing only"), ("signed", "raw")):
            wf = result.persistence[(horizon, metric)]
            lines.append(f"**{horizon}-day window, {label}:** top-minus-bottom spread averaged {_pct(wf['mean_spread'])} per call, positive in "
                         f"{wf['positive_years']} of {len(wf['years'])} years, t-stat of yearly spreads {_num(wf['t_stat'])}.")
        lines.append("")
    horizon = result.horizons[-1]
    lines += [f"Year by year ({horizon}-day window, timing only):", "", "| Year | Firms ranked | Top-third calls | Bottom-third calls | Top avg | Bottom avg | Spread |",
              "|---|---|---|---|---|---|---|"]
    for row in result.persistence[(horizon, "signed_timing")]["years"]:
        lines.append(f"| {row['year']} | {row['firms_ranked']} | {row['top_calls']} | {row['bottom_calls']} | {_pct(row['top_mean'])} | "
                     f"{_pct(row['bottom_mean'])} | {_pct(row['spread'])} |")
    table = result.firm_table[horizon]
    lines += ["", f"## 3. Firms with the best and worst record ({horizon}-day window, at least 20 calls)", "",
              "With dozens of firms, a few will top any list by luck. Use this to see the spread, not to pick a winner.", "",
              "| Firm | Calls | Avg (timing only) | Avg (raw) | Calls beating the market |", "|---|---|---|---|---|"]
    for name, row in pd.concat([table.head(8), table.tail(8)]).iterrows():
        lines.append(f"| {name} | {int(row['calls'])} | {_pct(row['mean_timing'])} | {_pct(row['mean_signed'])} | {row['hit_rate'] * 100:.0f}% |")
    t = result.target_overall
    lines += ["", "## 4. Price targets (12-month window, 252 trading days)", ""]
    if t.get("n"):
        lines += [f"Targets scored: {t['n']:,} ({t['split_adjusted']:,} adjusted for stock splits that came after them; dropped as probable "
                  f"units mismatches: {t['excluded']}). The average target implied "
                  f"{_pct(t['mean_implied'])}; the stocks actually returned {_pct(t['mean_realized'])}, so targets were {_pct(t['mean_optimism'])} too "
                  f"optimistic on average. The typical miss was {t['median_abs_err'] * 100:.0f} percentage points. Reached at the end of 12 months: "
                  f"{t['hit_end'] * 100:.0f}%. Reached at some point within 12 months: {t['hit_any'] * 100:.0f}%.", ""]
        p = result.target_persist
        if p.get("spearman") is not None:
            lines += [f"Persistence: {p['firms']} firms with enough targets both before and after {p['split']}. The rank correlation between a firm's "
                      f"relative miss before and after is {_num(p['spearman'])} (0 = no persistence, 1 = perfect). The best third before missed by "
                      f"{_pct(p['best_third_later'])} relative to peers afterward; the worst third by {_pct(p['worst_third_later'])}.", ""]
        tf = result.target_firms
        lines += ["| Firm | Targets | Miss vs peers (lower is better) | Optimism | Reached at end | Reached within a year |", "|---|---|---|---|---|---|"]
        for name, row in pd.concat([tf.head(6), tf.tail(6)]).iterrows():
            lines.append(f"| {name} | {int(row['targets'])} | {_pct(row['rel_abs_err'])} | {_pct(row['optimism'])} | {row['hit_end'] * 100:.0f}% | {row['hit_any'] * 100:.0f}% |")
    else:
        lines.append("No price targets could be scored.")
    lines += ["", "## 5. Limits", "",
              "- Yahoo names the firm, not the analyst; a firm's calls mix many analysts, and analysts move between firms.",
              "- The stocks are today's large companies, so they are survivors: bullish calls on them look better than bullish calls on all stocks were.",
              "- Yahoo's history can be incomplete, and firm names change; some firms may be split or merged wrongly.",
              "- Calls overlap in time and stocks move together, so the statistics here overstate how sure we can be.",
              "- Dozens of firms are compared, so some will rank high by luck alone.",
              "- Past accuracy of a firm says little about a particular new call; this study asks only whether ranking firms on the past helped."]
    return "\n".join(lines) + "\n"
