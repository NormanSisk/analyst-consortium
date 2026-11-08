"""The count of structured-output calls and of the silent retries as plain text."""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents import structured


@pytest.fixture(autouse=True)
def _fresh_counts():
    structured.reset_fallback_counts()
    yield
    structured.reset_fallback_counts()


def _llm(result=None, error=None):
    llm = MagicMock()
    if error:
        llm.invoke.side_effect = error
    else:
        llm.invoke.return_value = result
    return llm


@pytest.mark.unit
class TestCounts:
    def test_a_good_call_counts_as_a_call_and_not_as_a_fallback(self):
        assert structured.invoke_structured(_llm("plan"), "p", "Research Manager") == "plan"
        assert structured.fallback_counts() == {"Research Manager": {"calls": 1, "fallbacks": 0}}

    def test_an_error_counts_as_a_fallback_and_returns_none(self):
        assert structured.invoke_structured(_llm(error=ValueError("bad args")), "p", "Research Manager") is None
        assert structured.fallback_counts()["Research Manager"] == {"calls": 1, "fallbacks": 1}

    def test_an_empty_parse_counts_as_a_fallback(self):
        assert structured.invoke_structured(_llm(None), "p", "Trader") is None
        assert structured.fallback_counts()["Trader"] == {"calls": 1, "fallbacks": 1}

    def test_no_structured_binding_is_not_counted_at_all(self):
        assert structured.invoke_structured(None, "p", "Trader") is None
        assert structured.fallback_counts() == {}

    def test_the_helper_that_retries_as_text_is_counted_too(self):
        plain = MagicMock()
        plain.invoke.return_value = MagicMock(content="free text")
        out = structured.invoke_structured_or_freetext(_llm(error=ValueError("x")), plain, "p", str, "Research Manager")
        assert out == "free text"
        assert structured.fallback_counts()["Research Manager"] == {"calls": 1, "fallbacks": 1}

    def test_counts_from_many_threads_add_up(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda i: structured.invoke_structured(
                _llm(error=ValueError("x")) if i % 2 else _llm("ok"), "p", "Research Manager"), range(40)))
        assert structured.fallback_counts()["Research Manager"] == {"calls": 40, "fallbacks": 20}

    def test_reset_starts_a_fresh_count(self):
        structured.invoke_structured(_llm("ok"), "p", "Trader")
        structured.reset_fallback_counts()
        assert structured.fallback_counts() == {}


@pytest.mark.unit
class TestSummary:
    def test_all_good(self):
        counts = {"Research Manager": {"calls": 5, "fallbacks": 0}, "Trader": {"calls": 1, "fallbacks": 0}}
        assert structured.fallback_summary(counts) == "Structured output worked on all 6 calls."

    def test_some_fallbacks_name_where(self):
        counts = {"Research Manager": {"calls": 5, "fallbacks": 2}, "Trader": {"calls": 1, "fallbacks": 0}}
        assert structured.fallback_summary(counts) == (
            "Structured output fell back to plain text 2 times in 6 calls (Research Manager 2).")

    def test_no_calls(self):
        assert structured.fallback_summary({}) == "No structured-output calls were made."
