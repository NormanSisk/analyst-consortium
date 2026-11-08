"""Structured output: one retry when the answer comes back malformed, and a count of calls, retries and plain-text fallbacks."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tradingagents.agents import structured
from tradingagents.agents.schemas import PortfolioRating, ResearchPlan


@pytest.fixture(autouse=True)
def _fresh_counts():
    structured.reset_fallback_counts()
    yield
    structured.reset_fallback_counts()


def _llm(*outcomes):
    """A structured model that answers with each outcome in turn (an exception is raised, anything else returned);
    the last outcome repeats."""
    llm = MagicMock()
    sequence = list(outcomes)

    def invoke(_prompt):
        outcome = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    llm.invoke.side_effect = invoke
    return llm


def _validation_error():
    try:
        ResearchPlan(recommendation=PortfolioRating.HOLD, rationale="r")   # strategic_actions missing
    except ValidationError as exc:
        return exc
    raise AssertionError("the schema should have refused this")


@pytest.mark.unit
class TestRetryAndCounts:
    def test_a_good_call_counts_as_a_call_only(self):
        llm = _llm("plan")
        assert structured.invoke_structured(llm, "p", "Research Manager") == "plan"
        assert llm.invoke.call_count == 1
        assert structured.fallback_counts() == {"Research Manager": {"calls": 1, "retries": 0, "fallbacks": 0}}

    def test_a_malformed_first_answer_is_tried_once_more_and_the_second_is_used(self):
        llm = _llm(ValueError("bad args"), "plan")
        assert structured.invoke_structured(llm, "p", "Research Manager") == "plan"
        assert llm.invoke.call_count == 2
        assert structured.fallback_counts()["Research Manager"] == {"calls": 1, "retries": 1, "fallbacks": 0}

    def test_the_missing_field_failure_seen_in_real_runs_is_retried(self):
        # a pydantic ValidationError (the 'strategic_actions field required' answer) is a ValueError
        llm = _llm(_validation_error(), "plan")
        assert structured.invoke_structured(llm, "p", "Research Manager") == "plan"
        assert structured.fallback_counts()["Research Manager"]["retries"] == 1

    def test_two_malformed_answers_in_a_row_fall_back_to_plain_text(self):
        llm = _llm(ValueError("bad args"))
        assert structured.invoke_structured(llm, "p", "Research Manager") is None
        assert llm.invoke.call_count == 2
        assert structured.fallback_counts()["Research Manager"] == {"calls": 1, "retries": 1, "fallbacks": 1}

    def test_an_empty_parse_is_treated_as_malformed_and_retried(self):
        llm = _llm(None, "plan")
        assert structured.invoke_structured(llm, "p", "Trader") == "plan"
        assert structured.fallback_counts()["Trader"] == {"calls": 1, "retries": 1, "fallbacks": 0}
        structured.reset_fallback_counts()
        assert structured.invoke_structured(_llm(None), "p", "Trader") is None
        assert structured.fallback_counts()["Trader"] == {"calls": 1, "retries": 1, "fallbacks": 1}

    def test_an_api_or_network_error_is_not_retried_here(self):
        llm = _llm(RuntimeError("provider unavailable"))
        assert structured.invoke_structured(llm, "p", "Research Manager") is None
        assert llm.invoke.call_count == 1          # the SDK already retries these; a repeat would only add waiting
        assert structured.fallback_counts()["Research Manager"] == {"calls": 1, "retries": 0, "fallbacks": 1}

    def test_no_structured_binding_is_not_counted_at_all(self):
        assert structured.invoke_structured(None, "p", "Trader") is None
        assert structured.fallback_counts() == {}

    def test_the_helper_that_falls_back_to_text_uses_the_plain_model_only_after_the_retry(self):
        plain = MagicMock()
        plain.invoke.return_value = MagicMock(content="free text")
        bound = _llm(ValueError("x"))
        out = structured.invoke_structured_or_freetext(bound, plain, "p", str, "Research Manager")
        assert out == "free text"
        assert bound.invoke.call_count == 2 and plain.invoke.call_count == 1
        assert structured.fallback_counts()["Research Manager"] == {"calls": 1, "retries": 1, "fallbacks": 1}

    def test_a_recovered_answer_never_reaches_the_plain_model(self):
        plain = MagicMock()
        out = structured.invoke_structured_or_freetext(_llm(ValueError("x"), "plan"), plain, "p", lambda r: f"rendered {r}", "Research Manager")
        assert out == "rendered plan"
        plain.invoke.assert_not_called()

    def test_counts_from_many_threads_add_up(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda i: structured.invoke_structured(
                _llm(RuntimeError("x")) if i % 2 else _llm("ok"), "p", "Research Manager"), range(40)))
        assert structured.fallback_counts()["Research Manager"] == {"calls": 40, "retries": 0, "fallbacks": 20}

    def test_reset_starts_a_fresh_count(self):
        structured.invoke_structured(_llm("ok"), "p", "Trader")
        structured.reset_fallback_counts()
        assert structured.fallback_counts() == {}


@pytest.mark.unit
class TestSummary:
    def test_all_good(self):
        counts = {"Research Manager": {"calls": 5, "retries": 0, "fallbacks": 0}, "Trader": {"calls": 1, "retries": 0, "fallbacks": 0}}
        assert structured.fallback_summary(counts) == "Structured output worked on all 6 calls."

    def test_retries_and_fallbacks_name_where(self):
        counts = {"Research Manager": {"calls": 5, "retries": 2, "fallbacks": 1}, "Trader": {"calls": 1, "retries": 0, "fallbacks": 0}}
        assert structured.fallback_summary(counts) == (
            "Structured output was retried 2 times and fell back to plain text 1 time in 6 calls "
            "(Research Manager: 2 retries, 1 fallback).")

    def test_a_retry_that_worked_is_still_reported(self):
        counts = {"Research Manager": {"calls": 5, "retries": 1, "fallbacks": 0}}
        assert structured.fallback_summary(counts) == (
            "Structured output was retried 1 time and fell back to plain text 0 times in 5 calls (Research Manager: 1 retry).")

    def test_counts_without_a_retries_field_still_read(self):
        counts = {"Research Manager": {"calls": 5, "fallbacks": 2}}
        assert "fell back to plain text 2 times in 5 calls (Research Manager: 2 fallbacks)" in structured.fallback_summary(counts)

    def test_no_calls(self):
        assert structured.fallback_summary({}) == "No structured-output calls were made."
