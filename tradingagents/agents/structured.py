"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# A local addition: how often each agent's structured call worked, how often its first attempt came back malformed
# and was tried once more, and how often it still had to fall back to plain text. The fallback is silent by design,
# so without a count nobody can tell whether it is rare or routine. Counts are per process; the graph resets them
# when a run starts and writes them into the run's state log.
_counts_lock = threading.Lock()
_calls: Counter = Counter()
_retries: Counter = Counter()
_fallbacks: Counter = Counter()


def reset_fallback_counts() -> None:
    """Start a fresh count (the graph calls this at the start of a run)."""
    with _counts_lock:
        _calls.clear()
        _retries.clear()
        _fallbacks.clear()


def fallback_counts() -> dict[str, dict[str, int]]:
    """``{agent: {"calls": n, "retries": r, "fallbacks": m}}`` for every agent that made a structured call since the reset.

    ``retries`` counts first attempts that came back malformed and were tried once more; ``fallbacks`` counts calls
    that still failed and went to plain text.
    """
    with _counts_lock:
        return {name: {"calls": _calls[name], "retries": _retries[name], "fallbacks": _fallbacks[name]}
                for name in sorted(_calls)}


def fallback_summary(counts: dict[str, dict[str, int]] | None = None) -> str:
    """One plain sentence on the counts, e.g. "Structured output was retried 3 times and fell back to plain text 1 time in 31 calls (Research Manager: 3 retries, 1 fallback)."."""
    counts = fallback_counts() if counts is None else counts
    calls = sum(c["calls"] for c in counts.values())
    if not calls:
        return "No structured-output calls were made."
    retried = {name: c.get("retries", 0) for name, c in counts.items() if c.get("retries", 0)}
    fell = {name: c["fallbacks"] for name, c in counts.items() if c["fallbacks"]}
    if not retried and not fell:
        return f"Structured output worked on all {calls} calls."
    r, f = sum(retried.values()), sum(fell.values())

    def times(n: int) -> str:
        return "1 time" if n == 1 else f"{n} times"

    detail = []
    for name in sorted(set(retried) | set(fell)):
        bits = []
        if retried.get(name):
            bits.append(f"{retried[name]} {'retry' if retried[name] == 1 else 'retries'}")
        if fell.get(name):
            bits.append(f"{fell[name]} {'fallback' if fell[name] == 1 else 'fallbacks'}")
        detail.append(f"{name}: {', '.join(bits)}")
    return (f"Structured output was retried {times(r)} and fell back to plain text {times(f)} in {calls} calls "
            f"({'; '.join(detail)}).")

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.

    A local addition: when the first attempt comes back malformed (a missing field, arguments that do not parse,
    no parsed result) it is tried once more before the caller falls back to plain text, because a second sample
    almost always parses. A failure of another kind (an API error, a timeout) is not retried here: the SDK
    already retries those, and a second identical request would only repeat the wait.
    """
    if structured_llm is None:
        return None
    with _counts_lock:
        _calls[agent_name] += 1
    for attempt in (1, 2):
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                # A thinking model can answer in plain text instead of calling
                # the tool, leaving the parser with nothing to return. Treat it
                # as a structured miss, with a clear reason.
                raise ValueError("structured output returned no parsed result")
            return result
        except Exception as exc:
            if attempt == 1 and isinstance(exc, ValueError):   # pydantic and LangChain parse errors are ValueErrors
                with _counts_lock:
                    _retries[agent_name] += 1
                logger.warning("%s: structured output came back malformed (%s); trying once more",
                               agent_name, exc)
                continue
            with _counts_lock:
                _fallbacks[agent_name] += 1
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name, exc,
            )
            return None
    return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure."""
    result = invoke_structured(structured_llm, prompt, agent_name)
    if result is not None:
        return render(result)
    return plain_llm.invoke(prompt).content
