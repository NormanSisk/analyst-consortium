"""Tests for the SEC filing-events vendor (a local addition): items, offerings, late notices, holders, dilution."""

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import sec_filing_events as fe

AS_OF = "2026-11-06"


def _f(form, filed, items="", report=""):
    return {"form": form, "filingDate": filed, "reportDate": report, "items": items, "accessionNumber": "0"}


FILINGS = [
    _f("8-K", "2026-09-03", "3.01,9.01"), _f("8-K", "2026-08-05", "2.02,9.01"), _f("8-K", "2026-07-01", "1.01,3.02"),
    _f("8-K", "2026-06-02", "5.02"), _f("8-K", "2025-12-01", "4.01"),                      # the last is outside the window
    _f("424B5", "2026-09-10"), _f("424B5", "2026-09-01"), _f("S-3", "2026-08-20"), _f("S-8", "2026-08-21"),
    _f("NT 10-Q", "2026-08-14"), _f("10-Q/A", "2026-09-20", report="2026-06-30"),
    _f("10-Q", "2026-08-11", report="2026-06-30"), _f("10-K", "2026-03-10", report="2025-12-31"),
]
HOLDERS = [{"form": "SCHEDULE 13D", "filed": "2026-10-01", "filer": "Activist Capital LP"},
           {"form": "SCHEDULE 13G/A", "filed": "2026-07-27", "filer": "Theon International Plc."}]
SHARES = [("2025-08-01", 100_000_000, "2025-08-05"), ("2025-11-12", 105_000_000, "2025-11-14"),
          ("2026-04-13", 112_000_000, "2026-04-15"), ("2026-08-11", 125_000_000, "2026-08-12")]


def _report(filings=FILINGS, holders=HOLDERS, shares=SHARES, as_of=AS_OF):
    return fe.build_report("TEST", filings, holders, shares, as_of)


def _flags(text):
    return [line[2:] for line in text.split("## Flags to state in the report\n")[1].splitlines() if line.startswith("- ")]


@pytest.mark.unit
class TestReport:
    def test_the_8k_table_is_newest_first_names_items_skips_exhibits_and_the_window(self):
        text = _report()
        assert "## Form 8-K events, last 180 days (4 filed)" in text
        rows = [l for l in text.splitlines() if l.startswith("| 2026-")]
        assert rows[0] == "| 2026-09-03 | 3.01 Notice of delisting or failure to meet a listing standard |"
        assert "| 2026-08-05 | 2.02 Results of operations (earnings release) |" in rows
        assert "9.01" not in text and "4.01" not in text.split("## Flags")[0]            # exhibits skipped; old item outside

    def test_distress_and_financing_items_are_flagged_and_the_other_events_stay_listed(self):
        flags = _flags(_report())
        assert ("Distress-type Form 8-K items in the last 180 days: 3.01 (Notice of delisting or failure to meet a listing "
                "standard) on 2026-09-03.") in flags
        assert ("Financing-type Form 8-K items in the last 180 days: 3.02 (Unregistered sales of equity securities) on "
                "2026-07-01.") in flags
        text = _report()
        assert "1.01 Entry into a material definitive agreement" in text and "5.02 Departure or appointment" in text

    def test_registrations_late_notices_and_amendments_are_listed_and_flagged_without_reading_into_them(self):
        text, flags = _report(), _flags(_report())
        assert "Registration and prospectus filings: 424B5 x2, S-3 x1 (latest 2026-09-10)." in text
        assert any(f.startswith("3 registration or prospectus filings in the last 180 days (424B5, S-3; latest 2026-09-10)")
                   and "does not say how much was sold" in f for f in flags)
        assert "Late-filing notice: NT 10-Q filed 2026-08-14." in text
        assert "A late-filing notice (NT 10-Q) was filed on 2026-08-14 (within the last year)." in flags
        assert "10-Q/A was filed on 2026-09-20: an amended periodic report can mean a correction." in flags
        assert "Latest 10-Q: period ending 2026-06-30, filed 2026-08-11." in text and "S-8" not in text

    def test_holders_are_tabled_a_13d_is_flagged_and_a_13g_is_only_listed(self):
        text, flags = _report(), _flags(_report())
        assert "| 2026-10-01 | SCHEDULE 13D | Activist Capital LP |" in text and "| 2026-07-27 | SCHEDULE 13G/A | Theon International Plc. |" in text
        assert "A Schedule 13G is a passive holder's filing and a 13D states an intention to influence or control" in text
        assert "A Schedule 13D (the filer states an intention to influence or control) was filed on 2026-10-01 by Activist Capital LP." in flags
        only_13g = _flags(_report(holders=HOLDERS[1:]))
        assert not any("Schedule 13D" in f for f in only_13g)

    def test_share_growth_over_about_a_year_is_worked_out_and_flagged_from_ten_percent(self):
        text, flags = _report(), _flags(_report())
        assert "125,000,000 on 2026-08-11 against 100,000,000 on 2025-08-01: +25.0% over about a year." in text
        assert "The share count grew 25.0% in about a year (100,000,000 to 125,000,000): dilution." in flags
        small = [("2025-08-01", 100_000_000, "x"), ("2026-08-11", 103_000_000, "y")]
        assert not any("dilution" in f for f in _flags(_report(shares=small)))
        assert "Fewer than a year" in _report(shares=SHARES[-2:])

    def test_a_past_run_sees_only_what_had_been_filed(self):
        text = _report(as_of="2026-08-31")
        assert "## Form 8-K events, last 180 days (3 filed)" in text
        assert "3.01" not in text and "424B5" not in text and "| 2026-09-03" not in text
        assert "Registration and prospectus filings: S-3 x1 (latest 2026-08-20)." in text
        assert "10-Q/A" not in text and "Late-filing notice: NT 10-Q filed 2026-08-14." in text

    def test_missing_sources_and_empty_lists_are_said_not_filled_in(self):
        text = _report(filings=[], holders=None, shares=None)
        assert "None with an item that carries news." in text and "No registration or prospectus filings in the last 180 days." in text
        assert "EDGAR's search could not be reached this time." in text and "EDGAR's company facts could not be reached this time." in text
        assert "No rule-based flags." in text
        empty = _report(filings=[], holders=[], shares=[])
        assert "No Schedule 13D or 13G filings about the company in the last year." in empty and "No cover-page share counts are on file." in empty


