"""Tests for the FRED macro backdrop (a local addition): worked-out series, flags, release calendar, conditional series."""

from datetime import date, timedelta

import pytest

from tradingagents.dataflows.vendors import fred_backdrop as fb

AS_OF = "2026-11-06"


def _points(values, end=AS_OF, step=1):
    last = date.fromisoformat(end)
    return [((last - timedelta(days=step * (len(values) - 1 - i))).isoformat(), v) for i, v in enumerate(values)]


def _flags(text):
    return [line[2:] for line in text.split("## Flags to state in the report\n")[1].split("\n\n")[0].splitlines() if line.startswith("- ")]


@pytest.mark.unit
class TestSeries:
    def test_a_line_gives_the_latest_the_changes_the_year_range_and_the_rank(self):
        # 366 daily readings rising from 2.6 to 3.48: today is the year's high
        values = [2.6 + (3.48 - 2.6) * i / 365 for i in range(366)]
        line, rank, month = fb.series_lines("High-yield spread", "%", _points(values))
        assert line.startswith("- High-yield spread: 3.48% on 2026-11-06; +0.07 in a month; +0.22 in a quarter; past-year range 2.60 to 3.48")
        assert "the latest is the highest reading of the past year" in line and "above 100%" not in line
        assert rank == pytest.approx(1.0, abs=0.01) and month == pytest.approx(0.0723, abs=0.001)

    def test_where_the_latest_reading_sits_is_put_in_words(self):
        assert fb.standing(_points([3.0, 2.0, 4.0, 3.5])) == "above 50% of the past year's readings"
        assert fb.standing(_points([3.0, 2.0, 4.0, 5.0])) == "the highest reading of the past year"
        assert fb.standing(_points([3.0, 4.0, 5.0, 2.0])) == "the lowest reading of the past year"
        assert fb.standing(_points([3.0] * 20)) == "not different from the rest of the past year's readings"

    def test_a_short_history_gives_only_what_it_can(self):
        # six weekly readings reach back 35 days: a month's change can be worked out, a quarter's cannot
        line, rank, month = fb.series_lines("Weekly index", "", _points([-0.4, -0.42, -0.44, -0.46, -0.48, -0.5], step=7))
        assert "-0.10 in a month" in line and "in a quarter" not in line and rank == 0.0 and month == pytest.approx(-0.10)
        # three weekly readings reach back only 14 days: neither change is given, the range still is
        line, rank, month = fb.series_lines("Weekly index", "", _points([-0.4, -0.45, -0.5], step=7))
        assert "in a month" not in line and "past-year range -0.50 to -0.40" in line and month is None


@pytest.mark.unit
class TestReport:
    def _report(self, spread, nfci=None, releases=(), note=""):
        groups = [("BAMLH0A0HYM2", "High-yield spread", "%", spread), ("NFCI", "Financial conditions", "", nfci or [])]
        return fb.build_report(AS_OF, groups, list(releases), note)

    def test_a_spread_at_the_top_of_its_year_is_flagged_and_a_calm_one_is_not(self):
        rising = _points([2.6 + 0.9 * i / 365 for i in range(366)])
        assert any(f.startswith("High-yield spreads are 3.50%, the highest reading of the past year") for f in _flags(self._report(rising)))
        calm = _points([3.0 + 0.4 * ((i % 50) / 50) for i in range(366)])
        calm[-1] = (calm[-1][0], 3.1)
        assert not any("High-yield" in f for f in _flags(self._report(calm)))

    def test_a_fast_widening_is_flagged_even_below_the_top_of_the_range(self):
        points = _points([4.0] * 300 + [3.0] * 36 + [3.6] * 30)
        assert any("+0.60 points in a month" in f or "points in a month" in f for f in _flags(self._report(points)))

    def test_tighter_than_average_financial_conditions_are_flagged_and_looser_are_not(self):
        spread = _points([3.0] * 366)
        assert any("Chicago Fed financial conditions index is +0.12" in f for f in _flags(self._report(spread, _points([0.12] * 60, step=7))))
        assert not any("Chicago Fed" in f for f in _flags(self._report(spread, _points([-0.46] * 60, step=7))))

    def test_the_financial_conditions_line_says_in_words_whether_conditions_are_loose_or_tight(self):
        spread = _points([3.0] * 366)
        assert "Reading: conditions are looser than the long-run average." in self._report(spread, _points([-0.46] * 60, step=7))
        assert "Reading: conditions are tighter than the long-run average." in self._report(spread, _points([0.12] * 60, step=7))

    def test_releases_are_listed_and_one_within_two_days_is_flagged(self):
        spread = _points([3.0] * 366)
        text = self._report(spread, releases=[("2026-11-08", "Consumer Price Index (CPI)"), ("2026-11-17", "Advance retail sales")])
        assert "Data releases due in the next 14 days: 2026-11-08 Consumer Price Index (CPI); 2026-11-17 Advance retail sales." in text
        assert _flags(text) == ["Consumer Price Index (CPI) is due on 2026-11-08, within 2 days."]
        assert "No CPI, jobs, GDP, PCE or retail-sales release is due in the next 14 days." in self._report(spread)

    def test_a_missing_series_is_said_and_the_notes_label_macro_as_noisy(self):
        text = self._report([], note="Series linked to the stock's country are not shown for a past run.")
        assert "- High-yield spread: not available this time." in text and "No rule-based flags." in text
        assert "the noisiest input" in text and "FOMC meeting dates are not in FRED's calendar" in text
        assert "Series linked to the stock's country are not shown" in text


