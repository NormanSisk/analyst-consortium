"""Running the stock picker from the page (a local addition).

A search takes from a few seconds (your own list) to about a minute (a screen of the market), so it runs in a background
thread and the page polls it, reading its progress messages as it goes. One search runs at a time. Nothing here spends on AI:
the picker's quick look is free. The last few searches are kept in memory so their results can be shown and saved as CSV;
each list shown can also be recorded in the picker's ledger (``picks.csv``), which is the future scoreboard.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from tradingagents.picker import custom as custom_tools
from tradingagents.picker import ledger, lists, quicklook, report
from tradingagents.picker import scoreboard as board_tools
from tradingagents.picker.cache import FundamentalsCache
from tradingagents.picker.conditions import TITLES
from tradingagents.picker.providers import Provider, YahooProvider
from tradingagents.picker.settings import PickerSettings

SEARCH_ID = re.compile(r"^p\d{8}-\d{6}-[0-9a-f]{4}$")
MAX_TEXT = 1_000_000                            # characters of a typed or loaded list (a file may be up to 1 MB)

# The settings the page may change. ``scale`` turns the stored number into the one shown (a fraction into a percent, dollars
# into billions); the page converts back before sending.
_SCREEN = ["beaten-down"]       # these settings shape the screen of the market, so they mean nothing for a list you name
ADJUSTABLE = (
    {"name": "below_high", "label": "At least this far below the 52-week high", "unit": "percent", "scale": 100, "min": 0, "max": 95,
     "integer": False, "for": _SCREEN, "help": "20 means at least 20% below the high."},
    {"name": "min_market_cap", "label": "Smallest company", "unit": "billion of market value", "scale": 1e-9, "min": 0, "max": 10000,
     "integer": False, "for": _SCREEN, "help": "Smaller companies are left out. Lower it to look at smaller ones; they are riskier and thinner to trade."},
    {"name": "min_avg_volume", "label": "Smallest trading volume", "unit": "thousand shares a day", "scale": 1e-3, "min": 0, "max": 1e7,
     "integer": False, "for": _SCREEN, "help": "Thinly traded stocks are left out."},
    {"name": "universe_limit", "label": "How many of the largest stocks to screen", "unit": "stocks", "scale": 1, "min": 10, "max": 3000,
     "integer": True, "for": _SCREEN, "help": "More stocks take longer."},
    {"name": "top", "label": "How many to show", "unit": "stocks", "scale": 1, "min": 1, "max": 500,
     "integer": True, "for": _SCREEN, "help": "The full list can still be saved as a CSV file."},
)
RECIPES = (
    {"id": "beaten-down", "label": "Beaten-down quality",
     "help": "Screens the largest US stocks for those well below their highs where the market explains the fall, then checks them against the "
             "analysis's five deal conditions. Takes about a minute."},
    {"id": "list", "label": "Your own list",
     "help": "Type or load symbols and get the same checks on each one. A handful takes about half a minute; "
             "stocks already looked at today are quicker."},
    {"id": "custom", "label": "Custom search",
     "help": "Choose your own filters: size, price, valuation, growth, profits, debt, who owns it and more. Every filter is optional; "
             "a blank one means no filter. Takes about a minute."},
)


class SearchBusy(RuntimeError):
    """A search is already running."""


@dataclass
class SearchJob:
    id: str
    recipe: str
    settings: PickerSettings
    symbols: list[str] | None
    skipped: list[str]
    record: bool
    query: custom_tools.CustomQuery | None = None       # the filters of a custom search
    state: str = "running"                      # running, done or failed
    started: float = 0.0
    finished: float | None = None
    messages: list[str] = field(default_factory=list)
    error: str = ""
    result: dict | None = None
    pick: object | None = None                  # the PickResult, kept for the CSV
    recorded: int = 0


class PickerManager:
    """Starts searches, one at a time, and answers what each is doing and found."""

    def __init__(self, folder: str | Path | None = None, provider: Provider | Callable[[], Provider] | None = None,
                 now: Callable[[], float] = time.time, keep: int = 5, workers: int = 6, run=quicklook.run) -> None:
        self.folder = None if folder is None else Path(folder)
        self._provider = provider
        self._now, self._keep, self._workers, self._run_recipe = now, keep, workers, run
        self._jobs: list[SearchJob] = []
        self._lock = threading.Lock()
        self._board_lock = threading.Lock()
        self._board = None                           # the last scoreboard, kept for its CSV

    # ---- what the page offers
    def options(self) -> dict:
        defaults = PickerSettings()
        settings = [{**item, "default": getattr(defaults, item["name"])} for item in ADJUSTABLE]
        with self._lock:
            latest = self._jobs[-1].id if self._jobs else None
        return {"recipes": list(RECIPES), "settings": settings, "busy": self.busy(), "latest": latest,
                "ledger_path": str(self.folder / "picks.csv") if self.folder else None, "max_text": MAX_TEXT, "log": self.log_summary(),
                "custom": custom_tools.spec_for_page()}

    def count(self, custom_request: dict | None) -> dict:
        """How many stocks Yahoo says match the filters so far (one cheap call; the code-side filters are not counted)."""
        query = custom_tools.from_request(custom_request)
        total = self._provider_now().count(custom_tools.yahoo_terms(query), list(query.sectors))
        return {"matches": total}

    # ---- the picks log and how its stocks did
    def log_summary(self) -> dict:
        """What the picks log holds (read from the file; no download)."""
        path = self.folder / "picks.csv" if self.folder else None
        picks, _ = board_tools.parse_rows(ledger.read_picks(path) if path else [])
        days = sorted({p.shown for p in picks})
        return {"path": str(path) if path else None, "stocks": len(picks), "lists": len({(p.recipe, p.shown) for p in picks}),
                "first": days[0].isoformat() if days else None, "last": days[-1].isoformat() if days else None}

    def scoreboard(self) -> dict:
        """Measure every recorded stock against the S&P 500 (one download of prices). SearchBusy when one is already running."""
        if not self._board_lock.acquire(blocking=False):
            raise SearchBusy("The scoreboard is already being worked out. Wait a moment.")
        try:
            path = self.folder / "picks.csv" if self.folder else None
            board = board_tools.build(ledger.read_picks(path) if path else [], self._provider_now())
            self._board = board
            return board_tools.to_json(board)
        finally:
            self._board_lock.release()

    def scoreboard_csv(self) -> bytes | None:
        board = self._board
        return None if board is None else board_tools.to_csv(board)

    def busy(self) -> bool:
        with self._lock:
            return any(j.state == "running" for j in self._jobs)

    # ---- starting
    def start(self, recipe: str, text: str = "", overrides: dict | None = None, record: bool = True,
              custom_request: dict | None = None) -> dict:
        """Check the request and start the search; ValueError (in plain words) when it cannot run, SearchBusy when one is running."""
        if recipe not in quicklook.RECIPES:
            raise ValueError("Choose Beaten-down quality, Your own list or Custom search.")
        overrides = dict(overrides or {})
        symbols: list[str] | None = None
        skipped: list[str] = []
        query = None
        if recipe == "custom":
            query = custom_tools.from_request(custom_request)
            overrides = {"top": query.show}              # a custom search's own limits; the screening settings do not apply
        elif recipe == "list":
            if len(text) > MAX_TEXT:
                raise ValueError("That is too much text for a list of stocks (the limit is about 1 MB). Keep it to a few hundred symbols.")
            symbols, skipped = lists.parse_symbols(text)
            if not symbols:
                raise ValueError("Type at least one stock symbol, for example NVDA MU TSM.")
            if len(symbols) > quicklook.MAX_LIST:
                raise ValueError(f"A list may hold at most {quicklook.MAX_LIST} stocks; this one has {len(symbols)}.")
            overrides["top"] = len(symbols)          # every stock you named is shown and recorded
        settings = PickerSettings.with_overrides(**overrides)
        with self._lock:
            if any(j.state == "running" for j in self._jobs):
                raise SearchBusy("A search is already running. Wait for it to finish.")
            stamp = datetime.fromtimestamp(self._now()).strftime("%Y%m%d-%H%M%S")
            job = SearchJob(id=f"p{stamp}-{secrets.token_hex(2)}", recipe=recipe, settings=settings, symbols=symbols, skipped=skipped,
                            record=bool(record), query=query, started=self._now())
            self._jobs.append(job)
            del self._jobs[:-self._keep]
        if skipped:
            job.messages.append(f"Skipped (not usable as symbols): {', '.join(skipped[:10])}{' ...' if len(skipped) > 10 else ''}")
        threading.Thread(target=self._work, args=(job,), name="picker-search", daemon=True).start()
        return self.get(job.id)

    def _provider_now(self) -> Provider:
        """The data source for one search: the one given (tests), a function that makes one, or Yahoo."""
        given = self._provider
        if given is None:
            return YahooProvider()
        return given if hasattr(given, "closes") else given()

    def _work(self, job: SearchJob) -> None:
        try:
            cache = FundamentalsCache(self.folder / "cache" if self.folder else None, date.today())
            extra = {"query": job.query} if job.query is not None else {}
            result = self._run_recipe(job.recipe, self._provider_now(), job.settings, symbols=job.symbols, cache=cache,
                                      workers=self._workers, log=job.messages.append, **extra)
            job.pick = result
            job.result = {**report.to_json(result), "top": job.settings.top, "condition_titles": dict(TITLES)}
            if job.record and result.rows and self.folder is not None:
                job.recorded = ledger.append_picks(self.folder / "picks.csv", result, job.settings.top)
            job.state = "done"
        except (quicklook.PickerError, ValueError) as exc:
            job.state, job.error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - the page gets a plain sentence; the details are not shown
            job.state, job.error = "failed", f"The search stopped unexpectedly ({type(exc).__name__}). Try again in a minute."
        finally:
            job.finished = self._now()

    # ---- what the page sees
    def _find(self, job_id: str) -> SearchJob | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def get(self, job_id: str) -> dict | None:
        job = self._find(job_id)
        if job is None:
            return None
        end = job.finished or self._now()
        return {"id": job.id, "recipe": job.recipe, "state": job.state, "messages": list(job.messages), "error": job.error,
                "elapsed": max(0, int(end - job.started)), "skipped": job.skipped, "recorded": job.recorded,
                "ledger_path": str(self.folder / "picks.csv") if self.folder and job.recorded else None,
                "result": job.result if job.state == "done" else None}

    def csv(self, job_id: str) -> bytes | None:
        job = self._find(job_id)
        return None if job is None or job.pick is None else report.to_csv(job.pick)
