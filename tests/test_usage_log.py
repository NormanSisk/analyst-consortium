"""Tests for the per-run token totals and estimated cost kept by the CLI."""

import csv
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from cli import usage_log
from cli.usage_log import UsageStatsHandler, estimate_cost, price_for, record_usage, summarize


def _result(model, tokens_in, tokens_out, cache_read=0, cache_write=0):
    message = AIMessage(
        content="x",
        response_metadata={"model_name": model},
        usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out,
                        "input_token_details": {"cache_read": cache_read, "cache_creation": cache_write}},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


@pytest.mark.unit
class TestPrices:
    def test_dated_variants_match_the_longest_known_model(self):
        assert price_for("claude-sonnet-5") == (2.0, 10.0)
        assert price_for("claude-sonnet-5-20261001") == (2.0, 10.0)
        assert price_for("claude-sonnet-5-5") == (2.0, 10.0)
        assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)

    def test_an_unlisted_model_has_no_price(self):
        assert price_for("some-other-model") is None
        assert estimate_cost({"input": 1, "output": 1, "cache_read": 0, "cache_write": 0}, "some-other-model") is None

    def test_cost_at_list_prices_including_cache_reads_and_writes(self):
        plain = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0, "cache_write": 0}
        assert estimate_cost(plain, "claude-sonnet-5") == pytest.approx(12.0)
        cached = {"input": 1_000_000, "output": 0, "cache_read": 600_000, "cache_write": 100_000}
        # 300k fresh at $2 + 600k read at $0.20 + 100k written at $2.50, per million
        assert estimate_cost(cached, "claude-sonnet-5") == pytest.approx(0.6 + 0.12 + 0.25)


@pytest.mark.unit
class TestHandler:
    def test_tokens_are_kept_per_model_and_totals_still_work(self):
        handler = UsageStatsHandler()
        handler.on_llm_end(_result("claude-sonnet-5", 1000, 200))
        handler.on_llm_end(_result("claude-sonnet-5", 500, 100))
        handler.on_llm_end(_result("claude-haiku-4-5", 4000, 300))
        assert handler.get_stats()["tokens_in"] == 5500 and handler.get_stats()["tokens_out"] == 600
        assert handler.by_model["claude-sonnet-5"]["calls"] == 2
        assert handler.by_model["claude-sonnet-5"]["input"] == 1500
        assert handler.by_model["claude-haiku-4-5"]["output"] == 300

    def test_a_response_without_usage_is_ignored(self):
        handler = UsageStatsHandler()
        handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]))
        assert handler.by_model == {} and handler.get_stats()["tokens_in"] == 0

    def test_summary_costs_each_model_and_gives_none_if_one_is_unpriced(self):
        handler = UsageStatsHandler()
        handler.on_llm_end(_result("claude-sonnet-5", 1_000_000, 100_000))
        usage = summarize(handler)
        assert usage["estimated_cost_usd"] == pytest.approx(3.0)
        handler.on_llm_end(_result("mystery-model", 10, 10))
        assert summarize(handler)["estimated_cost_usd"] is None


@pytest.mark.unit
class TestRecord:
    def test_usage_is_saved_beside_the_reports_and_appended_to_the_log(self, tmp_path):
        handler = UsageStatsHandler()
        handler.on_llm_end(_result("claude-sonnet-5", 200_000, 40_000))
        config = {"results_dir": str(tmp_path / "logs")}
        run_dir = tmp_path / "logs" / "GOOG" / "2026-11-03"
        first = record_usage(handler, config, "GOOG", "2026-11-03", run_dir, 271.4)
        record_usage(handler, config, "SPY", "2026-11-03", tmp_path / "logs" / "SPY" / "2026-11-03", 120.0)

        saved = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
        assert saved["tokens_in"] == 200_000 and saved["tokens_out"] == 40_000
        assert saved["estimated_cost_usd"] == pytest.approx(0.8)
        assert saved["seconds"] == 271 and first["ticker"] == "GOOG"

        rows = list(csv.reader((tmp_path / "logs" / "usage-log.csv").open(encoding="utf-8")))
        assert rows[0] == list(usage_log.LOG_COLUMNS)           # one header, written once
        assert len(rows) == 3 and rows[1][1] == "GOOG" and rows[2][1] == "SPY"
        assert rows[1][8] == "0.8" and rows[1][9] == "claude-sonnet-5"

    def test_the_console_line_is_plain(self):
        handler = UsageStatsHandler()
        handler.on_llm_end(_result("claude-sonnet-5", 200_000, 40_000))
        line = usage_log.describe(summarize(handler))
        assert "200,000 in" in line and "40,000 out" in line and "about $0.80" in line