@pytest.mark.unit
class TestEntryPoint:
    @pytest.fixture
    def world(self, monkeypatch):
        asked = []

        def readings(series_id, as_of):
            asked.append(series_id)
            if series_id == "BROKEN":
                raise ValueError("bad series")
            return _points([3.0] * 366)

        monkeypatch.setattr(fb, "_readings", readings)
        monkeypatch.setattr(fb, "_release_dates", lambda as_of: [])
        profile = {"info": {}}
        monkeypatch.setattr(fb, "yf", type("FakeYF", (), {"Ticker": lambda symbol: type("T", (), {"info": profile["info"]})()}))
        return asked, profile

    def test_the_two_core_series_are_always_read_and_sector_series_follow_the_profile(self, world):
        asked, profile = world
        fb.get_macro_backdrop("TEST", None)
        assert asked == ["BAMLH0A0HYM2", "NFCI"]
        asked.clear()
        profile["info"] = {"country": "Japan", "sectorKey": "consumer-cyclical"}
        fb.get_macro_backdrop("TM", None)
        assert asked == ["BAMLH0A0HYM2", "NFCI", "DEXJPUS"]
        asked.clear()
        profile["info"] = {"country": "United States", "sectorKey": "energy"}
        fb.get_macro_backdrop("XOM", None)
        assert asked == ["BAMLH0A0HYM2", "NFCI", "DCOILWTICO", "DHHNGSP"]

    def test_a_past_run_reads_no_profile_and_says_so(self, world, monkeypatch):
        monkeypatch.setattr(fb, "yf", type("NoYF", (), {"Ticker": lambda symbol: pytest.fail("the profile must not be read")}))
        out = fb.get_macro_backdrop("TM", "2025-06-30")
        assert "not shown for a past run" in out and "as known on 2025-06-30" in out

    def test_a_failed_series_or_calendar_does_not_stop_the_report(self, world, monkeypatch):
        monkeypatch.setattr(fb, "ALWAYS", (("BROKEN", "Broken series", ""),) + fb.ALWAYS)
        monkeypatch.setattr(fb, "_release_dates", lambda as_of: (_ for _ in ()).throw(ValueError("down")))
        out = fb.get_macro_backdrop("TEST", None)
        assert "- Broken series: not available this time." in out and "- High-yield bond spread over Treasuries" in out
        assert "No CPI, jobs, GDP, PCE or retail-sales release is due" in out

    def test_the_news_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import news_analyst, toolset
        from tradingagents.dataflows import router

        assert "get_macro_backdrop" in [t.name for t in news_analyst.TOOLS]
        assert "get_macro_backdrop" in toolset.guidance(news_analyst.TOOLS, news_analyst._GUIDANCE)
        assert router.get_category_for_method("get_macro_backdrop") == "macro_data"
        assert "fred" in router.VENDOR_METHODS["get_macro_backdrop"] and "macro_data" in router.OPTIONAL_CATEGORIES
