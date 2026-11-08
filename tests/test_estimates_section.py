"""Tests for the estimates, revisions and analyst-actions section of the earnings tool (a local addition)."""

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import earnings

NOW = pd.Timestamp("2026-11-04")
LABELS = {"0q": "current quarter", "+1q": "next quarter", "0y": "this fiscal year (ending Jan 2027)",
          "+1y": "next fiscal year (ending Jan 2028)"}


def _trend(rows):
    return pd.DataFrame(rows, index=["current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"]).T


def _revisions(rows):
    return pd.DataFrame(rows, index=["upLast7days", "upLast30days", "downLast30days", "downLast7Days"]).T


def _revenue(rows):
    return pd.DataFrame(rows, index=["avg", "low", "high", "numberOfAnalysts", "yearAgoRevenue", "growth"]).T


def _actions(rows):
    frame = pd.DataFrame(rows, columns=["GradeDate", "Firm", "ToGrade", "FromGrade", "Action", "priceTargetAction",
                                        "currentPriceTarget", "priorPriceTarget"])
    return frame.set_index("GradeDate")


NVDA_TREND = _trend({"0q": [2.47, 2.47, 2.47, 2.47, 2.34], "+1q": [2.75, 2.75, 2.74, 2.74, 2.65],
                     "0y": [9.31, 9.31, 9.31, 9.28, 8.95], "+1y": [15.91, 15.91, 15.68, 15.24, 12.81]})
NVDA_REVISIONS = _revisions({"0q": [0, 0, 1, 1], "+1q": [0, 0, 1, 1], "0y": [0, 2, 0, 0], "+1y": [0, 7, 0, 0]})
NVDA_REVENUE = _revenue({"0q": [109e9, 105e9, 116e9, 45, 57e9, 0.912], "+1q": [124.5e9, 117e9, 138e9, 42, 68e9, 0.827],
                         "0y": [411.7e9, 403e9, 427e9, 54, 216e9, 0.906], "+1y": [692.2e9, 506e9, 778e9, 57, 411.7e9, 0.682]})


@pytest.mark.unit
class TestEstimateTable:
    def test_each_period_shows_the_consensus_its_movement_and_the_revisions(self):
        text = "\n".join(earnings.estimate_lines(NVDA_TREND, NVDA_REVISIONS, NVDA_REVENUE, None, LABELS, 202.0, NOW))
        assert "| next fiscal year (ending Jan 2028) | 15.91 | 15.68 | 12.81 | +24.2% | $692.2B | +68.2% | 57 | 7 up / 0 down |" in text
        assert "| current quarter | 2.47 | 2.47 | 2.34 | +5.6% | $109.0B | +91.2% | 45 | 0 up / 1 down |" in text

    def test_estimates_that_are_rising_with_analysts_raising_are_flagged_as_raised(self):
        text = "\n".join(earnings.estimate_lines(NVDA_TREND, NVDA_REVISIONS, NVDA_REVENUE, None, LABELS, 202.0, NOW))
        assert ("Estimates for next fiscal year (ending Jan 2028) are being raised: consensus EPS +24.2% over 90 days, "
                "7 analysts raised and 0 cut it in the last 30 days.") in text

    def test_a_jump_with_no_analyst_raising_is_called_a_reported_results_effect(self):
        trend = _trend({"0y": [16.12, 11.81, 11.81, 11.75, 11.70], "+1y": [15.49, 15.07, 15.07, 14.9, 14.80]})
        revisions = _revisions({"0y": [0, 0, 2, 0], "+1y": [0, 0, 0, 0]})
        text = "\n".join(earnings.estimate_lines(trend, revisions, None, None, LABELS, 290.0, NOW))
        assert "consensus EPS moved +37.8% over 90 days, but 0 analysts raised and 2 cut it in the last 30 days, so the move" in text
        assert "reported results replacing estimates" in text
        assert "are being raised: consensus EPS +37.8%" not in text

    def test_falling_estimates_and_quiet_ones(self):
        falling = _trend({"0y": [9.0, 9.4, 9.5, 9.6, 9.5]})
        text = "\n".join(earnings.estimate_lines(falling, _revisions({"0y": [0, 0, 4, 0]}), None, None, LABELS, 100.0, NOW))
        assert "Estimates for this fiscal year (ending Jan 2027) are being cut: consensus EPS -5.3% over 90 days" in text
        quiet = _trend({"0y": [9.6, 9.6, 9.6, 9.55, 9.5]})
        text = "\n".join(earnings.estimate_lines(quiet, _revisions({"0y": [0, 1, 1, 0]}), None, None, LABELS, 100.0, NOW))
        assert "Flags to state in the report" not in text

    def test_missing_tables_give_nothing_and_missing_revenue_gives_n_a(self):
        assert earnings.estimate_lines(None, None, None, None, LABELS, None, NOW) == []
        text = "\n".join(earnings.estimate_lines(NVDA_TREND, None, None, None, LABELS, None, NOW))
        assert "| n/a | n/a | n/a | n/a |" in text


