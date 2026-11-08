"""Extra runs after a borderline call, started with one answer at the end of a run.

A borderline call (see ``tradingagents.agents.conviction``) is one a second run could put on the other
side of the line. Instead of leaving the user to start the spread by hand, the end of the run offers it:
two more full analyses of the same stock, with the same settings and holdings, saved beside each other,
and a combined verdict on the three. The data is fetched again for each run (see the notes on caching):
prices and news can have moved, and a failed fetch in one run does not carry into the next.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import typer

from cli.usage_log import summarize
from tradingagents.agents.conviction import mean_verdict, read_score
from tradingagents.agents.rating import parse_rating

EXTRA_RUNS = 2


def decide(flags: dict, borderline: bool, interactive: bool) -> str:
    """What to do about extra runs: 'skip', 'ask' or 'run'.

    ``--rerun`` and ``--no-rerun`` answer the question; without either the user is asked, but only at a
    keyboard and only when the report questions are being asked too (a scripted run passes its answers
    as flags and must never stop to wait).
    """
    if not borderline:
        return "skip"
    answer = flags.get("rerun")
    if answer is not None:
        return "run" if answer else "skip"
    scripted = flags.get("save") is not None or flags.get("show") is not None
    return "ask" if interactive and not scripted else "skip"


def interactive_terminal() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def offer_text(usage: dict | None, runs: int = EXTRA_RUNS) -> str:
    """The question, with what the extra runs are expected to cost and take when this run's figures are known."""
    cost = usage.get("estimated_cost_usd") if usage else None
    seconds = usage.get("seconds") if usage else None
    detail = []
    if cost:
        detail.append(f"about ${cost * runs:.2f}")
    if seconds:
        detail.append(f"about {max(1, round(seconds * runs / 60))} minutes")
    words = {2: "two"}.get(runs, str(runs))
    return f"Run {words} more analyses now" + (f" ({', '.join(detail)})" if detail else "") + "? [Y/n]"


def combined_report(decisions: list[str]) -> str | None:
    """The three-run verdict sentence from the first decision and the extra ones, or None without two scores."""
    ratings = [parse_rating(d) for d in decisions]
    scores = [s for s in (read_score(d) for d in decisions) if s is not None]
    verdict = mean_verdict(ratings, scores)
    return verdict[0] if verdict else None


def run_extra_runs(graph, selections: dict, portfolio, config: dict, stats_handler, console,
                   runs: int = EXTRA_RUNS) -> tuple[list[tuple[str, float | None]], Path]:
    """Run the analysis ``runs`` more times, headless, saving each beside the others.

    Returns [(decision text, estimated cost)] and the folder holding the saved reports. The graph was
    built in debug mode for the live display; that prints every message, so it is switched off for these.
    """
    ticker, date = selections["ticker"], selections["analysis_date"]
    folder = Path(config["results_dir"]).parent / "spread" / f"{ticker}-{date}-{datetime.now():%H%M}"
    folder.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, float | None]] = []
    was_debug = graph.debug
    graph.debug = False
    try:
        for number in range(1, runs + 1):
            before = summarize(stats_handler)["estimated_cost_usd"]
            with console.status(f"Extra analysis {number} of {runs} for {ticker}..."):
                final_state, _ = graph.propagate(ticker, date, selections["asset_type"], portfolio)
            after = summarize(stats_handler)["estimated_cost_usd"]
            cost = None if before is None or after is None else after - before
            decision = final_state.get("final_trade_decision", "")
            results.append((decision, cost))
            try:
                graph.save_reports(final_state, ticker, folder / f"run{number + 1}", html=True)
            except Exception as exc:  # noqa: BLE001 - the verdict matters more than the saved copy
                console.print(f"[yellow]Could not save the reports of extra run {number}: {type(exc).__name__}[/yellow]")
    finally:
        graph.debug = was_debug
    return results, folder


def offer_more_runs(flags: dict, borderline: bool, usage: dict | None, first_decision: str, graph,
                    selections: dict, portfolio, config: dict, stats_handler, console) -> None:
    """Ask (or follow --rerun), run the extra analyses and print the combined verdict."""
    action = decide(flags, borderline, interactive_terminal())
    if action == "skip":
        return
    if action == "ask" and typer.prompt(offer_text(usage), default="Y").strip().upper() not in ("Y", "YES", ""):
        return
    results, folder = run_extra_runs(graph, selections, portfolio, config, stats_handler, console)
    decisions = [first_decision, *[d for d, _ in results]]
    sentence = combined_report(decisions) or "The extra runs gave too few scores to combine."
    spent = sum(c for _, c in results if c is not None)
    console.print(f"\n[bold cyan]Combined result[/bold cyan]\n{sentence}")
    console.print(f"[dim]The extra runs cost about ${spent:.2f}. Their reports are in {folder}. "
                  "The first run's reports are in the usual place.[/dim]\n")
    try:
        (folder / "summary.txt").write_text(sentence + f"\nExtra runs cost about ${spent:.2f}.\n", encoding="utf-8")
    except OSError:
        pass
