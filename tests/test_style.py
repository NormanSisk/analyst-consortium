"""The investing-style lens (a local addition): standard changes nothing, growth reaches every debater and both managers,
the survival test stays, and the report says which lens was used."""

import pytest

from tests.test_selloff_rules import _prompts
from tradingagents import reporting
from tradingagents.agents import selloff, style

MARKER = "## Investing style: growth lens"


@pytest.mark.unit
class TestName:
    @pytest.mark.parametrize("value, expected", [
        (None, "standard"), ("", "standard"), ("standard", "standard"), ("growth", "growth"), (" GROWTH ", "growth"),
        ("value", "standard"), ("aggressive", "standard"), ("growth; rm -rf", "standard"),
    ])
    def test_unknown_or_missing_means_standard(self, monkeypatch, value, expected):
        monkeypatch.delenv(style.ENV, raising=False)
        if value is not None:
            monkeypatch.setenv(style.ENV, value)
        assert style.style_name() == expected

    def test_an_environment_can_be_passed_in(self):
        assert style.style_name({style.ENV: "growth"}) == "growth" and style.style_name({}) == "standard"

    def test_every_style_has_a_label_and_help_for_the_page(self):
        assert set(style.STYLES) == set(style.LABELS) == set(style.HELP) == {"standard", "growth"}
        assert style.LABELS["standard"].startswith("Standard") and "not been tested" in style.HELP["growth"]


@pytest.mark.unit
class TestLens:
    def test_standard_adds_nothing_for_any_role(self, monkeypatch):
        monkeypatch.delenv(style.ENV, raising=False)
        assert all(style.style_lens(role) == "" for role in style.ROLE_LINES)

    def test_growth_adds_the_lens_and_the_roles_own_line(self, monkeypatch):
        monkeypatch.setenv(style.ENV, "growth")
        for role, line in style.ROLE_LINES.items():
            text = style.style_lens(role)
            assert MARKER in text and line in text and text.endswith("\n"), role

    def test_the_roles_match_the_selloff_rules_roles(self):
        assert set(style.ROLE_LINES) == set(selloff.ROLE_LINES)

    @pytest.mark.parametrize("value", ["standard", "growth"])
    def test_an_unknown_role_is_an_error_whatever_the_style(self, monkeypatch, value):
        monkeypatch.setenv(style.ENV, value)
        with pytest.raises(KeyError):
            style.style_lens("trader")

    def test_the_lens_keeps_the_survival_test_and_the_duty_to_name_what_would_prove_it_wrong(self):
        core = style.CORE_GROWTH
        assert "survival test in the selloff rules stays as written" in core
        assert "financing risk, not a growth story" in core
        assert "Name the single fact that would show growth is not turning into a business" in core
        assert "Conditions (a), (b) and (d) are unchanged" in core

    def test_a_claim_without_a_figure_is_only_a_signal(self):
        core = style.CORE_GROWTH
        assert "without a figure" in core and "unresolved signal" in core and "not proof of revenue" in core
        assert '"not established"' in core

    def test_the_lens_does_not_tell_anyone_to_rate_higher(self):
        text = (style.CORE_GROWTH + " ".join(style.ROLE_LINES.values())).lower()
        for pushy in ("rate it higher", "more positive", "upgrade", "buy "):
            assert pushy not in text, pushy
        assert "same five-point scale and with the same evidence standard" in style.ROLE_LINES["portfolio_manager"]

    def test_both_sides_are_told_that_a_multiple_or_a_loss_alone_is_not_enough(self):
        assert "high multiple or the absence of profit is not enough" in style.ROLE_LINES["bear"]
        assert "losing money or a high multiple is not enough" in style.ROLE_LINES["conservative"]


@pytest.mark.unit
class TestReachesTheAgents:
    def test_all_seven_agents_send_the_lens_with_their_own_line(self, monkeypatch):
        monkeypatch.delenv(selloff.DISABLE_ENV, raising=False)
        monkeypatch.setenv(style.ENV, "growth")
        for role, prompt in _prompts().items():
            assert MARKER in prompt and style.ROLE_LINES[role] in prompt, role
            assert prompt.index("## Selloffs and deals") < prompt.index(MARKER), role      # the lens reads the deal test

    def test_the_lens_comes_before_the_output_instructions_in_the_managers(self, monkeypatch):
        monkeypatch.setenv(style.ENV, "growth")
        prompts = _prompts()
        for role in ("research_manager", "portfolio_manager"):
            assert prompts[role].index(MARKER) < prompts[role].index("## Output"), role

    def test_standard_sends_none_of_it(self, monkeypatch):
        monkeypatch.delenv(style.ENV, raising=False)
        for role, prompt in _prompts().items():
            assert "Investing style" not in prompt and "growth lens" not in prompt, role

    def test_choosing_growth_changes_nothing_else_in_any_prompt(self, monkeypatch):
        monkeypatch.delenv(style.ENV, raising=False)
        standard = _prompts()
        monkeypatch.setenv(style.ENV, "growth")
        growth = _prompts()
        for role in standard:
            added = style.CORE_GROWTH + "\n\n" + style.ROLE_LINES[role] + "\n"
            assert growth[role].replace(added, "") == standard[role], role

    def test_it_works_with_the_selloff_rules_switched_off(self, monkeypatch):
        monkeypatch.setenv(selloff.DISABLE_ENV, "1")
        monkeypatch.setenv(style.ENV, "growth")
        for role, prompt in _prompts().items():
            assert MARKER in prompt and "## Selloffs and deals" not in prompt, role


@pytest.mark.unit
class TestTheReportSaysWhichLensWasUsed:
    def test_the_markdown_header_names_a_non_standard_style_only(self):
        growth = reporting._header("NVDA", {"trade_date": "2026-11-08"}, {"investing_style": "growth", "analysts": ["market"]})
        standard = reporting._header("NVDA", {"trade_date": "2026-11-08"}, {"investing_style": "standard", "analysts": ["market"]})
        assert "Investing style: growth lens, chosen by the user" in growth
        assert "Investing style" not in standard
        assert "Investing style" not in reporting._header("NVDA", {}, {"analysts": ["market"]})     # an older run's settings

    def test_the_html_details_name_it_too(self):
        from tradingagents import report_html

        growth = report_html._run_details({}, {"investing_style": "growth", "analysts": ["market"]})
        assert ("Investing style", "growth lens, chosen by the user") in growth
        assert "Investing style" not in [label for label, _ in report_html._run_details({}, {"analysts": ["market"]})]

    def test_the_run_settings_record_the_style(self, monkeypatch):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        monkeypatch.setenv(style.ENV, "growth")
        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config, graph.selected_analysts = {"llm_provider": None}, ["market"]
        assert graph.run_settings()["investing_style"] == "growth"
        monkeypatch.delenv(style.ENV)
        assert graph.run_settings()["investing_style"] == "standard"
