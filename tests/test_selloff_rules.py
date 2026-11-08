"""The selloff rules (a local addition): they reach every debater and both managers, and one switch removes them."""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents import selloff
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, ResearchPlan

MARKER = "## Selloffs and deals"
REPORTS = {"company_of_interest": "AAPL", "asset_type": "stock", "market_report": "m", "sentiment_report": "s",
           "news_report": "n", "fundamentals_report": "f"}


def _debate_llm(captured):
    llm = MagicMock()
    llm.invoke.side_effect = lambda prompt: captured.__setitem__("prompt", prompt) or MagicMock(content="argument")
    return llm


def _structured_llm(captured, result):
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: captured.__setitem__("prompt", prompt) or result
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _text(prompt):
    return prompt if isinstance(prompt, str) else "\n".join(str(getattr(m, "content", m)) for m in prompt)


INVESTMENT = {**REPORTS, "count": 0, "investment_debate_state": {
    "history": "", "bull_history": "", "bear_history": "", "current_response": "", "count": 0}}
RISK = {**REPORTS, "trader_investment_plan": "plan", "risk_debate_state": {
    "current_aggressive_response": "", "current_conservative_response": "", "current_neutral_response": "",
    "history": "", "aggressive_history": "", "conservative_history": "", "neutral_history": "", "count": 0}}


def _prompts():
    """(role, the prompt that agent sends) for the seven agents that carry the rules."""
    out = {}
    for role, factory, state in (("bull", create_bull_researcher, INVESTMENT), ("bear", create_bear_researcher, INVESTMENT),
                                 ("aggressive", create_aggressive_debator, RISK), ("conservative", create_conservative_debator, RISK),
                                 ("neutral", create_neutral_debator, RISK)):
        captured = {}
        factory(_debate_llm(captured))(state)
        out[role] = _text(captured["prompt"])
    captured = {}
    plan = ResearchPlan(recommendation=PortfolioRating.HOLD, rationale="x", strategic_actions="y")
    create_research_manager(_structured_llm(captured, plan))({
        "company_of_interest": "AAPL",
        "investment_debate_state": {"history": "h", "bull_history": "b", "bear_history": "r", "current_response": "", "count": 1}})
    out["research_manager"] = _text(captured["prompt"])
    captured = {}
    decision = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="y")
    create_portfolio_manager(_structured_llm(captured, decision))({
        "company_of_interest": "AAPL", "investment_plan": "plan", "trader_investment_plan": "trader plan",
        "risk_debate_state": {"history": "h", "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
                              "current_aggressive_response": "", "current_conservative_response": "",
                              "current_neutral_response": "", "latest_speaker": "Neutral", "count": 1}})
    out["portfolio_manager"] = _text(captured["prompt"])
    return out


@pytest.mark.unit
class TestRules:
    def test_the_rules_say_the_five_conditions_the_survival_test_and_that_a_low_price_alone_is_never_a_deal(self):
        text = selloff.CORE
        assert "Weakness the market explains is not evidence against the company" in text
        assert "Survival test" in text and "at least 12 months of cash burn" in text and "going-concern" in text
        assert "A deal needs all five" in text and "A condition the reports do not establish counts as not met" in text
        assert "Never call a deal on a lower price alone" in text and "Cheap does not rescue it" in text

    def test_condition_b_fixes_both_windows_so_the_managers_cannot_pick_the_one_that_suits(self):
        text = selloff.CORE
        assert "60% or more of it over the 63 sessions and at least 45% over the last 21" in text
        assert "do not pick the window that suits the conclusion" in text
        assert "for (b) that means figures that are missing" in text

    def test_insider_selling_and_unexplained_filings_are_unresolved_signals_not_deterioration(self):
        text = selloff.CORE
        rule_2 = text.split("2. ", 1)[1].split("\n3. ", 1)[0]
        assert "insider selling" in rule_2 and "a filing whose content the reports do not give" in rule_2
        assert '"unresolved"' in rule_2 and "do not count it as deterioration" in rule_2
        # real deterioration still counts against the company, and still fails condition (c)
        for evidence in ("estimates or guidance cut", "results that missed", "a falling margin", "a drop in operating cash flow",
                         "dilution", "a weaker balance sheet"):
            assert evidence in rule_2
        assert "deterioration evidence does" in text and "unresolved signals do not fail it" in text

    def test_the_survival_test_and_the_valuation_condition_are_as_strict_as_before(self):
        text = selloff.CORE
        assert "at least 12 months of cash burn" in text and "no going-concern warning" in text
        assert "no run of equity sales, registrations or a fast-rising share count" in text
        assert "the valuation is low against its own history or its estimates, not stretched" in text

    def test_the_managers_diagnosis_includes_the_unresolved_signals_and_both_windows(self):
        for role in ("research_manager", "portfolio_manager"):
            line = selloff.ROLE_LINES[role]
            assert "unresolved signals" in line and "both windows" in line, role

    def test_every_role_has_a_line_and_an_unknown_role_is_an_error(self):
        assert set(selloff.ROLE_LINES) == {"bull", "bear", "aggressive", "conservative", "neutral", "research_manager",
                                           "portfolio_manager"}
        with pytest.raises(KeyError):
            selloff.selloff_rules("trader")

    def test_the_managers_are_told_to_open_with_a_selloff_diagnosis(self):
        assert "**Selloff diagnosis**" in selloff.ROLE_LINES["research_manager"]
        assert "**Selloff diagnosis**" in selloff.ROLE_LINES["portfolio_manager"]
        assert "counts as not met" in selloff.ROLE_LINES["research_manager"]


@pytest.mark.unit
class TestSwitch:
    @pytest.mark.parametrize("value, on", [("", True), ("0", True), ("no", True), ("1", False), ("true", False), ("YES", False), (" on ", False)])
    def test_the_switch_is_read_each_time(self, monkeypatch, value, on):
        monkeypatch.setenv(selloff.DISABLE_ENV, value)
        assert selloff.enabled() is on
        assert bool(selloff.selloff_rules("bull")) is on

    def test_unset_means_on(self, monkeypatch):
        monkeypatch.delenv(selloff.DISABLE_ENV, raising=False)
        assert selloff.enabled() is True


@pytest.mark.unit
class TestReachesTheAgents:
    def test_all_seven_agents_send_the_rules_with_their_own_line(self, monkeypatch):
        monkeypatch.delenv(selloff.DISABLE_ENV, raising=False)
        for role, prompt in _prompts().items():
            assert MARKER in prompt, role
            assert selloff.ROLE_LINES[role] in prompt, role

    def test_the_rules_come_before_the_output_instructions_in_the_managers(self, monkeypatch):
        monkeypatch.delenv(selloff.DISABLE_ENV, raising=False)
        prompts = _prompts()
        for role in ("research_manager", "portfolio_manager"):
            assert prompts[role].index(MARKER) < prompts[role].index("## Output"), role

    def test_with_the_switch_on_none_of_them_mentions_the_rules(self, monkeypatch):
        monkeypatch.setenv(selloff.DISABLE_ENV, "1")
        for role, prompt in _prompts().items():
            assert MARKER not in prompt and "Selloff diagnosis" not in prompt, role

    def test_switching_the_rules_off_leaves_the_rest_of_each_prompt_as_it_was(self, monkeypatch):
        monkeypatch.delenv(selloff.DISABLE_ENV, raising=False)
        on = _prompts()
        monkeypatch.setenv(selloff.DISABLE_ENV, "1")
        off = _prompts()
        for role in on:
            added = selloff.CORE + "\n\n" + selloff.ROLE_LINES[role] + "\n"
            assert on[role].replace(added, "") == off[role], role
