"""Rules for telling a market-driven selloff from a company in trouble (a local addition).

In a falling, frightened market good and bad companies are sold together. Fear and forced selling can push a sound
company's price below what its business is worth, and they can also hide a company that is in real trouble. These
rules give the debaters and the two managers one way to tell which it is, from evidence about the company and not
from the price: the market-context tool already says how much of a stock's decline the market explains, and the
fundamentals tools give the balance-sheet facts for a survival test.

The rules do not make a lower price a reason to buy. A company that fails the survival test, or whose decline is
mostly its own, is treated as having earned its price.

Set ``TRADINGAGENTS_NO_SELLOFF_RULES=1`` to leave the rules out of every prompt, so the same ticker can be run with
and without them and the ratings compared.
"""

import os

DISABLE_ENV = "TRADINGAGENTS_NO_SELLOFF_RULES"

CORE = """

## Selloffs and deals

A market in a pullback, correction or bear market (the market report's market-context section gives the label and the VIX percentile) sells good and bad companies together. Fear can push a sound company's price below what its business is worth; it can also hide a company in real trouble. Tell which from evidence about the company, not from the price.

1. Weakness the market explains is not evidence against the company. A decline, high volatility, a high beta, or weak technical readings (below moving averages, low RSI, a downtrend) count against the stock only to the extent the market does not explain them. The market report's market-context section says what share of the stock's recent decline is market-implied and what share is not (its "Reading" lines). Use those figures; if the reports do not give them, say "not established".
2. What counts against a company is company-specific evidence that the business got worse: estimates or guidance cut, results that missed, a falling margin or a drop in operating cash flow, distress or financing filings, dilution, lost customers or contracts, a weaker balance sheet. Two things are signals, not evidence, until something else confirms them: insider selling (nearly every large company has it; the reports usually cannot say whether it follows a pre-arranged plan), and a filing whose content the reports do not give. Name each with its figure as "unresolved", let it argue for a smaller or staged position, but do not count it as deterioration.
3. Survival test. A company can be called a deal only if it can get through a year of bad markets without a damaging financing: cash and equivalents cover at least 12 months of cash burn (or free cash flow is positive); no going-concern warning; no large debt due without funding in sight; no run of equity sales, registrations or a fast-rising share count. A company that fails is not a deal. Cheap does not rescue it.
4. A deal needs all five: (a) the market is in a drawdown (pullback or worse) or the VIX is in its top tenth of the past year; (b) the stock's decline is mostly market-driven: the market explains 60% or more of it over the 63 sessions and at least 45% over the last 21 (take both figures from the market-context "Reading" lines; do not pick the window that suits the conclusion); (c) no company-specific deterioration: the estimate, results, margin, cash-flow and balance-sheet data in the reports show none (rule 2; unresolved signals do not fail it, deterioration evidence does); (d) it passes the survival test; (e) the valuation is low against its own history or its estimates, not stretched. A condition the reports do not establish counts as not met: for (b) that means figures that are missing, and for (c) estimate, results or cash-flow data that are missing.
5. If all five hold, call it a market-driven selloff and a deal. The price decline and the volatility then do not lower the rating; rate the business and its forecasts, and the rating may be one step more positive than the same facts would earn in a calm market. Advise staged entry (for example in thirds), because volatility will stay high. If any condition fails, name it and do not treat the stock as a deal. If the decline is mostly the stock's own, or the survival test fails, the weakness is earned.
6. Never call a deal on a lower price alone, and never treat a stock as safe because the market is also down."""

ROLE_LINES = {
    "bull": "If you argue this stock is a deal, show the five conditions with figures from the reports; a condition you skip is a point conceded to the bear.",
    "bear": "Do not argue against the company from a price decline, volatility, beta or weak technicals that the market explains. Argue from company-specific evidence and the survival test. If the market explains the decline and the company passes the survival test, concede that and argue valuation or fundamentals instead.",
    "aggressive": "An aggressive case for buying a selloff must clear the survival test and the other four conditions first; say how each is met.",
    "conservative": "Market-wide volatility and a market-driven decline are not risks of this company. Argue from company-specific evidence and the survival test; say so if the company passes them.",
    "neutral": "Separate what the market explains from what is the company's own, and judge the company on the second.",
    "research_manager": "Open your Rationale with a **Selloff diagnosis** of two to four sentences: the market state, the share of the stock's decline the market explains (both windows), any company-specific deterioration and any unresolved signals (rule 2), the survival-test result, and whether all five conditions of rule 4 hold (name any the debate did not establish; that condition counts as not met). Then give the arguments that decided it. Weigh the bull and bear cases by rules 1 to 6: a bear argument that rests only on price weakness the market explains carries no weight, and a bull argument that skips the survival test carries none either.",
    "portfolio_manager": "Open your Investment Thesis with a **Selloff diagnosis** of two to four sentences in the same form: the market state, the share of the decline the market explains (both windows), company-specific deterioration and any unresolved signals, the survival-test result, and whether the five conditions hold. Check the Research Manager's diagnosis against the risk debate. A conservative argument that rests on market-wide volatility alone does not count against the stock, and an aggressive argument for buying must clear the survival test.",
}


def enabled() -> bool:
    return os.getenv(DISABLE_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def selloff_rules(role: str) -> str:
    """The rules plus the line for ``role``, or an empty string when the rules are switched off."""
    if not enabled():
        return ""
    return CORE + "\n\n" + ROLE_LINES[role] + "\n"
