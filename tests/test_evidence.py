"""Evidence, the five deal conditions and the mechanical cross-check, all worked out in code from the data tools' text.

The numbers below are made up (the fixture is an invented company); the market-context text is produced by the real tool
from synthetic prices, so the patterns that read it and the tool that writes it cannot drift apart unnoticed.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradingagents.agents import evidence as ev, scorecard
from tradingagents.dataflows.vendors.yahoo import market_context as mc
from tradingagents.picker.settings import PickerSettings

OUTPUTS = json.loads((Path(__file__).parent / "fixtures" / "scorecard_example.json").read_text(encoding="utf-8"))
SETTINGS = PickerSettings()


def _analysis(**replace):
    return ev.analyze({**OUTPUTS, **replace})


def _item(analysis, name_part):
    return next(i for i in analysis.items if name_part in i.name)


def _trend(margin="45.9%: +3.2 pts versus a year earlier (year-over-year view); +0.2 pts versus the prior quarter and "
                  "+0.9 pts versus two quarters earlier (sequential view)", ocf="+82.6%", shares="-0.7%",
           debt="21,540, 0.9 times its level a year earlier (23,900)", revenue="+78.9%", cash="31,870 versus 27,400 a year earlier"):
    return (f"# Trend facts for EXMP ## The latest quarter, both ways - Revenue: {revenue} versus the same quarter a year earlier; "
            f"+13.7% versus the prior quarter. - Operating margin is {margin}. - Operating cash flow: {ocf} versus the same quarter a "
            f"year earlier; -36.2% versus the prior quarter. - Free cash flow (operating cash flow minus capital spending, Yahoo's "
            f"definition, which can differ from a company's own) over the same quarters, oldest first: 6,210, 9,480, 14,320, 19,950, "
            f"12,100. - Diluted average shares: {shares} versus a year earlier. - Long-term debt: {debt}; 9,870 in the prior quarter. "
            f"- Cash and short-term investments: {cash}.")


def _market(sector_shock=0.0, own_shock=0.0):
    """The market-context tool's real output for synthetic prices (a sector selloff the S&P 500 barely shows when sector_shock<0)."""
    rng = np.random.default_rng(21)
    n = 300
    market, driver = rng.normal(0.0003, 0.009, n), rng.normal(0.0, 0.008, n)
    driver[-63:] += sector_shock

    def prices(returns):
        values = 100.0 * np.concatenate([[1.0], np.cumprod(1 + returns)])
        return pd.Series(values, index=pd.bdate_range(end="2026-11-06", periods=len(values)))

    stock_returns = market + 0.9 * driver + rng.normal(0, 0.003, n)
    stock_returns[-63:] += own_shock
    markets = {"S&P 500": prices(market), "Nasdaq Composite": prices(1.2 * market), "Russell 2000 (IWM)": prices(0.9 * market)}
    return mc.build_report("EXMP", prices(stock_returns), markets, None, ("Semiconductors (SMH)", prices(0.8 * market + driver)),
                           "2026-11-07")