@pytest.mark.unit
class TestEntryPoint:
    def test_the_submissions_are_read_into_filings(self):
        submissions = {"filings": {"recent": {"form": ["8-K", "10-Q"], "filingDate": ["2026-09-03", "2026-08-11"],
                                              "reportDate": ["", "2026-06-30"], "items": ["3.01,9.01", ""],
                                              "accessionNumber": ["a", "b"]}}}
        assert fe._filings(submissions) == [
            {"form": "8-K", "filingDate": "2026-09-03", "reportDate": "", "items": "3.01,9.01", "accessionNumber": "a"},
            {"form": "10-Q", "filingDate": "2026-08-11", "reportDate": "2026-06-30", "items": "", "accessionNumber": "b"}]
        assert fe._items_text("3.01, 9.01, 7.77") == "3.01 Notice of delisting or failure to meet a listing standard"

    def test_the_report_is_assembled_from_the_vendor_pieces(self, monkeypatch):
        monkeypatch.setattr(fe.sec_edgar, "cik_for", lambda ticker: "0000771266")
        monkeypatch.setattr(fe.sec_edgar, "_cached_json", lambda url, name: {"filings": {"recent": {
            "form": ["8-K"], "filingDate": ["2026-09-03"], "reportDate": [""], "items": ["3.01"], "accessionNumber": ["a"]}}})
        monkeypatch.setattr(fe, "_holder_filings", lambda cik, as_of: HOLDERS)
        monkeypatch.setattr(fe, "_share_counts", lambda cik, as_of: SHARES)
        out = fe.get_filing_events("kopn", "2026-11-06")
        assert out.startswith("# SEC filing events for KOPN (EDGAR filings through 2026-11-06; window 2026-05-10 to 2026-11-06)")
        assert "3.01 Notice of delisting" in out

    def test_a_ticker_with_no_sec_record_is_no_data(self, monkeypatch):
        monkeypatch.setattr(fe.sec_edgar, "cik_for", lambda ticker: None)
        with pytest.raises(NoMarketDataError):
            fe.get_filing_events("7203.T", None)

    def test_the_fundamentals_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import fundamentals_analyst, toolset
        from tradingagents.dataflows import router
        from tradingagents.default_config import DEFAULT_CONFIG

        assert "get_filing_events" in [t.name for t in fundamentals_analyst.TOOLS]
        assert "get_filing_events" in toolset.guidance(fundamentals_analyst.TOOLS, fundamentals_analyst._GUIDANCE)
        assert router.get_category_for_method("get_filing_events") == "filing_events"
        assert "filing_events" in router.OPTIONAL_CATEGORIES and "sec_edgar" in router.VENDOR_METHODS["get_filing_events"]
        assert DEFAULT_CONFIG["data_vendors"]["filing_events"] == "sec_edgar"
