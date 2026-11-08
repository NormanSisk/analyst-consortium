"""Tests for the borderline note (a local addition): how far a call sits from the line to the next rating."""

import pytest

from tradingagents.agents.conviction import (
    BORDERLINE_POINTS,
    borderline_note,
    borderline_points,
    nearest_line,
    read_score,
)
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, render_pm_decision


def _decision(rating, score):
    return render_pm_decision(PortfolioDecision(rating=rating, executive_summary="s", investment_thesis="t",
                                                conviction_score=score))


@pytest.mark.unit
class TestNearestLine:
    @pytest.mark.parametrize("rating, score, expected", [
        ("Hold", 45, (4.5, "Underweight")),        # the line to Underweight is 40/41, at 40.5
        ("Hold", 47, (6.5, "Underweight")),
        ("Hold", 58, (1.5, "Overweight")),         # the line to Overweight is 59/60, at 59.5
        ("Underweight", 35, (5.5, "Hold")),
        ("Underweight", 22, (1.5, "Sell")),
        ("Sell", 18, (2.5, "Underweight")),        # Sell has no line below it
        ("Sell", 0, (20.5, "Underweight")),
        ("Buy", 82, (2.5, "Overweight")),          # Buy has no line above it
        ("Overweight", 70, (9.5, "Buy")),
    ])
    def test_the_distance_and_the_neighbour(self, rating, score, expected):
        distance, neighbour = nearest_line(rating, score)
        assert (round(distance, 1), neighbour) == expected

    def test_a_score_outside_the_ratings_band_or_an_unknown_rating_gives_none(self):
        assert nearest_line("Hold", 70) is None
        assert nearest_line("Review", 50) is None


@pytest.mark.unit
class TestNote:
    def test_a_score_close_to_the_line_is_borderline_and_names_the_line(self):
        text, borderline = borderline_note("Hold", _decision(PortfolioRating.HOLD, 45))
        assert borderline and "Borderline call" in text
        assert "4.5 points from the line between Hold and Underweight (40/41)" in text and "3-run spread" in text

    def test_a_score_far_from_every_line_is_not_borderline(self):
        text, borderline = borderline_note("Hold", _decision(PortfolioRating.HOLD, 52))
        assert not borderline and text.startswith("Not borderline")
        assert f"more than {BORDERLINE_POINTS:g} points" in text

    def test_the_edge_of_the_threshold_counts_as_borderline(self):
        # 5.5 points from the line is outside, 4.5 inside; exactly 5.0 cannot occur on whole-number scores.
        assert borderline_note("Underweight", _decision(PortfolioRating.UNDERWEIGHT, 35))[1] is False
        assert borderline_note("Underweight", _decision(PortfolioRating.UNDERWEIGHT, 36))[1] is True

    def test_a_decision_without_a_score_says_the_closeness_cannot_be_judged(self):
        text, borderline = borderline_note("Hold", "**Rating**: Hold\n\nNo score here.")
        assert borderline and "no conviction score" in text

    def test_a_score_that_disagrees_with_the_rating_is_reported_as_such(self):
        text, borderline = borderline_note("Hold", "**Conviction Score**: 70 of 100, outside the Hold band 41-59; x\n")
        assert read_score("**Conviction Score**: 70 of 100, outside") == 70
        assert borderline and "disagree" in text

    def test_a_decision_with_no_readable_rating_gets_no_note(self):
        assert borderline_note("Review", "anything") is None

    def test_read_score_handles_the_rendered_line_and_its_absence(self):
        assert read_score(_decision(PortfolioRating.UNDERWEIGHT, 33)) == 33
        assert read_score("**Conviction Score**: not provided") is None
        assert read_score(None) is None


@pytest.mark.unit
class TestMargin:
    def test_the_default_is_five_points(self, monkeypatch):
        monkeypatch.delenv("TRADINGAGENTS_BORDERLINE_POINTS", raising=False)
        assert borderline_points() == BORDERLINE_POINTS == 5.0

    def test_the_environment_can_widen_the_margin_and_the_note_follows_it(self, monkeypatch):
        monkeypatch.setenv("TRADINGAGENTS_BORDERLINE_POINTS", "15")
        assert borderline_points() == 15.0
        text, borderline = borderline_note("Hold", _decision(PortfolioRating.HOLD, 48))     # 7.5 points from the line
        assert borderline and text.startswith("Borderline call")
        monkeypatch.delenv("TRADINGAGENTS_BORDERLINE_POINTS")
        assert borderline_note("Hold", _decision(PortfolioRating.HOLD, 48))[1] is False

    @pytest.mark.parametrize("junk", ["", "abc", "0", "-3", "nan"])
    def test_an_unusable_setting_falls_back_to_the_default(self, monkeypatch, junk):
        monkeypatch.setenv("TRADINGAGENTS_BORDERLINE_POINTS", junk)
        assert borderline_points() == 5.0

    def test_the_mean_verdict_uses_the_same_margin(self, monkeypatch):
        from tradingagents.agents.conviction import mean_verdict

        monkeypatch.setenv("TRADINGAGENTS_BORDERLINE_POINTS", "8")
        text, borderline = mean_verdict(["Hold", "Hold"], [47, 47])                   # 6.5 points from the line
        assert borderline and "within 8 points" in text


@pytest.mark.unit
def test_the_three_real_runs_of_nov_3_and_4_come_out_as_expected():
    """GOOG's 45 was borderline and its 47 was not; IONQ's 33 and EGHT's 35 sit outside the five-point margin."""
    assert borderline_note("Hold", _decision(PortfolioRating.HOLD, 45))[1] is True
    assert borderline_note("Hold", _decision(PortfolioRating.HOLD, 47))[1] is False
    assert borderline_note("Underweight", _decision(PortfolioRating.UNDERWEIGHT, 33))[1] is False
    assert borderline_note("Underweight", _decision(PortfolioRating.UNDERWEIGHT, 35))[1] is False