# ---------------------------------------------------------------- the mirrored evidence
@pytest.mark.unit
class TestMirroredEvidence:
    @pytest.mark.parametrize("pts, direction", [(+3.0, 1), (+2.9, 0), (0.0, 0), (-2.9, 0), (-3.0, -1), (+8.1, 1), (-5.0, -1)])
    def test_the_margin_on_a_year_ago_counts_by_the_same_size_in_both_directions(self, pts, direction):
        trend = _trend(margin=f"45.9%: {pts:+.1f} pts versus a year earlier (year-over-year view); +0.2 pts versus the prior quarter")
        assert _item(_analysis(get_trend_facts=trend), "margin on a year ago").direction == direction

    @pytest.mark.parametrize("ocf, direction", [("+25.0%", 1), ("+24.9%", 0), ("-24.9%", 0), ("-25.0%", -1), ("+116.0%", 1)])
    def test_operating_cash_flow_counts_at_plus_and_minus_a_quarter(self, ocf, direction):
        assert _item(_analysis(get_trend_facts=_trend(ocf=ocf)), "operating cash flow").direction == direction

    @pytest.mark.parametrize("shares, direction", [("-5.0%", 1), ("-4.9%", 0), ("+4.9%", 0), ("+5.0%", -1)])
    def test_the_share_count_counts_at_plus_and_minus_five_percent(self, shares, direction):
        assert _item(_analysis(get_trend_facts=_trend(shares=shares)), "share count").direction == direction

    @pytest.mark.parametrize("debt, direction", [("10,000, 0.5 times its level a year earlier (20,000)", 1),
                                                 ("15,000, 0.8 times its level a year earlier (18,750)", 0),
                                                 ("40,000, 2.0 times its level a year earlier (20,000)", -1)])
    def test_debt_counts_at_half_and_double_its_level_a_year_ago_when_cash_does_not_cover_it(self, debt, direction):
        item = _item(_analysis(get_trend_facts=_trend(debt=debt, cash="5,000 versus 4,000 a year earlier")), "debt")
        assert item.direction == direction and item.text.endswith(", and cash does not cover it")

    @pytest.mark.parametrize("debt, cash", [("40,000, 2.0 times its level a year earlier (20,000)", "90,000 versus 80,000 a year earlier"),
                                            ("10,000, 0.5 times its level a year earlier (20,000)", "31,870 versus 27,400 a year earlier"),
                                            ("38,000, 3.8 times its level a year earlier (10,000)", "38,000 versus 30,000 a year earlier")])
    def test_a_change_in_debt_that_cash_covers_is_not_counted_in_either_direction(self, debt, cash):
        """NVDA's long-term debt was 3.8 times its level a year ago while its cash was many times the debt."""
        a = _analysis(get_trend_facts=_trend(debt=debt, cash=cash))
        item = _item(a, "debt")
        assert item.direction == 0 and "so the change is not counted" in item.text and "cash covers it" in item.text
        assert a.conditions["c"].status == "PASS" and "so the change is not counted" in a.conditions["c"].detail

    def test_without_cash_the_size_of_a_debt_change_cannot_be_judged_and_it_is_a_gap_not_a_finding(self):
        trend = _trend(debt="40,000, 2.0 times its level a year earlier (20,000)").replace(
            "- Cash and short-term investments: 31,870 versus 27,400 a year earlier.", "")
        a = _analysis(get_trend_facts=trend)
        item = _item(a, "debt")
        assert item.direction is None and "cash is not available, so the size of the change cannot be judged" in item.text
        assert any(g.startswith("long-term debt on a year ago:") for g in a.gaps)
        assert a.conditions["c"].status == "FAIL"           # the picker's own check still flags the doubled debt it cannot weigh

    def test_a_one_quarter_margin_move_is_a_watch_item_either_way_and_counts_only_when_large(self):
        for seq, direction, watched in ((-1.7, 0, True), (+1.7, 0, True), (-0.5, 0, False), (-3.0, -1, False), (+3.0, 1, False)):
            trend = _trend(margin=f"58.7%: +8.1 pts versus a year earlier (year-over-year view); {seq:+.1f} pts versus the prior quarter")
            item = _item(_analysis(get_trend_facts=trend), "prior quarter")
            assert item.direction == direction and bool(item.watch) is watched, seq
        assert "(a dip of less than 3 points)" in _item(_analysis(get_trend_facts=_trend(
            margin="58.7%: +8.1 pts versus a year earlier (year-over-year view); -1.7 pts versus the prior quarter")), "prior quarter").watch

    def test_estimates_are_judged_net_and_a_few_cuts_among_many_raises_are_a_watch_item(self):
        earnings = ("| this fiscal year (ending Dec 2026) | 17.50 | 16.93 | 16.70 | +4.8% | $5,582.1B | +46.6% | 40 | 9 up / 2 down |"
                    " | next fiscal year (ending Dec 2027) | 23.39 | 21.92 | 21.28 | +9.9% | $7,691.2B | +37.8% | 41 | 10 up / 2 down |")
        a = _analysis(get_earnings_facts=earnings)
        this_year = _item(a, "this year's EPS")
        assert this_year.direction == 1 and "net up, so not counted against it" in this_year.watch
        mirrored = ("| this fiscal year (ending Dec 2026) | 17.50 | 18.0 | 18.5 | -4.8% | $1.0B | +4.6% | 40 | 2 up / 9 down |")
        down = _item(_analysis(get_earnings_facts=mirrored), "this year's EPS")
        assert down.direction == -1 and "net down, so not counted for it" in down.watch
        flat = ("| this fiscal year (ending Dec 2026) | 17.50 | 17.4 | 17.3 | +0.5% | $1.0B | +4.6% | 40 | 2 up / 1 down |")
        assert _item(_analysis(get_earnings_facts=flat), "this year's EPS").direction == 0

    def test_raises_and_cuts_that_disagree_with_the_consensus_move_are_not_counted_either_way(self):
        odd = "| this fiscal year (ending Dec 2026) | 17.50 | 18.0 | 18.5 | -4.8% | $1.0B | +4.6% | 40 | 9 up / 2 down |"
        assert _item(_analysis(get_earnings_facts=odd), "this year's EPS").direction == 0

    def test_valuation_is_judged_against_the_treasury_its_own_history_and_its_peers(self):
        a = _analysis()
        assert _item(a, "earnings yield against the 10-year Treasury").direction == 1        # 5.8% against 4.12% (+1.7 points)
        assert _item(a, "own 10-year history").direction == 1                                 # higher than only 18% of its readings
        assert _item(a, "its peers' median").direction is None                                # no median: too few peers
        val = OUTPUTS["get_valuation_context"].replace("higher than 18% of", "higher than 80% of")
        assert _item(_analysis(get_valuation_context=val), "own 10-year history").direction == -1
        val = OUTPUTS["get_valuation_context"].replace("higher than 18% of", "lower than 20% of")      # i.e. above 80% of its readings
        assert _item(_analysis(get_valuation_context=val), "own 10-year history").direction == -1
        peers = OUTPUTS["get_valuation_context"] + " | Median of the similar-size peers (positive values only) | | 11.0x | 20.0x |"
        assert _item(_analysis(get_valuation_context=peers), "its peers' median").direction == -1     # 17.1x against 11.0x

    @pytest.mark.parametrize("this_eps, next_eps, pe, direction", [
        (4.00, 5.40, 17.1, 1),       # 35% growth: PEG 0.49, paid for by growth
        (4.00, 4.80, 24.0, 0),       # 20% growth: PEG 1.2, neither cheap nor stretched
        (4.00, 4.40, 22.0, -1),      # 10% growth: PEG 2.2, growth does not pay for the multiple
    ])
    def test_the_growth_adjusted_pe_uses_consensus_growth_defined_in_the_text(self, this_eps, next_eps, pe, direction):
        val = (f"# Valuation context for EXMP ## Where the price stands Price 92.40. Trailing P/E (GAAP) 27.4x; forward P/E {pe}x "
               f"(on the next fiscal year's estimate); price/sales 9.8x. ## What the price implies - this fiscal year (ending Jan 2027): "
               f"EPS {this_eps:.2f} from 44 analysts (range 3.80 to 4.30); P/E 23.1x; earnings yield 4.3%. - next fiscal year (ending "
               f"Jan 2028): EPS {next_eps:.2f} from 43 analysts (range 4.70 to 6.20); P/E {pe}x; earnings yield {100 / pe:.1f}%. "
               f"10-year Treasury yield: 4.12% (2026-11-05).")
        item = _item(_analysis(get_valuation_context=val), "PEG")
        peg = pe / ((next_eps / this_eps - 1) * 100)
        assert item.direction == (1 if peg <= ev.PEG_CHEAP else -1 if peg >= ev.PEG_STRETCHED else 0)
        assert f"PEG {peg:.2f}" in item.text and "same consensus growth the estimates leg tests" in item.text

    @pytest.mark.parametrize("peg_pe, direction", [(16.0, 1), (24.0, 0), (32.0, -1), (50.0, -1)])
    def test_the_peg_counts_at_one_and_at_two_with_growth_of_sixteen_percent(self, peg_pe, direction):
        # growth is 16% (4.00 to 4.64), so a forward P/E of 16 is a PEG of 1.0 and 32 is a PEG of 2.0
        val = ("# Valuation context ## Where the price stands Trailing P/E (GAAP) 27.4x; forward P/E " f"{peg_pe}x (on the next "
               "fiscal year's estimate). ## What the price implies - this fiscal year (ending Jan 2027): EPS 4.00 from 44 analysts "
               f"(range 3.80 to 4.30); P/E 23.1x; earnings yield 4.3%. - next fiscal year (ending Jan 2028): EPS 4.64 from 43 "
               f"analysts (range 4.70 to 6.20); P/E {peg_pe}x; earnings yield {100 / peg_pe:.1f}%. 10-year Treasury yield: 4.12%.")
        assert _item(_analysis(get_valuation_context=val), "PEG").direction == direction

    def test_no_growth_to_adjust_for_is_neutral_and_missing_consensus_is_a_gap(self):
        shrinking = ("# Valuation context ## Where the price stands forward P/E 12.0x (on the next fiscal year's estimate). "
                     "## What the price implies - this fiscal year (ending Jan 2027): EPS 4.00 from 44 analysts (range 3.80 to 4.30); P/E 14.0x; "
                     "earnings yield 7.1%. - next fiscal year (ending Jan 2028): EPS 3.50 from 43 analysts (range 3.0 to 4.0); P/E 12.0x; "
                     "earnings yield 8.3%. 10-year Treasury yield: 4.12%.")
        item = _item(_analysis(get_valuation_context=shrinking), "PEG")
        assert item.direction == 0 and "no growth to adjust for" in item.text
        no_eps = OUTPUTS["get_valuation_context"].replace("- this fiscal year", "- this year")
        assert _item(_analysis(get_valuation_context=no_eps), "PEG").direction is None

    def test_condition_e_forgives_a_peer_premium_that_growth_pays_for_and_not_one_it_does_not(self):
        peers = OUTPUTS["get_valuation_context"] + " | Median of the similar-size peers (positive values only) | | 11.0x | 20.0x |"
        paid = _analysis(get_valuation_context=peers).conditions["e"]                    # 17.1x against 11.0x (limit 13.75x), PEG 0.49
        assert paid.status == "PASS" and "the premium to the peers is paid for by expected growth" in paid.detail
        slow = peers.replace("EPS 5.40 from 43 analysts", "EPS 4.20 from 43 analysts")      # growth 5%: PEG 3.4
        unpaid = _analysis(get_valuation_context=slow).conditions["e"]
        assert unpaid.status == "FAIL" and "does not pay for it" in unpaid.detail and "growth does not pay for" in unpaid.detail

    @staticmethod
    def _valuation(pe, this_eps, next_eps, treasury=4.86):
        return (f"# Valuation context ## Where the price stands Trailing P/E (GAAP) 30.0x; forward P/E {pe}x (on the next fiscal year's "
                f"estimate). ## What the price implies - this fiscal year (ending Jan 2027): EPS {this_eps:.2f} from 44 analysts "
                f"(range 3.80 to 4.30); P/E {pe * 1.3:.1f}x; earnings yield {100 / (pe * 1.3):.1f}%. - next fiscal year (ending Jan 2028): "
                f"EPS {next_eps:.2f} from 43 analysts (range 4.70 to 6.20); P/E {pe}x; earnings yield {100 / pe:.1f}%. "
                f"10-year Treasury yield: {treasury}% (2026-11-05).")

    def test_condition_e_lets_growth_pay_for_an_earnings_yield_a_little_below_the_treasury(self):
        """ASML at 23.8x forward earnings has a yield of 4.2% against a 4.86% Treasury, and grows earnings 36%."""
        c = _analysis(get_valuation_context=self._valuation(23.8, 10.0, 13.6)).conditions["e"]       # PEG 0.66, 0.7 points short
        assert c.status == "PASS" and "points below the Treasury yield, which expected growth pays for" in c.detail

    def test_condition_e_does_not_let_slow_growth_pay_for_a_low_yield(self):
        c = _analysis(get_valuation_context=self._valuation(23.8, 10.0, 11.5)).conditions["e"]        # PEG 1.59 (15% growth)
        assert c.status == "FAIL" and "below the Treasury yield and expected growth does not pay for it" in c.detail

    def test_condition_e_caps_how_far_below_the_treasury_growth_may_pay_for(self):
        c = _analysis(get_valuation_context=self._valuation(66.5, 10.0, 20.0)).conditions["e"]        # PEG 0.67, yield 1.5%: 3.4 short
        assert c.status == "FAIL" and "more than growth is allowed to pay for (2.0)" in c.detail

    def test_the_scorecard_tally_still_records_the_raw_yield_gap_beside_the_growth_leg(self):
        a = _analysis(get_valuation_context=self._valuation(30.0, 10.0, 13.6))        # yield 3.3% against 4.86%: 1.5 points short, PEG 0.83
        assert _item(a, "earnings yield against the 10-year Treasury").direction == -1
        assert _item(a, "PEG").direction == 1 and a.conditions["e"].status == "PASS"

    def test_condition_e_fails_a_multiple_growth_does_not_pay_for_even_when_the_peers_and_the_treasury_agree(self):
        slow = OUTPUTS["get_valuation_context"].replace("EPS 5.40 from 43 analysts", "EPS 4.20 from 43 analysts")
        c = _analysis(get_valuation_context=slow).conditions["e"]
        assert c.status == "FAIL" and "which growth does not pay for" in c.detail

    # -- every two-sided criterion turns at the same distance either side of its balance point, so a mirrored input gets the mirrored answer
    @pytest.mark.parametrize("pct, direction", [(0, 1), (25, 1), (26, 0), (50, 0), (74, 0), (75, -1), (100, -1)])
    def test_trailing_pe_against_its_own_history_turns_at_the_25th_and_75th_percentiles(self, pct, direction):
        val = OUTPUTS["get_valuation_context"].replace("higher than 18% of", f"higher than {pct}% of")
        assert _item(_analysis(get_valuation_context=val), "own 10-year history").direction == direction

    def test_a_percentile_and_its_mirror_get_mirrored_answers(self):
        for pct in (5, 20, 25, 30, 40):
            cheap = OUTPUTS["get_valuation_context"].replace("higher than 18% of", f"higher than {pct}% of")
            dear = OUTPUTS["get_valuation_context"].replace("higher than 18% of", f"higher than {100 - pct}% of")
            assert _item(_analysis(get_valuation_context=cheap), "own 10-year history").direction == -_item(
                _analysis(get_valuation_context=dear), "own 10-year history").direction

    @pytest.mark.parametrize("ratio, direction", [(0.50, 1), (0.80, 1), (0.81, 0), (1.00, 0), (1.25, 0), (1.26, -1), (2.00, -1)])
    def test_forward_pe_against_the_peers_median_turns_at_0_8_and_1_25_times_the_median(self, ratio, direction):
        peers = " | Median of the similar-size peers (positive values only) | | 10.0x | 20.0x |"
        val = self._valuation(round(ratio * 10.0, 3), 4.0, 5.4) + peers
        assert _item(_analysis(get_valuation_context=val), "its peers' median").direction == direction

    @pytest.mark.parametrize("gap, direction", [(2.0, 1), (1.0, 1), (0.9, 0), (0.1, 0), (-0.1, 0), (-0.9, 0), (-1.0, -1), (-2.0, -1)])
    def test_the_earnings_yield_against_the_treasury_turns_at_plus_and_minus_one_point(self, gap, direction):
        treasury = 4.0
        pe = 100 / (treasury + gap)
        val = self._valuation(pe, 4.0, 5.4, treasury=treasury)
        assert _item(_analysis(get_valuation_context=val), "earnings yield against").direction == direction

    @pytest.mark.parametrize("beats, direction", [((4, 4, 0), 1), ((4, 3, 0), 1), ((4, 3, 1), 0), ((4, 2, 0), 0), ((4, 2, 2), 0),
                                                  ((4, 1, 3), 0), ((4, 0, 3), -1), ((4, 0, 4), -1), ((4, 1, 1), 0)])
    def test_earnings_against_estimates_is_judged_by_beats_minus_misses_with_the_same_size_both_ways(self, beats, direction):
        n, beat, missed = beats
        text = OUTPUTS["get_earnings_facts"].replace("Of the last 4 reports, 4 beat the estimate and 0 missed",
                                                     f"Of the last {n} reports, {beat} beat the estimate and {missed} missed")
        assert _item(_analysis(get_earnings_facts=text), "against estimates").direction == direction

    def test_a_beat_record_and_its_mirror_get_mirrored_answers(self):
        for beat, missed in ((4, 0), (3, 0), (3, 1), (2, 1), (2, 2), (1, 0)):
            def rate(b, m):
                text = OUTPUTS["get_earnings_facts"].replace("Of the last 4 reports, 4 beat the estimate and 0 missed",
                                                             f"Of the last 4 reports, {b} beat the estimate and {m} missed")
                return _item(_analysis(get_earnings_facts=text), "against estimates").direction
            assert rate(beat, missed) == -rate(missed, beat), (beat, missed)

    @pytest.mark.parametrize("series, direction", [("5, 6, 7, 8", 1), ("5, 6, -1, 8", 0), ("5, -30, -8, -6", 0), ("9, -2, 8, -3", 0),
                                                   ("-5, 6, -7, -8", 0), ("-5, -6, -7, -8", -1)])
    def test_free_cash_flow_improves_when_all_four_quarters_are_positive_and_worsens_when_all_four_are_negative(self, series, direction):
        trend = _trend().replace("oldest first: 6,210, 9,480, 14,320, 19,950, 12,100.", f"oldest first: 1, {series}.")
        assert _item(_analysis(get_trend_facts=trend), "free cash flow").direction == direction

    def test_the_scorecard_text_names_the_improving_the_worsening_and_the_watch_items(self):
        trend = _trend(margin="58.7%: +8.1 pts versus a year earlier (year-over-year view); -1.7 pts versus the prior quarter",
                       debt="40,000, 2.0 times its level a year earlier (20,000)")
        line = scorecard.build_scorecard({**OUTPUTS, "get_trend_facts": trend}).split("\nJ. ")[0].split("\nI. ")[1]
        assert "Improving (" in line and "operating margin on a year ago (58.7%, +8.1 pts on a year ago)" in line
        assert "Worsening (1): long-term debt on a year ago (2.0 times its level a year ago, and cash does not cover it)" in line
        assert "Watch, named and not counted: operating margin -1.7 pts on the prior quarter (a dip of less than 3 points)" in line


