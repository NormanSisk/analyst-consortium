"""Starting analyses from the browser and following them (a local addition).

An analysis is started exactly as ``tradingagents`` starts one from a terminal: the same command, the same flags and
the same environment variables, in a child process. Three things follow from that choice:

* what the browser runs is what the command line runs, so every result matches the tested path;
* a crash, a hung data vendor or a Stop click can end the child without touching this server;
* progress is read from the files the run already writes while it works (one report per step), so the analysis code
  does not know the browser exists.

One analysis runs at a time. Several symbols in one request wait in a queue, which keeps the load on the AI provider and
the data vendors the same as a person typing the commands one after another.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any

from tradingagents.agents.conviction import borderline_note, read_score
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.agents.style import STYLES
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.doctor import scrub
from tradingagents.live_events import FILE_NAME, read_events

# --------------------------------------------------------------------------- what the form offers

# The key is the analyst's name inside the program ("social" is the sentiment analyst).
ANALYSTS = (
    ("market", "Market analyst", "Reads the price history and technical indicators such as moving averages and RSI."),
    ("social", "Sentiment analyst", "Reads what investors and the public are saying about the company."),
    ("news", "News analyst", "Reads recent company and world news that could move the stock."),
    ("fundamentals", "Fundamentals analyst", "Reads financial statements, valuation, insider trades and analyst opinion."),
)
ANALYST_KEYS = tuple(key for key, _, _ in ANALYSTS)
REPORT_FILE = {"market": "market_report.md", "social": "sentiment_report.md", "news": "news_report.md",
               "fundamentals": "fundamentals_report.md"}

# Research depth is the number of debate rounds (the same number sets the risk-team rounds).
DEPTHS = (
    (1, "Shallow", "One round of debate. The quickest and cheapest; every measurement in this project used it."),
    (3, "Medium", "Three rounds of debate. Longer arguments, so more time and cost (not measured here)."),
    (5, "Deep", "Five rounds of debate. The longest and most expensive (not measured here)."),
)
DEPTH_VALUES = tuple(value for value, _, _ in DEPTHS)
LANGUAGES = ("English", "Chinese", "Japanese", "Korean", "Hindi", "Spanish", "Portuguese", "French", "German",
             "Arabic", "Russian")
# A language name: one to four words of letters (any script), joined by single spaces or hyphens, 40 characters at most.
LANGUAGE = re.compile(r"[^\W\d_]+(?:[ \-][^\W\d_]+){0,3}")
MAX_TICKERS = 10
MAX_WAITING = 30
PASSES_RANGE = (1, 15)
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")     # the same list the command line uses
STATUS_ACTIVE = ("waiting", "running")
JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")

# Measured with Anthropic's small and mid models at depth 1 (about 0.13 dollars and 5 minutes for one pass;
# 5 passes cost 0.35 to 0.49 dollars and took 5.5 to 7.7 minutes).
COST_BASE, COST_STEP = 0.13, 0.0675
MINUTES_BASE, MINUTES_STEP = 4.5, 0.45


class RequestError(ValueError):
    """The request cannot run; the message says why in plain words."""


def estimate(provider: str, quick: str, deep: str, passes: int) -> dict:
    """What one analysis costs and how long it takes, when that has been measured for these models.

    Only Anthropic's small plus mid model pair was measured, at the shallowest depth. For anything else the
    price is unknown, and the answer says so rather than guessing.
    """
    minutes = int(MINUTES_BASE + MINUTES_STEP * (passes - 1) + 0.5)          # half rounds up (round() would give 4 for 4.5)
    measured = provider == "anthropic" and quick.startswith("claude-haiku-5") and deep.startswith("claude-sonnet-5")
    return {"passes": passes, "minutes": minutes, "measured": measured,
            "usd": round(COST_BASE + COST_STEP * (passes - 1), 2) if measured else None}


def is_crypto(symbol: str) -> bool:
    return symbol.endswith(CRYPTO_SUFFIXES)


def find_holdings(folders: list[Path]) -> dict[str, dict]:
    """Holdings files the form may offer: JSON files in the given folders that read as a book.

    A file counts only if it is a JSON object with at least one of ``positions``, ``cash`` or ``profile`` and the
    program's own reader accepts it; any other JSON file lying around is not offered. The form sends a file's name,
    never a path, and the name is looked up here.
    """
    from tradingagents.portfolio import load_portfolio

    found: dict[str, dict] = {}
    for folder in folders:
        try:
            files = sorted(Path(folder).glob("*.json"))[:60]
        except OSError:
            continue
        for path in files:
            if path.name in found:
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not ({"positions", "cash", "profile"} & raw.keys()):
                    continue
                book = load_portfolio(path)
            except (OSError, ValueError):
                continue
            found[path.name] = {"path": path, "folder": str(Path(folder)), "positions": len(book.positions),
                                "cash": book.cash, "currency": book.currency,
                                "profile": bool(book.profile and book.profile.strip())}
    return found


# --------------------------------------------------------------------------- checking a request

def clean_request(raw: Mapping[str, Any], *, today: _date, holdings: Mapping[str, Path]) -> dict:
    """The request as the run needs it, or RequestError naming the first thing that is wrong.

    Everything here ends up in a command line, an environment variable or a folder name, so each value is checked
    against a short list or a strict pattern. Nothing the page sends is used as a path or a command.
    """
    tickers = []
    names = raw.get("tickers")
    if not isinstance(names, list) or not names:
        raise RequestError("Type at least one stock symbol, for example NVDA.")
    for name in names:
        text = str(name).strip()
        if not text:
            continue
        try:
            symbol = normalize_symbol(text)
            safe_ticker_component(symbol)
        except ValueError:
            raise RequestError(f"'{text[:20]}' is not a symbol this program can use. Examples: NVDA, 0700.HK, BTC-USD.") from None
        if symbol not in tickers:
            tickers.append(symbol)
    if not tickers:
        raise RequestError("Type at least one stock symbol, for example NVDA.")
    if len(tickers) > MAX_TICKERS:
        raise RequestError(f"Up to {MAX_TICKERS} symbols at a time, please. You gave {len(tickers)}.")

    try:
        day = datetime.strptime(str(raw.get("date", "")).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise RequestError("Pick a date (year-month-day).") from None
    if day > today:
        raise RequestError("The date is in the future. Pick today or an earlier day.")

    wanted = raw.get("analysts")
    if not isinstance(wanted, list):
        raise RequestError("Choose at least one analyst.")
    unknown = [str(a) for a in wanted if str(a) not in ANALYST_KEYS]
    if unknown:
        raise RequestError("One of the analysts is not recognized. Reload the page and try again.")
    analysts = [key for key in ANALYST_KEYS if key in wanted]
    if not analysts:
        raise RequestError("Choose at least one analyst.")

    depth, passes = raw.get("depth"), raw.get("passes")
    if isinstance(depth, bool) or depth not in DEPTH_VALUES:
        raise RequestError("Choose Shallow, Medium or Deep research.")
    low, high = PASSES_RANGE
    if isinstance(passes, bool) or not isinstance(passes, int) or not low <= passes <= high:
        raise RequestError(f"Research passes must be a whole number from {low} to {high}.")

    style = str(raw.get("style") or "standard").strip().lower()
    if style not in STYLES:
        raise RequestError("Choose Standard or Growth as the investing style.")

    language = str(raw.get("language") or "English").strip()
    if language not in LANGUAGES and not (len(language) <= 40 and LANGUAGE.fullmatch(language)):
        raise RequestError("The report language must be a language name such as English or Turkish.")

    book = raw.get("holdings")
    holdings_name = None
    if book not in (None, ""):
        if str(book) not in holdings:
            raise RequestError("That holdings file is not one of the files offered. Reload the page.")
        holdings_name = str(book)

    return {"tickers": tickers, "date": day.isoformat(), "analysts": analysts, "depth": depth, "passes": passes,
            "language": language, "holdings": holdings_name, "style": style,
            "selloff": bool(raw.get("selloff", True)), "scorecard": bool(raw.get("scorecard", True)),
            "open_report": bool(raw.get("open_report", True))}


# --------------------------------------------------------------------------- one run's command and environment

def build_command(job: Job, python: str, portfolio: Path | None) -> list[str]:
    """The command line for one analysis, answering every question the command line would otherwise ask."""
    code = "import sys; from cli.main import app; sys.argv[0] = 'tradingagents'; app()"
    command = [python, "-c", code, "--ticker", job.ticker, "--date", job.date, "--analysts", ",".join(job.analysts),
               "--save", "--no-show", "--html", "--no-rerun"]
    if portfolio is not None:
        command += ["--portfolio", str(portfolio)]
    return command


def build_env(job: Job, base: Mapping[str, str]) -> dict[str, str]:
    """The environment for one analysis: the user's own, plus the choices made on the form."""
    env = dict(base)
    env.update({
        "TRADINGAGENTS_OUTPUT_LANGUAGE": job.language,
        "TRADINGAGENTS_MAX_DEBATE_ROUNDS": str(job.depth),
        "TRADINGAGENTS_MAX_RISK_ROUNDS": str(job.depth),
        "TRADINGAGENTS_RESEARCH_PASSES": str(job.passes),
        "TRADINGAGENTS_INVESTING_STYLE": job.style,
        "TRADINGAGENTS_LLM_PROVIDER": job.provider,
        "TRADINGAGENTS_QUICK_THINK_LLM": job.quick,
        "TRADINGAGENTS_DEEP_THINK_LLM": job.deep,
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
    })
    for name, on in (("TRADINGAGENTS_NO_SELLOFF_RULES", job.selloff), ("TRADINGAGENTS_NO_SCORECARD", job.scorecard)):
        if on:
            env.pop(name, None)
        else:
            env[name] = "1"
    return env


