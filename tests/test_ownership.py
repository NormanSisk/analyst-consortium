"""Tests for the ownership and short-interest vendor (a local addition): worked-out percentages, flags, lags."""

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import ownership

TODAY = pd.Timestamp("2026-11-06")


def _epoch(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def _ionq_info(**overrides) -> dict:
    """Short interest of 47.9M shares against 429M outstanding, up 18% on the month."""
    info = {
        "sharesShort": 47_907_371, "sharesShortPriorMonth": 40_486_062,
        "sharesShortPreviousMonthDate": _epoch("2026-09-15"), "dateShortInterest": _epoch("2026-10-15"),
        "shortRatio": 1.84, "shortPercentOfFloat": 0.1177,
        "floatShares": 429_491_633, "sharesOutstanding": 429_407_678,
        "heldPercentInstitutions": 0.48229, "heldPercentInsiders": 0.00663,
    }
    info.update(overrides)
    return info


def _holders(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["Date Reported", "Holder", "pctHeld", "Shares", "Value", "pctChange"]).assign(
        **{"Date Reported": lambda d: pd.to_datetime(d["Date Reported"])})


INSTITUTIONS = _holders([
    ["2026-06-30", "BlackRock Inc.", 0.0511, 20_896_455, 681_642_339, -0.3004],
    ["2026-06-30", "Morgan Stanley", 0.0643, 26_279_397, 857_233_902, 0.2419],
    ["2026-06-30", "Vanguard Capital Management LLC", 0.0381, 15_598_546, 508_824_553, 0.0355],
    ["2026-06-30", "Oddly | Named LLC", 0.0100, 4_000_000, 130_000_000, 0.6000],
])
FUNDS = _holders([
    ["2026-06-30", "Vanguard Total Stock Market Index Fund", 0.0261, 10_666_391, 347_937_663, 0.0170],
])


def _short(info, today=TODAY):
    return ownership._short_section(info, today)


@pytest.mark.unit
class TestShortInterest:
    def test_the_figures_name_their_date_age_and_denominator(self):
        lines, _ = _short(_ionq_info())
        text = "\n".join(lines)
        assert "Shares short: 47,907,371 as of 2026-10-15 (22 days before today)." in text
        assert "That is 11.2% of the 429,407,678 shares outstanding, and 11.2% of the float of 429,491,633 shares." in text
        assert "The report before that (2026-09-15): 40,486,062 shares short, a change of +18%." in text
        assert "Days to cover: 1.8" in text and "lags" in text

    def test_a_heavily_shorted_stock_is_flagged_and_a_steady_one_is_not(self):
        _, flags = _short(_ionq_info())
        assert flags == ["11.2% of the shares outstanding are sold short, 10% or more."]
        _, calm = _short(_ionq_info(sharesShort=4_000_000, sharesShortPriorMonth=3_900_000, shortRatio=1.2,
                                    shortPercentOfFloat=0.0093))
        assert calm == []

    def test_a_big_move_and_many_days_to_cover_are_flagged(self):
        _, flags = _short(_ionq_info(sharesShort=60_000_000, shortRatio=6.81))
        assert "Shares short rose 48% from 2026-09-15 to 2026-10-15." in flags
        assert "Days to cover is 6.8, 5 or more: the shorted shares would take that many days of average volume to buy back." in flags
        _, falling = _short(_ionq_info(sharesShort=20_000_000))
        assert "Shares short fell 51% from 2026-09-15 to 2026-10-15." in falling

    def test_a_float_that_contradicts_yahoos_own_percent_is_flagged_and_not_quoted(self):
        # A small-company shape: more shares short than the float Yahoo gives, yet 23.5% "of float".
        info = _ionq_info(sharesShort=3_082_775, sharesShortPriorMonth=508_658, floatShares=3_073_156,
                          sharesOutstanding=13_685_941, shortPercentOfFloat=0.2354, shortRatio=0.13)
        lines, flags = _short(info)
        text = "\n".join(lines)
        assert "That is 22.5% of the 13,685,941 shares outstanding." in text and "of the float" not in text
        mismatch = next(f for f in flags if "does not match" in f)
        assert "23.5%" in mismatch and "quote 22.5% of shares outstanding and not a percent of float" in mismatch
        assert "Shares short rose 506%" in " ".join(flags)

    def test_shares_short_above_the_float_with_no_yahoo_percent_is_still_flagged(self):
        _, flags = _short(_ionq_info(sharesShort=500, floatShares=400, sharesOutstanding=1000, shortPercentOfFloat=None,
                                     sharesShortPriorMonth=None))
        assert any("exceed Yahoo's float figure" in f for f in flags)

    def test_days_to_cover_that_implies_an_impossible_volume_is_flagged_and_marked_unreliable(self):
        # GRML's shape: 3.08M shares short, 0.13 days to cover, 13.7M shares outstanding.
        info = _ionq_info(sharesShort=3_082_775, sharesOutstanding=13_685_941, floatShares=13_000_000,
                          shortPercentOfFloat=0.237, shortRatio=0.13, sharesShortPriorMonth=3_000_000)
        lines, flags = _short(info)
        assert "- Days to cover: 0.1 (shares short divided by average daily volume; this figure looks unreliable, see the flags)." in lines
        flag = next(f for f in flags if f.startswith("Days to cover (0.1) implies"))
        assert "23,713,654 shares, 173% of all shares outstanding every day" in flag
        assert "do not quote days to cover for this stock" in flag

    def test_plausible_days_to_cover_is_left_alone_and_the_edge_is_where_half_the_shares_trade_daily(self):
        lines, flags = _short(_ionq_info())            # 1.84 days: about 6% of the shares a day
        assert not any("unreliable" in line for line in lines) and not any("Days to cover" in f for f in flags)
        quiet = dict(sharesShort=100_000, sharesOutstanding=1_000_000, floatShares=1_000_000, shortPercentOfFloat=0.1,
                     sharesShortPriorMonth=100_000)
        assert not any(f.startswith("Days to cover") for f in _short(_ionq_info(shortRatio=0.4, **quiet))[1])   # 25%
        assert any(f.startswith("Days to cover") for f in _short(_ionq_info(shortRatio=0.19, **quiet))[1])      # 53%

    def test_a_zero_days_to_cover_does_not_divide_by_zero(self):
        lines, flags = _short(_ionq_info(shortRatio=0.0))
        assert any(line.startswith("- Days to cover: 0.0") for line in lines)
        assert not any(f.startswith("Days to cover") for f in flags)

    def test_an_unreliable_days_to_cover_is_not_also_flagged_as_long(self):
        info = _ionq_info(sharesShort=3_000_000, sharesOutstanding=1_000_000, floatShares=1_000_000,
                          shortPercentOfFloat=3.0, shortRatio=5.0, sharesShortPriorMonth=3_000_000)
        flags = _short(info)[1]
        assert sum(f.startswith("Days to cover") for f in flags) == 1 and not any("5 or more" in f for f in flags)

    def test_a_small_float_with_consistent_figures_is_a_fact_not_an_error(self):
        _, flags = _short(_ionq_info(sharesShort=1_000_000, floatShares=4_000_000, sharesOutstanding=20_000_000,
                                     shortPercentOfFloat=0.25, sharesShortPriorMonth=1_000_000, shortRatio=1.0))
        assert any("is 20% of the shares outstanding, so most shares are not freely traded" in f for f in flags)

    def test_no_short_figure_gives_no_section(self):
        assert _short({"heldPercentInstitutions": 0.5}) == ([], [])


@pytest.mark.unit
class TestOwnership:
    def _section(self, institutions=INSTITUTIONS, funds=FUNDS, info=None):
        return ownership._owner_section(info if info is not None else _ionq_info(),
                                        ownership._holder_rows(institutions, 10),
                                        ownership._holder_rows(funds, 5), TODAY)

    def test_totals_lag_and_the_largest_holders_come_first_by_shares(self):
        lines, _ = self._section()
        text = "\n".join(lines)
        assert "institutions hold 48.2% of the shares outstanding and insiders hold 0.7%" in text
        assert "The newest institutional report is dated 2026-06-30 (129 days before today)." in text
        rows = [line for line in lines if line.startswith("| ") and "Holder" not in line][:4]
        assert [r.split("|")[1].strip() for r in rows] == [
            "Morgan Stanley", "BlackRock Inc.", "Vanguard Capital Management LLC", "Oddly / Named LLC"]
        assert "| Morgan Stanley | 26,279,397 | 6.43% | +24.2% | 2026-06-30 |" in lines

    def test_big_position_changes_are_flagged_largest_first(self):
        _, flags = self._section()
        assert flags == [
            "Oddly / Named LLC's position changed by +60% in its latest report (2026-06-30); "
            "it holds 1.0% of the shares outstanding.",
            "BlackRock Inc.'s position changed by -30% in its latest report (2026-06-30); "
            "it holds 5.1% of the shares outstanding."]

    def test_a_small_stake_with_a_big_change_is_in_the_table_but_not_flagged(self):
        small = _holders([["2026-06-30", "Tiny Capital", 0.0079, 107_475, 1_000_000, 1.0],
                          ["2026-06-30", "Big Capital", 0.0500, 900_000, 9_000_000, 0.0100]])
        lines, flags = self._section(institutions=small, funds=None)
        assert any("Tiny Capital" in line and "+100.0%" in line for line in lines) and flags == []

    def test_increases_and_decreases_are_counted_and_funds_are_labelled_as_included(self):
        text = "\n".join(self._section()[0])
        assert "Of the 4 largest institutions with a stated change, 3 show an increase and 1 a decrease" in text
        assert "does not say whether a +100% is a new position" in text
        assert "already part of the institutional figures, so do not add them" in text

    def test_a_missing_change_or_date_prints_as_not_available(self):
        frame = _holders([[None, "Ghost Capital", None, 1_000, None, None]])
        lines, flags = self._section(institutions=frame, funds=None, info={})
        assert "| Ghost Capital | 1,000 | n/a | n/a | n/a |" in lines and flags == []

    def test_nothing_at_all_gives_no_section(self):
        assert ownership._owner_section({}, [], [], TODAY) == ([], [])


class _Ticker:
    """A Yahoo ticker with the three things this vendor reads."""

    info = _ionq_info()
    institutional_holders = INSTITUTIONS
    mutualfund_holders = FUNDS


def _fake_yahoo(monkeypatch, ticker=_Ticker):
    monkeypatch.setattr(ownership, "yf", type("FakeYF", (), {"Ticker": lambda symbol: ticker()}))
    monkeypatch.setattr(ownership, "_today", lambda: TODAY)
    monkeypatch.setattr(ownership, "_finra_section", lambda symbol, as_of, prefer=False: ([], []))   # no network in tests


@pytest.mark.unit
class TestEntryPoint:
    def test_a_run_dated_in_the_past_is_withheld(self):
        out = ownership.get_ownership_and_short_interest("GOOG", "2020-01-02")
        assert "withheld for this date" in out and "Point-in-time as of: 2020-01-02" in out

    def test_the_report_has_its_sections_in_order_and_the_flags_last(self, monkeypatch):
        _fake_yahoo(monkeypatch)
        out = ownership.get_ownership_and_short_interest("IONQ", None)
        order = ["## Short interest", "## Who owns the shares", "## Flags to state in the report"]
        assert [out.index(h) for h in order] == sorted(out.index(h) for h in order)
        assert out.startswith("# Ownership and short interest for IONQ (present-day figures)")
        assert "Oddly / Named LLC's position changed by +60%" in out.split("## Flags to state in the report")[1]

    def test_a_listing_with_no_data_says_so_instead_of_failing(self, monkeypatch):
        class Empty:
            info = {"trailingPegRatio": None}
            institutional_holders = None
            mutualfund_holders = pd.DataFrame()

        _fake_yahoo(monkeypatch, Empty)
        monkeypatch.setattr(ownership, "vendor_reachable", lambda host: True)
        out = ownership.get_ownership_and_short_interest("DRAM", None)
        assert out == "No ownership or short-interest data is reported for 'DRAM' (funds and some listings have none)."

    def test_an_unreachable_yahoo_is_an_outage_not_an_absence(self, monkeypatch):
        class Empty:
            info = {}
            institutional_holders = None
            mutualfund_holders = None

        _fake_yahoo(monkeypatch, Empty)
        monkeypatch.setattr(ownership, "vendor_reachable", lambda host: False)
        with pytest.raises(VendorUnavailableError):
            ownership.get_ownership_and_short_interest("IONQ", None)

    def test_a_failed_holder_list_does_not_take_the_short_section_down(self, monkeypatch):
        class Broken(_Ticker):
            @property
            def institutional_holders(self):
                raise RuntimeError("boom")

        _fake_yahoo(monkeypatch, Broken)
        out = ownership.get_ownership_and_short_interest("IONQ", None)
        assert "## Short interest" in out and "the institutional holder list could not be retrieved" in out

    def test_the_fundamentals_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import fundamentals_analyst
        from tradingagents.dataflows import router

        assert "get_ownership_and_short_interest" in [t.name for t in fundamentals_analyst.TOOLS]
        assert router.get_category_for_method("get_ownership_and_short_interest") == "fundamental_data"
        assert "yfinance" in router.VENDOR_METHODS["get_ownership_and_short_interest"]