# ---------------------------------------------------------------- the five conditions
@pytest.mark.unit
class TestConditions:
    def test_a_is_met_by_any_broad_index_or_the_sector_fund_or_a_high_vix(self):
        c = _analysis().conditions["a"]
        assert c.status == "PASS" and "Semiconductors (SMH) is 27.2% below its high" in c.detail and "VIX 23.8" in c.detail
        calm = OUTPUTS["get_market_context"].replace("| -6.4% |", "| -1.0% |").replace("| -11.8% |", "| -2.0% |") \
                                             .replace("| -27.2% |", "| -3.0% |").replace("above 91%", "above 50%")
        assert _analysis(get_market_context=calm).conditions["a"].status == "FAIL"
        assert _analysis(get_market_context="nothing to read here").conditions["a"].status in ("UNKNOWN", "FAIL")

    def test_b_is_met_on_the_sector_lines_when_the_s_and_p_500_alone_would_fail_it(self):
        sector_selloff = _market(sector_shock=-0.006)
        c = _analysis(get_market_context=sector_selloff).conditions["b"]
        assert c.status == "PASS" and "(met on the sector lines)" in c.detail and "Semiconductors (SMH)" in c.detail
        sp_only = sector_selloff.split("- Beta to the sector fund")[0] + "\n\n## Flags to state in the report\nNo rule-based flags."
        assert _analysis(get_market_context=sp_only).conditions["b"].status == "FAIL"

    def test_b_fails_when_the_fall_is_the_stocks_own_and_there_is_nothing_to_explain_when_it_has_not_fallen(self):
        own = _market(own_shock=-0.005)
        c = _analysis(get_market_context=own).conditions["b"]
        assert c.status == "FAIL" and "explains" in c.detail
        rising = _market(own_shock=+0.004)
        assert _analysis(get_market_context=rising).conditions["b"].status == "FAIL"
        assert "there is no fall to explain" in _analysis(get_market_context=rising).conditions["b"].detail

    def test_c_d_and_e_use_the_stock_pickers_own_checks_with_its_thresholds(self):
        a = _analysis()
        assert a.conditions["c"].status == "PASS" and "so the change is not counted" in a.conditions["c"].detail    # 2.9x, but cash covers it
        uncovered = _analysis(get_trend_facts=_trend(debt="90,000, 4.0 times its level a year earlier (22,500)"))
        assert uncovered.conditions["c"].status == "FAIL" and "total debt 4.0 times a year ago" in uncovered.conditions["c"].detail
        assert a.conditions["d"].status == "PASS" and "free cash flow over the last four quarters is positive" in a.conditions["d"].detail
        assert a.conditions["e"].status == "PASS" and "10-year Treasury 4.12%" in a.conditions["e"].detail
        clean = _analysis(get_trend_facts=_trend())
        assert clean.conditions["c"].status == "PASS"

    def test_d_passes_on_the_numbers_when_cash_covers_all_the_debt_so_unread_filings_do_not_block_a_foreign_filer(self):
        """TSM files 20-F and 6-K, which the filing tool does not read: cash about 4 times its debt made (d) an 'unestablished gap'."""
        a = _analysis()                                      # cash 31,870 against long-term debt 21,540
        d = a.conditions["d"]
        assert d.status == "PASS" and a.survival_by_cash is True
        assert "cash covers all long-term debt 1.5 times, so nothing can fall due that cash cannot pay" in d.detail
        text = scorecard.build_scorecard(OUTPUTS)
        assert "going-concern language and debt maturities are not needed here: cash covers all the debt" in text
        assert "going-concern language, when debt falls due" not in text.split("\nI. ")[0].split("\nH. ")[1]

    def test_d_still_asks_for_the_maturities_and_going_concern_text_when_cash_does_not_cover_the_debt_or_is_unknown(self):
        short = _analysis(get_trend_facts=_trend(debt="90,000, 4.0 times its level a year earlier (22,500)"))
        assert short.conditions["d"].status == "PASS" and short.survival_by_cash is False       # free cash flow is positive
        line = scorecard.build_scorecard({**OUTPUTS, "get_trend_facts": _trend(debt="90,000, 4.0 times its level a year earlier (22,500)")})
        h = line.split("\nI. ")[0].split("\nH. ")[1]
        assert "Not checked by code: going-concern language, when debt falls due, whether a filing is a financing" in h
        assert "cash does not cover all the debt, or is unknown" in h and "unread, they are a data gap" in h
        no_cash = _trend().replace("- Cash and short-term investments: 31,870 versus 27,400 a year earlier.", "")
        assert _analysis(get_trend_facts=no_cash).survival_by_cash is False

    def test_a_failed_survival_test_is_not_rescued_by_cash_that_covers_the_debt(self):
        # free cash flow negative over the year and cash smaller than twelve months of burn: (d) fails whatever the debt is
        burning = _trend().replace("oldest first: 6,210, 9,480, 14,320, 19,950, 12,100.", "oldest first: -6,210, -9,480, -14,320, -19,950, -12,100.") \
                          .replace("Cash and short-term investments: 31,870", "Cash and short-term investments: 30,000")
        a = _analysis(get_trend_facts=burning)
        assert a.conditions["d"].status == "FAIL" and a.survival_by_cash is False

    def test_a_condition_the_tools_could_not_show_is_unknown_and_a_data_gap_not_a_finding(self):
        a = ev.analyze({k: v for k, v in OUTPUTS.items() if k not in ("get_trend_facts", "get_valuation_context")})
        assert a.conditions["d"].status == "UNKNOWN" and a.conditions["e"].status == "UNKNOWN"
        assert any(g.startswith("condition (d):") for g in a.gaps) and any(g.startswith("condition (e):") for g in a.gaps)
        text = scorecard.build_scorecard({k: v for k, v in OUTPUTS.items() if k != "get_trend_facts"})
        assert "UNKNOWN (our data gap, counts as not met)" in text


