"""Tests for the extra runs offered after a borderline call (a local addition): the decision to ask, the
question, the combined verdict, and the headless runs, with the graph stubbed out."""

import contextlib

import pytest

from cli import rerun
from cli.usage_log import UsageStatsHandler
from tradingagents.agents.conviction import mean_verdict
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, render_pm_decision


def _decision(rating: PortfolioRating, score: int) -> str:
    return render_pm_decision(PortfolioDecision(rating=rating, executive_summary="s", investment_thesis="t",
                                                conviction_score=score))


HOLD_45 = _decision(PortfolioRating.HOLD, 45)
HOLD_47 = _decision(PortfolioRating.HOLD, 47)
UNDER_38 = _decision(PortfolioRating.UNDERWEIGHT, 38)


@pytest.mark.unit
class TestDecide:
    def test_a_call_that_is_not_borderline_never_asks(self):
        assert rerun.decide({}, borderline=False, interactive=True) == "skip"
        assert rerun.decide({"rerun": True}, borderline=False, interactive=True) == "skip"

    def test_at_a_keyboard_a_borderline_call_asks(self):
        assert rerun.decide({}, True, interactive=True) == "ask"

    def test_the_flags_answer_the_question(self):
        assert rerun.decide({"rerun": True}, True, interactive=False) == "run"
        assert rerun.decide({"rerun": False}, True, interactive=True) == "skip"

    def test_a_scripted_run_or_a_terminal_less_run_never_waits_for_an_answer(self):
        assert rerun.decide({"save": True, "show": False}, True, interactive=True) == "skip"
        assert rerun.decide({}, True, interactive=False) == "skip"


@pytest.mark.unit
class TestQuestionAndVerdict:
    def test_the_question_gives_the_expected_cost_and_time_from_this_run(self):
        text = rerun.offer_text({"estimated_cost_usd": 0.118, "seconds": 233})
        assert text == "Run two more analyses now (about $0.24, about 8 minutes)? [Y/n]"

    def test_without_figures_the_question_is_still_asked(self):
        assert rerun.offer_text(None) == "Run two more analyses now? [Y/n]"

    def test_three_agreeing_runs_far_from_the_line_are_not_borderline(self):
        verdict = mean_verdict(["Hold", "Hold", "Hold"], [52, 53, 54])
        assert verdict[1] is False and "all agree" in verdict[0] and "mean 53.0, range 2 points" in verdict[0]
        assert "so the call is not borderline" in verdict[0]

    def test_the_goog_morning_spread_averages_just_outside_the_margin(self):
        text, borderline = mean_verdict(["Hold", "Hold", "Hold"], [47, 45, 47])
        assert not borderline and "mean 46.3" in text and "5.8 points from the nearest line (Underweight/Hold, 40/41)" in text

    def test_three_agreeing_runs_near_the_line_stay_borderline(self):
        text, borderline = mean_verdict(["Hold", "Hold", "Hold"], [43, 44, 45])
        assert borderline and "3.5 points from the line between Underweight and Hold (40/41)" in text
        assert "within 5 points, so the call stays borderline" in text

    def test_disagreeing_runs_are_called_unsettled_whatever_the_mean(self):
        text, borderline = mean_verdict(["Hold", "Underweight", "Underweight"], [45, 35, 32])
        assert borderline and "the runs disagree" in text and "Underweight x2" in text
        assert "unsettled: read it as a lean, not a verdict" in text

    def test_fewer_than_two_scores_give_no_verdict(self):
        assert mean_verdict(["Hold"], [45]) is None

    def test_the_combined_report_reads_ratings_and_scores_from_the_decisions(self):
        text = rerun.combined_report([HOLD_45, HOLD_47, HOLD_47])
        assert text.startswith("Across 3 runs: Hold x3 (all agree). Scores 45, 47, 47")
        assert rerun.combined_report([HOLD_45, "**Rating**: Hold\n\nNo score."]) is None


class FakeConsole:
    def __init__(self):
        self.lines = []

    def print(self, text="", *args, **kwargs):
        self.lines.append(str(text))

    def status(self, message):
        self.lines.append(f"[status] {message}")
        return contextlib.nullcontext()


