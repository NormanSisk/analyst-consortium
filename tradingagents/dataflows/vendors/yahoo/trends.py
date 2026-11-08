"""Trend facts: each quarterly figure seen two ways, with the rule-based flags that follow.

An analyst handed a quarter's statements picks which comparison to lead with, and the pick
decides the story: Alphabet's operating margin was "strong" against the same quarter a year
earlier and "falling" against the two quarters before it, both true of one table. This works
the comparisons out in code, so every report starts from the same set: the last five quarters,
the change from a year earlier and from the quarters just before, where the latest sits in its
five-quarter range, and flags where the two views disagree or a pattern deserves a sentence
(cash flow, dilution, debt, buybacks, earnings moved by non-operating items).

The figures are Yahoo's quarterly statements, which carry no filing date, so a run dated in the
past is not served them.
"""

from __future__ import annotations

import logging
import math
from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import withhold_undated_statements
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry

logger = logging.getLogger(__name__)

QUARTERS = 5                    # the latest quarter and the four before it, so the quarter a year back is among them
OPPOSITE_VIEWS_POINTS = 0.3     # margin moves smaller than this are noise, not a disagreement
NON_OPERATING_GAP = 0.4         # net income this far from operating income means non-operating items
SHARE_CHANGE_FLAG = 1.0         # percent change in diluted shares worth a sentence
DEBT_MULTIPLE_FLAG = 1.5        # long-term debt this many times its level a year ago


def _row(frame: pd.DataFrame | None, *labels: str) -> dict[pd.Timestamp, float]:
    """{period end: value} for the first of ``labels`` that the statement has, NaN left out."""
    if frame is None or frame.empty:
        return {}
    for label in labels:
        if label in frame.index:
            series = frame.loc[label]
            return {pd.Timestamp(period): float(value) for period, value in series.items()
                    if value is not None and not (isinstance(value, float) and math.isnan(value))}
    return {}


def _percent(new: float | None, old: float | None) -> float | None:
    """Percent change, undefined when either end is missing or the base is not positive."""
    if new is None or old is None or old <= 0:
        return None
    return (new / old - 1) * 100


def _m(value: float | None) -> str:
    """Millions with a thousands separator; one decimal under 1,000 so small companies are not rounded away."""
    if value is None:
        return "n/a"
    if value == 0:
        return "0"
    millions = value / 1e6
    return f"{millions:,.0f}" if abs(millions) >= 1000 else f"{millions:,.1f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def _pts(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f} pts"


def _streak(values: list[float], falling: bool) -> int:
    """How many quarters in a row, ending with the latest, the figure fell (or rose). ``values`` newest first."""
    count = 0
    for newer, older in zip(values, values[1:], strict=False):
        if (newer < older) if falling else (newer > older):
            count += 1
        else:
            break
    return count


