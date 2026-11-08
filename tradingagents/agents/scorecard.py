"""A shared fact sheet for the bull, the bear and the Research Manager (a local addition).

The debaters each read about 14,000 tokens of analyst reports and then write one speech apiece, so which facts reach the
Research Manager depends on which each speaker happens to pick. The verdict on a borderline stock then flips with the
draw. The scorecard puts the same short list of decisive facts, worked out in code from the data tools (never by a model),
in front of all three, in the order the selloff rules ask for them: the market, how much of the decline the market
explains, evidence about the business, signals that need confirming, survival inputs, valuation, the credit backdrop.

It asks for no opinion and takes no side: good news and bad news appear in the same neutral words the tools use. A
section whose tool is switched off, unavailable for this stock or dated out of reach is simply left out.
Set ``TRADINGAGENTS_NO_SCORECARD=1`` to leave it out of every prompt.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

DISABLE_ENV = "TRADINGAGENTS_NO_SCORECARD"

# The tools the scorecard reads: the same vendor functions the analysts' tools call.
SOURCES = ("get_market_context", "get_earnings_facts", "get_trend_facts", "get_valuation_context",
           "get_insider_transactions", "get_filing_events", "get_macro_backdrop")

HEADERS = {
    "debater": ("SHARED SCORECARD (worked out in code from the tool data, the same for the bull, the bear and the manager). "
                "Address every lettered line in your argument, quoting its figures, and say plainly where you think a line is misleading."),
    "manager": ("SHARED SCORECARD (worked out in code from the tool data; the bull and the bear were given the same sheet). "
                "Check each side's claims against it: a figure a speaker quotes that conflicts with it counts against that "
                "speaker, and a line neither side addressed is still part of the evidence."),
}


def enabled() -> bool:
    return os.getenv(DISABLE_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else None


def _flat(text: str | None) -> str:
    """One line of text, so the patterns can use ' - ' as the bullet separator whatever the tool's line breaks."""
    return re.sub(r"\s+", " ", text or "").strip()


