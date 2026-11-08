"""A stock's fundamentals as plain numbers, read from the dicts the data layer returns (a local addition).

``build_facts`` never raises on missing or odd data: a number it cannot find is None, and the checks treat None as "not
known" (which counts as a condition not met, as in the analysis). Statement lines come as {label: {quarter end: value}}.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

CASH_LABELS = ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")
YEAR_AGO_WINDOW = (335, 395)           # days before the latest quarter end that count as "the same quarter a year ago"


@dataclass
class Estimate:
    """What the analysts' consensus for one fiscal year is doing."""

    up: float | None = None             # analysts who raised it in the last 30 days
    down: float | None = None           # analysts who cut it in the last 30 days
    change_90d: float | None = None     # consensus EPS now against 90 days ago (0.05 = up 5%)
    growth: float | None = None         # expected EPS growth for the year


@dataclass
class Facts:
    symbol: str
    name: str = ""
    sector: str = ""
    industry: str = ""
    sector_key: str = ""
    industry_key: str = ""
    currency: str = ""                  # the currency the statements are in
    market_cap: float | None = None
    forward_pe: float | None = None
    trailing_pe: float | None = None
    analysts: int | None = None
    revenue_growth: float | None = None
    financial: bool = False             # banks, insurers and the like: cash-flow tests do not describe them
    fy0: Estimate = field(default_factory=Estimate)
    fy1: Estimate = field(default_factory=Estimate)
    latest_quarter: str | None = None
    op_margin: float | None = None
    op_margin_year_ago: float | None = None
    ocf: float | None = None
    ocf_year_ago: float | None = None
    fcf_quarters: list[float] = field(default_factory=list)      # newest first, up to four
    cash: float | None = None
    debt: float | None = None
    debt_year_ago: float | None = None
    shares: float | None = None
    shares_year_ago: float | None = None
    errors: list[str] = field(default_factory=list)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def _series(lines: dict, label: str) -> list[tuple[date, float]]:
    """(quarter end, value) newest first for a label, or an empty list."""
    out = []
    for key, value in (lines.get(label) or {}).items():
        try:
            out.append((date.fromisoformat(key), float(value)))
        except (TypeError, ValueError):
            continue
    return sorted(out, reverse=True)


def _latest_and_year_ago(series: list[tuple[date, float]]) -> tuple[date | None, float | None, float | None]:
    """The newest value and the value for the same quarter a year earlier (None if the statement does not reach back)."""
    if not series:
        return None, None, None
    latest_date, latest = series[0]
    low, high = (latest_date - timedelta(days=d) for d in YEAR_AGO_WINDOW[::-1])
    for when, value in series[1:]:
        if low <= when <= high:
            return latest_date, latest, value
    return latest_date, latest, None


def _first_series(lines: dict, labels) -> list[tuple[date, float]]:
    for label in labels:
        found = _series(lines, label)
        if found:
            return found
    return []


def _estimate(raw: dict, period: str) -> Estimate:
    trend = (raw.get("eps_trend") or {}).get(period) or {}
    revisions = (raw.get("eps_revisions") or {}).get(period) or {}
    estimate = (raw.get("estimate") or {}).get(period) or {}
    now, before = _number(trend.get("current")), _number(trend.get("90daysAgo"))
    change = (now / before - 1) if now is not None and before is not None and before > 0 else None
    return Estimate(up=_number(revisions.get("upLast30days")), down=_number(revisions.get("downLast30days")),
                    change_90d=change, growth=_number(estimate.get("growth")))


def build_facts(raw: dict) -> Facts:
    """Facts from the data layer's dicts (``fetch_fundamentals``). Pieces that are missing are None."""
    info = raw.get("info") or {}
    facts = Facts(symbol=str(raw.get("symbol") or ""), name=str(info.get("shortName") or info.get("longName") or ""),
                  sector=str(info.get("sector") or ""), industry=str(info.get("industry") or ""),
                  sector_key=str(info.get("sectorKey") or ""), industry_key=str(info.get("industryKey") or ""),
                  currency=str(info.get("financialCurrency") or info.get("currency") or ""),
                  market_cap=_number(info.get("marketCap")), forward_pe=_number(info.get("forwardPE")),
                  trailing_pe=_number(info.get("trailingPE")), revenue_growth=_number(info.get("revenueGrowth")),
                  errors=list(raw.get("errors") or []))
    analysts = _number(info.get("numberOfAnalystOpinions"))
    facts.analysts = int(analysts) if analysts is not None else None
    facts.financial = facts.sector == "Financial Services"
    facts.fy0, facts.fy1 = _estimate(raw, "0y"), _estimate(raw, "+1y")

    income, cashflow, balance = raw.get("income") or {}, raw.get("cashflow") or {}, raw.get("balance") or {}
    when, revenue, revenue_ago = _latest_and_year_ago(_series(income, "Total Revenue"))
    _, profit, profit_ago = _latest_and_year_ago(_series(income, "Operating Income"))
    facts.latest_quarter = when.isoformat() if when else None
    if revenue and profit is not None and revenue > 0:
        facts.op_margin = profit / revenue
    if revenue_ago and profit_ago is not None and revenue_ago > 0:
        facts.op_margin_year_ago = profit_ago / revenue_ago
    _, facts.shares, facts.shares_year_ago = _latest_and_year_ago(_series(income, "Diluted Average Shares"))
    _, facts.ocf, facts.ocf_year_ago = _latest_and_year_ago(_series(cashflow, "Operating Cash Flow"))
    facts.fcf_quarters = [value for _, value in _series(cashflow, "Free Cash Flow")[:4]]
    cash = _first_series(balance, CASH_LABELS)
    facts.cash = cash[0][1] if cash else None
    _, facts.debt, facts.debt_year_ago = _latest_and_year_ago(_series(balance, "Total Debt"))
    return facts
