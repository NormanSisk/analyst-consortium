"""Tests for the FINRA short-sale data (a local addition): file parsing, settlement dates, trend flags, ownership tie-in."""

from datetime import date

import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import finra_short as fs
from tradingagents.dataflows.vendors.yahoo import ownership

HEADER = ('"accountingYearMonthNumber"|"symbolCode"|"issueName"|"issuerServicesGroupExchangeCode"|"marketClassCode"|'
          '"currentShortPositionQuantity"|"previousShortPositionQuantity"|"stockSplitFlag"|"averageDailyVolumeQuantity"|'
          '"daysToCoverQuantity"|"revisionFlag"|"changePercent"|"changePreviousNumber"|"settlementDate"')
INTEREST = "\n".join([
    HEADER,
    '"20261015"|"KOPN"|"Kopin Corporation Common Stock"|"R"|"SC"|"24111572"|"24396434"||"3213146"|"7.50"||"-1.17"|"-284862"|"2026-10-15"',
    '"20261015"|"GRML"|"Greenland Mines Ltd. Common St"|"R"|"SC"|"3082775"|"935276"||"6124750"|"1.00"||"229.61"|"2147499"|"2026-10-15"',
])
DAILY = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20261106|KOPN|1054918.110988|94453.599054|2071325.2251|B,Q,N\n20261106|GRML|154919.3|3199|283263.7|B,Q,N\n"


def _row(settled, short, change=None, avg=1_000_000, dtc=2.0):
    return {"settled": settled, "short": short, "previous": None, "avg_volume": avg, "days_to_cover": dtc, "change_percent": change}


@pytest.mark.unit
class TestParsing:
    def test_a_short_interest_row_is_read_with_its_blank_fields(self):
        assert fs.parse_interest(INTEREST, "KOPN") == {"settled": "2026-10-15", "short": 24111572.0, "previous": 24396434.0,
                                                       "avg_volume": 3213146.0, "days_to_cover": 7.5, "change_percent": -1.17}
        assert fs.parse_interest(INTEREST, "GRML")["days_to_cover"] == 1.0
        assert fs.parse_interest(INTEREST, "ZZZZ") is None and fs.parse_interest("", "KOPN") is None

    def test_a_daily_row_gives_short_and_total_volume(self):
        assert fs.parse_daily(DAILY, "KOPN") == (1054918.110988, 2071325.2251)
        assert fs.parse_daily(DAILY, "ZZZZ") is None

    def test_settlement_dates_are_the_fifteenth_and_the_month_end_moved_back_to_a_business_day(self):
        days = fs.settlement_dates(date(2026, 11, 6), 5)
        assert days[:4] == [date(2026, 10, 15), date(2026, 9, 30), date(2026, 9, 15), date(2026, 8, 31)]
        assert date(2026, 8, 14) in days             # the 15th of August 2026 is a Saturday
        assert all(d <= date(2026, 10, 26) for d in days)                  # not yet published: later than the lag allows