def build_scorecard(outputs: dict[str, str]) -> str:
    """The scorecard text from the tools' own output (``{tool name: text}``), or '' when nothing could be read."""
    mc, earn, trend, ins, fil, val, macro = (_flat(outputs.get(name)) for name in (
        "get_market_context", "get_earnings_facts", "get_trend_facts", "get_insider_transactions",
        "get_filing_events", "get_valuation_context", "get_macro_backdrop"))
    out: list[tuple[str, str]] = []

    # A. the market
    rows = re.findall(r"\| ([A-Za-z0-9&.() ]+?) \| (-?[\d.]+%) \| -?[\d.]+% \| -?[\d.]+% \| -?[\d.]+% \| ([^|]+?) \|", mc)
    vix = re.search(r"VIX ([\d.]+); its range over the last year is ([\d.]+) to ([\d.]+), and it is above (\d+)%", mc)
    if rows:
        text = "; ".join(f"{name.strip()} {label.strip()} ({off} from its high)" for name, off, label in rows)
        if vix:
            text += f"; VIX {vix.group(1)}, above {vix.group(4)}% of the past year's readings"
        out.append(("A. The market", text))

    # B. how much of the stock's decline the market explains
    parts = []
    for window in ("63", "21"):
        m = re.search(rf"Last {window} sessions: the stock moved ([-+\d.]+)% while the S&P 500 moved ([-+\d.]+)%.*?Reading: (.*?)(?: - |$)", mc)
        if m:
            parts.append(f"last {window} sessions: stock {m.group(1)}%, S&P 500 {m.group(2)}%. {m.group(3).strip()}")
    if parts:
        out.append(("B. How much of the stock's decline the market explains", " | ".join(parts)))

    # C. evidence about the business
    c = []
    for pattern in (r"- (Operating cash flow: .*?)\.(?= - [A-Z]| ## |$)", r"- (Operating margin is .*?)\.(?= - [A-Z]| ## |$)",
                    r"- (Long-term debt: .*?)\.(?= - [A-Z]| ## |$)", r"- (Diluted average shares: .*?)\.(?= - [A-Z]| ## |$)"):
        value = _first(pattern, trend)
        if value:
            c.append(value)
    flags = _first(r"Flags to state in the report:?\s*(.*?)$", earn)
    if flags:
        c += [x.strip(" -") for x in re.split(r"\s-\s(?=[A-Z])", " " + flags) if len(x.strip(" -")) > 20]
    nxt = _first(r"(Next scheduled report: .*?)\. Yahoo", earn)
    if nxt:
        c.append(nxt)
    if c:
        out.append(("C. Evidence about the business (estimates, margin, cash flow, debt, shares)", " | ".join(c)))

    # C2. signals that need confirming
    u = []
    for pattern in (r"- (Open-market purchases: .*?)\.(?= - [A-Z])", r"- (Open-market sales: .*?)\.(?= - [A-Z])"):
        value = _first(pattern, ins)
        if value:
            u.append(value)
    f = _first(r"Flags to state in the report\s*-\s*(.*?)$", fil)
    if f:
        u.append("Filings: " + f.split(": these usually")[0])
    if u:
        out.append(("C2. Unresolved signals (not evidence of deterioration until confirmed)", " | ".join(u)))

    # D. survival inputs
    d = []
    for pattern in (r"- (Cash and short-term investments: .*?)\.(?= - [A-Z]| ## |$)",
                    r"- (Free cash flow \(.*?oldest first: .*?)\.(?= - [A-Z]| ## |$)"):
        value = _first(pattern, trend)
        if value:
            d.append(value)
    if d:
        out.append(("D. Survival test inputs", " | ".join(d)))

    # E. valuation
    e = []
    value = _first(r"(The current [\d.]+x is (?:higher|lower) than \d+% of those readings)", val)
    if value:
        e.append(value + " (trailing P/E on reported earnings, 10 years)")
    value = _first(r"Price [\d.]+\. Trailing P/E \(GAAP\) [\d.]+x; (forward P/E [\d.]+x)", val)
    if value:
        e.append(value)
    for m in re.finditer(r"- (this fiscal year[^:]*|next fiscal year[^:]*): EPS ([\d.]+) from (\d+) analysts[^;]*; P/E ([\d.]+x); earnings yield ([\d.]+%)", val):
        e.append(f"{m.group(1)}: P/E {m.group(4)}, earnings yield {m.group(5)}")
    value = _first(r"(10-year Treasury yield: [\d.]+%)", val)
    if value:
        e.append(value)
    if e:
        out.append(("E. Valuation", " | ".join(e)))

    # F. the credit backdrop
    hy = _first(r"(High-yield spreads are .*?)\.\s", macro) if macro else None
    if hy:
        out.append(("F. Credit backdrop", hy))

    # G. sell-side opinion, as context only
    g = []
    value = _first(r"(Ratings now \(\d+ analysts\): [^.]*)\.", earn)
    if value:
        g.append(value)
    m = re.search(r"Mean ([\d.]+), ([+-]\d+)% from the current price", earn)
    if m:
        g.append(f"mean price target {m.group(1)}, {m.group(2)}% from the current price")
    if g:
        out.append(("G. Sell-side opinion (it skews positive; context, not a signal)", " | ".join(g)))

    return "\n".join(f"{k}: {v}" for k, v in out)


def collect_outputs(ticker: str, trade_date: str) -> dict[str, str]:
    """Call each source tool as the analysts' tools do; a tool that fails or is unavailable is left out."""
    from tradingagents.agents.analysts import toolset
    from tradingagents.dataflows.router import route_to_vendor

    off = toolset.disabled()
    outputs: dict[str, str] = {}
    for name in SOURCES:
        if name in off:
            continue
        try:
            text = route_to_vendor(name, ticker, trade_date)
        except Exception as exc:   # a source that is down must not stop the run
            logger.warning("Scorecard: %s failed (%s); leaving that section out", name, exc)
            continue
        if isinstance(text, str) and text and not text.startswith("DATA_UNAVAILABLE"):
            outputs[name] = text
    return outputs


def create_scorecard_node():
    """The graph node that writes ``scorecard`` into the state, alongside the analysts."""

    def scorecard_node(state) -> dict:
        if not enabled():
            return {"scorecard": ""}
        try:
            text = build_scorecard(collect_outputs(state["company_of_interest"], state["trade_date"]))
        except Exception as exc:
            logger.warning("Scorecard could not be built (%s); the debate runs without it", exc)
            text = ""
        return {"scorecard": text}

    return scorecard_node


def scorecard_block(state, role: str = "debater") -> str:
    """The scorecard as a prompt block for ``role`` ('debater' or 'manager'), or '' when there is none.

    An empty result leaves a prompt exactly as it was without the scorecard.
    """
    text = (state.get("scorecard") or "").strip() if enabled() else ""
    return f"\n{HEADERS[role]}\n{text}\n" if text else ""