# ---------------------------------------------------------------- the three kinds of missing
@pytest.mark.unit
class TestThreeKindsOfMissing:
    LATE = OUTPUTS["get_filing_events"] + (" ## Offerings, late notices and periodic reports - Late-filing notice: NT 10-K filed 2026-09-10."
                                           " ## Flags to state in the report - A late-filing notice (NT 10-K) was filed on 2026-09-10 (within the last year).")

    def test_what_the_company_withheld_counts_against_it_whatever_the_filing_says(self):
        a = _analysis(get_filing_events=self.LATE)
        assert a.withheld == ["late-filing notice: NT 10-K filed 2026-09-10"]
        assert any(i.group == "event" and i.direction == -1 for i in a.items)
        text = scorecard.build_scorecard({**OUTPUTS, "get_filing_events": self.LATE})
        assert "J. Disclosure and data gaps: Withheld or serious (count against the company): late-filing notice: NT 10-K filed 2026-09-10" in text

    def test_a_non_reliance_notice_a_delisting_notice_and_a_bankruptcy_are_serious_but_an_auditor_change_is_only_named(self):
        flags = ("## Flags to state in the report - Distress-type Form 8-K items in the last 180 days: 4.02 (Non-reliance on previously "
                 "issued financial statements) on 2026-09-01; 3.01 (Notice of delisting or failure to meet a listing standard) on "
                 "2026-10-01; 4.01 (Change of auditor) on 2026-08-20.")
        a = _analysis(get_filing_events=OUTPUTS["get_filing_events"] + " " + flags)
        assert [w for w in a.withheld if w.startswith("8-K item 4.02")] and [w for w in a.withheld if w.startswith("8-K item 3.01")]
        assert not any("4.01" in w for w in a.withheld)
        assert [w for w in a.watch_events if "4.01 (Change of auditor)" in w]
        assert "Filings named, not counted: 8-K item 4.01 (Change of auditor) on 2026-08-20" in scorecard.build_scorecard(
            {**OUTPUTS, "get_filing_events": OUTPUTS["get_filing_events"] + " " + flags})

    def test_routine_filings_are_not_withheld_information(self):
        a = _analysis()
        assert a.withheld == [] and a.watch_events == []                       # financing items stay "unresolved", as before

    def test_what_we_could_not_see_counts_neither_way_and_is_listed_as_our_gap(self):
        a = ev.analyze({k: v for k, v in OUTPUTS.items() if k != "get_earnings_facts"})
        for name in ("this year's EPS", "next year's EPS", "against estimates"):
            assert _item(a, name).direction is None
        assert a.withheld == []
        assert any(g.startswith("this year's EPS estimates:") for g in a.gaps)
        text = scorecard.build_scorecard({k: v for k, v in OUTPUTS.items() if k != "get_earnings_facts"})
        assert "Our data gaps (neutral for the direction of the rating, they block a deal call and lower confidence): " in text

    def test_a_missing_filing_tool_is_a_gap_in_the_check_not_a_clean_bill_of_health(self):
        a = ev.analyze({k: v for k, v in OUTPUTS.items() if k != "get_filing_events"})
        assert any(g.startswith("SEC filing events") for g in a.gaps) and a.withheld == []

    def test_a_gap_never_lowers_the_mechanical_rating_but_a_withheld_event_does(self):
        full = ev.mechanical_check(_analysis())
        gappy = ev.mechanical_check(ev.analyze({k: v for k, v in OUTPUTS.items() if k != "get_filing_events"}))
        late = ev.mechanical_check(_analysis(get_filing_events=self.LATE))
        assert gappy["score"] == full["score"] and gappy["gaps"] == full["gaps"] + 1
        assert late["score"] == pytest.approx(full["score"] - ev.EVENT_PENALTY)