class FakeGraph:
    def __init__(self, decisions):
        self.debug = True
        self.decisions = list(decisions)
        self.calls, self.saved, self.debug_during = [], [], []

    def propagate(self, ticker, date, asset_type, portfolio):
        self.calls.append((ticker, date, asset_type, portfolio))
        self.debug_during.append(self.debug)
        return {"final_trade_decision": self.decisions.pop(0)}, "Hold"

    def save_reports(self, state, ticker, path, html):
        self.saved.append((ticker, path.name, html))


SELECTIONS = {"ticker": "GOOG", "analysis_date": "2026-11-04", "asset_type": "stock"}


@pytest.mark.unit
class TestRunsAndOffer:
    def _config(self, tmp_path):
        return {"results_dir": str(tmp_path / "logs")}

    def test_the_extra_runs_are_headless_saved_beside_each_other_and_restore_the_debug_setting(self, tmp_path):
        graph, console = FakeGraph([HOLD_47, HOLD_47]), FakeConsole()
        results, folder = rerun.run_extra_runs(graph, SELECTIONS, "BOOK", self._config(tmp_path), UsageStatsHandler(), console)
        assert [r[0] for r in results] == [HOLD_47, HOLD_47]
        assert graph.calls == [("GOOG", "2026-11-04", "stock", "BOOK")] * 2
        assert graph.debug_during == [False, False] and graph.debug is True
        assert [s[1] for s in graph.saved] == ["run2", "run3"] and all(s[2] for s in graph.saved)
        assert folder.parent == tmp_path / "spread" and folder.name.startswith("GOOG-2026-11-04-")

    def test_answering_yes_runs_two_more_and_prints_the_combined_result(self, tmp_path, monkeypatch):
        graph, console = FakeGraph([HOLD_47, HOLD_47]), FakeConsole()
        monkeypatch.setattr(rerun, "interactive_terminal", lambda: True)
        asked = []
        monkeypatch.setattr(rerun.typer, "prompt", lambda text, default: asked.append(text) or "Y")
        rerun.offer_more_runs({}, True, {"estimated_cost_usd": 0.12, "seconds": 240}, HOLD_45, graph, SELECTIONS, None,
                              self._config(tmp_path), UsageStatsHandler(), console)
        assert len(asked) == 1 and asked[0].startswith("Run two more analyses now (about $0.24")
        assert len(graph.calls) == 2
        output = "\n".join(console.lines)
        assert "Combined result" in output and "Across 3 runs: Hold x3 (all agree)" in output
        summaries = list((tmp_path / "spread").glob("*/summary.txt"))
        assert len(summaries) == 1 and "Across 3 runs" in summaries[0].read_text(encoding="utf-8")

    def test_answering_no_runs_nothing(self, tmp_path, monkeypatch):
        graph, console = FakeGraph([]), FakeConsole()
        monkeypatch.setattr(rerun, "interactive_terminal", lambda: True)
        monkeypatch.setattr(rerun.typer, "prompt", lambda text, default: "n")
        rerun.offer_more_runs({}, True, None, HOLD_45, graph, SELECTIONS, None, self._config(tmp_path),
                              UsageStatsHandler(), console)
        assert graph.calls == [] and console.lines == []

    def test_the_rerun_flag_runs_without_asking_and_a_scripted_run_never_prompts(self, tmp_path, monkeypatch):
        def no_prompt(text, default):
            raise AssertionError("must not ask")

        monkeypatch.setattr(rerun.typer, "prompt", no_prompt)
        monkeypatch.setattr(rerun, "interactive_terminal", lambda: True)
        graph, console = FakeGraph([UNDER_38, UNDER_38]), FakeConsole()
        rerun.offer_more_runs({"rerun": True}, True, None, HOLD_45, graph, SELECTIONS, None, self._config(tmp_path),
                              UsageStatsHandler(), console)
        assert len(graph.calls) == 2 and "the runs disagree" in "\n".join(console.lines)

        scripted = FakeGraph([])
        rerun.offer_more_runs({"save": True}, True, None, HOLD_45, scripted, SELECTIONS, None, self._config(tmp_path),
                              UsageStatsHandler(), FakeConsole())
        assert scripted.calls == []

