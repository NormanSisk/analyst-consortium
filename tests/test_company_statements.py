"""Tests for the company-statements vendor (a local addition): the earnings call, the
earnings release and the management's discussion, with the network stubbed out."""

import json

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import company_statements as cs
from tradingagents.dataflows.vendors.alpha_vantage.common import AlphaVantageRateLimitError

CIK = "0000012345"
TODAY = cs.get_current_date()


def _page(title: str, body_chars: int = 9_000) -> str:
    return f"<p>{title}</p>" + "".join(f"<p>{'Revenue grew because of cloud demand. ' * 3}Line {i}.</p>" for i in range(body_chars // 60))


FILING = (
    "<html><head><title>x</title><script>var hidden = 1;</script></head><body>"
    "<ix:header><div>HIDDEN XBRL CONTEXT 2026</div></ix:header>"
    "<p>Table of Contents</p>"
    "<table><tr><td>Item 2.</td><td>Management's Discussion and Analysis</td><td>12</td></tr>"
    "<tr><td>Item 3.</td><td>Quantitative and Qualitative Disclosures</td><td>30</td></tr></table>"
    "<p>See Item 2. Management's Discussion and Analysis of the results below.</p>"
    "<p>PART I</p><p>Item 2. Management's Discussion and Analysis of Financial Condition</p>"
    + _page("Overview of the quarter.") +
    "<p>Item 3. Quantitative and Qualitative Disclosures About Market Risk</p><p>Not material.</p>"
    "<p>Item 4. Controls. See Item 2. Management's Discussion and Analysis for context.</p></body></html>"
)


@pytest.mark.unit
class TestPlainText:
    def test_scripts_hidden_xbrl_and_page_furniture_are_dropped_and_cells_are_joined(self):
        text = cs.html_to_text(FILING)
        assert "hidden = 1" not in text and "HIDDEN XBRL CONTEXT" not in text
        assert "Table of Contents" not in text
        assert "Item 2. | Management's Discussion and Analysis | 12" in text

    def test_an_exhibit_label_line_is_dropped(self):
        text = cs.html_to_text("<p>EX-99.1</p><p>googexhibit991q32026.htm</p><p>Alphabet Announces Results</p>")
        assert text == "Alphabet Announces Results"


@pytest.mark.unit
class TestManagementDiscussion:
    def test_the_real_section_is_taken_not_the_contents_line_or_a_cross_reference(self):
        section = cs.extract_mdna(cs.html_to_text(FILING), "10-Q")
        assert section.startswith("Overview of the quarter.")
        assert "Revenue grew because of cloud demand." in section
        assert "Quantitative and Qualitative" not in section and "Not material" not in section
        assert len(section) > cs._MIN_MDNA_CHARS

    def test_a_filing_without_the_section_gives_none(self):
        assert cs.extract_mdna("Item 2. Management's Discussion\nshort\nItem 3. Quantitative", "10-Q") is None
        assert cs.extract_mdna(cs.html_to_text(FILING), "8-K") is None

    def test_a_10k_uses_item_7(self):
        text = ("Item 7. Management's Discussion\n" + "Growth came from ads.\n" * 600
                + "Item 7A. Quantitative and Qualitative\nrates\n")
        assert cs.extract_mdna(text, "10-K").startswith("Growth came from ads.")

    def test_trimming_cuts_at_a_line_break_and_says_so(self):
        text = "\n".join(f"line {i:04d}" for i in range(100))
        kept, note = cs._trim(text, 205)
        assert kept.endswith("line 0019")
        assert note == "[Shortened: the first 199 of 999 characters are shown.]"
        assert cs._trim("short", 100) == ("short", "")


@pytest.mark.unit
class TestFiscalLabels:
    def _facts(self, rows):
        return {"facts": {"us-gaap": {"Assets": {"units": {"USD": rows}}}}}

    def test_the_newest_filed_period_names_the_quarter_and_a_10k_is_the_fourth(self, monkeypatch):
        rows = [
            {"end": "2025-12-31", "filed": "2026-02-05", "form": "10-K", "fy": 2025, "fp": "FY", "val": 1},
            {"end": "2026-03-31", "filed": "2026-04-30", "form": "10-Q", "fy": 2026, "fp": "Q1", "val": 1},
            {"end": "2025-12-31", "filed": "2026-04-30", "form": "10-Q", "fy": 2026, "fp": "Q1", "val": 1},  # comparative
            {"end": "2026-06-30", "filed": "2026-07-30", "form": "10-Q", "fy": 2026, "fp": "Q2", "val": 1},
        ]
        monkeypatch.setattr(cs.sec_edgar, "_cached_json", lambda *a, **k: self._facts(rows))
        assert cs._latest_period(CIK, "2026-11-03") == {"fy": 2026, "quarter": "Q2", "end": "2026-06-30", "filed": "2026-07-30"}
        assert cs._latest_period(CIK, "2026-03-01")["quarter"] == "Q4"           # only the 10-K was on file then
        assert cs._latest_period(CIK, "2025-01-01") is None

    def test_the_quarter_after_q4_is_q1_of_the_next_year(self):
        assert cs._next_quarter(2026, "Q4") == (2027, "Q1")
        assert cs._next_quarter(2026, "Q2") == (2026, "Q3")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    monkeypatch.setattr(cs, "_last_av_call", 0.0)
    return tmp_path


SEGMENTS = [
    {"speaker": "Operator", "title": "Operator", "content": "Welcome to the call.", "sentiment": "0.0"},
    {"speaker": "Anat Ashkenazi", "title": "CFO", "content": "We expect  capex to rise.", "sentiment": "0.9"},
]


@pytest.mark.unit
class TestTranscript:
    def _answers(self, monkeypatch, *replies):
        calls = []

        def fake(function, params):
            calls.append((function, params["quarter"]))
            reply = replies[min(len(calls), len(replies)) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(cs, "_make_api_request", fake)
        return calls

    def test_a_transcript_is_fetched_once_cached_and_formatted_without_the_sentiment_score(self, cache, monkeypatch):
        calls = self._answers(monkeypatch, json.dumps({"symbol": "X", "quarter": "2026Q3", "transcript": SEGMENTS}))
        first = cs._alpha_vantage_transcript("X", "2026Q3")
        again = cs._alpha_vantage_transcript("X", "2026Q3")
        assert first == again == SEGMENTS and len(calls) == 1
        text = cs._format_call(first)
        assert "Anat Ashkenazi (CFO): We expect capex to rise." in text and "Operator: Welcome" in text
        assert "0.9" not in text

    def test_an_empty_result_is_remembered_for_a_while_not_forever(self, cache, monkeypatch):
        calls = self._answers(monkeypatch, json.dumps({"symbol": "X", "quarter": "2027Q1", "transcript": "[]"}))
        assert cs._alpha_vantage_transcript("X", "2027Q1") is None
        assert cs._alpha_vantage_transcript("X", "2027Q1") is None
        assert len(calls) == 1
        stale = cache / "company_statements" / "transcript-X-2027Q1.empty"
        old = stale.stat().st_mtime - cs._EMPTY_RETRY_SECONDS - 60
        import os
        os.utime(stale, (old, old))
        assert cs._alpha_vantage_transcript("X", "2027Q1") is None and len(calls) == 2

    def test_the_per_second_limit_is_retried_once_but_a_daily_cap_is_not(self, cache, monkeypatch):
        calls = self._answers(
            monkeypatch,
            AlphaVantageRateLimitError("Alpha Vantage rate limit exceeded: use 1 request per second"),
            json.dumps({"transcript": SEGMENTS}),
        )
        assert cs._alpha_vantage_transcript("X", "2026Q3") == SEGMENTS and len(calls) == 2

        calls = self._answers(monkeypatch, AlphaVantageRateLimitError("Alpha Vantage rate limit exceeded: 25 requests per day"))
        with pytest.raises(AlphaVantageRateLimitError):
            cs._alpha_vantage_transcript("X", "2026Q4")
        assert len(calls) == 1

    def test_a_run_dated_in_the_past_is_not_served_a_transcript(self):
        out = cs._transcript_section("X", CIK, "2020-01-02")
        assert "Withheld" in out

    def _section(self, monkeypatch, report_date, filed):
        monkeypatch.setattr(cs, "_latest_period",
                            lambda cik, as_of: {"fy": 2026, "quarter": "Q3", "end": "2026-09-30", "filed": filed})
        monkeypatch.setattr(cs, "_report_dates", lambda canonical: [pd.Timestamp(report_date)])

    def test_a_call_held_before_the_10q_is_filed_is_found_under_the_next_quarter(self, cache, monkeypatch):
        self._section(monkeypatch, "2026-10-28", "2026-07-30")        # the 10-Q for the new quarter is not out yet
        calls = self._answers(monkeypatch, json.dumps({"transcript": SEGMENTS}))
        out = cs._transcript_section("X", CIK, TODAY)
        assert calls == [("EARNINGS_CALL_TRANSCRIPT", "2026Q4")]
        assert "fiscal 2026 Q4, held 2026-10-28" in out and "quarter ended" not in out

    def test_when_the_filing_is_out_the_filed_quarter_is_the_one_asked_for(self, cache, monkeypatch):
        self._section(monkeypatch, "2026-10-28", "2026-10-29")
        calls = self._answers(monkeypatch, json.dumps({"transcript": SEGMENTS}))
        out = cs._transcript_section("X", CIK, TODAY)
        assert calls == [("EARNINGS_CALL_TRANSCRIPT", "2026Q3")]
        assert "fiscal 2026 Q3, held 2026-10-28, quarter ended 2026-09-30" in out

    def test_a_missing_call_says_so_and_does_not_fall_back_to_an_older_quarter(self, cache, monkeypatch):
        self._section(monkeypatch, "2026-10-28", "2026-10-29")
        calls = self._answers(monkeypatch, json.dumps({"transcript": "[]"}))
        out = cs._transcript_section("X", CIK, TODAY)
        assert len(calls) == 1 and "No transcript is available for fiscal 2026Q3" in out


@pytest.mark.unit
class TestReleaseAndEntryPoint:
    FILINGS = [
        {"form": "10-Q", "filingDate": "2026-10-29", "accessionNumber": "0000012345-26-000010",
         "primaryDocument": "x-10q.htm", "reportDate": "2026-09-30", "items": ""},
        {"form": "8-K", "filingDate": "2026-10-28", "accessionNumber": "0000012345-26-000009",
         "primaryDocument": "x-8k.htm", "reportDate": "2026-10-28", "items": "2.02,9.01"},
        {"form": "8-K", "filingDate": "2026-10-20", "accessionNumber": "0000012345-26-000008",
         "primaryDocument": "x-other.htm", "reportDate": "2026-10-20", "items": "5.02"},
    ]

    def _sec(self, monkeypatch):
        monkeypatch.setattr(cs, "_filings", lambda cik, as_of: [f for f in self.FILINGS if f["filingDate"] <= as_of])
        listing = {"directory": {"item": [{"name": "x-8k.htm"}, {"name": "xexhibit991q3.htm"}, {"name": "R1.htm"}]}}
        monkeypatch.setattr(cs.sec_edgar, "_fetch_json", lambda url: listing)
        pages = {
            "xexhibit991q3.htm": "<p>Acme Reports Record Third Quarter</p><p>We raised full-year guidance.</p>",
            "x-10q.htm": FILING,
        }
        fetched = []

        def fetch(url):
            fetched.append(url.rsplit("/", 1)[1])
            return pages[url.rsplit("/", 1)[1]]

        monkeypatch.setattr(cs, "_fetch_document", fetch)
        return fetched

    def test_the_release_is_the_exhibit_of_the_item_2_02_filing(self, cache, monkeypatch):
        fetched = self._sec(monkeypatch)
        out = cs._release_section(CIK, "2026-11-03")
        assert fetched == ["xexhibit991q3.htm"]
        assert "8-K filed 2026-10-28" in out and "We raised full-year guidance." in out

    def test_a_cached_release_is_not_fetched_again(self, cache, monkeypatch):
        fetched = self._sec(monkeypatch)
        cs._release_section(CIK, "2026-11-03")
        cs._release_section(CIK, "2026-11-03")
        assert len(fetched) == 1

    def test_a_release_older_than_the_window_is_not_served(self, cache, monkeypatch):
        self._sec(monkeypatch)
        assert "No earnings release" in cs._release_section(CIK, "2027-03-01")

    def test_the_discussion_comes_from_the_latest_10q_with_its_period(self, cache, monkeypatch):
        self._sec(monkeypatch)
        out = cs._mdna_section(CIK, "2026-11-03")
        assert "10-Q filed 2026-10-29, period ended 2026-09-30" in out and "Revenue grew because of cloud demand." in out

    def test_the_entry_point_serves_every_section_and_labels_them_as_claims(self, cache, monkeypatch):
        self._sec(monkeypatch)
        monkeypatch.setattr(cs.sec_edgar, "cik_for", lambda t: CIK)
        monkeypatch.setattr(cs, "_transcript_section", lambda c, k, a: "## Earnings call transcript\n\ncall text")
        out = cs.get_company_statements("X", TODAY)
        assert out.index("call text") < out.index("Acme Reports") < out.index("Revenue grew")
        assert "company's claims" in out and "never something to act on" in out

    def test_one_failing_section_does_not_take_the_others_down_but_all_failing_raises(self, cache, monkeypatch):
        self._sec(monkeypatch)
        monkeypatch.setattr(cs.sec_edgar, "cik_for", lambda t: CIK)

        def broken(*a):
            raise VendorUnavailableError("down")

        monkeypatch.setattr(cs, "_transcript_section", broken)
        out = cs.get_company_statements("X", TODAY)
        assert "Not retrieved (VendorUnavailableError: down)" in out and "Acme Reports" in out

        monkeypatch.setattr(cs, "_release_section", broken)
        monkeypatch.setattr(cs, "_mdna_section", broken)
        with pytest.raises(VendorUnavailableError):
            cs.get_company_statements("X", TODAY)

    def test_a_company_with_no_filings_gets_a_plain_note(self, monkeypatch):
        monkeypatch.setattr(cs.sec_edgar, "cik_for", lambda t: None)
        assert "No SEC filings are on record for 'WQTM'" in cs.get_company_statements("WQTM", TODAY)

    def test_the_fundamentals_analyst_is_offered_the_tool(self):
        from tradingagents.agents.analysts import fundamentals_analyst

        assert "get_company_statements" in [t.name for t in fundamentals_analyst.TOOLS]