# ---------------------------------------------------------------- the mechanical cross-check
def _items(business, valuation, events=0):
    out = [ev.Item("business", f"b{i}", d, "") for i, d in enumerate(business)]
    out += [ev.Item("valuation", f"v{i}", d, "") for i, d in enumerate(valuation)]
    a = ev.Analysis(items=out, readable=True)
    a.withheld = ["x"] * events
    return a


@pytest.mark.unit
class TestMechanicalCheck:
    @pytest.mark.parametrize("business, valuation, rating", [
        ([1] * 6, [1, 1], "Buy"), ([1] * 6, [0, 0], "Overweight"), ([1, 0, 0, 0, 0, 0], [0], "Hold"),
        ([0] * 6, [0], "Hold"), ([-1] * 6, [0, 0], "Underweight"), ([-1] * 6, [-1, -1], "Sell"), ([1] * 4 + [-1] * 4, [1], "Overweight"),
    ])
    def test_fixed_weights_give_the_rating(self, business, valuation, rating):
        assert ev.mechanical_check(_items(business, valuation))["rating"] == rating

    def test_the_two_groups_weigh_the_same_whatever_their_sizes(self):
        a = ev.mechanical_check(_items([1] * 10, [-1]))
        assert a["score"] == 0.0 and a["rating"] == "Hold"

    def test_too_few_criteria_give_no_rating_rather_than_a_guess(self):
        assert ev.mechanical_check(_items([1, 1, 1], [1])) is None
        assert ev.mechanical_check(_items([1] * 6, [])) is None
        assert ev.mechanical_check(_items([1] * 6, [None, None])) is None
        assert ev.mechanical_check(ev.Analysis()) is None

    def test_a_serious_withheld_event_lowers_the_score_once_whatever_the_count(self):
        assert ev.mechanical_check(_items([1] * 6, [1], events=1))["score"] == pytest.approx(1 - ev.EVENT_PENALTY)
        assert ev.mechanical_check(_items([1] * 6, [1], events=3))["score"] == pytest.approx(1 - ev.EVENT_PENALTY)

    def test_the_checklist_rating_is_kept_out_of_the_scorecard_the_debaters_read(self):
        text = scorecard.build_scorecard(OUTPUTS)
        mech = ev.mechanical_check(_analysis())
        assert "cross-check" not in text.lower() and "checklist" not in text.lower() and "mechanical" not in text.lower()
        assert f"{mech['score']:+.2f}" not in text

    def test_the_report_text_sets_the_checklist_beside_the_final_rating_and_flags_two_steps(self):
        mech = {"rating": "Overweight", "score": 0.35, "business": 0.7, "business_n": 10, "valuation": 0.0, "valuation_n": 3,
                "withheld": [], "gaps": 0}
        agree = ev.cross_check_text(mech, "Overweight")
        assert "rates this company Overweight (score +0.35" in agree and "final rating, Overweight, agrees" in agree
        one = ev.cross_check_text(mech, "Hold")
        assert "is 1 step below it" in one and "A gap of two steps or more" not in one
        two = ev.cross_check_text(mech, "Underweight")
        assert "is 2 steps below it" in two and "A gap of two steps or more" in two and "which way the debate leans" in two
        above = ev.cross_check_text({**mech, "rating": "Hold"}, "Buy")
        assert "is 2 steps above it" in above
        assert ev.cross_check_text(None, "Hold") == "" and "final rating" not in ev.cross_check_text(mech, None)
        assert "(2 inputs were data gaps on our side.)" in ev.cross_check_text({**mech, "gaps": 2}, "Hold")
        assert "(1 input was a data gap on our side.)" in ev.cross_check_text({**mech, "gaps": 1}, "Hold")

    def test_the_graph_node_stores_the_checklist_in_the_state_and_never_in_the_scorecard(self, monkeypatch):
        monkeypatch.setattr(scorecard, "collect_outputs", lambda ticker, date: OUTPUTS)
        monkeypatch.delenv(scorecard.DISABLE_ENV, raising=False)
        out = scorecard.create_scorecard_node()({"company_of_interest": "EXMP", "trade_date": "2026-11-07"})
        assert out["mechanical_check"]["rating"] in ev.SCALE and "H. Rule check" in out["scorecard"]
        assert "mechanical" not in out["scorecard"].lower()

    def test_a_failure_in_the_evidence_code_never_stops_the_scorecard(self, monkeypatch):
        monkeypatch.setattr(ev, "scorecard_lines", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
        text = scorecard.build_scorecard(OUTPUTS)
        assert "A. The market" in text and "H. Rule check" not in text

    def test_nothing_readable_gives_no_extra_lines_and_no_checklist(self):
        junk = {name: f"{name} data" for name in scorecard.SOURCES}
        assert ev.analyze(junk).readable is False and ev.scorecard_lines(ev.analyze(junk)) == []
        assert ev.mechanical_check(ev.analyze(junk)) is None


@pytest.mark.unit
class TestInTheReport:
    def test_the_report_has_a_cross_check_section_that_names_the_final_rating(self, tmp_path):
        from tradingagents.reporting import write_report_tree

        state = {"final_trade_decision": "**Rating**: Hold\n\n**Executive Summary**: x", "scorecard": "A. The market: x",
                 "investment_plan": "RM PLAN",
                 "mechanical_check": {"rating": "Buy", "score": 0.8, "business": 0.6, "business_n": 10, "valuation": 1.0,
                                      "valuation_n": 2, "withheld": [], "gaps": 0}}
        out = write_report_tree(state, "EXMP", tmp_path, html=False)
        text = (tmp_path / "2_research" / "mechanical.md").read_text(encoding="utf-8")
        assert "rates this company Buy (score +0.80" in text and "final rating, Hold, is 2 steps below it" in text
        whole = out.read_text(encoding="utf-8")
        assert whole.index("Scorecard") < whole.index("Mechanical cross-check") < whole.index("Research Manager")

    def test_a_run_without_a_checklist_writes_no_such_file(self, tmp_path):
        from tradingagents.reporting import write_report_tree

        write_report_tree({"investment_plan": "RM PLAN", "final_trade_decision": "**Rating**: Hold"}, "EXMP", tmp_path, html=False)
        assert not (tmp_path / "2_research" / "mechanical.md").exists()

    def test_the_html_report_carries_the_section_too(self, tmp_path):
        from tradingagents.reporting import write_report_tree

        state = {"final_trade_decision": "**Rating**: Hold", "investment_plan": "RM PLAN",
                 "mechanical_check": {"rating": "Hold", "score": 0.0, "business": 0.0, "business_n": 5, "valuation": 0.0,
                                      "valuation_n": 1, "withheld": [], "gaps": 0}}
        write_report_tree(state, "EXMP", tmp_path, html=True)
        assert "Mechanical cross-check" in (tmp_path / "complete_report.html").read_text(encoding="utf-8")
