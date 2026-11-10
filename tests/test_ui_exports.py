"""Saving an analysis as CSV or PDF: the columns, the spreadsheet-formula defence, and the PDF maker (with a fake browser,
and once with a real one when this computer has Chrome, Edge or Chromium)."""

import csv
import io
import subprocess
from pathlib import Path

import pytest

from tradingagents.ui import exports as ex, runs as rn


def done_job(**changes):
    job = rn.Job(id="20261108-101010-abcd", ticker="KO", date="2026-11-08", analysts=["market", "news"], depth=1, passes=5,
                 language="English", holdings=None, selloff=True, scorecard=True, provider="anthropic",
                 quick="claude-haiku-5-5", deep="claude-sonnet-5", status="done", finished=1_794_000_000.0)
    job.result = {
        "rating": "Hold", "review": False, "score": 48, "borderline": False, "borderline_text": "Not borderline: far from a line.",
        "summary": "Hold existing exposure.", "thesis": "Because of the cash flow.", "price_target": "not provided",
        "time_horizon": "2-6 weeks",
        "panel": {"passes": 5, "ratings": ["Hold", "Hold", "Underweight", "Hold", "Hold"], "median": "Hold", "agreement": 0.8, "label": "strong"},
        "cost_usd": 0.38, "seconds": 351, "llm_calls": 29, "report_dir": "C:/results/reports/KO_1", "fallbacks": 0}
    for key, value in changes.items():
        setattr(job, key, value)
    return job


def parse(data: bytes):
    text = data.decode("utf-8")
    assert text.startswith("\ufeff")
    return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline="")))


