"""Saving a finished analysis as a spreadsheet file (CSV) or a PDF (a local addition).

* CSV: one row per analysis, the same columns every time, so rows from many analyses can sit in one sheet. Text from
  the AI models goes into cells, and a spreadsheet treats a cell that starts with ``=``, ``+``, ``-`` or ``@`` as a
  formula, so such text is prefixed with an apostrophe (the usual defence against CSV injection). The file is UTF-8
  with a byte-order mark so Excel reads accented and non-Latin text correctly.
* PDF: the saved HTML report printed by Chrome, Edge or Chromium running in the background. That needs no extra
  package and keeps the report's own layout; if none of them is installed the error says what to do instead.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from tradingagents.csv_safe import safe_cell  # noqa: F401  (also re-exported: the analysis export has always offered it here)

CSV_COLUMNS = (
    "ticker", "analysis_date", "finished_at", "rating", "conviction_score", "borderline", "borderline_note",
    "panel_passes", "panel_ratings", "panel_median", "panel_agreement", "panel_label", "price_target", "time_horizon",
    "executive_summary", "investment_thesis", "research_depth", "investing_style", "analysts", "language", "ai_provider", "quick_model",
    "deep_model", "estimated_cost_usd", "seconds", "ai_calls", "holdings_file", "report_folder",
)
_NUMERIC = {"conviction_score", "panel_passes", "panel_agreement", "research_depth", "estimated_cost_usd", "seconds", "ai_calls"}


class ExportError(Exception):
    """The file could not be made; the message says why and what to do, in plain words."""


def row_for(job) -> dict:
    """One analysis as a CSV row (a dict keyed by ``CSV_COLUMNS``). ``job`` is a ``runs.Job``."""
    result = job.result or {}
    panel = result.get("panel") or {}
    finished = datetime.fromtimestamp(job.finished).astimezone().isoformat(timespec="seconds") if job.finished else ""
    row = {
        "ticker": job.ticker, "analysis_date": job.date, "finished_at": finished,
        "rating": "" if result.get("review") else result.get("rating", ""),
        "conviction_score": result.get("score"),
        "borderline": "" if result.get("borderline") is None else ("yes" if result.get("borderline") else "no"),
        "borderline_note": result.get("borderline_text", ""),
        "panel_passes": panel.get("passes"), "panel_ratings": "; ".join(panel.get("ratings") or []),
        "panel_median": panel.get("median", ""), "panel_agreement": panel.get("agreement"),
        "panel_label": panel.get("label", ""),
        "price_target": result.get("price_target", ""), "time_horizon": result.get("time_horizon", ""),
        "executive_summary": result.get("summary", ""), "investment_thesis": result.get("thesis", ""),
        "research_depth": job.depth, "investing_style": job.style, "analysts": ", ".join(job.analysts), "language": job.language,
        "ai_provider": job.provider, "quick_model": job.quick, "deep_model": job.deep,
        "estimated_cost_usd": result.get("cost_usd"), "seconds": result.get("seconds"), "ai_calls": result.get("llm_calls"),
        "holdings_file": job.holdings or "", "report_folder": result.get("report_dir") or "",
    }
    return row


def build_csv(jobs: Sequence) -> bytes:
    """The CSV file for these analyses: a header row, then one row each."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for job in jobs:
        row = row_for(job)
        writer.writerow([("" if row[c] is None else row[c]) if c in _NUMERIC else safe_cell(row[c]) for c in CSV_COLUMNS])
    return ("﻿" + out.getvalue()).encode("utf-8")


# --------------------------------------------------------------------------- PDF

def browser_candidates(platform: str = sys.platform, env: dict | None = None) -> list[str]:
    """Where a Chromium-family browser usually lives on this system, best first. TRADINGAGENTS_PDF_BROWSER wins."""
    env = os.environ if env is None else env
    chosen = [env["TRADINGAGENTS_PDF_BROWSER"]] if env.get("TRADINGAGENTS_PDF_BROWSER") else []
    if platform.startswith("win"):
        roots = [env.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA") if env.get(k)]
        names = (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe")
        return chosen + [str(Path(root) / name) for name in names for root in roots]
    if platform == "darwin":
        apps = ("Microsoft Edge", "Google Chrome", "Chromium")
        return chosen + [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in apps]
    return chosen + ["microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def find_browser(candidates: Sequence[str] | None = None, exists: Callable[[str], bool] = os.path.isfile,
                 which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The first installed Chromium-family browser, as a path to run, or None."""
    for item in candidates if candidates is not None else browser_candidates():
        if os.path.isabs(item) or "/" in item or "\\" in item:      # a path to a file; anything else is a command name
            if exists(item):
                return item
        else:
            found = which(item)
            if found:
                return found
    return None


def html_to_pdf(page: Path, browser: str | None = None, timeout: float = 90,
                run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bytes:
    """The PDF of a saved HTML report, printed in the background by a browser; raises ExportError with advice."""
    browser = browser or find_browser()
    if browser is None:
        raise ExportError("Making a PDF needs Chrome, Edge or Chromium on this computer, and none was found. "
                          "Open the HTML report instead and use your browser's Print command, then choose Save as PDF.")
    page = Path(page)
    if not page.is_file():
        raise ExportError("This analysis has no saved HTML report to turn into a PDF.")
    flags: dict = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    with tempfile.TemporaryDirectory(prefix="consortium-pdf-") as tmp:
        target = Path(tmp) / "report.pdf"
        command = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--disable-extensions", "--no-pdf-header-footer", f"--user-data-dir={Path(tmp) / 'profile'}",
                   f"--print-to-pdf={target}", page.resolve().as_uri()]
        try:
            run(command, capture_output=True, timeout=timeout, check=False, **flags)         # noqa: S603
        except subprocess.TimeoutExpired:
            raise ExportError("The browser took too long to make the PDF. Try again, or print the HTML report to PDF yourself.") from None
        except OSError:
            raise ExportError("The browser could not be started to make the PDF. Print the HTML report to PDF yourself.") from None
        try:
            data = target.read_bytes()
        except OSError:
            data = b""
    if not data.startswith(b"%PDF"):
        raise ExportError("The browser did not produce a PDF. Print the HTML report to PDF yourself.")
    return data
