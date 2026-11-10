"""Evidence, the five deal conditions and a mechanical cross-check, all worked out in code (a local addition).

The debaters used to decide, run by run and in prose, which facts counted as good or bad evidence and whether a deal
condition was met. The rules listed what counts against a company (estimates cut, a falling margin, a drop in cash flow...)
and had no matching list of what counts for it, a one-quarter dip was counted as "deterioration" by the letter of the rule,
and a gap in our own data could read as a finding about the company. This module fixes the standard before the debate:

1. **A mirrored evidence tally.** Every criterion is two-sided (improving, flat or worsening) and counts by size, with the
   same thresholds in both directions. A small adverse or favorable move is named as a "watch item" and not counted.
2. **The five deal conditions with fixed thresholds** (the stock picker's thresholds for (c), (d) and (e); the analysis's own
   60% and 45% bars for (b), met by the S&P 500 or by the stock's sector fund; (a) by any broad index or the sector fund).
3. **Three kinds of "missing"**: something the company did to withhold information (a late-filing notice, a non-reliance
   notice, a delisting notice, a bankruptcy filing) counts against it; something we could not see (our data gap) counts
   neither way, blocks a deal call and lowers confidence; something this company never reports is neutral.
4. **A mechanical cross-check rating** from fixed weights, kept out of the debate so it cannot anchor it. The report shows
   it beside the final rating, and a gap of two steps or more is flagged: over many runs that gap measures which way the
   debate leans.

Everything is read from the data tools' own text, the same text the analysts and debaters see: no new data, no model call.
"""

from __future__ import annotations

import contextlib
import re
import statistics
from dataclasses import dataclass, field

SCALE = ("Sell", "Underweight", "Hold", "Overweight", "Buy")        # worst to best

# Fixed, in one place. The deterioration thresholds are the stock picker's (PickerSettings), so the two tools agree.
REVENUE_GROWTH_PTS = 10.0         # revenue up or down this many percent on a year ago counts
SEQUENTIAL_MARGIN_WATCH = 1.0     # a one-quarter margin move this many points is named as a watch item, not counted
SEQUENTIAL_MARGIN_COUNT = 3.0     # ...and counts once it is this many points
FORWARD_YIELD_EDGE = 1.0          # next year's earnings yield this many points above the Treasury counts as cheap
PEG_CHEAP, PEG_STRETCHED = 1.0, 2.0   # forward P/E over expected EPS growth (points): at or below is paid for by growth, at or above is not
YIELD_SHORTFALL_MAX = 2.0         # an earnings yield below the Treasury's by at most this many points can be paid for by growth (PEG at 1.0 or less)
DEBT_COVER = 1.0                  # a change in debt counts only when cash does not cover the debt (cash below debt times this)
OWN_HISTORY_LOW, OWN_HISTORY_HIGH = 25, 75     # trailing P/E above this share of its own readings: stretched; at or below the low: cheap (50 is the middle)
BEATS_NET = 3                     # beats minus misses over the last reports: this many or more improves, this many fewer worsens
MIN_SPLIT_MOVE = 0.03             # a move smaller than this (3%) is not a fall to explain
PULLBACK = 0.05
VIX_TOP_TENTH = 90
MIN_BUSINESS_CRITERIA = 4         # fewer business criteria than this: no mechanical rating
GROUP_WEIGHT = 0.5                # business evidence and valuation weigh the same
EVENT_PENALTY = 0.4               # a serious withheld-information event lowers the score by this much (once)
BANDS = ((0.6, "Buy"), (0.2, "Overweight"), (-0.2, "Hold"), (-0.6, "Underweight"))      # score at or above: that rating

SERIOUS_8K = {"1.03": "bankruptcy or receivership", "3.01": "a notice of delisting or of failing a listing standard",
              "4.02": "non-reliance on previously issued financial statements"}
WATCH_8K = {"2.04": "an acceleration of a debt obligation", "2.06": "a material impairment", "4.01": "a change of auditor",
            "5.01": "a change in control"}