@pytest.mark.unit
class TestAnalystActions:
    ACTIONS = _actions([
        ("2026-11-02 12:29:14", "Melius Research", "Buy", "Buy", "main", "Lowers", 315.0, 420.0),
        ("2026-10-22 09:50:43", "Needham", "Buy", "Buy", "reit", "Maintains", 300.0, 300.0),
        ("2026-10-10 10:00:00", "Cantor", "Overweight", "Neutral", "up", "Raises", 350.0, 300.0),
        ("2026-09-10 14:02:40", "Piper Sandler", "Overweight", "", "init", "Announces", 300.0, 0.0),
        ("2026-08-27 14:05:01", "Oppenheimer", "Outperform", "Outperform", "main", "Raises", 315.0, 265.0),
        ("2026-05-01 10:00:00", "Old Firm", "Buy", "Buy", "main", "Raises", 999.0, 900.0),     # outside the window
    ])

    def test_the_window_counts_target_moves_and_rating_changes(self):
        text = "\n".join(earnings.estimate_lines(None, None, None, self.ACTIONS, LABELS, 200.0, NOW))
        assert "Analyst actions in the last 90 days (5): price target raised 2, lowered 1, maintained 1, new coverage 1; " \
               "rating upgrades 1, downgrades 0." in text
        assert "Old Firm" not in text

    def test_the_target_spread_and_distance_from_the_price_are_stated(self):
        text = "\n".join(earnings.estimate_lines(None, None, None, self.ACTIONS, LABELS, 210.0, NOW))
        assert "Among the 5 firms with a target in that window, each firm's latest target ranges from 300 to 350, median 315 (+50% from the price)." in text

    def test_recent_actions_are_listed_newest_first_with_the_target_move(self):
        text = "\n".join(earnings.estimate_lines(None, None, None, self.ACTIONS, LABELS, 200.0, NOW))
        lines = [line for line in text.splitlines() if line.startswith("- 2026")]
        assert lines[0] == "- 2026-11-02 Melius Research: Buy, target lowers 420 to 315"
        assert "Cantor: Overweight (from Neutral), target raises 300 to 350" in lines[2]
        assert "Piper Sandler: Overweight, target 300 (new coverage)" in lines[3]

    def test_three_recent_target_cuts_raise_a_flag(self):
        cuts = _actions([("2026-10-29 10:00:00", "A", "Buy", "Buy", "main", "Lowers", 375.0, 425.0),
                         ("2026-10-29 09:00:00", "B", "Buy", "Buy", "main", "Lowers", 460.0, 485.0),
                         ("2026-10-29 08:00:00", "C", "Buy", "Buy", "main", "Lowers", 375.0, 450.0),
                         ("2026-10-15 08:00:00", "A", "Buy", "Buy", "main", "Raises", 425.0, 420.0)])
        text = "\n".join(earnings.estimate_lines(None, None, None, cuts, LABELS, 290.0, NOW))
        assert "3 of the 3 most recent analyst actions lowered the price target." in text


@pytest.mark.unit
class TestInTheTool:
    HISTORY = pd.DataFrame(
        {"EPS Estimate": [3.27, 2.98, 2.91], "Reported EPS": [float("nan"), 2.67, 2.85],
         "Surprise(%)": [float("nan"), -10.53, -2.15]},
        index=pd.DatetimeIndex(["2027-02-03 16:00", "2026-10-28 16:00", "2026-07-22 16:00"], tz="America/New_York"))

    def test_the_section_is_part_of_the_earnings_facts_and_withheld_for_past_runs(self, monkeypatch):
        monkeypatch.setattr(earnings, "yf_retry", lambda fn: self.HISTORY)
        monkeypatch.setattr(earnings, "_analyst_section", lambda c: [])
        monkeypatch.setattr(earnings, "_estimates_section", lambda c: ["## Analyst estimates and how they are moving", "row"])
        monkeypatch.setattr(earnings, "is_historical", lambda d: False)
        assert "## Analyst estimates and how they are moving" in earnings.get_earnings_facts("GOOG", None)
        monkeypatch.setattr(earnings, "is_historical", lambda d: True)
        out = earnings.get_earnings_facts("GOOG", "2026-08-01")
        assert "Analyst estimates" not in out and "estimates and analyst actions are withheld" in out
