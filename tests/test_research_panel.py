"""The research panel: several complete debates side by side, the median verdict goes on."""

import threading

import pytest

from tradingagents.agents import state as agent_state  # noqa: F401  (the new state keys must import cleanly)
from tradingagents.graph import research_panel as rp
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup


@pytest.mark.unit
class TestMedian:
    @pytest.mark.parametrize("ratings, median", [
        (["Hold"], "Hold"),
        (["Overweight", "Hold", "Hold"], "Hold"),
        (["Overweight", "Overweight", "Hold", "Hold", "Overweight"], "Overweight"),
        (["Underweight", "Overweight", "Hold"], "Hold"),
        (["Sell", "Buy", "Buy", "Buy", "Sell"], "Buy"),
        # an even number: the more cautious of the two middle ratings
        (["Hold", "Overweight"], "Hold"),
        (["Overweight", "Overweight", "Hold", "Hold"], "Hold"),
    ])
    def test_the_middle_rating_with_the_cautious_one_on_a_tie(self, ratings, median):
        assert rp.median_rating(ratings) == median


@pytest.mark.unit
class TestPlanRating:
    @pytest.mark.parametrize("plan, rating", [
        ("**Recommendation**: Overweight\n\n**Rationale**: x", "Overweight"),
        ("Recommendation: hold", "Hold"),
        ("**Recommendation**：Underweight", "Underweight"),     # fullwidth colon
        ("**Rating**: Sell\n\nthe rest", "Sell"),                 # a Portfolio-Manager style label still reads
        ("**Recommendation**: Hold\n\nWe would not Buy more now.", "Hold"),
        ("no label here, just prose about Hold and Buy", None),
        ("", None),
    ])
    def test_the_verdict_comes_from_the_label_not_from_words_in_the_prose(self, plan, rating):
        assert rp.plan_rating(plan) == rating

    def test_it_reads_the_text_the_real_plan_renderer_writes(self):
        from tradingagents.agents.schemas import PortfolioRating, ResearchPlan, render_research_plan
        plan = ResearchPlan(recommendation=PortfolioRating.OVERWEIGHT, rationale="r", strategic_actions="a")
        assert rp.plan_rating(render_research_plan(plan)) == "Overweight"


@pytest.mark.unit
class TestSummary:
    def test_strong_agreement(self):
        text = rp.summarize(["Hold"] * 4 + ["Overweight"], 5)
        assert "Overweight 1, Hold 4" in text and "Median Hold, agreed on by 4 of 5" in text and "Strong agreement." in text

    def test_moderate_agreement_names_the_other_view(self):
        text = rp.summarize(["Hold", "Hold", "Hold", "Overweight", "Overweight"], 5)
        assert "Moderate agreement; the other passes said Overweight." in text

    def test_a_split_is_called_borderline(self):
        text = rp.summarize(["Hold", "Overweight", "Hold", "Overweight", "Underweight"], 5)
        assert "Borderline: the passes split between Underweight and Hold and Overweight" in text
        assert "treat this rating as uncertain" in text

    def test_failed_passes_are_reported(self):
        assert "2 of 5 passes failed or gave no readable rating and were left out." in rp.summarize(["Hold"] * 3, 5)


class _Nodes:
    """Stand-ins for the debate nodes: the manager hands out the scripted ratings in the order passes finish."""

    def __init__(self, ratings, fail_on=()):
        self.ratings, self.fail_on, self.lock, self.n = list(ratings), set(fail_on), threading.Lock(), 0
        self.order = []

    def _speak(self, who):
        def node(state):
            d = state["investment_debate_state"]
            self.order.append(who)
            return {"investment_debate_state": {**d, "history": d["history"] + f"|{who}", "count": d["count"] + 1,
                                                "current_response": f"{who.capitalize()} Analyst: x"}}
        return node

    @property
    def bull(self):
        return self._speak("Bull")

    @property
    def bear(self):
        return self._speak("Bear")

    def manager(self, state):
        with self.lock:
            index = self.n
            self.n += 1
        if index in self.fail_on:
            raise RuntimeError("provider unavailable")
        rating = self.ratings[index]
        plan = f"**Recommendation**: {rating}\n\n**Rationale**: pass {index}" if rating else "no rating here"
        return {"investment_debate_state": {**state["investment_debate_state"], "current_response": plan},
                "investment_plan": plan}