@pytest.mark.unit
class TestCsv:
    def test_one_row_per_analysis_under_a_fixed_header(self):
        data = ex.build_csv([done_job(), done_job(ticker="MU", id="20261108-101011-abce")])
        assert data.startswith("\ufeff".encode("utf-8")) and b"\r\n" in data
        rows = parse(data)
        assert [r["ticker"] for r in rows] == ["KO", "MU"]
        assert list(rows[0]) == list(ex.CSV_COLUMNS)

    def test_the_cells_carry_the_result(self):
        row = parse(ex.build_csv([done_job()]))[0]
        assert (row["rating"], row["conviction_score"], row["borderline"]) == ("Hold", "48", "no")
        assert row["panel_ratings"] == "Hold; Hold; Underweight; Hold; Hold" and row["panel_median"] == "Hold"
        assert row["panel_agreement"] == "0.8" and row["panel_label"] == "strong" and row["panel_passes"] == "5"
        assert row["executive_summary"] == "Hold existing exposure." and row["investment_thesis"] == "Because of the cash flow."
        assert row["price_target"] == "not provided" and row["time_horizon"] == "2-6 weeks"
        assert (row["estimated_cost_usd"], row["seconds"], row["ai_calls"]) == ("0.38", "351", "29")
        assert (row["analysts"], row["research_depth"], row["ai_provider"]) == ("market, news", "1", "anthropic")
        assert row["investing_style"] == "standard"
        assert parse(ex.build_csv([done_job(style="growth")]))[0]["investing_style"] == "growth"
        assert row["quick_model"] == "claude-haiku-5-5" and row["deep_model"] == "claude-sonnet-5"
        assert row["report_folder"] == "C:/results/reports/KO_1" and row["holdings_file"] == ""
        assert row["finished_at"].startswith("2026-") and "T" in row["finished_at"]

    def test_a_borderline_call_says_yes(self):
        job = done_job()
        job.result["borderline"] = True
        assert parse(ex.build_csv([job]))[0]["borderline"] == "yes"

    def test_a_run_with_no_readable_rating_leaves_the_rating_blank(self):
        job = done_job()
        job.result.update(review=True, rating="REVIEW", borderline=None, score=None, borderline_text="")
        row = parse(ex.build_csv([job]))[0]
        assert row["rating"] == "" and row["borderline"] == "" and row["conviction_score"] == ""

    def test_missing_parts_are_blank_cells_not_the_word_none(self):
        job = done_job()
        job.result = {"rating": "Buy"}
        job.finished = None
        row = parse(ex.build_csv([job]))[0]
        assert row["rating"] == "Buy" and "None" not in row.values() and row["panel_ratings"] == "" and row["finished_at"] == ""
        assert parse(ex.build_csv([done_job(result=None)]))[0]["rating"] == ""

    @pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
    def test_text_a_spreadsheet_would_run_as_a_formula_is_defused(self, start):
        job = done_job()
        job.result["summary"] = start + 'HYPERLINK("http://evil","x")'
        job.result["thesis"] = start + "1+1"
        row = parse(ex.build_csv([job]))[0]
        assert row["executive_summary"].startswith("'" + start) and row["investment_thesis"].startswith("'" + start)

    def test_ordinary_text_and_numbers_are_left_alone(self):
        job = done_job()
        job.result["summary"] = "Hold - and watch the 10-day EMA"
        job.result["cost_usd"] = 0.38
        row = parse(ex.build_csv([job]))[0]
        assert row["executive_summary"] == "Hold - and watch the 10-day EMA" and row["estimated_cost_usd"] == "0.38"

    def test_commas_quotes_and_line_breaks_stay_inside_their_cell(self):
        job = done_job()
        job.result["summary"] = 'He said "hold", then\nwaited.'
        rows = parse(ex.build_csv([done_job(), job]))
        assert rows[1]["executive_summary"] == 'He said "hold", then\nwaited.' and len(rows) == 2

    def test_non_latin_text_survives(self):
        job = done_job()
        job.result["summary"] = "株価は上昇 €"
        assert parse(ex.build_csv([job]))[0]["executive_summary"] == "株価は上昇 €"

    def test_no_analyses_give_just_the_header(self):
        assert parse(ex.build_csv([])) == []
        assert ex.build_csv([]).decode("utf-8").lstrip("\ufeff").split("\r\n")[0].split(",")[0] == "ticker"

    def test_safe_cell(self):
        assert ex.safe_cell(None) == "" and ex.safe_cell(5) == "5" and ex.safe_cell("=SUM(A1)") == "'=SUM(A1)"


@pytest.mark.unit
class TestFindingABrowser:
    def test_the_users_own_choice_comes_first(self):
        assert ex.browser_candidates("win32", {"TRADINGAGENTS_PDF_BROWSER": "C:/mine/browser.exe"})[0] == "C:/mine/browser.exe"

    def test_windows_looks_for_edge_then_chrome_in_the_usual_places(self):
        found = ex.browser_candidates("win32", {"PROGRAMFILES(X86)": "C:/PF86", "PROGRAMFILES": "C:/PF", "LOCALAPPDATA": "C:/LA"})
        assert found[0].replace("\\", "/") == "C:/PF86/Microsoft/Edge/Application/msedge.exe"
        assert any(p.replace("\\", "/").endswith("Google/Chrome/Application/chrome.exe") for p in found)
        assert len(found) == 9

    def test_mac_and_linux_candidates(self):
        assert ex.browser_candidates("darwin", {})[1] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        assert "chromium" in ex.browser_candidates("linux", {}) and "google-chrome" in ex.browser_candidates("linux", {})

    def test_the_first_one_that_exists_is_used(self):
        got = ex.find_browser(["/a/none", "/b/chrome", "/c/other"], exists=lambda p: p != "/a/none", which=lambda n: None)
        assert got == "/b/chrome"

    def test_a_bare_name_is_looked_up_on_the_path(self):
        assert ex.find_browser(["nope", "chromium"], exists=lambda p: False, which=lambda n: "/usr/bin/chromium" if n == "chromium" else None) == "/usr/bin/chromium"

    def test_none_when_nothing_is_installed(self):
        assert ex.find_browser(["/a", "b"], exists=lambda p: False, which=lambda n: None) is None


