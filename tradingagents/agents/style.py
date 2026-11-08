"""An investing-style lens: how the same facts are weighed (a local addition).

The standard rules lean value-minded. They want a company that is funded, improving and not expensive, and they count heavy
spending and share issuance against it. That is a sound test for a "deal", but it will rarely pass a fast-growing company
that spends heavily on growth, whatever its demand news. The growth lens is for someone who has chosen to judge a company
as a growth investment.

The lens changes what is weighed, never the facts. The survival test stays, a claim without a figure is still only a claim,
and the lens has to name what would show the case is wrong. It is chosen by the person (``TRADINGAGENTS_INVESTING_STYLE``,
or the run form), the report says which lens was used, and it has not been tested against outcomes: it makes the analysis
more open to risky companies, not more accurate.
"""

import os

ENV = "TRADINGAGENTS_INVESTING_STYLE"
STYLES = ("standard", "growth")
LABELS = {"standard": "Standard (value-minded)", "growth": "Growth"}
HELP = {
    "standard": "The rules tested so far. It wants a company that is funded, improving and not expensive, and it counts "
                "heavy spending and share issuance against a company.",
    "growth": "Judges the price against growth and margins instead of profits, counts demand that comes with figures "
              "(orders, backlog) in the company's favor, and does not hold spending that buys growth against it. The "
              "survival test stays. It has not been tested against outcomes and can make the analysis more open to risky companies.",
}

CORE_GROWTH = """

## Investing style: growth lens

The person who asked chose to judge this company as a growth investment. That changes how the same facts are weighed. It does not change the facts, the survival test, or the duty to say what would show the case is wrong. Say in your first sentence that the growth lens is in use.

1. Judge the price against growth, not against profits. Little or no earnings does not make a company expensive by itself. Compare price-to-sales (or enterprise value to sales) with the company's disclosed revenue growth, its gross margin and the trend in that margin, and with peers growing at a similar rate, using only figures in the reports. A high multiple with high disclosed growth and a steady or rising gross margin is arguable. A high multiple with slowing growth or a falling gross margin is stretched. If the reports lack the growth or margin figures, say "not established".
2. Demand that comes with figures counts for the company: backlog or remaining performance obligations, booked orders, a guidance raise, contracted or sold-out capacity with revenue, units or price from a filing or the call. The same claim without a figure (for example "sold out" with no revenue, units or price) is an unresolved signal. It argues for the case, but it is not proof of revenue, and you should say what number is missing.
3. Spending that buys disclosed growth is not deterioration by itself. Operating losses and cash burn count as deterioration when losses grow faster than revenue and gross margin or unit economics are not improving. Issuing shares is judged by what it funds and the runway it leaves, not by the fact of the issue. Dilution still counts against the case when it is large against the market value, repeated, or not tied to disclosed growth.
4. The survival test in the selloff rules stays as written. A company that cannot fund itself for 12 months without a damaging raise is a financing risk, not a growth story.
5. When you apply the five conditions of a deal, read (c) and (e) through points 1 to 3 above. Conditions (a), (b) and (d) are unchanged.
6. Judge the case over one to three years, not the next quarter, and size for volatility: staged entries and a position small enough to survive the price halving. A strong growth case does not make a large position safe.
7. Do not let the lens turn hope into a finding. Name the single fact that would show growth is not turning into a business (for example gross margin falling while revenue rises) and what you would watch next."""

ROLE_LINES = {
    "bull": "Argue the growth case with figures: growth rate, gross margin trend, orders or backlog, runway. Say which demand evidence is quantified and which is only a claim.",
    "bear": "Argue against the growth case with figures: slowing growth, a falling gross margin, losses growing faster than revenue, dilution out of proportion to what it funds, a backlog that is a claim with no number. Under this lens a high multiple or the absence of profit is not enough on its own.",
    "aggressive": "Make the case for committing to the growth thesis, and say what size and staging the risks allow.",
    "conservative": "Guard against the growth case failing: name the facts that would show it, the runway, and the position size that survives a halving. Under this lens, losing money or a high multiple is not enough on its own.",
    "neutral": "Separate what is quantified from what is only claimed in the growth case, and weigh the quantified part.",
    "research_manager": "Add to your Rationale a sentence on the growth lens: the growth rate and gross margin trend, demand that has figures against demand that is only claimed, and whether losses are growing faster than revenue. Weigh the bull and bear cases by points 1 to 7: a bear argument that rests only on a high multiple or on the absence of profit carries no weight under this lens, and a bull argument that treats a claim without a figure as proof carries none either.",
    "portfolio_manager": "State in the first sentence of your Investment Thesis that the growth lens was used. Check the Research Manager's growth assessment against the risk debate, keep the survival test, and size the position for volatility. Give the rating on the same five-point scale and with the same evidence standard as always.",
}


def style_name(env=None) -> str:
    """The investing style in force: ``standard`` unless the environment names a known other one."""
    value = ((os.environ if env is None else env).get(ENV) or "").strip().lower()
    return value if value in STYLES else "standard"


def style_lens(role: str) -> str:
    """The lens text plus the line for ``role``, or an empty string for the standard style."""
    line = ROLE_LINES[role]                       # an unknown role is an error, whatever the style
    if style_name() == "standard":
        return ""
    return CORE_GROWTH + "\n\n" + line + "\n"
