"""Tests for the Price Target guard (a local addition): a target is shown only when the text states it."""

import pytest

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, render_pm_decision


def _line(target, summary="s", thesis="t", rating=PortfolioRating.OVERWEIGHT):
    text = render_pm_decision(PortfolioDecision(rating=rating, executive_summary=summary, investment_thesis=thesis,
                                                price_target=target))
    return next(line for line in text.splitlines() if line.startswith("**Price Target**"))


@pytest.mark.unit
class TestGuard:
    @pytest.mark.parametrize("target, thesis", [
        (239.0, "Reference level $239 from the stated multiple."),
        (239.0, "A level of 239.0 follows from 26x on EPS of 9.2."),
        (239.0, "Applying the median to EPS gives 239.00."),
        (2.58, "The 2.58 level is 12x the forward EPS."),
        (1150.25, "Worked out as 1,150.25 using the median multiple."),
        (1150.25, "Worked out as 1150.25 using the median multiple."),
        (3400.0, "The level is 3,400 on the stated basis."),
    ])
    def test_a_target_the_thesis_states_is_shown(self, target, thesis):
        assert _line(target, thesis=thesis) == f"**Price Target**: {target}"

    def test_a_number_in_the_summary_also_explains_it(self):
        assert _line(239.0, summary="Add on weakness toward 239 where the multiple reaches its median.") == "**Price Target**: 239.0"

    @pytest.mark.parametrize("target, thesis", [
        (239.0, "No number given here."),
        (239.0, "The stop sits at 239.5 on the chart."),         # 239.5 is not 239
        (2.58, "The stock traded at 12.58 last year."),          # 12.58 is not 2.58
        (2.58, "Revenue was 2.581 billion."),                    # 2.581 is not 2.58
        (239.0, "Targets of 1239 were quoted."),                 # 1239 is not 239
    ])
    def test_a_target_the_text_never_states_is_not_offered(self, target, thesis):
        line = _line(target, thesis=thesis)
        assert line.startswith("**Price Target**: not provided (the model suggested ")
        assert "do not say where it comes from" in line

    def test_no_target_is_still_not_provided(self):
        assert _line(None) == "**Price Target**: not provided"

    def test_the_field_description_tells_the_model_about_the_guard(self):
        description = PortfolioDecision.model_fields["price_target"].description
        assert "shows the target only when the summary or thesis states the same number" in description