def launch(command: list[str], env: dict[str, str], cwd: str | None, console: Path):
    """Start the child with no terminal (so nothing waits for a keypress), its output going to a file."""
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        flags["start_new_session"] = True
    # stdin is a pipe closed at once, not the null device: on Windows the null device reports itself as a terminal,
    # and the command line would then believe someone is there to answer questions. A closed pipe is plainly not
    # one, so a question that was missed is named in the log instead of waiting for an answer that never comes.
    with open(console, "ab") as out:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,      # noqa: S603
                                env=env, cwd=cwd, **flags)
    if proc.stdin is not None:
        proc.stdin.close()
    return proc


def open_in_browser(page: Path) -> bool:
    """Show a saved report in the user's own browser (this program runs on their computer, so it can)."""
    import webbrowser

    return webbrowser.open(Path(page).resolve().as_uri())


def folder_command(path: str, platform: str) -> list[str]:
    """The command that shows a folder in the file manager on macOS and Linux (Windows uses ``os.startfile``)."""
    return ["open", path] if platform == "darwin" else ["xdg-open", path]


def open_folder(folder: Path) -> bool:
    """Show a folder in the computer's own file manager (Explorer, Finder or the desktop's). The folder is one the
    program chose, never text from the page, and it is passed as one argument with no shell. False when nothing could be
    started; a file manager that opens and then fails cannot be detected from here."""
    path = str(Path(folder).resolve())
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.Popen(folder_command(path, sys.platform), stdin=subprocess.DEVNULL,      # noqa: S603
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def kill_tree(proc) -> None:
    """End the child and anything it started."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20, check=False)  # noqa: S603,S607
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001 - fall back to ending just the child
        with contextlib.suppress(Exception):
            proc.kill()


# --------------------------------------------------------------------------- reading a run's files

def _fresh(path: Path, since: float) -> bool:
    """Whether a file was written by this run, not left over from an earlier one the same day."""
    try:
        return path.stat().st_mtime >= since - 2
    except OSError:
        return False


def _text(path: Path, since: float) -> str:
    if not _fresh(path, since):
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last_line(path: Path, since: float, now: float) -> dict | None:
    """The newest line of the run's activity log, and how long ago the log last changed."""
    if not _fresh(path, since):
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            tail = handle.read().decode("utf-8", errors="replace")
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        age = max(0, int(now - path.stat().st_mtime))
    except OSError:
        return None
    return {"text": scrub(lines[-1], os.environ, 160), "seconds_ago": age} if lines else None


def read_progress(run_dir: Path, job: Job, now: float, finished: bool = False) -> dict:
    """Which steps are done, which is under way, and what the run last did, from the files it has written."""
    since = job.started or 0.0
    reports = Path(run_dir) / "reports"
    filed = [a for a in job.analysts if _fresh(reports / REPORT_FILE[a], since)]
    done = [
        len(filed) == len(job.analysts),
        "Research Manager Decision" in _text(reports / "investment_plan.md", since),
        _fresh(reports / "trader_investment_plan.md", since),
        "Portfolio Manager Decision" in _text(reports / "final_trade_decision.md", since),
        finished,
    ]
    for i in range(len(done) - 2, -1, -1):          # a later step finished means the earlier ones did
        done[i] = done[i] or done[i + 1]
    labels = ("Analysts gather data and file their reports",
              f"Research panel: {job.passes} bull-and-bear debate{'s' if job.passes != 1 else ''}, then a vote",
              "Trader drafts a plan",
              "Risk team debates and the portfolio manager decides",
              "Saving the report and the totals")
    notes = (f"{len(filed)} of {len(job.analysts)} reports filed", "", "", "", "")
    current = next((i for i, flag in enumerate(done) if not flag), None)
    stages = []
    for i, (label, flag) in enumerate(zip(labels, done, strict=False)):
        state = "done" if flag else ("now" if i == current else "waiting")
        stages.append({"label": label, "state": state, "note": notes[i] if state != "waiting" else ""})
    return {"stages": stages, "step": len(done) if current is None else current + 1, "steps": len(done),
            "latest": _last_line(Path(run_dir) / "message_tool.log", since, now)}


_SUMMARY = re.compile(r"\*\*Executive Summary\*\*:\s*(.+?)(?:\n\s*\n|\Z)", re.DOTALL)
_FIELD = re.compile(r"^\*\*([^*\n]+?)\*\*:[ \t]*", re.MULTILINE)


def decision_fields(text: str) -> dict[str, str]:
    """The labeled parts of a rendered decision (``**Price Target**: ...``): {label: text up to the next label}."""
    found = list(_FIELD.finditer(text or ""))
    return {m.group(1).strip(): text[m.end(): found[i + 1].start() if i + 1 < len(found) else len(text)].strip()
            for i, m in enumerate(found)}


def read_result(results_dir: Path, ticker: str, day: str, since: float) -> dict | None:
    """What a finished analysis decided, or None when it left no decision of its own behind."""
    folder = Path(results_dir) / safe_ticker_component(ticker)
    state_path = folder / "TradingAgentsStrategy_logs" / f"full_states_log_{day}.json"
    if not _fresh(state_path, since):
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    decision = str(state.get("final_trade_decision") or "")
    rating = run_rating(state)
    review = is_review(rating)
    note = None if review else borderline_note(rating, decision)
    summary = _SUMMARY.search(decision)
    panel = state.get("research_panel")
    if isinstance(panel, dict):
        panel = {k: panel.get(k) for k in ("passes", "requested", "ratings", "median", "agreement", "label", "range", "summary")}
    else:
        panel = None
    usage = {}
    usage_path = folder / day / "usage.json"
    if _fresh(usage_path, since):
        try:
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            usage = {}
    fallbacks = 0
    for row in (state.get("structured_fallbacks") or {}).values():
        if isinstance(row, dict):
            fallbacks += int(row.get("fallbacks") or 0)
    fields = decision_fields(decision)
    return {
        "rating": rating, "review": review, "score": read_score(decision),
        "borderline": None if note is None else bool(note[1]), "borderline_text": "" if note is None else note[0],
        "summary": summary.group(1).strip() if summary else "", "panel": panel,
        "price_target": fields.get("Price Target", ""), "time_horizon": fields.get("Time Horizon", ""),
        "thesis": fields.get("Investment Thesis", ""),
        "cost_usd": usage.get("estimated_cost_usd"), "seconds": usage.get("seconds"), "llm_calls": usage.get("llm_calls"),
        "fallbacks": fallbacks, "report_dir": _newest_report(Path(results_dir), ticker, since),
    }


# ---- the live view: who said what, and each agent's report as it appears

# (id, title) for every part of the team a report file can hold; the analysts come first, in the program's order.
TEAM_TITLES = {
    "market": "Market analyst", "social": "Sentiment analyst", "news": "News analyst", "fundamentals": "Fundamentals analyst",
    "bull": "Bull researcher", "bear": "Bear researcher", "manager": "Research manager", "trader": "Trader",
    "aggressive": "Aggressive risk analyst", "conservative": "Conservative risk analyst", "neutral": "Neutral risk analyst",
    "pm": "Portfolio manager",
}
# The heading a debate role's text sits under inside investment_plan.md and final_trade_decision.md.
DEBATE_HEADINGS = {
    "Bull Researcher Analysis": "bull", "Bear Researcher Analysis": "bear", "Research Manager Decision": "manager",
    "Aggressive Analyst Analysis": "aggressive", "Conservative Analyst Analysis": "conservative",
    "Neutral Analyst Analysis": "neutral", "Portfolio Manager Decision": "pm",
}
# The groups the page shows, in the order the work happens, with the step of read_progress that covers each.
LIVE_GROUPS = (
    ("analysts", "Analysts", ("market", "social", "news", "fundamentals"), 0),
    ("research", "Research panel", ("bull", "bear", "manager"), 1),
    ("trading", "Trader", ("trader",), 2),
    ("risk", "Risk team and portfolio manager", ("aggressive", "conservative", "neutral", "pm"), 3),
)
_HEADING = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def _split_headings(text: str) -> dict[str, str]:
    """A report file's text cut at its ``###`` headings: {heading: the text under it}."""
    found = list(_HEADING.finditer(text))
    return {m.group(1): text[m.end(): found[i + 1].start() if i + 1 < len(found) else len(text)].strip()
            for i, m in enumerate(found)}


def read_sections(reports: Path, since: float) -> dict[str, str]:
    """The reports filed so far by this run, by team-member id. Files from an earlier run are ignored."""
    out: dict[str, str] = {}
    for key, name in REPORT_FILE.items():
        text = _text(reports / name, since).strip()
        if text:
            out[key] = text
    trader = _text(reports / "trader_investment_plan.md", since).strip()
    if trader:
        out["trader"] = trader
    for name in ("investment_plan.md", "final_trade_decision.md"):
        for heading, body in _split_headings(_text(reports / name, since)).items():
            if heading in DEBATE_HEADINGS and body:
                out[DEBATE_HEADINGS[heading]] = body
    return out


# Where the saved report folder keeps each team member's text. When a run ends, its working files are overwritten
# with the final text (the debate headings are gone), so a finished run's views come from here instead.
SAVED_FILES = {
    "market": "1_analysts/market.md", "social": "1_analysts/sentiment.md", "news": "1_analysts/news.md",
    "fundamentals": "1_analysts/fundamentals.md", "bull": "2_research/bull.md", "bear": "2_research/bear.md",
    "manager": "2_research/manager.md", "trader": "3_trading/trader.md", "aggressive": "4_risk/aggressive.md",
    "conservative": "4_risk/conservative.md", "neutral": "4_risk/neutral.md", "pm": "5_portfolio/decision.md",
}


def read_saved_sections(folder: Path) -> dict[str, str]:
    """Each team member's text from a finished run's saved report folder (members with no file are left out)."""
    out: dict[str, str] = {}
    for key, relative in SAVED_FILES.items():
        try:
            text = (Path(folder) / relative).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            out[key] = text
    return out


def team_status(job: Job, sections: Mapping[str, str], stages: list[dict] | None) -> list[dict]:
    """Every team member this run involves, with a state: waiting, working, done or missing (the run ended without it)."""
    over = job.status not in STATUS_ACTIVE
    out = []
    for group, group_title, members, step in LIVE_GROUPS:
        for member in members:
            if group == "analysts" and member not in job.analysts:
                continue
            if member in sections:
                state = "done"
            elif over:
                state = "missing"
            elif stages and stages[step]["state"] == "now":
                state = "working"
            else:
                state = "waiting"
            out.append({"id": member, "title": TEAM_TITLES[member], "group": group, "group_title": group_title, "state": state})
    return out


def _rev(text: str) -> str:
    return f"{zlib.crc32(text.encode('utf-8', errors='replace')):x}-{len(text)}"


def read_live(run_dir: Path, job: Job, now: float, after: int = 0, have: Mapping[str, str] | None = None,
              saved: Path | None = None) -> dict:
    """Everything the live view needs for one analysis, new since the caller's last look.

    ``after`` is the event-log offset the caller got last time; ``have`` maps a section id to the revision the caller
    already holds, and a section it already has comes back without its text. ``saved`` is the finished run's report
    folder, whose per-member files take over from the working files once the run has ended.
    """
    since = job.started or 0.0
    log = Path(run_dir) / FILE_NAME
    events, offset = ([], 0) if not _fresh(log, since) else read_events(log, after)
    for event in events:            # nothing that looks like a key reaches the page, whatever a data vendor echoed
        event["text"] = "\n".join(scrub(line, os.environ, 20000) for line in str(event.get("text", "")).split("\n"))
    sections = read_sections(Path(run_dir) / "reports", since)
    if saved is not None:
        sections.update(read_saved_sections(saved))
    progress = read_progress(run_dir, job, now, finished=job.status == "done") if job.started else None
    have = have or {}
    return {
        "status": job.status, "events": events, "next": offset, "passes": job.passes,
        "agents": team_status(job, sections, progress["stages"] if progress else None),
        "sections": [{"id": key, "title": TEAM_TITLES[key], "rev": _rev(text),
                      "text": None if have.get(key) == _rev(text) else text}
                     for key, text in sections.items()],
    }


def _newest_report(results_dir: Path, ticker: str, since: float) -> str | None:
    base = results_dir / "reports"
    try:
        found = [p for p in base.iterdir() if p.is_dir() and p.name.startswith(safe_ticker_component(ticker) + "_")
                 and _fresh(p, since)]
    except OSError:
        return None
    return str(max(found, key=lambda p: p.stat().st_mtime)) if found else None


def console_tail(path: Path, lines: int = 6) -> str:
    """The last few lines the run printed, with anything that looks like a key hidden."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 6000))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    kept = [line.strip() for line in text.splitlines() if line.strip()][-lines:]
    return "\n".join(scrub(line, os.environ, 300) for line in kept)


# --------------------------------------------------------------------------- jobs and the queue

@dataclass
class Job:
    id: str
    ticker: str
    date: str
    analysts: list[str]
    depth: int
    passes: int
    language: str
    holdings: str | None
    selloff: bool
    scorecard: bool
    provider: str
    quick: str
    deep: str
    open_report: bool = True                # open the HTML report in the browser when this analysis finishes
    style: str = "standard"                 # the investing-style lens the analysts used (standard or growth)
    notes: list[str] = field(default_factory=list)
    status: str = "waiting"                 # waiting, running, done, failed, stopped
    created: float = 0.0
    started: float | None = None
    finished: float | None = None
    exit_code: int | None = None
    error: str = ""
    detail: str = ""
    result: dict | None = None
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data.pop("cancel_requested")
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Job:
        names = set(cls.__dataclass_fields__) - {"cancel_requested"}
        return cls(**{k: v for k, v in data.items() if k in names})


class RunManager:
    """Keeps the queue of analyses, runs them one at a time and answers what each is doing.

    ``launcher`` starts a child and returns an object with ``poll()``, ``returncode`` and ``pid`` (a test passes a
    fake); ``base_env`` returns the environment to build on, read fresh for every run so a key saved a minute ago
    is used.
    """

    def __init__(self, results_dir: str | Path, *, holdings: Callable[[], Mapping[str, Path]] | None = None,
                 launcher: Callable[..., Any] = launch, python: str | None = None, cwd: str | None = None,
                 base_env: Callable[[], Mapping[str, str]] | None = None, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, poll_seconds: float = 0.5, keep: int = 30,
                 killer: Callable[[Any], None] = kill_tree, opener: Callable[[Path], Any] = open_in_browser,
                 folder_opener: Callable[[Path], Any] = open_folder, start_worker: bool = True) -> None:
        self.results_dir = Path(results_dir)
        self.root = self.results_dir / "ui-runs"
        self._opener, self._folder_opener = opener, folder_opener
        self._holdings = holdings or (lambda: {})
        self._launcher, self._python, self._cwd = launcher, python or sys.executable, cwd
        self._base_env = base_env or (lambda: dict(os.environ))
        self._now, self._sleep, self._poll, self._keep, self._kill = now, sleep, poll_seconds, keep, killer
        self._jobs: list[Job] = []
        self._proc: Any = None
        self._current: Job | None = None
        self._stop = False
        self._cv = threading.Condition(threading.RLock())
        self._load()
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._work, name="analysis-queue", daemon=True)
            self._thread.start()

    # ---- persistence
    def _dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _save(self, job: Job) -> None:
        folder = self._dir(job.id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "job.json.tmp"
        tmp.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, folder / "job.json")

    def _load(self) -> None:
        """Earlier analyses, newest last. One that was running when the server stopped is marked as stopped."""
        try:
            folders = sorted(p for p in self.root.iterdir() if p.is_dir() and JOB_ID.match(p.name))[-self._keep:]
        except OSError:
            return
        for folder in folders:
            try:
                job = Job.from_dict(json.loads((folder / "job.json").read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if job.status in STATUS_ACTIVE:
                job.status, job.error = "stopped", "The program was closed before this analysis finished."
                job.finished = job.finished or self._now()
                self._save(job)
            self._jobs.append(job)

    # ---- the queue
    def submit(self, request: Mapping[str, Any], provider: str, quick: str, deep: str) -> list[Job]:
        """Queue one analysis per symbol of a request that ``clean_request`` has already checked."""
        with self._cv:
            waiting = sum(1 for j in self._jobs if j.status in STATUS_ACTIVE)
            if waiting + len(request["tickers"]) > MAX_WAITING:
                raise RequestError("Too many analyses are waiting. Let some finish or stop some first.")
            for symbol in request["tickers"]:
                if any(j.ticker == symbol and j.date == request["date"] and j.status in STATUS_ACTIVE for j in self._jobs):
                    raise RequestError(f"{symbol} for {request['date']} is already waiting or running.")
            made: list[Job] = []
            taken = {j.id for j in self._jobs}
            stamp = datetime.fromtimestamp(self._now()).strftime("%Y%m%d-%H%M%S")
            for symbol in request["tickers"]:
                notes = []
                analysts = list(request["analysts"])
                if is_crypto(symbol) and "fundamentals" in analysts:
                    analysts.remove("fundamentals")
                    notes.append("The fundamentals analyst was left out: it does not cover cryptocurrencies.")
                if not analysts:
                    raise RequestError(f"{symbol} is a cryptocurrency, and the only analyst chosen does not cover it.")
                job_id = f"{stamp}-{secrets.token_hex(2)}"
                while job_id in taken:
                    job_id = f"{stamp}-{secrets.token_hex(2)}"
                taken.add(job_id)
                made.append(Job(id=job_id, ticker=symbol, date=request["date"], analysts=analysts,
                                depth=request["depth"], passes=request["passes"], language=request["language"],
                                holdings=request["holdings"], selloff=request["selloff"], scorecard=request["scorecard"],
                                provider=provider, quick=quick, deep=deep, notes=notes, created=self._now(),
                                open_report=bool(request.get("open_report", True)),
                                style=str(request.get("style", "standard"))))
            for job in made:                # nothing is queued unless every symbol in the request can be
                self._jobs.append(job)
                self._save(job)
            self._cv.notify_all()
            return made

    def cancel(self, job_id: str) -> bool:
        """Stop a waiting analysis (it never starts) or a running one (its process is ended)."""
        proc = None
        with self._cv:
            job = self._find(job_id)
            if job is None or job.status not in STATUS_ACTIVE:
                return False
            job.cancel_requested = True
            if job.status == "waiting":
                job.status, job.finished, job.error = "stopped", self._now(), "Stopped before it started."
                self._save(job)
            elif self._proc is not None and self._current is job:
                proc = self._proc
            self._cv.notify_all()
        if proc is not None:
            self._kill(proc)            # outside the lock: ending a process tree can take a moment
        return True

    def _delete_record(self, job_id: str) -> bool:
        """Delete one analysis's record under ``ui-runs`` (its job file and console log). Saved reports, run logs and the
        decision memory are elsewhere and are never touched. True when the record is gone (or was never there)."""
        folder = self._dir(job_id)
        if not JOB_ID.fullmatch(job_id) or folder.is_symlink():
            return False
        if not folder.exists():
            return True
        try:
            shutil.rmtree(folder)
        except OSError:
            return False
        return True

    def forget(self, job_id: str) -> bool:
        """Take one finished, failed or stopped analysis off the list and delete its record. False for an unknown id, one
        that is waiting or running (stop it first), or one whose record could not be deleted (it stays listed)."""
        with self._cv:
            job = self._find(job_id)
            if job is None or job.status in STATUS_ACTIVE or not self._delete_record(job_id):
                return False
            self._jobs.remove(job)
            return True

    def forget_finished(self) -> int:
        """Clear every finished, failed and stopped analysis from the list, and delete the records of earlier ones that
        are no longer shown (without that, they would come back at the next start). Waiting and running analyses stay.
        Returns how many listed analyses were removed; one whose record cannot be deleted stays listed."""
        with self._cv:
            listed = {j.id for j in self._jobs}
            removed = 0
            for job in [j for j in self._jobs if j.status not in STATUS_ACTIVE]:
                if self._delete_record(job.id):
                    self._jobs.remove(job)
                    removed += 1
            try:
                folders = [p.name for p in self.root.iterdir() if p.is_dir() and JOB_ID.fullmatch(p.name)]
            except OSError:
                folders = []
            for name in folders:
                if name not in listed:                  # an earlier record beyond the ones kept on the list
                    self._delete_record(name)
            return removed

    def shutdown(self) -> None:
        """End the running analysis and the queue (the server is closing)."""
        with self._cv:
            self._stop = True
            if self._proc is not None:
                self._kill(self._proc)
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _find(self, job_id: str) -> Job | None:
        return next((j for j in self._jobs if j.id == job_id), None)

    def busy(self) -> bool:
        with self._cv:
            return any(j.status in STATUS_ACTIVE for j in self._jobs)

    # ---- what the page sees
    def listing(self) -> list[dict]:
        """Every kept analysis, newest first, with live progress for the one that is running."""
        with self._cv:
            jobs = list(self._jobs)
            order = [j for j in jobs if j.status == "waiting"]
        now = self._now()
        out = []
        for job in reversed(jobs):
            out.append(self._public(job, now, order.index(job) + 1 if job in order else None))
        return out

    def get(self, job_id: str) -> dict | None:
        with self._cv:
            job = self._find(job_id)
        return None if job is None else self._public(job, self._now(), None)

    def live(self, job_id: str, after: int = 0, have: Mapping[str, str] | None = None) -> dict | None:
        """The live view of one analysis (new events, each agent's state and report); None for an unknown id."""
        with self._cv:
            job = self._find(job_id)
        if job is None:
            return None
        run_dir = self.results_dir / safe_ticker_component(job.ticker) / job.date
        return read_live(run_dir, job, self._now(), after, have, saved=self._saved_folder(job))

    def _saved_folder(self, job: Job) -> Path | None:
        """A finished analysis's saved report folder, only if it is still inside the results folder."""
        folder = (job.result or {}).get("report_dir") if job.status == "done" else None
        if not folder:
            return None
        try:
            path = Path(folder).resolve()
            return path if path.is_relative_to(self.results_dir.resolve()) and path.is_dir() else None
        except (OSError, ValueError):
            return None

    def report_file(self, job_id: str) -> Path | None:
        """The finished analysis's HTML report, if it has one and it is still inside the results folder."""
        with self._cv:
            job = self._find(job_id)
        folder = self._saved_folder(job) if job else None
        page = None if folder is None else folder / "complete_report.html"
        return page if page is not None and page.is_file() else None

    def show_report(self, job_id: str, automatic: bool = False) -> bool:
        """Open the analysis's HTML report in the user's browser. ``automatic`` obeys the job's own open-it-for-me choice."""
        with self._cv:
            job = self._find(job_id)
        if job is None or (automatic and not job.open_report):
            return False
        page = self.report_file(job_id)
        if page is None:
            return False
        try:
            return bool(self._opener(page))
        except Exception:  # noqa: BLE001 - a browser that will not start must not disturb the queue
            return False

    def reports_folder(self) -> dict:
        """Where every saved report lives, and whether that folder exists yet (it is made by the first report)."""
        folder = self.results_dir / "reports"
        try:
            shown = folder.resolve()
        except OSError:
            shown = folder
        return {"path": str(shown), "exists": folder.is_dir()}

    def show_reports_folder(self) -> dict:
        """Open the reports folder in the file manager. ``opened`` is False when there is no folder yet or nothing could
        be started; the path comes back either way so the page can tell the person where it is."""
        where = self.reports_folder()
        opened = False
        if where["exists"]:
            try:
                opened = bool(self._folder_opener(self.results_dir / "reports"))
            except Exception:  # noqa: BLE001 - a file manager that will not start must not disturb the page
                opened = False
        return {**where, "opened": opened}

    def finished(self, ids: list[str] | None = None) -> list[Job]:
        """Done analyses, oldest first, optionally only those with the given ids (unknown ids are ignored)."""
        with self._cv:
            return [j for j in self._jobs if j.status == "done" and (ids is None or j.id in ids)]

    def _public(self, job: Job, now: float, place: int | None) -> dict:
        data = job.to_dict()
        data["place"] = place
        data["log"] = str(self._dir(job.id) / "console.txt") if job.started else None
        end = job.finished or now
        data["elapsed"] = None if job.started is None else max(0, int(end - job.started))
        data["expected_minutes"] = estimate(job.provider, job.quick, job.deep, job.passes)["minutes"]
        data["progress"] = None
        if job.status == "running":
            data["progress"] = read_progress(self.results_dir / safe_ticker_component(job.ticker) / job.date, job, now)
        return data

    # ---- the worker
    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._stop and not any(j.status == "waiting" for j in self._jobs):
                    self._cv.wait(timeout=1.0)
                if self._stop:
                    return
                job = next(j for j in self._jobs if j.status == "waiting")
                job.status, job.started = "running", self._now()
                self._current = job
                self._save(job)
            try:
                self._run(job)
            except Exception as exc:  # noqa: BLE001 - one bad launch must not end the queue
                self._finish(job, "failed", error=f"Could not start the analysis: {type(exc).__name__}.")
            finally:
                with self._cv:
                    self._proc, self._current = None, None

    def _run(self, job: Job) -> None:
        books = self._holdings()
        portfolio = books.get(job.holdings) if job.holdings else None
        if job.holdings and portfolio is None:
            self._finish(job, "failed", error=f"The holdings file {job.holdings} is no longer there.")
            return
        folder = self._dir(job.id)
        folder.mkdir(parents=True, exist_ok=True)
        console = folder / "console.txt"
        proc = self._launcher(build_command(job, self._python, portfolio), build_env(job, self._base_env()), self._cwd, console)
        with self._cv:
            self._proc = proc
            if job.cancel_requested:
                self._kill(proc)
        while proc.poll() is None:
            self._sleep(self._poll)
            if self._stop:
                self._kill(proc)
        code = proc.returncode
        job.exit_code = code
        if job.cancel_requested or self._stop:
            self._finish(job, "stopped", error="Stopped by you." if job.cancel_requested else "The program was closed.")
            return
        result = read_result(self.results_dir, job.ticker, job.date, job.started or 0.0)
        if code == 0 and result is not None:
            job.result = result
            self._finish(job, "done")
            self.show_report(job.id, automatic=True)
        else:
            reason = ("The analysis ended without writing a decision." if code == 0
                      else f"The analysis stopped with an error (exit code {code}).")
            job.detail = console_tail(console)
            self._finish(job, "failed", error=reason)

    def _finish(self, job: Job, status: str, error: str = "") -> None:
        with self._cv:
            job.status, job.finished = status, self._now()
            if error:
                job.error = error
            self._save(job)
            self._cv.notify_all()
