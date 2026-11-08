"""Research panel: several independent passes of the research debate, the median verdict goes on (a local addition).

One pass of the debate (bull, bear, Research Manager) is a single draw, and on a borderline stock neighboring
ratings come up about as often as a coin lands. A pass that is repeated and judged by its median is far steadier,
and the way the passes split shows plainly when a stock is borderline.

The panel is one graph node standing in for Bull Researcher -> Bear Researcher -> Research Manager. It runs
``passes`` complete debates side by side from the same analyst reports, reads each plan's rating, takes the median
on the five-step scale (for an even number of passes the more cautious of the two middle ratings), and passes on the
plan and debate of the first pass that gave the median rating, together with a plain-language summary of the vote.
With a single pass the graph keeps its original nodes and this module is not used.
"""

from __future__ import annotations

import contextvars
import logging
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from tradingagents.agents.rating import extract_rating

logger = logging.getLogger(__name__)

SCALE = ("Sell", "Underweight", "Hold", "Overweight", "Buy")   # worst to best

# The Research Manager labels its verdict "Recommendation" (the Portfolio Manager's is "Rating").
_RECOMMENDATION = re.compile(r"recommendation\W*\s*(buy|overweight|hold|underweight|sell)\b", re.IGNORECASE)


def plan_rating(plan: str) -> str | None:
    """The rating a Research Manager plan gives, from its Recommendation line (or a Rating line), else None."""
    text = unicodedata.normalize("NFKC", plan or "")
    m = _RECOMMENDATION.search(text)
    if m:
        return m.group(1).capitalize()
    return extract_rating(text)


def median_rating(ratings: list[str]) -> str:
    """The middle rating; with an even number of ratings, the more cautious of the two middle ones."""
    ordered = sorted(ratings, key=SCALE.index)
    return ordered[(len(ordered) - 1) // 2]


STRONG_SHARE = 0.8     # at least this share of the passes gave the median rating: strong agreement
MODERATE_SHARE = 0.7   # at least this share: moderate; below it the call is borderline


def agreement_label(ratings: list[str]) -> str:
    """'strong', 'moderate' or 'borderline', from the share of passes that gave the median rating.

    The thresholds make the narrowest majority borderline whatever the number of passes (3 of 5, 2 of 3, 4 of 7),
    because that is the vote a single changed pass would overturn.
    """
    share = Counter(ratings)[median_rating(ratings)] / len(ratings)
    return "strong" if share >= STRONG_SHARE else "moderate" if share >= MODERATE_SHARE else "borderline"


def rating_range(ratings: list[str]) -> list[str]:
    """[lowest, highest] of the ratings the passes gave."""
    ordered = sorted(set(ratings), key=SCALE.index)
    return [ordered[0], ordered[-1]]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def summarize(ratings: list[str], requested: int | None = None) -> str:
    """How the passes voted and what that means, in plain words."""
    n = len(ratings)
    median = median_rating(ratings)
    counts = Counter(ratings)
    votes = ", ".join(f"{r} {counts[r]}" for r in reversed(SCALE) if counts.get(r))
    agree = counts[median]
    label = agreement_label(ratings)
    if label == "strong":
        reading = "Strong agreement."
    elif label == "moderate":
        others = _join([r for r in reversed(SCALE) if counts.get(r) and r != median])
        reading = f"Moderate agreement; the other passes said {others}."
    else:
        spread = _join(sorted(set(ratings), key=SCALE.index))
        reading = f"Borderline: the passes split between {spread}, so treat this rating as uncertain."
    text = (f"{n} independent passes of the research debate: {votes}. "
            f"Median {median}, agreed on by {agree} of {n}. {reading}")
    if requested and n < requested:
        text += f" ({requested - n} of {requested} passes failed or gave no readable rating and were left out.)"
    return text


def create_research_panel(bull_node, bear_node, manager_node, conditional_logic, passes: int):
    """One graph node that runs ``passes`` complete research debates and returns the median one."""

    def one_pass(state: dict) -> dict:
        debate = {"bull_history": "", "bear_history": "", "history": "", "current_response": "", "count": 0}
        s = {**state, "investment_debate_state": debate}
        # The same routing the graph uses between the debate nodes, so a change there reaches the panel too.
        while True:
            step = conditional_logic.should_continue_debate(s)
            if step == "Research Manager":
                break
            node = bull_node if step == "Bull Researcher" else bear_node
            s["investment_debate_state"] = node(s)["investment_debate_state"]
        out = manager_node(s)
        return {"plan": out["investment_plan"], "debate": out["investment_debate_state"],
                "rating": plan_rating(out["investment_plan"])}

    def panel_node(state) -> dict:
        results, errors = [], []
        with ThreadPoolExecutor(max_workers=passes) as pool:
            # Each pass runs in its own copy of the run's context, so run-scoped settings reach the threads.
            futures = [pool.submit(contextvars.copy_context().run, one_pass, dict(state)) for _ in range(passes)]
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:   # one failed pass must not end the run
                    errors.append(exc)
                    logger.warning("A research-panel pass failed (%s); continuing with the others", exc)
        if not results:
            raise errors[0]
        usable = [r for r in results if r["rating"] in SCALE]
        if not usable:
            logger.warning("No research-panel pass gave a readable rating; using the first plan as it is")
            first = results[0]
            panel = {"passes": 0, "requested": passes, "ratings": [], "median": None, "agreement": 0.0,
                     "label": "none", "range": [],
                     "summary": f"None of {passes} research passes gave a readable rating; the first plan was used."}
            return {"investment_debate_state": first["debate"], "investment_plan": first["plan"],
                    "research_panel": panel}
        ratings = [r["rating"] for r in usable]
        median = median_rating(ratings)
        chosen = next(r for r in usable if r["rating"] == median)
        panel = {"passes": len(usable), "requested": passes, "ratings": ratings, "median": median,
                 "agreement": Counter(ratings)[median] / len(ratings), "label": agreement_label(ratings),
                 "range": rating_range(ratings), "summary": summarize(ratings, passes)}
        return {"investment_debate_state": chosen["debate"], "investment_plan": chosen["plan"],
                "research_panel": panel}

    return panel_node