class FakeBrowser:
    """Stands in for the browser: writes (or does not write) the PDF named in its command line."""

    def __init__(self, content=b"%PDF-1.4 fake", raises=None):
        self.content, self.raises, self.commands = content, raises, []

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if self.raises:
            raise self.raises
        target = next(a.split("=", 1)[1] for a in command if a.startswith("--print-to-pdf="))
        if self.content is not None:
            Path(target).write_bytes(self.content)
        return subprocess.CompletedProcess(command, 0, b"", b"")


@pytest.mark.unit
class TestPdf:
    @pytest.fixture
    def page(self, tmp_path):
        page = tmp_path / "complete_report.html"
        page.write_text("<html><body><h1>Report</h1></body></html>", encoding="utf-8")
        return page

    def test_the_browser_prints_the_saved_page_in_the_background(self, page):
        fake = FakeBrowser()
        assert ex.html_to_pdf(page, "C:/edge.exe", run=fake) == b"%PDF-1.4 fake"
        command, kwargs = fake.commands[0]
        assert command[0] == "C:/edge.exe" and "--headless=new" in command and "--no-pdf-header-footer" in command
        assert command[-1] == page.resolve().as_uri() and any(a.startswith("--user-data-dir=") for a in command)
        assert kwargs["timeout"] == 90 and kwargs["capture_output"] is True and "shell" not in kwargs

    def test_the_temporary_folder_is_removed_afterwards(self, page):
        fake = FakeBrowser()
        ex.html_to_pdf(page, "b", run=fake)
        target = next(a.split("=", 1)[1] for a in fake.commands[0][0] if a.startswith("--print-to-pdf="))
        assert not Path(target).exists() and not Path(target).parent.exists()

    def test_no_browser_says_what_to_do_instead(self, page, monkeypatch):
        monkeypatch.setattr(ex, "find_browser", lambda *a, **k: None)
        with pytest.raises(ex.ExportError, match="needs Chrome, Edge or Chromium.*Print"):
            ex.html_to_pdf(page, None, run=FakeBrowser())

    def test_a_missing_page_is_reported(self, tmp_path):
        with pytest.raises(ex.ExportError, match="no saved HTML report"):
            ex.html_to_pdf(tmp_path / "gone.html", "b", run=FakeBrowser())

    def test_a_browser_that_takes_too_long(self, page):
        with pytest.raises(ex.ExportError, match="too long"):
            ex.html_to_pdf(page, "b", run=FakeBrowser(raises=subprocess.TimeoutExpired("b", 90)))

    def test_a_browser_that_cannot_start(self, page):
        with pytest.raises(ex.ExportError, match="could not be started"):
            ex.html_to_pdf(page, "b", run=FakeBrowser(raises=OSError("no such file")))

    @pytest.mark.parametrize("content", [None, b"", b"<html>not a pdf</html>"])
    def test_no_pdf_or_not_a_pdf_is_an_error_not_a_bad_file(self, page, content):
        with pytest.raises(ex.ExportError, match="did not produce a PDF"):
            ex.html_to_pdf(page, "b", run=FakeBrowser(content=content))

    def test_a_real_browser_makes_a_real_pdf(self, tmp_path):
        """Only where this computer has Chrome, Edge or Chromium: proves the flags work with a real one."""
        browser = ex.find_browser()
        if browser is None:
            pytest.skip("no Chrome, Edge or Chromium here")
        page = tmp_path / "complete_report.html"
        page.write_text("<html><head><meta charset='utf-8'></head><body><h1>Analyst Consortium</h1><p>Hold. 株価</p></body></html>",
                        encoding="utf-8")
        data = ex.html_to_pdf(page, browser, timeout=120)
        assert data.startswith(b"%PDF") and len(data) > 1000
