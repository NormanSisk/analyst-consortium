"""Which tools an analyst is offered, with an on/off switch for testing (a local addition).

Set ``TRADINGAGENTS_DISABLED_TOOLS`` to a comma-separated list of tool names (for example
``get_filing_events,get_market_context``) before a run and those tools are neither offered to the analyst nor
mentioned in its prompt. That makes it possible to run the same ticker with and without a source and compare
the ratings and what the reports cite, without changing code between runs.

The switch is read when the analyst module is imported, so it applies to a whole process.
"""

import os

DISABLE_ENV = "TRADINGAGENTS_DISABLED_TOOLS"


def disabled() -> set[str]:
    return {name.strip() for name in os.getenv(DISABLE_ENV, "").split(",") if name.strip()}


def offered(tools: tuple) -> tuple:
    """The tools that are not switched off, in their original order."""
    off = disabled()
    return tuple(tool for tool in tools if tool.name not in off)


def guidance(tools: tuple, paragraphs: dict[str, str]) -> str:
    """The prompt paragraphs for the tools this analyst is actually offered, in the order given."""
    names = {tool.name for tool in tools}
    return "".join(text for name, text in paragraphs.items() if name in names)