def build_trend_facts(canonical: str, income: pd.DataFrame, cashflow: pd.DataFrame | None,
                      balance: pd.DataFrame | None) -> str:
    """The trend-facts report from the three quarterly statements (columns are period ends)."""
    revenue = _row(income, "Total Revenue", "Operating Revenue")
    operating = _row(income, "Operating Income", "Total Operating Income As Reported")
    net = _row(income, "Net Income", "Net Income Common Stockholders")
    shares = _row(income, "Diluted Average Shares")
    periods = sorted((p for p in revenue if p in operating), reverse=True)[:QUARTERS]     # newest first
    if len(periods) < 2:
        return f"# Trend facts for {canonical}\n\nFewer than two quarters of revenue and operating income are available, so no trend can be worked out."
    latest, prior = periods[0], periods[1]
    two_back = periods[2] if len(periods) > 2 else None
    year_ago = periods[4] if len(periods) > 4 else None

    ocf = _row(cashflow, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    capex = {p: abs(v) for p, v in _row(cashflow, "Capital Expenditure").items()}
    fcf = _row(cashflow, "Free Cash Flow") or {p: ocf[p] - capex[p] for p in ocf if p in capex}
    buyback = {p: abs(v) for p, v in _row(cashflow, "Repurchase Of Capital Stock", "Common Stock Payments").items()}
    debt = _row(balance, "Long Term Debt", "Long Term Debt And Capital Lease Obligation")
    cash = _row(balance, "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")

    flags: list[str] = []
    margin = {p: operating[p] / revenue[p] * 100 for p in periods if revenue[p] > 0}

    # --- The table, oldest quarter first so the path reads left to right in time ---
    table = ["| Quarter end | Revenue | Operating income | Operating margin | Net income | Diluted shares | Operating cash flow | Capex | Free cash flow |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for p in reversed(periods):
        table.append(
            f"| {p:%Y-%m-%d} | {_m(revenue.get(p))} | {_m(operating.get(p))} | "
            f"{'n/a' if p not in margin else f'{margin[p]:.1f}%'} | {_m(net.get(p))} | {_m(shares.get(p))} | "
            f"{_m(ocf.get(p))} | {_m(capex.get(p))} | {_m(fcf.get(p))} |")

    out: list[str] = []

    def both_views(label: str, series: dict, formatter=_pct, base_of: dict | None = None) -> str:
        """'label: +25.0% vs the same quarter a year earlier; +6.5% vs the prior quarter.'

        ``base_of`` names the series the figure is a share of (revenue, for operating income): a
        percent change from a base under 1% of it is a swing from almost nothing, and says so.
        """
        yoy = _percent(series.get(latest), series.get(year_ago)) if year_ago else None
        qoq = _percent(series.get(latest), series.get(prior))
        year_text = formatter(yoy) if year_ago else "year-ago quarter not available"
        if yoy is not None and base_of and base_of.get(year_ago) and series[year_ago] / base_of[year_ago] < 0.01:
            year_text += " (from a very small base)"
        return f"- {label}: {year_text} versus the same quarter a year earlier; {formatter(qoq)} versus the prior quarter."

    if revenue:
        out.append(both_views("Revenue", revenue))
    if operating:
        out.append(both_views("Operating income", operating, base_of=revenue))

    # --- Margin, the figure whose framing flipped a verdict ---
    if latest in margin:
        now = margin[latest]
        yoy = now - margin[year_ago] if year_ago in margin else None
        seq1 = now - margin[prior] if prior in margin else None
        seq2 = now - margin[two_back] if two_back in margin else None
        path = [margin[p] for p in periods if p in margin]
        peak_period = max((p for p in periods if p in margin), key=lambda p: margin[p])
        low_period = min((p for p in periods if p in margin), key=lambda p: margin[p])
        line = (f"- Operating margin is {now:.1f}%: {_pts(yoy)} versus a year earlier (year-over-year view); "
                f"{_pts(seq1)} versus the prior quarter and {_pts(seq2)} versus two quarters earlier (sequential view). "
                f"Over these {len(path)} quarters it ranged from {margin[low_period]:.1f}% ({low_period:%Y-%m-%d}) to "
                f"{margin[peak_period]:.1f}% ({peak_period:%Y-%m-%d}).")
        out.append(line)
        falling, rising = _streak(path, True), _streak(path, False)
        if falling >= 2:
            flags.append(f"Operating margin has fallen {falling} quarters in a row, by {margin[periods[falling]] - now:.1f} points in total.")
        if rising >= 2:
            flags.append(f"Operating margin has risen {rising} quarters in a row.")
        if yoy is not None and seq1 is not None and abs(yoy) >= OPPOSITE_VIEWS_POINTS and abs(seq1) >= OPPOSITE_VIEWS_POINTS \
                and (yoy > 0) != (seq1 > 0):
            flags.append(f"The two views of operating margin disagree: {_pts(yoy)} year over year but {_pts(seq1)} from the prior "
                         "quarter. State both, and say where the latest figure sits in its range.")
        if now - margin[low_period] > 0 and margin[peak_period] - now > 0 and peak_period != latest:
            out.append(f"  The latest margin is {margin[peak_period] - now:.1f} points below the {len(path)}-quarter peak and "
                       f"{now - margin[low_period]:.1f} points above its low.")

    # --- Earnings moved by items that are not operations ---
    gaps = [p for p in periods if p in net and operating.get(p) and abs(net[p] - operating[p]) / abs(operating[p]) > NON_OPERATING_GAP]
    if len(gaps) >= 2:
        flags.append(f"Net income differed from operating income by more than {NON_OPERATING_GAP:.0%} in {len(gaps)} of the last "
                     f"{len(periods)} quarters, so net income and EPS are being moved by non-operating items. Judge the trend on operating income.")

    # --- Cash flow ---
    if ocf and capex:
        lines = []
        if latest in ocf:
            lines.append(both_views("Operating cash flow", ocf))
        if latest in capex:
            lines.append(both_views("Capital spending", capex))
        out.extend(lines)
        ratio_now = capex[latest] / ocf[latest] * 100 if ocf.get(latest) and latest in capex and ocf[latest] > 0 else None
        ratio_then = capex[year_ago] / ocf[year_ago] * 100 if year_ago and ocf.get(year_ago) and year_ago in capex and ocf[year_ago] > 0 else None
        if ratio_now is not None:
            out.append(f"- Capital spending was {ratio_now:.0f}% of operating cash flow"
                       + (f", against {ratio_then:.0f}% a year earlier." if ratio_then is not None else "."))
        if latest in fcf:
            path = ", ".join(f"{_m(fcf[p])}" for p in reversed(periods) if p in fcf)
            out.append(f"- Free cash flow (operating cash flow minus capital spending, Yahoo's definition, which can differ from a "
                       f"company's own) over the same quarters, oldest first: {path}.")
            negative = sum(1 for p in periods if fcf.get(p, 0) < 0)
            if negative >= 2:
                flags.append(f"Free cash flow was negative in {negative} of the last {len(periods)} quarters.")
        if ratio_now is not None and ratio_now > 100:
            flags.append(f"Capital spending exceeded operating cash flow in the latest quarter ({ratio_now:.0f}%).")

    # --- Share count ---
    if shares.get(latest) and year_ago and shares.get(year_ago):
        change = _percent(shares[latest], shares[year_ago])
        out.append(f"- Diluted average shares: {_pct(change)} versus a year earlier.")
        if change is not None and change > SHARE_CHANGE_FLAG:
            flags.append(f"The diluted share count rose {change:.1f}% in a year (dilution).")
        elif change is not None and change < -SHARE_CHANGE_FLAG:
            flags.append(f"The diluted share count fell {abs(change):.1f}% in a year (buybacks).")

    # --- Debt and cash ---
    if debt.get(latest):
        then = debt.get(year_ago) if year_ago else None
        multiple = debt[latest] / then if then else None
        out.append(f"- Long-term debt: {_m(debt[latest])}"
                   + (f", {multiple:.1f} times its level a year earlier ({_m(then)})" if multiple else "")
                   + (f"; {_m(debt[prior])} in the prior quarter." if debt.get(prior) else "."))
        if multiple and multiple >= DEBT_MULTIPLE_FLAG:
            flags.append(f"Long-term debt is {multiple:.1f} times its level a year earlier.")
    if cash.get(latest):
        then = cash.get(year_ago) if year_ago else None
        out.append(f"- Cash and short-term investments: {_m(cash[latest])}" + (f" versus {_m(then)} a year earlier." if then else "."))

    # --- Buybacks ---
    if buyback and latest in buyback:
        then = buyback.get(year_ago) if year_ago else None
        out.append(f"- Share repurchases: {_m(buyback[latest])} in the latest quarter" + (f" versus {_m(then)} a year earlier." if then is not None else "."))
        if buyback[latest] == 0 and then:
            flags.append(f"Share repurchases stopped: none in the latest quarter against {_m(then)} a year earlier.")

    flag_text = "\n".join(f"- {f}" for f in flags) if flags else "- No rule-based flags in the latest quarter."
    return (
        f"# Trend facts for {canonical}\n\n"
        "Worked out in code from Yahoo's quarterly statements, in millions of the reporting currency. Each trend is given two ways, "
        "against the same quarter a year earlier and against the quarters just before it, because the two can point in opposite "
        "directions and the choice of comparison changes the story. Report both whenever they differ.\n\n"
        + "\n".join(table) + "\n\n## The latest quarter, both ways\n\n" + "\n".join(out)
        + "\n\n## Flags to state in the report\n\n" + flag_text
    )


def get_trend_facts(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Year-over-year and sequential trends from the last five quarters, with rule-based flags."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undated_statements(as_of_date, canonical, "Trend facts")
    if withheld:
        return withheld
    income = yf_retry(lambda: yf.Ticker(canonical).quarterly_income_stmt)
    if income is None or income.empty:
        raise_for_empty(ticker, canonical, "quarterly statements")
    cashflow = yf_retry(lambda: yf.Ticker(canonical).quarterly_cashflow)
    balance = yf_retry(lambda: yf.Ticker(canonical).quarterly_balance_sheet)
    return build_trend_facts(canonical, income, cashflow, balance)