def _node(nodes, passes, rounds=1):
    return rp.create_research_panel(nodes.bull, nodes.bear, nodes.manager, ConditionalLogic(rounds, 1), passes)


STATE = {"company_of_interest": "NVDA", "market_report": "m"}


@pytest.mark.unit
class TestNode:
    def test_it_forwards_the_median_pass_and_says_how_the_vote_went(self):
        out = _node(_Nodes(["Overweight", "Hold", "Hold", "Overweight", "Hold"]), 5)(STATE)
        panel = out["research_panel"]
        assert panel["median"] == "Hold" and panel["passes"] == 5 and panel["requested"] == 5
        assert panel["ratings"] == ["Overweight", "Hold", "Hold", "Overweight", "Hold"] or sorted(panel["ratings"]) == sorted(
            ["Overweight", "Hold", "Hold", "Overweight", "Hold"])
        assert panel["agreement"] == pytest.approx(0.6)
        assert "Median Hold, agreed on by 3 of 5" in panel["summary"]
        assert out["investment_plan"].startswith("**Recommendation**: Hold")
        # the debate that goes on belongs to the same pass as the plan
        assert out["investment_debate_state"]["current_response"] == out["investment_plan"]

    def test_each_pass_is_a_complete_debate_bull_first_then_bear(self):
        nodes = _Nodes(["Hold"] * 3)
        out = _node(nodes, 3)(STATE)
        assert nodes.n == 3
        history = out["investment_debate_state"]["history"]
        assert history == "|Bull|Bear"                       # a fresh debate each pass, not one long one
        assert out["investment_debate_state"]["count"] == 2

    def test_more_debate_rounds_mean_more_turns_in_every_pass(self):
        nodes = _Nodes(["Hold"] * 2)
        out = _node(nodes, 2, rounds=2)(STATE)
        assert out["investment_debate_state"]["history"] == "|Bull|Bear|Bull|Bear"

    def test_the_passes_run_at_the_same_time(self):
        seen = set()

        class Slow(_Nodes):
            def manager(self, state):
                seen.add(threading.current_thread().name)
                import time
                time.sleep(0.05)
                return super().manager(state)

        _node(Slow(["Hold"] * 5), 5)(STATE)
        assert len(seen) > 1

    def test_a_failed_pass_is_left_out_and_the_vote_says_so(self):
        out = _node(_Nodes(["Hold", "Hold", "Overweight", "Hold", "Hold"], fail_on=(1,)), 5)(STATE)
        panel = out["research_panel"]
        assert panel["passes"] == 4 and panel["requested"] == 5
        assert "1 of 5 passes failed or gave no readable rating" in panel["summary"]

    def test_if_every_pass_fails_the_error_is_raised(self):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            _node(_Nodes(["Hold"] * 3, fail_on=(0, 1, 2)), 3)(STATE)

    def test_if_no_pass_gives_a_readable_rating_the_first_plan_goes_on_unchanged(self):
        out = _node(_Nodes([None, None, None]), 3)(STATE)
        assert out["research_panel"]["median"] is None and out["research_panel"]["passes"] == 0
        assert out["investment_plan"] == "no rating here"
        assert "None of 3 research passes gave a readable rating" in out["research_panel"]["summary"]

    def test_run_scoped_settings_reach_the_threads(self):
        import contextvars
        var = contextvars.ContextVar("v", default="unset")
        seen = []

        class Reads(_Nodes):
            def manager(self, state):
                seen.append(var.get())
                return super().manager(state)

        var.set("set-by-the-run")
        _node(Reads(["Hold"] * 3), 3)(STATE)
        assert seen == ["set-by-the-run"] * 3


