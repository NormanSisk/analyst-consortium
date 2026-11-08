"""Deterministic market-data verification snapshot.

The market analyst is an LLM that can confabulate exact numbers — citing a
Bollinger band or a "historically validated bounce" that the underlying data
doesn't support (#830). This module computes a ground-truth snapshot (latest
OHLCV row on or before the analysis date, common indicators, recent closes)
the analyst is told to treat as the source of truth for any exact numeric
claim. Deterministic, no LLM involved.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

# A fixed, common indicator set so the snapshot is the same shape every run.
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _verified_rows(symbol: str, as_of_date: str) -> pd.DataFrame:
    """OHLCV on or before as_of_date, date-sorted. Raises NoMarketDataError if nothing usable.

    ``load_ohlcv`` already normalizes the Date column and filters out
    look-ahead rows, but we re-apply the cutoff defensively — this is a
    verification path, so it must not trust its input to be pre-filtered.
    """
    # As reported: this snapshot is quoted by the agents as exact prices, so a
    # gap-filled cell would put the previous session's number under this date.
    data = load_ohlcv(symbol, as_of_date, fill_gaps=False)
    if data is None or data.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), "no price rows")

    df = data.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(as_of_date)].sort_values("Date")
    if df.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), f"no price rows on or before {as_of_date}")
    return df


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return price_text(value)
    return str(value)


def _signed_percent(value: float, reference: float) -> str:
    return f"{(value / reference - 1) * 100:+.1f}%"


def _computed_facts(df: pd.DataFrame, stock_df, window: int) -> list[str]:
    """Distances, drawdown, moving-average cross state and volume ratio, worked out in code.

    The analyst is an LLM and gets this arithmetic wrong (a drawdown off by two
    points, a death cross called absent while the 50-day sat under the 200-day), so
    every figure it is likely to quote is computed here.
    """
    lines: list[str] = []
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])

    if len(close) > 1:
        prior = float(close.iloc[-2])
        lines.append(
            f"- Change from the prior session's close ({_fmt(df['Date'].iloc[-2])}, "
            f"{price_text(prior)}): {_signed_percent(last, prior)}"
        )

    recent = df.tail(window)
    peak = recent.loc[recent["Close"].astype(float).idxmax()]
    trough = recent.loc[recent["Close"].astype(float).idxmin()]
    lines.append(
        f"- Highest close of the last {len(recent)} sessions: {price_text(float(peak['Close']))} on "
        f"{_fmt(peak['Date'])}; the latest close is {_signed_percent(last, float(peak['Close']))} from it"
    )
    lines.append(
        f"- Lowest close of the last {len(recent)} sessions: {price_text(float(trough['Close']))} on "
        f"{_fmt(trough['Date'])}; the latest close is {_signed_percent(last, float(trough['Close']))} from it"
    )

    year = df.tail(252)
    if len(year) >= 100:
        high, low = float(year["High"].max()), float(year["Low"].min())
        lines.append(
            f"- Range of the last {len(year)} sessions: high {price_text(high)} "
            f"({_signed_percent(last, high)} from it), low {price_text(low)} "
            f"({_signed_percent(last, low)} from it)"
        )

    for name, label in (("close_10_ema", "10-day EMA"), ("close_50_sma", "50-day SMA"),
                        ("close_200_sma", "200-day SMA")):
        try:
            value = float(stock_df[name].iloc[-1])
        except Exception:  # noqa: BLE001 — a missing average is skipped, not fatal
            continue
        if pd.notna(value):
            side = "above" if last >= value else "below"
            lines.append(f"- Latest close is {side} the {label} ({price_text(value)}): {_signed_percent(last, value)}")

    try:
        gap = (stock_df["close_50_sma"] - stock_df["close_200_sma"]).dropna()
    except Exception:  # noqa: BLE001
        gap = pd.Series(dtype=float)
    if len(gap) >= 2:
        fast_above = (gap > 0)
        state = ("The 50-day SMA is ABOVE the 200-day SMA (golden-cross regime)" if fast_above.iloc[-1]
                 else "The 50-day SMA is BELOW the 200-day SMA (death-cross regime)")
        flips = [i for i in range(1, len(fast_above)) if fast_above.iloc[i] != fast_above.iloc[i - 1]]
        if flips:
            cross = flips[-1]
            kind = "golden cross" if fast_above.iloc[cross] else "death cross"
            lines.append(
                f"- {state}. The last crossover was a {kind} on {_fmt(df.loc[gap.index[cross], 'Date'])}, "
                f"{len(gap) - 1 - cross} sessions ago"
            )
        else:
            lines.append(f"- {state}; no crossover in the {len(gap)} sessions with both averages")

    if len(df) >= 21 and df["Volume"].iloc[-21:-1].mean() > 0:
        average = float(df["Volume"].iloc[-21:-1].mean())
        volume = float(df["Volume"].iloc[-1])
        lines.append(
            f"- Latest volume {volume:,.0f} is {volume / average:.2f}x the average of the 20 sessions "
            f"before it ({average:,.0f})"
        )
    return lines


def build_verified_market_snapshot(
    symbol: str,
    as_of_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> str:
    """Render a ground-truth snapshot: latest OHLCV row, indicators, recent closes."""
    # `df` keeps the original capitalized OHLCV columns (Open/High/Low/Close/
    # Volume); stockstats `wrap()` lowercases columns and adds indicator
    # columns, so read raw prices from `df` and indicators from `stock_df`.
    df = _verified_rows(symbol, as_of_date)
    stock_df = wrap(df.copy())

    selected = tuple(indicators or DEFAULT_SNAPSHOT_INDICATORS)
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]  # triggers stockstats calculation
            indicator_values[name] = _fmt(stock_df.iloc[-1][name])
        except Exception as exc:  # noqa: BLE001 — one bad indicator shouldn't sink the snapshot
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {as_of_date}",
        f"- Latest trading row used: {latest_date}",
        "- Rows after the requested analysis date are excluded before verification.",
    ]
    if df.attrs.get("open_session"):
        lines.append(
            f"- The {df.attrs['open_session']} session was still open when this was retrieved. Its "
            "partial bar is left out, so every figure below is as of the previous finished session; "
            "the live price is in the fundamentals data (Last Price)."
        )
    lines += [
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        lines.append(f"| {field} | {_fmt(latest.get(field))} |")

    lines += ["", "### Verified technical indicators (latest row)", "",
              "| Indicator | Value |", "|---|---:|"]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += ["", f"### Recent verified closes (last {len(recent)} rows)", "",
              "| Date | Close |", "|---|---:|"]
    for _, row in recent.iterrows():
        lines.append(f"| {_fmt(row['Date'])} | {_fmt(row.get('Close'))} |")

    facts = _computed_facts(df, stock_df, window)
    if facts:
        lines += ["", "### Computed facts (worked out by code from the rows above)", ""] + facts
        lines.append(
            "Quote these figures for percentages, drawdowns, moving-average crossovers and "
            "volume comparisons instead of calculating your own."
        )

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return "\n".join(lines)
