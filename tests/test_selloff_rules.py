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

    def test_a_fall_the_stocks_sector_explains_counts_as_market_driven_but_only_for_condition_b(self):
        """The S&P 500 alone called half of TSM's 21-session fall its own when the semiconductor fund explained all of it."""
        text = selloff.CORE
        rule_1 = text.split("1. ", 1)[1].split("\n2. ", 1)[0]
        assert "Weakness the market explains is not evidence against the company" in rule_1
        assert "Sector reading" in rule_1 and "the market's panic in one part of it" in rule_1
        assert "The part of a fall the S&P 500 alone does not explain is not the company's own if the sector explains it" in rule_1
        rule_4 = text.split("4. ", 1)[1].split("\n5. ", 1)[0]
        assert "the stock's sector fund may explain the fall instead, by the same two bars" in rule_4
        assert "both windows from the S&P 500" in rule_4 and "never one of each" in rule_4
        assert "A sector explanation counts for (b) only" in rule_4
        assert "the company evidence of (c) and the survival test of (d) are judged exactly as before" in rule_4
        assert "60% or more of it over the 63 sessions and at least 45% over the last 21" in rule_4     # the bars did not move

    def test_the_debaters_and_managers_are_told_a_sector_explained_fall_is_not_the_companys_own(self):
        for role in ("bear", "conservative", "neutral", "research_manager", "portfolio_manager"):
            assert "sector" in selloff.ROLE_LINES[role], role
        for role in ("research_manager", "portfolio_manager"):
            assert "the sector fund's share when the reports give it" in selloff.ROLE_LINES[role]

    def test_evidence_counts_in_both_directions_by_size_and_a_small_move_is_neither(self):
        """Rule 2 used to list only what counts against a company, so a one-quarter dip counted as 'deterioration' by the letter."""
        rule_2 = selloff.CORE.split("2. ", 1)[1].split("\n3. ", 1)[0]
        assert "counts in both directions, by its size, and by the same standard in both" in rule_2
        for mirror in ("estimates or guidance raised", "results that beat", "a rising margin or a rise in operating cash flow",
                       "falling debt", "a shrinking share count", "a stronger balance sheet"):
            assert mirror in rule_2
        assert "net it against the opposite evidence" in rule_2 and '"watch item"' in rule_2
        assert "neither deterioration nor improvement" in rule_2 and "one quarter does not break a trend of several" in rule_2
        assert "scorecard's evidence tally (line I)" in rule_2

    def test_missing_information_is_of_three_kinds_and_only_the_companys_own_withholding_counts_against_it(self):
        rule_2 = selloff.CORE.split("2. ", 1)[1].split("\n3. ", 1)[0]
        assert "Missing information is of three kinds" in rule_2
        assert "a late-filing notice, non-reliance on its financial statements, a notice of delisting, a bankruptcy filing" in rule_2
        assert "what the company withheld" in rule_2 and "counts against it" in rule_2
        assert "what we could not see" in rule_2 and "counts neither for nor against the company" in rule_2
        assert "blocks a deal call" in rule_2 and '"not established: data gap"' in rule_2
        assert "what this company never reports is neutral" in rule_2

    def test_the_deal_conditions_start_from_the_codes_reading_and_a_threshold_is_never_moved_to_suit_a_conclusion(self):
        rule_4 = selloff.CORE.split("4. ", 1)[1].split("\n5. ", 1)[0]
        assert "The scorecard's rule check (line H)" in rule_4 and "fixed thresholds, worked out before the debate" in rule_4
        assert "overturn a result only with a specific figure from the reports (name the figure)" in rule_4
        assert "do not move a threshold to suit a conclusion" in rule_4
        assert "UNKNOWN is a gap in our data, not a finding about the company, and it still counts as not met for a deal call" in rule_4

    def test_condition_e_counts_growth_in_both_directions_and_says_the_peg_rests_on_the_same_consensus(self):
        rule_4 = selloff.CORE.split("4. ", 1)[1].split("\n5. ", 1)[0]
        assert "the valuation is low against its own history or its estimates, not stretched" in rule_4
        assert "growth counts, in both directions" in rule_4
        assert "a growth-adjusted P/E (PEG) of 1.0 or less" in rule_4 and "a PEG of 2.0 or more" in rule_4
        assert "rests on the same consensus growth the estimates test, so say so" in rule_4
        assert "a PEG of 1.0 or less also pays for an earnings yield up to 2 points below the 10-year Treasury yield" in rule_4

    def test_the_survival_test_is_passed_on_the_numbers_when_cash_covers_all_the_debt(self):
        rule_3 = selloff.CORE.split("3. ", 1)[1].split("\n4. ", 1)[0]
        assert "A company whose cash covers all its debt and whose free cash flow is positive passes on the numbers" in rule_3
        assert "nothing can fall due that cash cannot pay" in rule_3 and "is not a gap that blocks a deal call" in rule_3
        assert "a filing tool that cannot read a foreign company's 20-F or 6-K is our gap, not the company's" in rule_3
        assert "When cash does not cover the debt, or is unknown, the maturities and any going-concern language must be read" in rule_3
        assert "A company that fails is not a deal. Cheap does not rescue it." in rule_3        # the test itself is as strict as before

    def test_a_stock_that_is_not_a_deal_is_rated_by_its_evidence_and_not_held_at_hold_for_want_of_one(self):
        rule_5 = selloff.CORE.split("5. ", 1)[1].split("\n6. ", 1)[0]
        assert "withholds that extra step and nothing more: it lowers nothing" in rule_5
        assert "rated like any other stock in a calm market, by the evidence about its business (scorecard line I)" in rule_5
        assert "do not hold a stock at Hold for want of a deal when its evidence earns more, and do not lower one for it" in rule_5
        for role in ("research_manager", "portfolio_manager"):
            assert "withholds the deal step only" in selloff.ROLE_LINES[role], role

    def test_a_change_in_debt_counts_only_when_cash_does_not_cover_the_debt(self):
        rule_2 = selloff.CORE.split("2. ", 1)[1].split("\n3. ", 1)[0]
        assert "A change in debt counts, in either direction, only when cash does not cover the debt" in rule_2
        assert "debt that doubled while cash is many times larger is not a weaker balance sheet" in rule_2

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
