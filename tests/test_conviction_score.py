"""Tests for the 0-100 conviction score on the Portfolio Manager's decision (a local addition)."""

import pytest

from tradingagents.agents.rating import parse_rating
from tradingagents.agents.schemas import (
    SCORE_BANDS,
    PortfolioDecision,
    PortfolioRating,
    render_pm_decision,
)


def _decision(rating=PortfolioRating.HOLD, score=None):
    return PortfolioDecision(rating=rating, executive_summary="s", investment_thesis="t", conviction_score=score)


@pytest.mark.unit
class TestScoreCoercion:
    @pytest.mark.parametrize("written, expected", [
        (45, 45), ("45", 45), ("45/100", 45), (" 45 ", 45), (44.6, 45), ("62.4", 62), ("70%", 70), (0, 0), (100, 100),
    ])
    def test_a_usable_number_is_kept_as_a_whole_number(self, written, expected):
        assert _decision(score=written).conviction_score == expected

    @pytest.mark.parametrize("written", [None, "", "N/A", "about forty", "-5", -5, 101, "150", True, float("nan")])
    def test_anything_else_drops_only_the_score(self, written):
        decision = _decision(score=written)
        assert decision.conviction_score is None and decision.rating == PortfolioRating.HOLD


@pytest.mark.unit
class TestRendering:
    def test_a_score_inside_its_band_is_shown_with_the_band(self):
        text = render_pm_decision(_decision(PortfolioRating.UNDERWEIGHT, 38))
        assert text.endswith("**Conviction Score**: 38 of 100 (Underweight band 21-40)")

    def test_a_score_outside_the_ratings_band_says_the_rating_counts(self):
        text = render_pm_decision(_decision(PortfolioRating.HOLD, 70))
        assert "70 of 100, outside the Hold band 41-59; the rating is what counts" in text

    def test_no_score_says_so(self):
        assert render_pm_decision(_decision()).endswith("**Conviction Score**: not provided")

    def test_the_rating_still_parses_from_the_first_line(self):
        text = render_pm_decision(_decision(PortfolioRating.OVERWEIGHT, 66))
        assert text.splitlines()[0] == "**Rating**: Overweight"
        assert parse_rating(text) == "Overweight"

    def test_the_bands_tile_zero_to_one_hundred_without_gaps_or_overlaps(self):
        edges = sorted(SCORE_BANDS.values())
        assert edges[0][0] == 0 and edges[-1][1] == 100
        assert all(later[0] == earlier[1] + 1 for earlier, later in zip(edges, edges[1:], strict=False))
        assert set(SCORE_BANDS) == {rating.value for rating in PortfolioRating}


@pytest.mark.unit
def test_the_model_is_told_the_bands_and_to_stay_inside_them():
    description = PortfolioDecision.model_fields["conviction_score"].description
    assert "Sell 0-20, Underweight 21-40, Hold 41-59, Overweight 60-79, Buy 80-100" in description
    assert "must lie inside the band" in description