@pytest.mark.unit
class TestReachesTheReaders:
    def test_the_report_gets_a_panel_section_first_in_the_research_team_decision(self, tmp_path):
        from tradingagents.reporting import write_report_tree
        state = {"investment_plan": "RM PLAN", "scorecard": "A. The market: x",
                 "research_panel": {"summary": "5 independent passes: Hold 4, Overweight 1. Median Hold."}}
        out = write_report_tree(state, "AAPL", tmp_path, html=False)
        assert (tmp_path / "2_research" / "panel.md").read_text() == "5 independent passes: Hold 4, Overweight 1. Median Hold."
        assert (tmp_path / "2_research" / "scorecard.md").read_text() == "A. The market: x"
        text = out.read_text()
        assert text.index("Research Panel") < text.index("Scorecard") < text.index("Research Manager")

    def test_a_run_without_a_panel_writes_no_panel_file(self, tmp_path):
        from tradingagents.reporting import write_report_tree
        write_report_tree({"investment_plan": "RM PLAN"}, "AAPL", tmp_path, html=False)
        assert not (tmp_path / "2_research" / "panel.md").exists()

    def _pm_prompt(self, state_extra):
        from unittest.mock import MagicMock
        from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
        from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
        captured = {}
        bound = MagicMock()
        bound.invoke.side_effect = lambda prompt: captured.__setitem__("p", prompt) or PortfolioDecision(
            rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="y")
        llm = MagicMock()
        llm.with_structured_output.return_value = bound
        create_portfolio_manager(llm)({
            "company_of_interest": "AAPL", "investment_plan": "plan", "trader_investment_plan": "trader plan",
            "risk_debate_state": {"history": "h", "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
                                  "current_aggressive_response": "", "current_conservative_response": "",
                                  "current_neutral_response": "", "latest_speaker": "Neutral", "count": 1},
            **state_extra})
        return captured["p"]

    def test_the_portfolio_manager_is_told_how_the_panel_voted(self):
        prompt = self._pm_prompt({"research_panel": {"summary": "5 independent passes: Hold 3, Overweight 2. Moderate agreement."}})
        assert "- Research panel: 5 independent passes: Hold 3, Overweight 2. Moderate agreement." in prompt

    def test_without_a_panel_the_portfolio_managers_prompt_is_as_before(self):
        assert "Research panel" not in self._pm_prompt({})
        assert "Research panel" not in self._pm_prompt({"research_panel": {}})


class _Llm:
    def with_structured_output(self, schema):
        raise NotImplementedError


@pytest.mark.unit
class TestGraphShape:
    def _nodes(self, passes):
        setup = GraphSetup(_Llm(), _Llm(), ConditionalLogic(1, 1), 20, research_passes=passes)
        return set(setup.setup_graph(("market",)).nodes)

    def test_one_pass_keeps_the_original_chain(self):
        nodes = self._nodes(1)
        assert {"Bull Researcher", "Bear Researcher", "Research Manager"} <= nodes and "Research Panel" not in nodes

    def test_several_passes_use_one_panel_node_instead(self):
        nodes = self._nodes(5)
        assert "Research Panel" in nodes
        assert not ({"Bull Researcher", "Bear Researcher", "Research Manager"} & nodes)

    def test_the_scorecard_is_worked_out_alongside_the_analysts_in_both_shapes(self):
        assert "Scorecard" in self._nodes(1) and "Scorecard" in self._nodes(5)

    def test_the_default_config_asks_for_five_passes(self):
        from tradingagents.default_config import build_default_config
        assert build_default_config()["research_passes"] == 5

    def test_the_environment_can_set_the_number_of_passes(self, monkeypatch):
        from tradingagents.default_config import build_default_config
        monkeypatch.setenv("TRADINGAGENTS_RESEARCH_PASSES", "3")
        assert build_default_config()["research_passes"] == 3
