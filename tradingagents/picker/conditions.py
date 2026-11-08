"""The analysis's five "deal" conditions, checked in code (a local addition).

Each check gives PASS, FAIL or UNKNOWN, the numbers, and a plain-words reason. UNKNOWN counts as not met, as it does in the
analysis ("a condition the reports do not establish counts as not met"). The conditions:

  (a) the market is in a drawdown;
  (b) the stock's fall is mostly the market's;
  (c) the company shows no deterioration (estimates, margin, cash flow, debt, share count);
  (d) it passes a survival test (free cash flow, or a year of cash; no run of equity sales);
  (e) the valuation is not stretched (earnings yield against Treasuries, and forward P/E against its peers).

What the quick look does not check, because it needs filings or a judgment: going-concern warnings, when debt falls due, whether
a filing is a financing, and valuation against the stock's own ten-year history. The full analysis reads those.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradingagents.dataflows.vendors.yahoo import market_context as mc
from tradingagents.picker.facts import Facts
from tradingagents.picker.settings import PickerSettings
from tradingagents.picker.stage_a import MarketState, PriceMetrics

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
TITLES = {"a": "Market in a drawdown", "b": "Fall mostly the market's", "c": "No company deterioration",
          "d": "Passes the survival test", "e": "Valuation not stretched"}


@dataclass
class Condition:
    code: str
    status: str
    detail: str

    @property
    def title(self) -> str:
        return TITLES[self.code]

    @property
    def met(self) -> bool:
        return self.status == PASS


def _pct(value: float | None, digits: int = 0, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"


def money(value: float | None, currency: str = "") -> str:
    """A big number in words: 4.02T, 15.8B, 554M. The currency code is added when it is not dollars."""
    if value is None:
        return "n/a"
    sign, size = ("-" if value < 0 else ""), abs(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if size >= limit:
            text = f"{size / limit:.1f}{suffix}"
            break
    else:
        text = f"{size:.0f}"
    return f"{sign}{text}" + (f" {currency}" if currency and currency != "USD" else "")


# (a) --------------------------------------------------------------------------------------------------

def market_condition(state: MarketState) -> Condition:
    if state.drawdown <= -mc.PULLBACK or (state.vix_rank is not None and state.vix_rank >= mc.VIX_HIGH_PERCENTILE):
        parts = [f"the S&P 500 is {-state.drawdown * 100:.1f}% below its high ({state.label})"]
        if state.vix is not None and state.vix_rank is not None:
            parts.append(f"VIX {state.vix:.1f}, above {state.vix_rank * 100:.0f}% of the past year")
        return Condition("a", PASS, "; ".join(parts))
    return Condition("a", FAIL, f"the S&P 500 is only {-state.drawdown * 100:.1f}% below its high ({state.label}) and the VIX is not unusually high")


# (b) --------------------------------------------------------------------------------------------------

def explained_condition(metrics: PriceMetrics | None, settings: PickerSettings) -> Condition:
    if metrics is None or metrics.beta is None:
        return Condition("b", UNKNOWN, "not enough price history to work out a beta")
    needs = {63: settings.share_63, 21: settings.share_21}
    notes, failed, fell_any = [], [], False
    for sessions in (63, 21):
        window = metrics.window(sessions)
        if not window.fell:
            notes.append(f"{sessions} sessions: no fall of 3% or more ({_pct(window.stock, 1, True)})")
            continue
        fell_any = True
        share = window.share if window.share is not None else 0.0
        notes.append(f"{sessions} sessions: stock {_pct(window.stock, 1, True)}, market implies {_pct(window.implied, 1, True)}, "
                     f"so the market explains {'more than all' if share > 1 else _pct(share)} (needs {_pct(needs[sessions])})")
        if share < needs[sessions]:
            failed.append(sessions)
    if not fell_any:
        return Condition("b", FAIL, "; ".join(notes) + ": there is no fall to explain")
    return Condition("b", FAIL if failed else PASS, "; ".join(notes))


# (c) --------------------------------------------------------------------------------------------------

def deterioration_condition(facts: Facts, settings: PickerSettings) -> Condition:
    flags, notes, checked = [], [], 0
    for label, estimate in (("this year's", facts.fy0), ("next year's", facts.fy1)):
        if estimate.up is None and estimate.change_90d is None:
            continue
        checked += 1
        up, down = estimate.up or 0, estimate.down or 0
        change = estimate.change_90d
        if down - up >= settings.revision_min_analysts:
            flags.append(f"{label} EPS estimate: {down:.0f} analysts cut and {up:.0f} raised it in the last 30 days")
        elif change is not None and change <= -settings.estimate_cut_pct / 100:
            flags.append(f"{label} consensus EPS is {_pct(change, 1, True)} over 90 days")
        else:
            notes.append(f"{label} EPS estimate {up:.0f} raised, {down:.0f} cut" + (f", {_pct(change, 1, True)} in 90 days" if change is not None else ""))
    if facts.shares is not None and facts.shares_year_ago:
        checked += 1
        growth = facts.shares / facts.shares_year_ago - 1
        if growth * 100 >= settings.dilution_pct:
            flags.append(f"share count up {_pct(growth, 1)} in a year")
        else:
            notes.append(f"share count {_pct(growth, 1, True)}")
    if not facts.financial:
        if facts.op_margin is not None and facts.op_margin_year_ago is not None:
            checked += 1
            change = (facts.op_margin - facts.op_margin_year_ago) * 100
            if change <= -settings.margin_drop_points:
                flags.append(f"operating margin {_pct(facts.op_margin, 1)}, down {-change:.1f} points from a year ago")
            else:
                notes.append(f"operating margin {_pct(facts.op_margin, 1)} ({change:+.1f} points on the year)")
        if facts.ocf is not None and facts.ocf_year_ago and facts.ocf_year_ago > 0:
            checked += 1
            change = facts.ocf / facts.ocf_year_ago - 1
            if change <= -settings.cash_flow_drop:
                flags.append(f"operating cash flow {_pct(change, 0, True)} on a year ago")
            else:
                notes.append(f"operating cash flow {_pct(change, 0, True)} on the year")
        if facts.debt is not None and facts.debt_year_ago and facts.debt_year_ago > 0:
            checked += 1
            multiple = facts.debt / facts.debt_year_ago
            if multiple >= settings.debt_multiple:
                flags.append(f"total debt {multiple:.1f} times a year ago")
            else:
                notes.append(f"total debt {multiple:.1f} times a year ago")
    if flags:
        return Condition("c", FAIL, "; ".join(flags))
    if checked < 2:
        return Condition("c", UNKNOWN, "too little estimate or statement data to judge" + (f" ({'; '.join(notes)})" if notes else ""))
    return Condition("c", PASS, "; ".join(notes))


# (d) --------------------------------------------------------------------------------------------------

def survival_condition(facts: Facts, settings: PickerSettings) -> Condition:
    if facts.financial:
        return Condition("d", UNKNOWN, "cash-flow survival tests do not describe banks and insurers; the full analysis looks at their capital")
    if len(facts.fcf_quarters) < 4:
        return Condition("d", UNKNOWN, "fewer than four quarters of free cash flow are available")
    cur = facts.currency
    fcf = sum(facts.fcf_quarters)
    debt_note = ""
    if facts.cash is not None and facts.debt is not None:
        debt_note = f"; cash {money(facts.cash, cur)} against debt {money(facts.debt, cur)}"
    reasons = []
    if facts.shares is not None and facts.shares_year_ago and (facts.shares / facts.shares_year_ago - 1) * 100 >= settings.heavy_dilution_pct:
        reasons.append(f"share count up {_pct(facts.shares / facts.shares_year_ago - 1, 0)} in a year: a run of equity sales")
    if fcf > 0:
        text = f"free cash flow over the last four quarters is positive ({money(fcf, cur)}){debt_note}"
    else:
        burn = -fcf / 12
        months = (facts.cash / burn) if facts.cash is not None and burn > 0 else None
        if months is None:
            return Condition("d", UNKNOWN, f"free cash flow is negative ({money(fcf, cur)}) and cash is not known")
        text = f"free cash flow is negative ({money(fcf, cur)}); cash covers {months:.0f} months of burn (needs {settings.runway_months:.0f}){debt_note}"
        if months < settings.runway_months:
            reasons.append(f"cash covers only {months:.0f} months of burn")
    return Condition("d", FAIL if reasons else PASS, "; ".join([*reasons, text]) if reasons else text)


# (e) --------------------------------------------------------------------------------------------------

def valuation_condition(facts: Facts, peer_median_pe: float | None, peer_label: str, treasury: float | None,
                        settings: PickerSettings) -> Condition:
    pe = facts.forward_pe
    if pe is None:
        return Condition("e", UNKNOWN, "no forward P/E is available")
    if pe <= 0:
        return Condition("e", FAIL, "analysts expect a loss next year, so there is no forward earnings yield")
    yield_pct = 100 / pe
    parts, failed, known = [f"forward P/E {pe:.1f}x (earnings yield {yield_pct:.1f}%)"], [], 0
    if treasury is not None:
        known += 1
        parts.append(f"10-year Treasury {treasury:.2f}%")
        if yield_pct < treasury:
            failed.append("the earnings yield is below the Treasury yield")
    if peer_median_pe is not None:
        known += 1
        limit = peer_median_pe * settings.max_pe_vs_peers
        parts.append(f"{peer_label} median {peer_median_pe:.1f}x (allowed up to {limit:.1f}x)")
        if pe > limit:
            failed.append(f"the forward P/E is above {peer_label} median by more than {(settings.max_pe_vs_peers - 1) * 100:.0f}%")
    if known == 0:
        return Condition("e", UNKNOWN, "; ".join(parts) + "; nothing to compare it with")
    return Condition("e", FAIL if failed else PASS, "; ".join(parts) + (f"; {'; '.join(failed)}" if failed else ""))
