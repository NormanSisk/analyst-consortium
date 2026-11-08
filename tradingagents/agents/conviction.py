"""Reading the conviction score: how far a call sits from the line to its neighboring rating.

A rating owns a band of the 0-100 score. A score near the edge of its band is a call that a second
run could put on the other side, and the score says so; this turns that into a plain sentence at the
end of a run, so the caller knows whether one run is enough.
"""

from __future__ import annotations

import os
import re

from tradingagents.agents.schemas import SCORE_BANDS

# Within this many points of the line to the neighboring rating, a single run is not enough. Runs of the
# same analysis moved by about 1 to 2 points once the inputs were consistent, and by up to 13 before, so 5
# is a cautious starting point to be tightened as more runs are seen. TRADINGAGENTS_BORDERLINE_POINTS
# overrides it (a wider margin offers extra runs more often; it is also how the extra-runs path is tested).
BORDERLINE_POINTS = 5.0


def borderline_points() -> float:
    """The margin in force: the environment's, when it is a positive number, else the default."""
    try:
        value = float(os.environ.get("TRADINGAGENTS_BORDERLINE_POINTS", ""))
    except ValueError:
        return BORDERLINE_POINTS
    return value if value > 0 else BORDERLINE_POINTS


_SCORE_LINE = re.compile(r"\*\*Conviction Score\*\*:\s*(\d{1,3})\s+of\s+100", re.IGNORECASE)


def read_score(decision_text: str | None) -> int | None:
    """The conviction score in a rendered decision, or None when it has none."""
    match = _SCORE_LINE.search(decision_text or "")
    return int(match.group(1)) if match else None


def nearest_line(rating: str, score: float) -> tuple[float, str] | None:
    """(points to the nearest line of the rating's band, the rating on its other side), or None.

    The line between two bands sits half a point past the last whole number of the lower one (40/41 is
    40.5). Sell has no line below it and Buy none above. A score outside the rating's band gives None.
    """
    if rating not in SCORE_BANDS:
        return None
    low, high = SCORE_BANDS[rating]
    if not low <= score <= high:
        return None
    order = list(SCORE_BANDS)
    position = order.index(rating)
    options = []
    if position > 0:
        options.append((score - (low - 0.5), order[position - 1]))
    if position < len(order) - 1:
        options.append(((high + 0.5) - score, order[position + 1]))
    return min(options)


def mean_verdict(ratings: list[str], scores: list[float]) -> tuple[str, bool] | None:
    """(sentence, whether the call is still borderline) for several runs of the same analysis.

    The mean score is placed against the nearest line between two ratings, wherever it falls, and the
    runs are called unsettled when they gave different ratings. None when there are fewer than two scores.
    """
    if len(scores) < 2:
        return None
    mean = sum(scores) / len(scores)
    spread = max(scores) - min(scores)
    names = list(SCORE_BANDS)
    lines = [(SCORE_BANDS[names[i]][1] + 0.5, names[i], names[i + 1]) for i in range(len(names) - 1)]
    line, lower, upper = min(lines, key=lambda candidate: abs(candidate[0] - mean))
    distance = abs(line - mean)
    label = f"{int(line - 0.5)}/{int(line + 0.5)}"
    counts: dict[str, int] = {}
    for rating in ratings:
        counts[rating] = counts.get(rating, 0) + 1
    agreement = ("all agree" if len(counts) == 1 else "the runs disagree")
    tally = ", ".join(f"{name} x{count}" if count > 1 else name for name, count in sorted(counts.items(), key=lambda kv: -kv[1]))
    head = (f"Across {len(ratings)} runs: {tally} ({agreement}). Scores {', '.join(f'{s:g}' for s in scores)}: "
            f"mean {mean:.1f}, range {spread:g} points. ")
    if len(counts) > 1:
        return (head + f"The mean is {distance:.1f} points from the line between {lower} and {upper} ({label}). "
                "The call is unsettled: read it as a lean, not a verdict.", True)
    margin = borderline_points()
    if distance <= margin:
        return (head + f"The mean is {distance:.1f} points from the line between {lower} and {upper} ({label}), "
                f"within {margin:g} points, so the call stays borderline.", True)
    return (head + f"The mean is {distance:.1f} points from the nearest line ({lower}/{upper}, {label}), "
            "so the call is not borderline.", False)


def borderline_note(rating: str, decision_text: str | None) -> tuple[str, bool] | None:
    """(sentence, whether the call is borderline), or None for a decision that has no rating to judge."""
    if rating not in SCORE_BANDS:
        return None
    score = read_score(decision_text)
    if score is None:
        return ("This decision has no conviction score, so how close the call is to the next rating cannot be "
                "judged. Run the 3-run spread if it matters.", True)
    found = nearest_line(rating, score)
    if found is None:
        low, high = SCORE_BANDS[rating]
        return (f"The score ({score}) lies outside the {rating} band ({low} to {high}), so the rating and the score "
                "disagree. Trust the rating, and run the 3-run spread if it matters.", True)
    distance, neighbor = found
    low, high = SCORE_BANDS[rating]
    line = f"{low - 1}/{low}" if SCORE_BANDS[neighbor][0] < low else f"{high}/{high + 1}"
    margin = borderline_points()
    if distance <= margin:
        return (f"Borderline call: the score ({score}) is {distance:.1f} points from the line between {rating} and "
                f"{neighbor} ({line}). A single run can land on either side, so run the 3-run spread before relying "
                "on this rating.", True)
    return (f"Not borderline: the score ({score}) is {distance:.1f} points from the nearest line ({neighbor}, {line}), "
            f"more than {margin:g} points, so one run is a reasonable first look.", False)