@pytest.mark.unit
class TestSection:
    def test_the_history_table_and_a_run_of_rises_are_reported(self):
        rows = [_row("2026-10-15", 400_000, 60.0), _row("2026-09-30", 250_000, 56.3), _row("2026-09-15", 160_000, 33.3),
                _row("2026-08-31", 120_000, 20.0), _row("2026-08-14", 100_000, None)]
        lines, flags = fs.build_section("TEST", {"interest": rows, "daily": [], "threshold": None})
        text = "\n".join(lines)
        assert "| 2026-10-15 | 400,000 | +60.0% | 1,000,000 | 2.00 |" in text and "| 2026-08-14 | 100,000 | n/a |" in text
        assert "Short interest rose in each of the last 4 reports (100,000 to 400,000, +300%)." in flags
        assert "Too few daily short-sale volume files" in text and "about seven business days" in text

    def test_a_single_rise_or_a_small_one_is_not_flagged(self):
        flat = [_row("2026-10-15", 105_000), _row("2026-09-30", 100_000), _row("2026-09-15", 102_000), _row("2026-08-31", 101_000)]
        assert fs.build_section("TEST", {"interest": flat, "daily": [], "threshold": None})[1] == []
        slow = [_row("2026-10-15", 130_000), _row("2026-09-30", 120_000), _row("2026-09-15", 110_000), _row("2026-08-31", 100_000)]
        assert fs.build_section("TEST", {"interest": slow, "daily": [], "threshold": None})[1] == []

    def test_a_rising_short_sale_share_is_flagged_and_the_level_alone_is_not(self):
        daily = [("2026-11-0%d" % i, 600, 1000) for i in range(1, 6)] + [("2026-10-%02d" % i, 450, 1000) for i in range(15)]
        lines, flags = fs.build_section("TEST", {"interest": [], "daily": daily, "threshold": None})
        assert any("60% over the last 5 sessions against 49% over the last 20" in l for l in lines)
        assert flags == ["The short-sale share of volume has risen: 60% over the last 5 sessions against 49% over the last 20."]
        level = [("d%d" % i, 700, 1000) for i in range(20)]
        assert fs.build_section("TEST", {"interest": [], "daily": level, "threshold": None})[1] == []

    def test_the_threshold_list_is_reported_and_flagged_when_the_stock_is_on_it(self):
        on = fs.build_section("TEST", {"interest": [], "daily": [], "threshold": ("2026-11-06", True)})
        off = fs.build_section("TEST", {"interest": [], "daily": [], "threshold": ("2026-11-06", False)})
        assert "on the list" in "\n".join(on[0]) and any("persistent failures to deliver" in f for f in on[1])
        assert "not on the list" in "\n".join(off[0]) and off[1] == []

    def test_nothing_found_is_said(self):
        lines, flags = fs.build_section("TEST", {"interest": [], "daily": [], "threshold": None})
        assert lines[0] == "No FINRA short-interest report was found for this symbol." and flags == []


@pytest.mark.unit
class TestFetch:
    def test_unpublished_files_are_skipped_weekends_are_not_asked_for_and_the_newest_day_is_the_day_before(self, monkeypatch):
        asked = []

        def fake_text(url, name):
            asked.append(name)
            if name.startswith("CNMSshvol"):
                day = name[9:17]
                return f"Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n{day}|TEST|40|0|100|B\n" if day != "20261103" else None
            if name.startswith("shrt"):
                return INTEREST.replace("KOPN", "TEST") if name == "shrt20261015.csv" else None
            return "Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler\nTEST|Test Inc|G|Y|N|\n"

        monkeypatch.setattr(fs, "_text", fake_text)
        data = fs.fetch("TEST", date(2026, 11, 6))                      # a Friday
        assert [d[0] for d in data["daily"]][:3] == ["2026-11-05", "2026-11-04", "2026-11-02"]    # the 3rd is missing, the weekend skipped
        assert len(data["daily"]) == fs.DAILY_SESSIONS and all(d[1] == 40 and d[2] == 100 for d in data["daily"])
        assert len(data["interest"]) == 1 and data["interest"][0]["settled"] == "2026-10-15"
        assert data["threshold"] == ("2026-11-06", True)
        assert not any(name.endswith(("20261107.txt", "20261108.txt")) for name in asked)


@pytest.mark.unit
class TestOwnershipTieIn:
    def test_finra_lines_and_flags_join_the_report_and_unreachable_files_are_a_note(self, monkeypatch):
        monkeypatch.setattr(fs, "fetch", lambda symbol, as_of: {"interest": [], "daily": [], "threshold": ("2026-11-06", True)})
        lines, flags = ownership._finra_section("TEST", "2026-11-07")
        assert any("on the list" in l for l in lines) and flags
        monkeypatch.setattr(fs, "fetch", lambda symbol, as_of: (_ for _ in ()).throw(VendorUnavailableError("down")))
        assert ownership._finra_section("TEST", "2026-11-07") == (["FINRA's files could not be reached this time."], [])

    def test_finras_own_days_to_cover_is_offered_when_yahoos_was_called_unreliable(self, monkeypatch):
        rows = [_row("2026-10-15", 3_082_775, 229.6, avg=6_124_750, dtc=1.0)]
        monkeypatch.setattr(fs, "fetch", lambda symbol, as_of: {"interest": rows, "daily": [], "threshold": None})
        text = "\n".join(ownership._finra_section("TEST", "2026-11-07", True)[0])
        assert "FINRA's own days to cover for 2026-10-15 is 1.00 (its average daily volume is 6,124,750 shares)" in text
        assert "FINRA's own days to cover" not in "\n".join(ownership._finra_section("TEST", "2026-11-07", False)[0])
