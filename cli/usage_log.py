"""Token totals and estimated cost for a finished CLI run (a local addition).

The live view shows tokens while a run is going and then clears. This keeps the final
totals per model, estimates the cost at Anthropic's list prices, and saves both beside
the run's reports and in one running log, so what each analysis cost can be checked
against the billing page.
"""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.outputs import LLMResult

from cli.stats_handler import StatsCallbackHandler

# Dollars per million tokens, input then output, from Anthropic's list prices. A cache read
# costs 10% of the input price and a five-minute cache write 125% of it.
LIST_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
}

LOG_COLUMNS = ("finished_at", "ticker", "analysis_date", "seconds", "llm_calls", "tool_calls",
               "tokens_in", "tokens_out", "estimated_cost_usd", "models")


def price_for(model: str) -> tuple[float, float] | None:
    """The list price for ``model``, matching dated variants by their longest known prefix."""
    name = (model or "").lower()
    known = [key for key in LIST_PRICES if name.startswith(key)]
    return LIST_PRICES[max(known, key=len)] if known else None


class UsageStatsHandler(StatsCallbackHandler):
    """The CLI's stats handler, plus token counts kept per model."""

    def __init__(self) -> None:
        super().__init__()
        self.by_model: dict[str, dict[str, int]] = {}

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        super().on_llm_end(response, **kwargs)
        try:
            message = response.generations[0][0].message
        except (IndexError, TypeError, AttributeError):
            return
        usage = getattr(message, "usage_metadata", None) or {}
        if not usage:
            return
        meta = getattr(message, "response_metadata", None) or {}
        model = (meta.get("model_name") or meta.get("model")
                 or (response.llm_output or {}).get("model_name") or "unknown")
        details = usage.get("input_token_details") or {}
        with self._lock:
            row = self.by_model.setdefault(
                model, {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            row["calls"] += 1
            row["input"] += usage.get("input_tokens", 0)
            row["output"] += usage.get("output_tokens", 0)
            row["cache_read"] += details.get("cache_read", 0) or 0
            row["cache_write"] += details.get("cache_creation", 0) or 0


def estimate_cost(row: dict[str, int], model: str) -> float | None:
    """Dollars for one model's token counts at list prices, or None for an unknown model."""
    price = price_for(model)
    if price is None:
        return None
    price_in, price_out = price
    fresh = max(0, row["input"] - row["cache_read"] - row["cache_write"])
    cost = (fresh * price_in + row["cache_read"] * price_in * 0.10
            + row["cache_write"] * price_in * 1.25 + row["output"] * price_out)
    return cost / 1_000_000


def summarize(handler: UsageStatsHandler) -> dict[str, Any]:
    """Totals, the per-model split and the estimated cost for the run so far."""
    stats = handler.get_stats()
    with handler._lock:
        by_model = {model: dict(row) for model, row in handler.by_model.items()}
    costs = {model: estimate_cost(row, model) for model, row in by_model.items()}
    total = None if not costs or any(c is None for c in costs.values()) else sum(costs.values())
    return {
        "llm_calls": stats["llm_calls"], "tool_calls": stats["tool_calls"],
        "tokens_in": stats["tokens_in"], "tokens_out": stats["tokens_out"],
        "models": {model: {**row, "estimated_cost_usd": None if costs[model] is None else round(costs[model], 4)}
                   for model, row in by_model.items()},
        "estimated_cost_usd": None if total is None else round(total, 4),
    }


def describe(usage: dict[str, Any]) -> str:
    """One plain line for the console."""
    cost = usage["estimated_cost_usd"]
    worth = ("about $%.2f at list prices" % cost) if cost is not None else "cost not estimated (a model has no listed price)"
    return (f"Tokens: {usage['tokens_in']:,} in, {usage['tokens_out']:,} out across {usage['llm_calls']} model calls; {worth}. "
            "Saved as usage.json and in usage-log.csv.")


def record_usage(handler: UsageStatsHandler, config: dict, ticker: str, analysis_date: str,
                 run_dir: Path, seconds: float) -> dict[str, Any]:
    """Save the run's usage beside its reports and add a row to the running log; returns the usage."""
    usage = summarize(handler)
    usage.update(ticker=ticker, analysis_date=analysis_date, seconds=round(seconds),
                 finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                 note="Estimated at Anthropic list prices; compare with the billing page, which can lag.")
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "usage.json").write_text(json.dumps(usage, indent=2), encoding="utf-8")

    log = Path(config["results_dir"]) / "usage-log.csv"
    fresh = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if fresh:
            writer.writerow(LOG_COLUMNS)
        writer.writerow([usage["finished_at"], ticker, analysis_date, usage["seconds"], usage["llm_calls"],
                         usage["tool_calls"], usage["tokens_in"], usage["tokens_out"],
                         "" if usage["estimated_cost_usd"] is None else usage["estimated_cost_usd"],
                         "; ".join(sorted(usage["models"]))])
    return usage