def _flat(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _n(text: str) -> float:
    return float(text.replace(",", ""))


def _search(pattern: str, text: str, flags: int = 0):
    return re.search(pattern, text, flags)


@dataclass
class Item:
    """One criterion. ``direction``: +1 improving, 0 flat, -1 worsening, None = we could not see it (a data gap)."""

    group: str                 # "business", "valuation" or "event"
    name: str
    direction: int | None
    text: str
    watch: str = ""            # a small move that is named and not counted


@dataclass
class Analysis:
    items: list[Item] = field(default_factory=list)
    conditions: dict = field(default_factory=dict)          # "a".."e" -> picker Condition
    withheld: list[str] = field(default_factory=list)       # events that count against the company
    watch_events: list[str] = field(default_factory=list)   # filings named and not counted
    gaps: list[str] = field(default_factory=list)           # what we could not see
    readable: bool = False
    survival_by_cash: bool = False      # (d) passes on the numbers and cash covers all the debt, so no maturity can fall due unpaid


# ---------------------------------------------------------------- reading the tools' text
def _read_trend(text: str) -> dict:
    out: dict = {}
    m = _search(r"Operating margin is ([\d.]+)%: ([+-][\d.]+) pts versus a year earlier \(year-over-year view\); "
                r"([+-][\d.]+) pts versus the prior quarter", text)
    if m:
        out.update(margin=float(m.group(1)), margin_yoy=float(m.group(2)), margin_seq=float(m.group(3)))
    else:
        m = _search(r"Operating margin is ([\d.]+)%", text)
        if m:
            out["margin"] = float(m.group(1))
    m = _search(r"Operating cash flow: ([+-][\d.]+)% versus the same quarter a year earlier", text)
    if m:
        out["ocf_yoy"] = float(m.group(1))
    m = _search(r"Revenue: ([+-][\d.]+)% versus the same quarter a year earlier", text)
    if m:
        out["revenue_yoy"] = float(m.group(1))
    m = _search(r"oldest first: ([-\d.,\s]+?)\.(?= - |\s*##|$)", text)
    if m:
        with contextlib.suppress(ValueError):
            out["fcf"] = [_n(x) for x in m.group(1).split(", ") if x.strip()]
    m = _search(r"Diluted average shares: ([+-][\d.]+)% versus a year earlier", text)
    if m:
        out["shares_yoy"] = float(m.group(1))
    m = _search(r"Long-term debt: ([\d,]+), ([\d.]+) times its level a year earlier \(([\d,]+)\)", text)
    if m:
        out.update(debt=_n(m.group(1)), debt_ago=_n(m.group(3)))
    m = _search(r"Cash and short-term investments: ([\d,]+)", text)
    if m:
        out["cash"] = _n(m.group(1))
    return out


def _read_earnings(text: str) -> dict:
    out: dict = {}
    # The table's columns after "Change in 90 days" differ by stock (revenue columns come and go), so read the change by its place
    # and the raised-and-cut cell by its words.
    for m in re.finditer(r"\| (this fiscal year|next fiscal year)[^|]*\| [^|]*\| [^|]*\| [^|]*\| ([+-][\d.]+)% \|.*?(\d+) up / (\d+) down \|",
                         text):
        out["fy0" if m.group(1).startswith("this") else "fy1"] = (int(m.group(3)), int(m.group(4)), float(m.group(2)))
    m = _search(r"Of the last (\d+) reports, (\d+) beat the estimate and (\d+) missed", text)
    if m:
        out["beats"] = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return out


def _read_valuation(text: str) -> dict:
    out: dict = {}
    m = _search(r"forward P/E ([\d.]+)x", text)
    if m:
        out["forward_pe"] = float(m.group(1))
    for key, label in (("yield0", "this fiscal year"), ("yield1", "next fiscal year")):
        m = _search(rf"- {label}[^:]*: EPS [\d.]+ from \d+ analysts[^;]*; P/E [\d.]+x; earnings yield ([\d.]+)%", text)
        if m:
            out[key] = float(m.group(1))
    m = _search(r"10-year Treasury yield: ([\d.]+)%", text)
    if m:
        out["treasury"] = float(m.group(1))
    # Expected growth from the two consensus EPS lines, so the growth input is defined here (Yahoo's own PEG does not say).
    this_eps = _search(r"- this fiscal year[^:]*: EPS ([\d.]+) from", text)
    next_line = _search(r"- next fiscal year[^:]*: EPS ([\d.]+) from \d+ analysts[^;]*; P/E ([\d.]+)x", text)
    if this_eps and next_line:
        eps0, eps1, pe1 = float(this_eps.group(1)), float(next_line.group(1)), float(next_line.group(2))
        if eps0 > 0 and eps1 > 0:
            growth = eps1 / eps0 - 1
            out["growth"] = growth
            out["peg"] = round(pe1 / (growth * 100), 2) if growth > 0 else None      # two decimals, as shown, so 1.00 is 1.00
    m = _search(r"The current [\d.]+x is (higher|lower) than (\d+)% of those readings", text)
    if m:
        share = int(m.group(2))
        out["history_pct"] = share if m.group(1) == "higher" else 100 - share
    m = _search(r"\| Median of the similar-size peers[^|]*\|[^|]*\| ([\d.]+)x \|", text)
    if m:
        out["peer_pe"] = float(m.group(1))
    return out


def _read_market(text: str) -> dict:
    out: dict = {"rows": [], "sp": {}, "sector": {}}
    for name, off, label in re.findall(r"\| ([A-Za-z0-9&.() ]+?) \| (-?[\d.]+%) \| -?[\d.]+% \| -?[\d.]+% \| -?[\d.]+% \| ([^|]+?) \|",
                                       text):
        out["rows"].append((name.strip(), float(off.rstrip("%")) / 100, label.strip()))
    m = _search(r"VIX ([\d.]+); its range over the last year is [\d.]+ to [\d.]+, and it is above (\d+)% of that year's readings", text)
    if m:
        out["vix"], out["vix_rank"] = float(m.group(1)), int(m.group(2))
    for m in re.finditer(r"Last (\d+) sessions: the stock moved ([-+\d.]+)% while the S&P 500 moved ([-+\d.]+)%\. That market move "
                         r"implies ([-+\d.]+)% at this beta", text):
        out["sp"][int(m.group(1))] = (float(m.group(2)) / 100, float(m.group(4)) / 100)
    for m in re.finditer(r"Against the sector fund, last (\d+) sessions: the stock moved ([-+\d.]+)% while (.+?) moved ([-+\d.]+)%\. "
                         r"That sector move implies ([-+\d.]+)% at the stock's beta", text):
        out["sector"][int(m.group(1))] = (float(m.group(2)) / 100, float(m.group(5)) / 100)
        out["sector_name"] = m.group(3).strip()
    return out


def _read_filings(text: str) -> dict:
    out = {"late": [], "serious": [], "watch": []}
    for form, filed in re.findall(r"Late-filing notice: (NT [\w-]+) filed ([\d-]+)", text):
        out["late"].append(f"{form} filed {filed}")
    m = _search(r"Distress-type Form 8-K items in the last \d+ days: (.*?)\.(?= - |$)", text)
    if m:
        for code, what, when in re.findall(r"(\d\.\d\d) \((.*?)\) on ([\d,\- ]+)", m.group(1)):
            line = f"{code} ({what}) on {when.strip().rstrip(',')}"
            if code in SERIOUS_8K:
                out["serious"].append(line)
            elif code in WATCH_8K:
                out["watch"].append(line)
    return out


# ---------------------------------------------------------------- the mirrored evidence
def _pts(value: float, digits: int = 1) -> str:
    return f"{value:+.{digits}f} pts"


def _pct(value: float, digits: int = 0) -> str:
    return f"{value:+.{digits}f}%"


def _estimate_item(name: str, found, settings) -> Item:
    if found is None:
        return Item("business", name, None, "analyst estimates not available")
    up, down, change = found
    net = up - down
    detail = f"{up} analysts raised and {down} cut it in the last 30 days, consensus EPS {_pct(change, 1)} over 90 days"
    rising = net >= settings.revision_min_analysts or change >= settings.estimate_cut_pct
    falling = -net >= settings.revision_min_analysts or change <= -settings.estimate_cut_pct
    if rising and not falling:
        return Item("business", name, 1, detail, f"{name}: {down} cut against {up} raised (net up, so not counted against it)" if down else "")
    if falling and not rising:
        return Item("business", name, -1, detail, f"{name}: {up} raised against {down} cut (net down, so not counted for it)" if up else "")
    return Item("business", name, 0, detail)


def _mirror(name: str, value: float | None, up: float, down: float, text: str, group: str = "business") -> Item:
    """Improving at or above ``up``, worsening at or below ``-down``, flat between; a data gap when ``value`` is None."""
    if value is None:
        return Item(group, name, None, "not available")
    return Item(group, name, 1 if value >= up else -1 if value <= -down else 0, text)


def build_items(figures: dict, settings) -> list[Item]:
    t, e, v = figures["trend"], figures["earnings"], figures["valuation"]
    items = [_estimate_item("this year's EPS estimates", e.get("fy0"), settings),
             _estimate_item("next year's EPS estimates", e.get("fy1"), settings)]
    if "margin_yoy" in t:
        items.append(_mirror("operating margin on a year ago", t["margin_yoy"], settings.margin_drop_points,
                             settings.margin_drop_points, f"{t.get('margin', 0):.1f}%, {_pts(t['margin_yoy'])} on a year ago"))
    else:
        items.append(Item("business", "operating margin on a year ago", None, "not available"))
    seq = t.get("margin_seq")
    if seq is None:
        items.append(Item("business", "operating margin on the prior quarter", None, "not available"))
    else:
        item = _mirror("operating margin on the prior quarter", seq, SEQUENTIAL_MARGIN_COUNT, SEQUENTIAL_MARGIN_COUNT,
                       f"{_pts(seq)} on the prior quarter")
        if item.direction == 0 and abs(seq) >= SEQUENTIAL_MARGIN_WATCH:
            item.watch = f"operating margin {_pts(seq)} on the prior quarter ({'a dip' if seq < 0 else 'a rise'} of less than " \
                         f"{SEQUENTIAL_MARGIN_COUNT:.0f} points)"
        items.append(item)
    ocf = t.get("ocf_yoy")
    items.append(_mirror("operating cash flow on a year ago", ocf, settings.cash_flow_drop * 100, settings.cash_flow_drop * 100,
                         f"{_pct(ocf or 0)} on a year ago"))
    fcf = t.get("fcf") or []
    if len(fcf) >= 4:
        last4 = fcf[-4:]
        direction = 1 if all(x > 0 for x in last4) else -1 if all(x < 0 for x in last4) else 0        # all four either way, or flat
        items.append(Item("business", "free cash flow, last four quarters", direction,
                          f"{sum(1 for x in last4 if x > 0)} of 4 quarters positive, sum {sum(last4):,.0f}"))
    else:
        items.append(Item("business", "free cash flow, last four quarters", None, "fewer than four quarters available"))
    if t.get("debt") is not None and t.get("debt_ago"):
        multiple = t["debt"] / t["debt_ago"]
        cash = t.get("cash")
        if cash is not None and t["debt"] > 0 and cash >= t["debt"] * DEBT_COVER:
            # A change in debt that cash covers many times over is not a change in the company's footing, in either direction.
            items.append(Item("business", "long-term debt on a year ago", 0,
                              f"{multiple:.1f} times its level a year ago; cash covers it {cash / t['debt']:.1f} times, so the change "
                              "is not counted"))
        elif cash is None:
            items.append(Item("business", "long-term debt on a year ago", None,
                              f"{multiple:.1f} times its level a year ago; cash is not available, so the size of the change cannot be judged"))
        else:
            direction = 1 if multiple <= 1 / settings.debt_multiple else -1 if multiple >= settings.debt_multiple else 0
            items.append(Item("business", "long-term debt on a year ago", direction,
                              f"{multiple:.1f} times its level a year ago, and cash does not cover it"))
    else:
        items.append(Item("business", "long-term debt on a year ago", None, "not available"))
    items.append(_mirror("diluted share count on a year ago", None if "shares_yoy" not in t else -t["shares_yoy"], settings.dilution_pct,
                         settings.dilution_pct, f"{_pct(t.get('shares_yoy', 0), 1)} on a year ago"))
    items.append(_mirror("revenue on a year ago", t.get("revenue_yoy"), REVENUE_GROWTH_PTS, REVENUE_GROWTH_PTS,
                         f"{_pct(t.get('revenue_yoy', 0))} on a year ago"))
    beats = e.get("beats")
    if beats:
        n, beat, missed = beats
        net = beat - missed                                  # the same size in both directions: +3 or more improves, -3 or less worsens
        direction = 1 if net >= BEATS_NET else -1 if net <= -BEATS_NET else 0
        items.append(Item("business", "earnings against estimates", direction, f"of the last {n} reports, {beat} beat and {missed} missed"))
    else:
        items.append(Item("business", "earnings against estimates", None, "not available"))

    forward_yield = v.get("yield1")
    treasury = v.get("treasury")
    if forward_yield is None or treasury is None:
        items.append(Item("valuation", "next year's earnings yield against the 10-year Treasury", None, "not available"))
    else:
        gap = forward_yield - treasury
        items.append(Item("valuation", "next year's earnings yield against the 10-year Treasury",
                          1 if gap >= FORWARD_YIELD_EDGE else -1 if gap <= -FORWARD_YIELD_EDGE else 0,
                          f"{forward_yield:.1f}% against {treasury:.2f}% ({_pts(gap)})"))
    pct = v.get("history_pct")
    if pct is None:
        items.append(Item("valuation", "trailing P/E against its own 10-year history", None, "not available"))
    else:
        items.append(Item("valuation", "trailing P/E against its own 10-year history",
                          1 if pct <= OWN_HISTORY_LOW else -1 if pct >= OWN_HISTORY_HIGH else 0,
                          f"higher than {pct}% of its own readings"))
    pe, peers = v.get("forward_pe"), v.get("peer_pe")
    if pe is None or peers is None:
        items.append(Item("valuation", "forward P/E against its peers' median", None, "no peer median available"))
    else:
        items.append(Item("valuation", "forward P/E against its peers' median",
                          1 if pe <= peers / settings.max_pe_vs_peers else -1 if pe > peers * settings.max_pe_vs_peers else 0,
                          f"{pe:.1f}x against a median of {peers:.1f}x"))
    growth, peg = v.get("growth"), v.get("peg")
    name = "forward P/E against expected EPS growth (PEG)"
    if growth is None:
        items.append(Item("valuation", name, None, "consensus EPS for this and next fiscal year not both available"))
    elif peg is None:
        items.append(Item("valuation", name, 0, f"expected EPS growth is {growth * 100:+.0f}%, so there is no growth to adjust for"))
    else:
        items.append(Item("valuation", name, 1 if peg <= PEG_CHEAP else -1 if peg >= PEG_STRETCHED else 0,
                          f"PEG {peg:.2f} ({v.get('forward_pe', 0):.1f}x over {growth * 100:.0f}% expected growth; it uses the "
                          "same consensus growth the estimates leg tests)"))
    return items


# ---------------------------------------------------------------- the five conditions
def _condition_a(market: dict):
    from tradingagents.picker.conditions import FAIL, PASS, UNKNOWN, Condition

    rows = market["rows"]
    if not rows and "vix_rank" not in market:
        return Condition("a", UNKNOWN, "no market state available (our data gap)")
    hits = [f"{name} is {-off * 100:.1f}% below its high ({label})" for name, off, label in rows if off <= -PULLBACK]
    if market.get("vix_rank", 0) >= VIX_TOP_TENTH:
        hits.append(f"VIX {market['vix']:.1f}, above {market['vix_rank']}% of the past year")
    if hits:
        return Condition("a", PASS, "; ".join(hits))
    return Condition("a", FAIL, "no broad index or sector fund is a pullback or worse and the VIX is not in its top tenth")


def _window_shares(pairs: dict, needs: dict) -> tuple[bool | None, list[str]]:
    """(met, notes) for one benchmark: every window with a fall must clear its bar; None when there is nothing to judge."""
    notes, fell_any, failed = [], False, False
    for sessions in (63, 21):
        if sessions not in pairs:
            continue
        stock, implied = pairs[sessions]
        if stock > -MIN_SPLIT_MOVE:
            notes.append(f"{sessions} sessions: no fall of 3% or more ({_pct(stock * 100, 1)})")
            continue
        fell_any = True
        share = implied / stock if implied * stock > 0 else 0.0
        notes.append(f"{sessions} sessions: stock {_pct(stock * 100, 1)}, implied {_pct(implied * 100, 1)}, explains "
                     f"{'more than all' if share > 1 else f'{share * 100:.0f}%'} (needs {needs[sessions] * 100:.0f}%)")
        failed = failed or share < needs[sessions]
    if not notes:
        return None, []
    return (None if not fell_any else not failed), notes


def _condition_b(market: dict, settings):
    from tradingagents.picker.conditions import FAIL, PASS, UNKNOWN, Condition

    needs = {63: settings.share_63, 21: settings.share_21}
    sp_met, sp_notes = _window_shares(market["sp"], needs)
    sec_met, sec_notes = _window_shares(market["sector"], needs)
    if sp_met is None and sec_met is None and not sp_notes and not sec_notes:
        return Condition("b", UNKNOWN, "no market-explained figures available (our data gap)")
    parts = []
    if sp_notes:
        parts.append("S&P 500: " + "; ".join(sp_notes))
    if sec_notes:
        parts.append(f"{market.get('sector_name', 'sector fund')}: " + "; ".join(sec_notes))
    if sp_met is None and sec_met is None:
        return Condition("b", FAIL, " | ".join(parts) + ": there is no fall to explain")
    if sp_met:
        return Condition("b", PASS, " | ".join(parts) + " (met on the S&P 500 lines)")
    if sec_met:
        return Condition("b", PASS, " | ".join(parts) + " (met on the sector lines)")
    return Condition("b", FAIL, " | ".join(parts))


def _facts(figures: dict):
    """A picker ``Facts`` built from what the tools printed, so the stock picker's own (c), (d) and (e) checks can run."""
    from tradingagents.picker.facts import Estimate, Facts

    t, e, v = figures["trend"], figures["earnings"], figures["valuation"]
    facts = Facts(symbol="")

    def estimate(found):
        return Estimate() if not found else Estimate(up=found[0], down=found[1], change_90d=found[2] / 100)

    facts.fy0, facts.fy1 = estimate(e.get("fy0")), estimate(e.get("fy1"))
    if "margin" in t:
        facts.op_margin = t["margin"] / 100
        if "margin_yoy" in t:
            facts.op_margin_year_ago = (t["margin"] - t["margin_yoy"]) / 100
    if "ocf_yoy" in t and t["ocf_yoy"] > -100:
        facts.ocf, facts.ocf_year_ago = 1 + t["ocf_yoy"] / 100, 1.0
    facts.fcf_quarters = list(reversed((t.get("fcf") or [])[-4:]))
    facts.cash, facts.debt, facts.debt_year_ago = t.get("cash"), t.get("debt"), t.get("debt_ago")
    if facts.cash is not None and facts.debt and facts.cash >= facts.debt * DEBT_COVER:
        facts.debt_year_ago = None             # the picker's debt test then has nothing to flag: cash covers the debt
    if "shares_yoy" in t:
        facts.shares, facts.shares_year_ago = 1 + t["shares_yoy"] / 100, 1.0
    facts.forward_pe = v.get("forward_pe")
    return facts


def _condition_e(v: dict, settings):
    """The valuation condition: the stock picker's two legs (earnings yield against the Treasury, forward P/E against the peers'
    median) plus growth, in both directions: a premium to the peers that expected growth pays for (PEG at 1.0 or less) is not
    stretched, and a multiple that growth does not pay for (PEG at 2.0 or more) is, whatever the peers say."""
    from tradingagents.picker.conditions import FAIL, PASS, UNKNOWN, Condition

    pe = v.get("forward_pe")
    if pe is None:
        return Condition("e", UNKNOWN, "no forward P/E is available (our data gap)")
    if pe <= 0:
        return Condition("e", FAIL, "analysts expect a loss next year, so there is no forward earnings yield")
    yield_pct = 100 / pe
    parts, failed, known = [f"forward P/E {pe:.1f}x (earnings yield {yield_pct:.1f}%)"], [], 0
    treasury, peers, peg, growth = v.get("treasury"), v.get("peer_pe"), v.get("peg"), v.get("growth")
    if treasury is not None:
        known += 1
        parts.append(f"10-year Treasury {treasury:.2f}%")
        if yield_pct < treasury:
            shortfall = treasury - yield_pct
            if peg is not None and peg <= PEG_CHEAP and shortfall <= YIELD_SHORTFALL_MAX:
                parts.append(f"the earnings yield is {shortfall:.1f} points below the Treasury yield, which expected growth pays for")
            elif peg is not None and peg <= PEG_CHEAP:
                failed.append(f"the earnings yield is {shortfall:.1f} points below the Treasury yield, more than growth is allowed to "
                              f"pay for ({YIELD_SHORTFALL_MAX:.1f})")
            else:
                failed.append("the earnings yield is below the Treasury yield and expected growth does not pay for it"
                              if peg is not None else "the earnings yield is below the Treasury yield")
    if peg is not None and growth is not None:
        parts.append(f"growth-adjusted P/E (PEG) {peg:.2f} on {growth * 100:.0f}% expected EPS growth "
                     f"(paid for by growth at {PEG_CHEAP:.1f} or less, not at {PEG_STRETCHED:.1f} or more)")
    if peers is not None:
        known += 1
        limit = peers * settings.max_pe_vs_peers
        parts.append(f"its peers median {peers:.1f}x (allowed up to {limit:.1f}x)")
        if pe > limit:
            if peg is not None and peg <= PEG_CHEAP:
                parts.append("the premium to the peers is paid for by expected growth")
            else:
                failed.append("the forward P/E is above its peers median by more than "
                              f"{(settings.max_pe_vs_peers - 1) * 100:.0f}% and expected growth does not pay for it")
    if peg is not None and peg >= PEG_STRETCHED:
        known += 1
        failed.append(f"the multiple is {peg:.1f} times expected growth, which growth does not pay for")
    if known == 0:
        return Condition("e", UNKNOWN, "; ".join(parts) + "; nothing to compare it with")
    return Condition("e", FAIL if failed else PASS, "; ".join(parts) + (f"; {'; '.join(failed)}" if failed else ""))


def analyze(outputs: dict[str, str], settings=None) -> Analysis:
    """The evidence tally, the five conditions and the disclosure picture from the tools' text (``{tool name: text}``)."""
    from tradingagents.picker import conditions as cond
    from tradingagents.picker.settings import PickerSettings

    settings = settings or PickerSettings()
    texts = {k: _flat(outputs.get(k)) for k in ("get_market_context", "get_earnings_facts", "get_trend_facts",
                                                "get_valuation_context", "get_filing_events")}
    figures = {"trend": _read_trend(texts["get_trend_facts"]), "earnings": _read_earnings(texts["get_earnings_facts"]),
               "valuation": _read_valuation(texts["get_valuation_context"]), "market": _read_market(texts["get_market_context"]),
               "filings": _read_filings(texts["get_filing_events"])}
    result = Analysis()
    market_read = figures["market"]
    result.readable = bool(figures["trend"] or figures["earnings"] or figures["valuation"] or market_read["rows"]
                           or market_read["sp"] or market_read["sector"])
    if not result.readable:
        return result

    result.items = build_items(figures, settings)
    market = figures["market"]
    facts = _facts(figures)
    result.conditions = {
        "a": _condition_a(market), "b": _condition_b(market, settings),
        "c": cond.deterioration_condition(facts, settings), "d": cond.survival_condition(facts, settings),
        "e": _condition_e(figures["valuation"], settings),
    }
    debt_item = next((i for i in result.items if i.name == "long-term debt on a year ago"), None)
    if debt_item and "so the change is not counted" in debt_item.text:
        result.conditions["c"].detail += f"; long-term debt {debt_item.text}"
    # A company whose cash covers all its debt and whose free cash flow is positive passes the survival test on the numbers:
    # nothing can fall due that cash cannot pay, so unread going-concern text or an unread maturity schedule is not a gap that
    # blocks a deal call (it would otherwise block every foreign filer, whose 20-F and 6-K the filing tool does not read).
    cash, debt = figures["trend"].get("cash"), figures["trend"].get("debt")
    if result.conditions["d"].status == cond.PASS and cash is not None and debt and cash >= debt * DEBT_COVER:
        result.survival_by_cash = True
        result.conditions["d"].detail += (f"; cash covers all long-term debt {cash / debt:.1f} times, so nothing can fall due that cash "
                                          "cannot pay and going-concern or debt-maturity text is not needed")

    # What the company did to withhold or delay information counts against it; our own gaps do not.
    filings = figures["filings"]
    if texts["get_filing_events"]:
        for line in filings["late"]:
            result.withheld.append(f"late-filing notice: {line}")
        for line in filings["serious"]:
            result.withheld.append(f"8-K item {line}")
        result.watch_events = [f"8-K item {line}" for line in filings["watch"]]
        for line in result.withheld:
            result.items.append(Item("event", line.split(":")[0] if line.startswith("late") else "serious 8-K item", -1, line))
    else:
        result.gaps.append("SEC filing events (no late-filing or non-reliance check was possible)")
    for item in result.items:
        if item.direction is None:
            result.gaps.append(f"{item.name}: {item.text}")
    for letter, condition in result.conditions.items():
        if condition.status == cond.UNKNOWN:
            result.gaps.append(f"condition ({letter}): {condition.detail}")
    return result


# ---------------------------------------------------------------- the scorecard lines
def scorecard_lines(a: Analysis) -> list[tuple[str, str]]:
    """The extra scorecard lines (H, I, J), or [] when the tools gave nothing to read."""
    if not a.readable:
        return []
    out = []
    status = {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN (our data gap, counts as not met)"}
    conds = " | ".join(f"({k}) {status.get(c.status, c.status)}: {c.detail}" for k, c in a.conditions.items())
    if a.survival_by_cash:
        unchecked = ("Not checked by code: whether a filing is a financing (going-concern language and debt maturities are not needed "
                     "here: cash covers all the debt, so the survival test is passed on the numbers).")
    else:
        unchecked = ("Not checked by code: going-concern language, when debt falls due, whether a filing is a financing (cash does not "
                     "cover all the debt, or is unknown, so the survival test needs the first two read: unread, they are a data gap).")
    out.append(("H. Rule check (the five deal conditions, worked out in code with fixed thresholds, the same for every stock; "
                "contest a result only with a specific figure from the reports and say so)", conds + " | " + unchecked))
    counted = [i for i in a.items if i.direction is not None]
    better = [f"{i.name} ({i.text})" for i in counted if i.direction > 0]
    worse = [f"{i.name} ({i.text})" for i in counted if i.direction < 0]
    flat = [i.name for i in counted if i.direction == 0 and not i.watch]
    watch = [i.watch for i in a.items if i.watch]
    tally = (f"Improving ({len(better)}): " + ("; ".join(better) or "none") + f" | Worsening ({len(worse)}): " + ("; ".join(worse) or "none")
             + f" | Flat ({len(flat)}): " + (", ".join(flat) or "none"))
    if watch:
        tally += " | Watch, named and not counted: " + "; ".join(watch)
    out.append(("I. Evidence tally (the same size thresholds in both directions; a small move is a watch item, not deterioration "
                "and not improvement)", tally))
    withheld = ("; ".join(a.withheld) if a.withheld else "none")
    watch_events = f" | Filings named, not counted: {'; '.join(a.watch_events)}" if a.watch_events else ""
    gaps = ("; ".join(a.gaps) if a.gaps else "none")
    out.append(("J. Disclosure and data gaps", f"Withheld or serious (count against the company): {withheld}{watch_events} | "
                f"Our data gaps (neutral for the direction of the rating, they block a deal call and lower confidence): {gaps}"))
    return out


# ---------------------------------------------------------------- the mechanical cross-check
def mechanical_check(a: Analysis) -> dict | None:
    """A fixed-weight rating from the tally, or None when there is too little to rate. Not a verdict: a cross-check."""
    if not a.readable:
        return None
    business = [i.direction for i in a.items if i.group == "business" and i.direction is not None]
    valuation = [i.direction for i in a.items if i.group == "valuation" and i.direction is not None]
    if len(business) < MIN_BUSINESS_CRITERIA or not valuation:
        return None
    score = GROUP_WEIGHT * statistics.mean(business) + GROUP_WEIGHT * statistics.mean(valuation)
    if a.withheld:
        score -= EVENT_PENALTY
    rating = "Sell"
    for edge, name in BANDS:
        if score >= edge:
            rating = name
            break
    return {"rating": rating, "score": round(score, 3), "business": round(statistics.mean(business), 3), "business_n": len(business),
            "valuation": round(statistics.mean(valuation), 3), "valuation_n": len(valuation), "withheld": list(a.withheld),
            "gaps": len(a.gaps)}


def cross_check_text(mech: dict | None, final_rating: str | None) -> str:
    """The report section: the checklist rating beside the debate's final rating, and a flag at two steps or more."""
    if not mech:
        return ""
    text = (f"A fixed-weight checklist (business evidence and valuation weigh the same; a serious withheld-information event lowers "
            f"the score; no price action, risk or news) rates this company {mech['rating']} (score {mech['score']:+.2f}: business "
            f"evidence {mech['business']:+.2f} over {mech['business_n']} criteria, valuation {mech['valuation']:+.2f} over "
            f"{mech['valuation_n']} criteria" + (f", withheld: {'; '.join(mech['withheld'])}" if mech["withheld"] else "")
            + "). It was kept out of the debate so it could not anchor it. It is a cross-check, not a verdict.")
    if final_rating in SCALE and mech["rating"] in SCALE:
        steps = SCALE.index(final_rating) - SCALE.index(mech["rating"])
        if steps == 0:
            text += f" The debate's final rating, {final_rating}, agrees."
        else:
            way = "above" if steps > 0 else "below"
            text += f" The debate's final rating, {final_rating}, is {abs(steps)} step{'s' if abs(steps) > 1 else ''} {way} it."
            if abs(steps) >= 2:
                text += (" A gap of two steps or more: look for what the debate weighed that the checklist cannot see (price action, "
                         "risk, news) or what it missed. Over many runs, the direction of these gaps shows which way the debate leans.")
    if mech.get("gaps"):
        n = mech["gaps"]
        text += f" ({n} {'input was a data gap' if n == 1 else 'inputs were data gaps'} on our side.)"
    return text
