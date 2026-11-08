"""Tests for the tool on/off switch (a local addition): a disabled tool is neither offered nor mentioned."""

import importlib

import pytest

from tradingagents.agents.analysts import fundamentals_analyst, market_analyst, toolset


class _Tool:
    def __init__(self, name):
        self.name = name


@pytest.mark.unit
class TestToolset:
    def test_nothing_is_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(toolset.DISABLE_ENV, raising=False)
        tools = (_Tool("a"), _Tool("b"))
        assert toolset.disabled() == set() and toolset.offered(tools) == tools

    def test_named_tools_are_dropped_in_order_and_whitespace_and_blanks_are_ignored(self, monkeypatch):
        monkeypatch.setenv(toolset.DISABLE_ENV, " b , ,d,")
        tools = (_Tool("a"), _Tool("b"), _Tool("c"), _Tool("d"))
        assert [t.name for t in toolset.offered(tools)] == ["a", "c"]

    def test_guidance_covers_only_offered_tools_in_the_dictionary_order(self):
        paragraphs = {"b": " B.", "a": " A.", "z": " Z."}
        assert toolset.guidance((_Tool("a"), _Tool("b")), paragraphs) == " B. A."
        assert toolset.guidance((), paragraphs) == ""

    def test_the_analyst_modules_honor_the_switch_when_imported(self, monkeypatch):
        monkeypatch.setenv(toolset.DISABLE_ENV, "get_market_context,get_ownership_and_short_interest")
        try:
            importlib.reload(market_analyst)
            importlib.reload(fundamentals_analyst)
            assert "get_market_context" not in [t.name for t in market_analyst.TOOLS]
            assert "get_ownership_and_short_interest" not in [t.name for t in fundamentals_analyst.TOOLS]
            assert "get_market_context" not in toolset.guidance(market_analyst.TOOLS, market_analyst._GUIDANCE)
            assert "get_market_statistics" in toolset.guidance(market_analyst.TOOLS, market_analyst._GUIDANCE)
            assert "get_ownership_and_short_interest" not in toolset.guidance(
                fundamentals_analyst.TOOLS, fundamentals_analyst._GUIDANCE)
            assert "get_stock_data" in [t.name for t in market_analyst.TOOLS]       # the rest are untouched
        finally:
            monkeypatch.delenv(toolset.DISABLE_ENV, raising=False)
            importlib.reload(market_analyst)
            importlib.reload(fundamentals_analyst)
        assert "get_market_context" in [t.name for t in market_analyst.TOOLS]
        assert "get_market_context" in toolset.guidance(market_analyst.TOOLS, market_analyst._GUIDANCE)
