"""The scorecard: a fact sheet worked out in code from the data tools and shared by the bull, the bear and the manager."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tradingagents.agents import scorecard
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

# The seven data tools' output for an invented company (EXMP, Example Semiconductor Corp): every name and number is made up,
# and the layout follows what the tools print.
OUTPUTS = json.loads((Path(__file__).parent / "fixtures" / "scorecard_example.json").read_text(encoding="utf-8"))


@pytest.mark.unit
class TestBuild:
    def test_every_section_is_read_from_the_tool_output(self):
        text = scorecard.build_scorecard(OUTPUTS)
        lines = {line.split(":")[0].split(".")[0]: line for line in text.splitlines()}
        assert set(lines) == {"A", "B", "C", "C2", "D", "E", "F", "G"}
        assert "Semiconductors (SMH) bear market (-27.2% from its high)" in lines["A"]
        assert "S&P 500 pullback (-6.4% from its high)" in lines["A"]
        assert "VIX 23.8, above 91% of the past year's readings" in lines["A"]
        assert "last 63 sessions: stock -15.0%, S&P 500 -5.8%" in lines["B"] and "About 66% of the stock's move" in lines["B"]
        assert "last 21 sessions: stock -16.2%, S&P 500 -4.1%" in lines["B"] and "About 43% of the stock's move" in lines["B"]
        assert "-36.2% versus the prior quarter" in lines["C"] and "Long-term debt: 21,540, 2.9 times" in lines["C"]
        assert "Estimates for next fiscal year (ending Jan 2028) are being raised" in lines["C"]
        assert "Next scheduled report: 2026-11-17" in lines["C"]
        assert "Open-market purchases: 2, about $1.4M by 2 insiders" in lines["C2"]
        assert "Open-market sales: 18, about $412.3M by 4 insiders" in lines["C2"]
        assert "Filings: Financing-type Form 8-K items" in lines["C2"] and "these usually" not in lines["C2"]
        assert "Cash and short-term investments: 31,870" in lines["D"] and "6,210, 9,480, 14,320, 19,950, 12,100" in lines["D"]
        assert "The current 25.0x is higher than 18% of those readings" in lines["E"] and "forward P/E 17.1x" in lines["E"]
        assert "earnings yield 5.8%" in lines["E"] and "10-year Treasury yield: 4.12%" in lines["E"]
        assert "High-yield spreads are 3.12%" in lines["F"]
        assert "Ratings now (41 analysts)" in lines["G"] and "mean price target 118.50, +28%" in lines["G"]

    def test_it_states_both_good_and_bad_news_in_the_tools_own_words(self):
        text = scorecard.build_scorecard(OUTPUTS)
        assert "are being raised" in text          # estimates up
        assert "-36.2% versus the prior quarter" in text   # cash flow down
        assert "debate" not in text.lower() and "bullish" not in text.lower() and "bearish" not in text.lower()

    def test_line_breaks_in_the_tool_output_do_not_matter(self):
        broken = {name: out.replace(" - ", "\n- ").replace(" ## ", "\n\n## ") for name, out in OUTPUTS.items()}
        assert scorecard.build_scorecard(broken) == scorecard.build_scorecard(OUTPUTS)

    def test_a_missing_source_leaves_only_its_section_out(self):
        partial = {k: v for k, v in OUTPUTS.items() if k != "get_valuation_context"}
        text = scorecard.build_scorecard(partial)
        assert "E. Valuation" not in text and "A. The market" in text and "D. Survival test inputs" in text

    def test_nothing_readable_gives_an_empty_scorecard(self):
        assert scorecard.build_scorecard({}) == ""
        assert scorecard.build_scorecard({name: f"{name} data" for name in scorecard.SOURCES}) == ""


@pytest.mark.unit
class TestCollect:
    def test_it_asks_each_source_with_the_ticker_and_date_and_skips_what_is_unavailable(self, monkeypatch):
        calls = []

        def fake_route(method, *args):
            calls.append((method, args))
            if method == "get_trend_facts":
                return "DATA_UNAVAILABLE: no vendor"
            if method == "get_filing_events":
                raise RuntimeError("EDGAR down")
            return f"{method} text"

        monkeypatch.setattr("tradingagents.dataflows.router.route_to_vendor", fake_route)
        out = scorecard.collect_outputs("NVDA", "2026-11-07")

        assert [c[0] for c in calls] == list(scorecard.SOURCES)
        assert all(c[1] == ("NVDA", "2026-11-07") for c in calls)
        assert "get_trend_facts" not in out and "get_filing_events" not in out
        assert out["get_market_context"] == "get_market_context text"

    def test_a_tool_switched_off_for_the_run_is_not_called(self, monkeypatch):
        called = []
        monkeypatch.setattr("tradingagents.dataflows.router.route_to_vendor", lambda m, *a: called.append(m) or "x")
        monkeypatch.setenv("TRADINGAGENTS_DISABLED_TOOLS", "get_market_context, get_macro_backdrop")
        scorecard.collect_outputs("NVDA", "2026-11-07")
        assert "get_market_context" not in called and "get_macro_backdrop" not in called
        assert "get_earnings_facts" in called


@pytest.mark.unit
class TestNode:
    def test_the_node_writes_the_scorecard_into_the_state(self, monkeypatch):
        monkeypatch.setattr(scorecard, "collect_outputs", lambda t, d: OUTPUTS)
        result = scorecard.create_scorecard_node()({"company_of_interest": "NVDA", "trade_date": "2026-11-07"})
        assert result["scorecard"].startswith("A. The market:")

    def test_the_switch_leaves_it_empty_and_calls_nothing(self, monkeypatch):
        monkeypatch.setenv(scorecard.DISABLE_ENV, "1")
        monkeypatch.setattr(scorecard, "collect_outputs", lambda t, d: pytest.fail("must not be called"))
        assert scorecard.create_scorecard_node()({"company_of_interest": "NVDA", "trade_date": "d"}) == {"scorecard": ""}

    def test_a_failure_never_stops_the_run(self, monkeypatch):
        def boom(t, d):
            raise RuntimeError("network")
        monkeypatch.setattr(scorecard, "collect_outputs", boom)
        assert scorecard.create_scorecard_node()({"company_of_interest": "NVDA", "trade_date": "d"}) == {"scorecard": ""}

    @pytest.mark.parametrize("value, on", [("", True), ("0", True), ("1", False), ("true", False), (" YES ", False)])
    def test_the_switch_is_read_each_time(self, monkeypatch, value, on):
        monkeypatch.setenv(scorecard.DISABLE_ENV, value)
        assert scorecard.enabled() is on


@pytest.mark.unit
class TestBlock:
    def test_roles_get_their_own_header(self):
        state = {"scorecard": "A. The market: x"}
        debater, manager = scorecard.scorecard_block(state), scorecard.scorecard_block(state, "manager")
        assert "Address every lettered line" in debater and "A. The market: x" in debater
        assert "Check each side's claims against it" in manager and "A. The market: x" in manager

    def test_no_scorecard_means_no_block(self, monkeypatch):
        assert scorecard.scorecard_block({}) == "" and scorecard.scorecard_block({"scorecard": "  "}) == ""
        monkeypatch.setenv(scorecard.DISABLE_ENV, "1")
        assert scorecard.scorecard_block({"scorecard": "A. x"}) == ""


def _capture(factory, state, structured=None):
    captured = {}
    llm = MagicMock()
    llm.invoke.side_effect = lambda prompt: captured.__setitem__("prompt", prompt) or MagicMock(content="argument")
    if structured:
        bound = MagicMock()
        bound.invoke.side_effect = lambda prompt: captured.__setitem__("prompt", prompt) or structured
        llm.with_structured_output.return_value = bound
    factory(llm)(state)
    prompt = captured["prompt"]
    return prompt if isinstance(prompt, str) else "\n".join(str(getattr(m, "content", m)) for m in prompt)


REPORTS = {"company_of_interest": "AAPL", "asset_type": "stock", "market_report": "m", "sentiment_report": "s",
           "news_report": "n", "fundamentals_report": "f"}
DEBATE = {"history": "", "bull_history": "", "bear_history": "", "current_response": "", "count": 0}


@pytest.mark.unit
class TestReachesThePrompts:
    PLAN = ResearchPlan(recommendation=PortfolioRating.HOLD, rationale="r", strategic_actions="a")

    def _prompts(self, scorecard_text):
        base = {**REPORTS, "investment_debate_state": DEBATE}
        if scorecard_text is not None:
            base["scorecard"] = scorecard_text
        manager_state = {"company_of_interest": "AAPL", "investment_debate_state": {**DEBATE, "history": "h", "count": 2},
                         **({"scorecard": scorecard_text} if scorecard_text is not None else {})}
        return (_capture(create_bull_researcher, base), _capture(create_bear_researcher, base),
                _capture(create_research_manager, manager_state, structured=self.PLAN))

    def test_the_bull_the_bear_and_the_manager_all_get_the_same_facts(self):
        bull, bear, manager = self._prompts("A. The market: SHARED-FACT")
        for prompt in (bull, bear, manager):
            assert "SHARED-FACT" in prompt
        assert "Address every lettered line" in bull and "Address every lettered line" in bear
        assert "Check each side's claims against it" in manager
        # the manager sees the sheet before the debate it has to judge
        assert manager.index("SHARED-FACT") < manager.index("Debate History")

    def test_without_a_scorecard_the_prompts_are_exactly_as_before(self):
        none = self._prompts(None)
        empty = self._prompts("")
        assert none == empty
        assert all("SHARED SCORECARD" not in p for p in none)
