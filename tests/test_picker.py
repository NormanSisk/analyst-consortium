"""The stock picker: settings, facts, the price-only pass, the five conditions, the funnel and its ranking, the cache, the
ledger and the report. All on made-up data (see picker_world.py); no network."""

import json
import re
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from tests import picker_world as w
from tradingagents.dataflows.vendors.yahoo import market_context as mc
from tradingagents.picker import conditions as cond
from tradingagents.picker import ledger, quicklook, report
from tradingagents.picker.cache import FundamentalsCache
from tradingagents.picker.facts import Estimate, Facts, build_facts
from tradingagents.picker.settings import PickerSettings
from tradingagents.picker.stage_a import (
    MarketState,
    PriceMetrics,
    Window,
    explained_share,
    market_label,
    market_state,
    price_metrics,
    sector_windows,
)

S = PickerSettings()


# ---------------------------------------------------------------------------------------------- settings

@pytest.mark.unit
class TestSettings:
    def test_the_defaults_are_valid_and_match_the_analysis_thresholds(self):
        assert S.validated() is S
        assert (S.share_63, S.share_21, S.below_high, S.top, S.min_market_cap) == (0.60, 0.45, 0.20, 15, 2e9)

    def test_overrides_replace_only_what_is_given(self):
        s = PickerSettings.with_overrides(below_high=0.30, top=5, share_63=None)
        assert (s.below_high, s.top, s.share_63) == (0.30, 5, 0.60)

    def test_an_unknown_setting_is_an_error(self):
        with pytest.raises(ValueError, match="unknown picker setting: nonsense"):
            PickerSettings.with_overrides(nonsense=1)

    @pytest.mark.parametrize("name, value", [
        ("below_high", 1.5), ("below_high", -0.1), ("share_63", 2), ("top", 0), ("top", 9999), ("carry", 0), ("universe_limit", 5),
        ("min_market_cap", -1), ("runway_months", -3), ("max_pe_vs_peers", 0), ("low_pe_flag", 500), ("top", True), ("below_high", "20%"),
    ])
    def test_out_of_range_values_are_refused_by_name(self, name, value):
        with pytest.raises(ValueError, match=name):
            PickerSettings.with_overrides(**{name: value})

    def test_only_the_us_is_supported(self):
        with pytest.raises(ValueError, match="US-only"):
            PickerSettings.with_overrides(region="uk")


# ---------------------------------------------------------------------------------------------- facts

@pytest.mark.unit
class TestFacts:
    def test_a_healthy_company_is_read_in_full(self):
        f = build_facts(w.raw("AAA"))
        assert (f.symbol, f.name, f.sector, f.industry, f.currency) == ("AAA", "AAA Corp", "Technology", "Semiconductors", "USD")
        assert f.op_margin == pytest.approx(0.25) and f.op_margin_year_ago == pytest.approx(0.25)
        assert (f.shares, f.shares_year_ago, f.cash, f.debt, f.debt_year_ago) == (1000, 1000, 500, 100, 100)
        assert f.fcf_quarters == [20, 18, 17, 16] and f.ocf == 30 and f.ocf_year_ago == 26
        assert f.fy1.up == 6 and f.fy1.down == 1 and f.fy1.change_90d == pytest.approx(12 / 11 - 1) and f.fy1.growth == 0.2
        assert f.fy0.change_90d == pytest.approx(10 / 9.5 - 1) and f.analysts == 20 and f.latest_quarter == "2026-09-30"
        assert f.financial is False and f.forward_pe == 12.0

    def test_missing_pieces_are_none_never_an_error(self):
        f = build_facts({})
        assert f.symbol == "" and f.op_margin is None and f.cash is None and f.fcf_quarters == [] and f.fy1 == Estimate()
        assert build_facts({"symbol": "X", "info": {"forwardPE": "n/a", "marketCap": True}}).forward_pe is None

    def test_the_year_ago_figure_needs_a_quarter_a_year_back(self):
        r = w.raw()
        for part in ("income", "cashflow", "balance"):
            for label, series in r[part].items():
                r[part][label] = {k: v for k, v in series.items() if k >= "2026-03-31"}      # only three quarters
        f = build_facts(r)
        assert f.op_margin == pytest.approx(0.25) and f.op_margin_year_ago is None and f.ocf_year_ago is None and f.shares_year_ago is None

    def test_a_bank_is_marked_financial_and_cash_falls_back_to_the_plain_cash_line(self):
        r = w.raw(sector="Financial Services")
        del r["balance"]["Cash Cash Equivalents And Short Term Investments"]
        r["balance"]["Cash And Cash Equivalents"] = {"2026-09-30": 77.0}
        f = build_facts(r)
        assert f.financial is True and f.cash == 77.0

    def test_an_estimate_with_no_90_day_base_has_no_change(self):
        r = w.raw()
        r["eps_trend"]["+1y"]["90daysAgo"] = -2.0
        assert build_facts(r).fy1.change_90d is None

    def test_bad_dates_and_values_are_ignored_and_errors_are_carried(self):
        r = w.raw()
        r["income"]["Total Revenue"]["not-a-date"] = 5.0
        r["income"]["Total Revenue"]["2026-09-30"] = "oops"
        r["errors"] = ["info: OSError"]
        f = build_facts(r)
        assert f.errors == ["info: OSError"]


# ---------------------------------------------------------------------------------------------- the price-only pass

@pytest.mark.unit
class TestExplainedShare:
    @pytest.mark.parametrize("stock, implied, expected", [
        (-0.10, -0.07, (True, 0.7)), (-0.05, -0.10, (True, 2.0)), (-0.10, 0.02, (True, 0.0)), (-0.10, 0.0, (True, 0.0)),
        (-0.02, -0.05, (False, None)), (0.05, -0.05, (False, None)), (None, -0.05, (False, None)), (-0.10, None, (False, None)),
    ])
    def test_the_rule(self, stock, implied, expected):
        fell, share = explained_share(stock, implied)
        assert fell == expected[0] and (share == pytest.approx(expected[1]) if expected[1] is not None else share is None)

    def test_a_fall_of_exactly_three_percent_counts(self):
        assert explained_share(-mc.MIN_SPLIT_MOVE - 1e-9, -0.01)[0] is True


@pytest.mark.unit
class TestPriceMetrics:
    def test_a_stock_that_follows_the_market_at_twice_its_beta(self):
        m = w.market_returns()
        metrics = price_metrics(w.levels(2 * m), w.levels(m))
        assert metrics.beta == pytest.approx(2.0, abs=1e-6) and metrics.r2 == pytest.approx(1.0)
        assert metrics.w63.fell and metrics.w63.share == pytest.approx(1.07, abs=0.03) and metrics.w21.share == pytest.approx(1.01, abs=0.03)
        assert metrics.off_high <= -0.20 and metrics.as_of == w.AS_OF and metrics.price == pytest.approx(float(w.levels(2 * m).iloc[-1]))

    def test_a_stock_that_fell_on_its_own(self):
        m = w.market_returns()
        own = np.concatenate([w.base_returns(), np.full(63, -0.012)])
        metrics = price_metrics(w.levels(own), w.levels(m))
        assert metrics.w63.fell and metrics.w63.share < 0.5 and metrics.w21.share < 0.5

    def test_a_stock_that_did_not_fall_has_no_share(self):
        m = w.market_returns()
        flat = np.concatenate([w.base_returns(), np.full(63, 0.001)])
        metrics = price_metrics(w.levels(flat), w.levels(m))
        assert not metrics.w63.fell and metrics.w63.share is None and metrics.off_high > -0.20

    def test_too_little_history_gives_nothing(self):
        assert price_metrics(w.levels(w.market_returns()[-50:]), w.levels(w.market_returns())) is None
        assert price_metrics(None, w.levels(w.market_returns())) is None

    def test_a_market_that_never_varies_gives_no_beta_and_unknown_windows(self):
        flat_market = pd.Series(100.0, index=w.INDEX)
        metrics = price_metrics(w.levels(w.market_returns()), flat_market)
        assert metrics.beta is None and not metrics.w63.fell and metrics.w63.share is None
        assert cond.explained_condition(metrics, S).status == cond.UNKNOWN

    def test_the_share_is_the_one_the_analysis_prints(self):
        """The picker and the analysis use one piece of arithmetic, so what they say the market explains cannot differ."""
        m = w.market_returns()
        own = np.concatenate([w.base_returns(), np.full(63, -0.012)])
        stock, market = w.levels(own), w.levels(m)
        reg = mc.regression(stock, market)
        for sessions in (63, 21):
            part = mc.attribution(stock, market, reg["beta"], sessions)
            fell, share = explained_share(part["stock"], part["implied"])
            assert fell and share < 1
            printed = re.search(r"About (\d+)%", mc._reading(part))
            assert printed and int(printed.group(1)) == round(share * 100)
        assert price_metrics(stock, market).w63.share == pytest.approx(explained_share(*(lambda p: (p["stock"], p["implied"]))(mc.attribution(stock, market, reg["beta"], 63)))[1])

    def test_the_public_names_are_the_analysis_functions(self):
        assert mc.regression is mc._regression and mc.attribution is mc._attribution
        assert mc.drawdown is mc._drawdown and mc.move is mc._move


@pytest.mark.unit
class TestMarketState:
    @pytest.mark.parametrize("last, label", [(99, "near its 52-week high"), (94, "pullback"), (89, "correction"), (79, "bear market")])
    def test_the_drawdown_labels(self, last, label):
        series = pd.Series([100.0] * 100 + [float(last)], index=pd.bdate_range(end="2026-11-06", periods=101))
        state = market_state(series, None)
        assert state.label == label == market_label(state.drawdown) and state.vix is None and state.vix_rank is None

    def test_the_vix_rank_is_the_share_of_the_year_it_is_above(self):
        state = market_state(w.levels(w.market_returns()), w.vix(True))
        assert state.vix == 31.0 and state.vix_rank == pytest.approx(251 / 252)
        assert market_state(w.levels(w.market_returns()), w.vix(False)).vix_rank == 0.0

    def test_a_short_vix_history_is_ignored(self):
        assert market_state(w.levels(w.market_returns()), w.vix(True).iloc[-30:]).vix is None

    def test_sector_windows(self):
        m = w.market_returns()
        both = sector_windows(w.levels(2 * m), w.levels(1.8 * m))
        assert both is not None and both[0].fell and both[0].share > 0.9
        assert sector_windows(w.levels(m), pd.Series(100.0, index=w.INDEX)) is None


# ---------------------------------------------------------------------------------------------- the conditions

def metrics_with(w63=(-0.2, -0.15, True, 0.75), w21=(-0.1, -0.06, True, 0.6), beta=1.2) -> PriceMetrics:
    return PriceMetrics(price=100.0, as_of=w.AS_OF, off_high=-0.3, beta=beta, r2=0.5, w63=Window(63, *w63), w21=Window(21, *w21))


@pytest.mark.unit
class TestMoney:
    @pytest.mark.parametrize("value, currency, expected", [
        (4.02e12, "", "4.0T"), (15.8e9, "USD", "15.8B"), (554e6, "", "554.0M"), (12_300, "", "12.3K"), (999, "", "999"),
        (-2.5e9, "", "-2.5B"), (1.5e12, "TWD", "1.5T TWD"), (None, "", "n/a"),
    ])
    def test_formats(self, value, currency, expected):
        assert cond.money(value, currency) == expected


@pytest.mark.unit
class TestMarketCondition:
    def test_a_pullback_passes(self):
        c = cond.market_condition(MarketState(-0.057, "pullback", 25.6, 0.95))
        assert c.status == cond.PASS and "5.7% below its high" in c.detail and "VIX 25.6" in c.detail and c.met and c.title == "Market in a drawdown"

    def test_a_calm_market_with_a_high_vix_passes(self):
        assert cond.market_condition(MarketState(-0.01, "near its 52-week high", 31, 0.97)).status == cond.PASS

    def test_a_calm_market_fails(self):
        c = cond.market_condition(MarketState(-0.01, "near its 52-week high", 14, 0.2))
        assert c.status == cond.FAIL and not c.met and "only 1.0%" in c.detail


@pytest.mark.unit
class TestExplainedCondition:
    def test_both_windows_clear_their_bars(self):
        c = cond.explained_condition(metrics_with(), S)
        assert c.status == cond.PASS and "75%" in c.detail and "needs 60%" in c.detail and "needs 45%" in c.detail

    def test_a_share_over_a_hundred_percent_is_more_than_all(self):
        c = cond.explained_condition(metrics_with(w63=(-0.05, -0.16, True, 3.2)), S)
        assert c.status == cond.PASS and "more than all" in c.detail

    def test_one_window_short_of_its_bar_fails(self):
        assert cond.explained_condition(metrics_with(w21=(-0.1, -0.03, True, 0.30)), S).status == cond.FAIL
        assert cond.explained_condition(metrics_with(w63=(-0.2, -0.05, True, 0.25)), S).status == cond.FAIL

    def test_a_window_with_no_fall_is_ignored(self):
        c = cond.explained_condition(metrics_with(w63=(0.04, -0.05, False, None)), S)
        assert c.status == cond.PASS and "no fall of 3% or more" in c.detail

    def test_no_fall_anywhere_fails(self):
        c = cond.explained_condition(metrics_with(w63=(0.04, 0.02, False, None), w21=(0.0, 0.0, False, None)), S)
        assert c.status == cond.FAIL and "no fall to explain" in c.detail

    def test_the_market_not_falling_explains_nothing(self):
        assert cond.explained_condition(metrics_with(w63=(-0.2, 0.01, True, 0.0)), S).status == cond.FAIL

    def test_the_thresholds_come_from_the_settings(self):
        strict = PickerSettings.with_overrides(share_63=0.9)
        assert cond.explained_condition(metrics_with(), strict).status == cond.FAIL

    def test_no_metrics_or_no_beta_is_unknown(self):
        assert cond.explained_condition(None, S).status == cond.UNKNOWN
        assert cond.explained_condition(metrics_with(beta=None), S).status == cond.UNKNOWN


def facts(**changes) -> Facts:
    f = build_facts(w.raw())
    for key, value in changes.items():
        setattr(f, key, value)
    return f


@pytest.mark.unit
class TestDeteriorationCondition:
    def test_a_healthy_company_passes_with_its_numbers(self):
        c = cond.deterioration_condition(facts(), S)
        assert c.status == cond.PASS and "6 raised, 1 cut" in c.detail and "operating margin 25.0%" in c.detail and "cash flow +15%" in c.detail

    @pytest.mark.parametrize("change, words", [
        ({"fy1": Estimate(up=1, down=5, change_90d=0.01)}, "next year's EPS estimate: 5 analysts cut and 1 raised"),
        ({"fy0": Estimate(up=2, down=2, change_90d=-0.05)}, "consensus EPS is -5.0% over 90 days"),
        ({"op_margin": 0.20}, "operating margin 20.0%, down 5.0 points"),
        ({"ocf": 18.0}, "operating cash flow -31% on a year ago"),
        ({"debt": 250.0}, "total debt 2.5 times a year ago"),
        ({"shares": 1060.0}, "share count up 6.0% in a year"),
    ])
    def test_each_kind_of_deterioration_fails_and_is_named(self, change, words):
        c = cond.deterioration_condition(facts(**change), S)
        assert c.status == cond.FAIL and words in c.detail

    def test_several_flags_are_all_listed(self):
        c = cond.deterioration_condition(facts(ocf=10.0, debt=300.0), S)
        assert c.status == cond.FAIL and "operating cash flow" in c.detail and "total debt" in c.detail

    def test_a_small_change_is_not_deterioration(self):
        assert cond.deterioration_condition(facts(op_margin=0.23, ocf=24.0, debt=150.0, shares=1020.0), S).status == cond.PASS

    def test_too_little_data_is_unknown(self):
        assert cond.deterioration_condition(Facts(symbol="X"), S).status == cond.UNKNOWN

    def test_a_bank_is_judged_on_estimates_and_shares_only(self):
        bank = facts(financial=True, op_margin=0.01, ocf=1.0, debt=9999.0)
        assert cond.deterioration_condition(bank, S).status == cond.PASS
        assert cond.deterioration_condition(facts(financial=True, shares=1100.0), S).status == cond.FAIL

    def test_the_limits_come_from_the_settings(self):
        loose = PickerSettings.with_overrides(margin_drop_points=10)
        assert cond.deterioration_condition(facts(op_margin=0.20), loose).status == cond.PASS


@pytest.mark.unit
class TestSurvivalCondition:
    def test_positive_free_cash_flow_passes(self):
        c = cond.survival_condition(facts(), S)
        assert c.status == cond.PASS and "positive" in c.detail and "cash 500 against debt 100" in c.detail

    def test_negative_free_cash_flow_with_a_long_runway_passes(self):
        c = cond.survival_condition(facts(fcf_quarters=[-10, -10, -10, -10], cash=120.0), S)       # burn 40 a year: 36 months of cash
        assert c.status == cond.PASS and "36 months" in c.detail

    def test_a_short_runway_fails(self):
        c = cond.survival_condition(facts(fcf_quarters=[-10, -10, -10, -10], cash=20.0), S)
        assert c.status == cond.FAIL and "only 6 months" in c.detail

    def test_a_run_of_equity_sales_fails_even_with_cash_flow(self):
        c = cond.survival_condition(facts(shares=1150.0), S)
        assert c.status == cond.FAIL and "a run of equity sales" in c.detail

    def test_unknowns(self):
        assert cond.survival_condition(facts(fcf_quarters=[1, 2, 3]), S).status == cond.UNKNOWN
        assert cond.survival_condition(facts(financial=True), S).status == cond.UNKNOWN
        assert cond.survival_condition(facts(fcf_quarters=[-5, -5, -5, -5], cash=None), S).status == cond.UNKNOWN

    def test_other_currencies_are_named(self):
        c = cond.survival_condition(facts(currency="TWD", fcf_quarters=[5e11, 5e11, 5e11, 5e11], cash=4e12, debt=9.8e11), S)
        assert "2.0T TWD" in c.detail and "4.0T TWD" in c.detail


@pytest.mark.unit
class TestValuationCondition:
    def test_cheap_against_bonds_and_peers_passes(self):
        c = cond.valuation_condition(facts(forward_pe=12.0), 15.0, "Semiconductors (38 peers)", 4.8, S)
        assert c.status == cond.PASS and "earnings yield 8.3%" in c.detail and "allowed up to 18.8x" in c.detail

    def test_an_earnings_yield_below_the_treasury_fails(self):
        c = cond.valuation_condition(facts(forward_pe=25.0), 40.0, "its peers", 4.8, S)
        assert c.status == cond.FAIL and "below the Treasury yield" in c.detail

    def test_a_p_e_far_above_peers_fails_even_if_the_yield_is_fine(self):
        c = cond.valuation_condition(facts(forward_pe=20.0), 12.0, "its peers", 4.0, S)
        assert c.status == cond.FAIL and "above its peers median by more than 25%" in c.detail and "Treasury" not in c.detail.split("; ")[-1]

    def test_a_p_e_just_inside_the_allowance_passes(self):
        assert cond.valuation_condition(facts(forward_pe=18.7), 15.0, "its peers", 4.0, S).status == cond.PASS

    def test_a_loss_or_no_p_e(self):
        assert cond.valuation_condition(facts(forward_pe=-3.0), 10, "p", 4.8, S).status == cond.FAIL
        assert cond.valuation_condition(facts(forward_pe=None), 10, "p", 4.8, S).status == cond.UNKNOWN

    def test_nothing_to_compare_with_is_unknown_and_one_reference_is_enough(self):
        assert cond.valuation_condition(facts(forward_pe=12.0), None, "", None, S).status == cond.UNKNOWN
        assert cond.valuation_condition(facts(forward_pe=12.0), None, "", 4.8, S).status == cond.PASS
        assert cond.valuation_condition(facts(forward_pe=12.0), 15.0, "p", None, S).status == cond.PASS

    def test_the_allowance_comes_from_the_settings(self):
        tight = PickerSettings.with_overrides(max_pe_vs_peers=1.0)
        assert cond.valuation_condition(facts(forward_pe=16.0), 15.0, "p", 4.0, tight).status == cond.FAIL


# ---------------------------------------------------------------------------------------------- the funnel

def run_beaten(provider=None, settings=S, **kw):
    return quicklook.run("beaten-down", provider or w.pullback_world(), settings, **kw)


@pytest.mark.unit
class TestBeatenDownRecipe:
    def test_the_funnel_counts_every_step(self):
        result = run_beaten()
        assert dict(result.funnel) == {
            "Stocks to look at": 5, "With enough price history": 4, "At least 20% below the 52-week high": 3,
            "Market explains the fall (the analysis's thresholds)": 2, "Fundamentals checked": 2,
            "Meeting at least 4 of 5 conditions": 2, "Meeting all 5": 1}
        assert result.as_of == w.AS_OF and result.market.label in ("pullback", "correction", "bear market") and result.treasury == 4.8
        assert any("1 stocks had too little price history" in x or "stocks had too little price history" in x for x in result.warnings)

    def test_the_good_stock_meets_all_five_and_the_deteriorating_one_does_not(self):
        rows = {r.symbol: r for r in run_beaten().rows}
        assert set(rows) == {"GOOD", "BAD"}
        assert rows["GOOD"].met == 5 and all(c.met for c in rows["GOOD"].conditions.values())
        assert rows["BAD"].met == 4 and rows["BAD"].conditions["c"].status == cond.FAIL
        assert rows["BAD"].conditions["a"].met and rows["BAD"].conditions["b"].met
        assert [c.code for c in rows["BAD"].failed] == ["c"] and rows["GOOD"].failed == []

    def test_the_ranking_puts_the_most_conditions_first(self):
        assert [r.symbol for r in run_beaten().rows] == ["GOOD", "BAD"]

    def test_a_stock_that_fell_on_its_own_or_barely_fell_is_screened_out(self):
        symbols = {r.symbol for r in run_beaten().rows}
        assert "OWN" not in symbols and "FLAT" not in symbols and "SHORT" not in symbols

    def test_the_sector_fund_is_used_to_flag_a_sector_selloff(self):
        row = run_beaten().rows[0]
        assert row.sector_label == "Semiconductors (SMH)" and row.sector_windows is not None and row.sector_driven is True

    def test_peers_are_the_industry_when_there_are_enough_of_them(self):
        row = run_beaten().rows[0]
        assert row.peers == "Semiconductors (6 peers)" and "median 12.5x" in row.conditions["e"].detail

    def test_the_sector_is_used_when_the_industry_has_too_few_peers(self):
        provider = w.pullback_world()
        provider._industry_pes = {"Semiconductors": [10, 11]}
        provider._universe = [w.record(s, forward_pe=pe) for s, pe in zip(("GOOD", "OWN", "FLAT", "SHORT", "BAD"), (12, 14, 16, 18, 20))]
        row = run_beaten(provider).rows[0]
        assert row.peers.startswith("Technology sector (5 stocks)")

    def test_a_very_low_forward_p_e_is_flagged_but_still_judged_by_the_rules(self):
        provider = w.pullback_world()
        provider._raws["GOOD"] = w.raw("GOOD", forwardPE=4.0)
        row = next(r for r in run_beaten(provider).rows if r.symbol == "GOOD")
        assert row.low_pe is True and row.conditions["e"].status == cond.PASS
        assert next(r for r in run_beaten().rows if r.symbol == "GOOD").low_pe is False

    def test_the_carry_limit_keeps_the_deepest_and_says_so(self):
        result = run_beaten(settings=PickerSettings.with_overrides(carry=1))
        assert len(result.rows) == 1 and any("only the 1 deepest" in x for x in result.warnings)

    def test_a_calm_market_warns_that_condition_a_cannot_be_met(self):
        result = quicklook.run("list", w.calm_world(), S, symbols=["GOOD"])
        assert result.rows[0].conditions["a"].status == cond.FAIL and result.rows[0].met <= 4
        assert any("market is not in a drawdown" in x for x in result.warnings)

    def test_no_treasury_yield_is_reported_and_peers_alone_are_used(self):
        provider = w.pullback_world()
        provider._treasury = None
        result = run_beaten(provider)
        assert result.treasury is None and any("10-year Treasury yield could not be fetched" in x for x in result.warnings)
        assert result.rows[0].conditions["e"].status == cond.PASS

    def test_a_stock_whose_download_failed_is_kept_with_a_note_and_unknowns(self):
        provider = w.pullback_world()
        provider._fail = {"GOOD"}
        row = next(r for r in run_beaten(provider).rows if r.symbol == "GOOD")
        assert "data trouble" in row.note and row.conditions["c"].status == cond.UNKNOWN and row.met < 5

    def test_nothing_surviving_the_price_screen_gives_an_empty_answer(self):
        result = run_beaten(settings=PickerSettings.with_overrides(below_high=0.9))
        assert result.rows == [] and result.funnel[-1][1] == 0

    def test_ties_are_broken_by_the_score(self):
        provider = w.pullback_world()
        provider._raws["BAD"] = w.raw("BAD", forwardPE=11.0)                    # as healthy as GOOD
        provider._raws["BAD"]["estimate"]["+1y"]["growth"] = 0.9               # but with far better expected growth
        rows = run_beaten(provider).rows
        assert rows[0].met == rows[1].met == 5 and rows[0].symbol == "BAD" and rows[0].score > rows[1].score


@pytest.mark.unit
class TestListRecipe:
    def test_every_stock_you_name_is_checked_whether_or_not_it_fell(self):
        result = quicklook.run("list", w.pullback_world(), S, symbols=["GOOD", "FLAT", "NOPE", "GOOD"])
        assert dict(result.funnel)["Stocks to look at"] == 3 and {r.symbol for r in result.rows} == {"GOOD", "FLAT"}
        assert any("1 stocks had too little price history" in x or "had too little price history" in x for x in result.warnings)
        assert next(r for r in result.rows if r.symbol == "FLAT").conditions["b"].status == cond.FAIL

    def test_errors(self):
        with pytest.raises(quicklook.PickerError, match="at least one stock"):
            quicklook.run("list", w.pullback_world(), S, symbols=[])
        with pytest.raises(quicklook.PickerError, match="at most 300"):
            quicklook.run("list", w.pullback_world(), S, symbols=[f"S{i}" for i in range(301)])
        with pytest.raises(quicklook.PickerError, match="unknown recipe"):
            quicklook.run("magic", w.pullback_world(), S)

    def test_bad_settings_are_refused_before_anything_is_downloaded(self):
        provider = w.pullback_world()
        with pytest.raises(ValueError):
            quicklook.run("beaten-down", provider, PickerSettings(below_high=7.0))
        assert provider.closes_calls == []

    def test_no_s_and_p_prices_or_no_universe_stops_with_a_message(self):
        provider = w.pullback_world()
        del provider._closes["^GSPC"]
        with pytest.raises(quicklook.PickerError, match="S&P 500 prices"):
            run_beaten(provider)
        with pytest.raises(quicklook.PickerError, match="returned no stocks"):
            run_beaten(w.FakeProvider())


@pytest.mark.unit
class TestScoring:
    def test_percentiles_give_neutral_to_missing_and_to_a_lone_value(self):
        assert quicklook._percentiles({"a": 1.0, "b": None}) == {"a": 0.5, "b": 0.5}
        ranked = quicklook._percentiles({"a": 1.0, "b": 2.0, "c": None})
        assert ranked["b"] > ranked["a"] and ranked["c"] == 0.5

    def test_a_row_without_windows_is_not_sector_driven(self):
        row = run_beaten().rows[0]
        row.sector_windows = None
        assert row.sector_driven is False


# ---------------------------------------------------------------------------------------------- the cache

@pytest.mark.unit
class TestCache:
    def test_a_second_run_the_same_day_reads_from_disk(self, tmp_path):
        cache = FundamentalsCache(tmp_path, w.TODAY)
        first = w.pullback_world()
        run_beaten(first, cache=cache)
        assert sorted(first.fundamentals_calls) == ["BAD", "GOOD"]
        second = w.pullback_world()
        logs = []
        run_beaten(second, cache=cache, log=logs.append)
        assert second.fundamentals_calls == [] and any("2 of 2 read from today's saved copy" in x for x in logs)

    def test_refresh_downloads_again(self, tmp_path):
        cache = FundamentalsCache(tmp_path, w.TODAY)
        run_beaten(w.pullback_world(), cache=cache)
        again = w.pullback_world()
        run_beaten(again, cache=cache, refresh=True)
        assert sorted(again.fundamentals_calls) == ["BAD", "GOOD"]

    def test_a_failed_download_is_not_saved_so_the_next_run_tries_again(self, tmp_path):
        cache = FundamentalsCache(tmp_path, w.TODAY)
        broken = w.pullback_world()
        broken._fail = {"GOOD"}
        run_beaten(broken, cache=cache)
        assert cache.get("GOOD") is None and cache.get("BAD") is not None
        fixed = w.pullback_world()
        run_beaten(fixed, cache=cache)
        assert fixed.fundamentals_calls == ["GOOD"]

    def test_days_are_kept_apart(self, tmp_path):
        FundamentalsCache(tmp_path, date(2026, 11, 7)).put("AAA", w.raw("AAA"))
        assert FundamentalsCache(tmp_path, date(2026, 11, 8)).get("AAA") is None
        assert FundamentalsCache(tmp_path, date(2026, 11, 7)).get("AAA")["symbol"] == "AAA"

    def test_unsafe_symbols_and_damaged_files_and_no_folder(self, tmp_path):
        cache = FundamentalsCache(tmp_path, w.TODAY)
        cache.put("../evil", w.raw("X"))
        assert cache.get("../evil") is None and not list(tmp_path.rglob("evil*"))
        cache.put("AAA", w.raw("AAA"))
        (tmp_path / w.TODAY.isoformat() / "AAA.json").write_text("{not json", encoding="utf-8")
        assert cache.get("AAA") is None
        (tmp_path / w.TODAY.isoformat() / "AAA.json").write_text("[1, 2]", encoding="utf-8")
        assert cache.get("AAA") is None
        nowhere = FundamentalsCache(None, w.TODAY)
        nowhere.put("AAA", w.raw("AAA"))
        assert nowhere.get("AAA") is None


# ---------------------------------------------------------------------------------------------- the ledger

@pytest.mark.unit
class TestLedger:
    def test_rows_are_appended_with_one_header(self, tmp_path):
        path = tmp_path / "picker" / "picks.csv"
        result = run_beaten()
        assert ledger.append_picks(path, result, top=5, now=datetime(2026, 11, 8, 10, 0)) == 2
        assert ledger.append_picks(path, result, top=1) == 1
        rows = ledger.read_picks(path)
        assert [r["symbol"] for r in rows] == ["GOOD", "BAD", "GOOD"] and [r["rank"] for r in rows] == ["1", "2", "1"]
        assert path.read_text(encoding="utf-8").count("logged_at,as_of") == 1
        first = rows[0]
        assert first["recipe"] == "beaten-down" and first["conditions_met"] == "5" and first["as_of"] == w.AS_OF.isoformat()
        assert first["conditions"] == "a:PASS b:PASS c:PASS d:PASS e:PASS" and float(first["off_high_pct"]) < -20
        assert json.loads(first["settings"])["below_high"] == 0.2

    def test_text_is_defused_for_spreadsheets(self, tmp_path):
        result = run_beaten()
        result.rows[0].name = "=HYPERLINK(\"http://evil\")"
        ledger.append_picks(tmp_path / "p.csv", result, top=1)
        assert ledger.read_picks(tmp_path / "p.csv")[0]["name"].startswith("'=")

    def test_an_empty_answer_writes_nothing(self, tmp_path):
        result = run_beaten(settings=PickerSettings.with_overrides(below_high=0.9))
        assert ledger.append_picks(tmp_path / "p.csv", result) == 0 and not (tmp_path / "p.csv").exists()
        assert ledger.read_picks(tmp_path / "missing.csv") == []


# ---------------------------------------------------------------------------------------------- the report

@pytest.mark.unit
class TestReport:
    def test_the_text_has_the_market_the_funnel_the_table_and_the_details(self):
        provider = w.pullback_world()
        provider._raws["GOOD"] = w.raw("GOOD", forwardPE=4.0)
        text = report.render_text(run_beaten(provider), top=15, details=2)
        for needed in ("BEATEN-DOWN QUALITY", "Market: the S&P 500 is", "HOW MANY STOCKS AT EACH STEP", "THE TOP 2", "GOOD", "BAD",
                       "5 of 5", "SECTOR-DRIVEN", "LOW P/E: CHECK", "PASS    (a) Market in a drawdown", "FAIL    (c) No company deterioration",
                       "Run the full analysis on the few you choose"):
            assert needed in text, needed

    def test_the_table_columns_do_not_run_together(self):
        text = report.render_text(run_beaten(), details=0)
        row = next(line for line in text.splitlines() if " GOOD " in line)
        assert re.search(r"5 of 5\s+\d+%\s", row)

    def test_a_list_has_its_own_heading_and_no_rows_says_what_to_do(self):
        assert "YOUR LIST" in report.render_text(quicklook.run("list", w.pullback_world(), S, symbols=["GOOD"]))
        empty = report.render_text(run_beaten(settings=PickerSettings.with_overrides(below_high=0.9)))
        assert "No stocks to show" in empty and "--below-high" in empty

    def test_warnings_are_printed(self):
        provider = w.pullback_world()
        provider._treasury = None
        assert "NOTES" in report.render_text(run_beaten(provider)) and "Treasury yield could not be fetched" in report.render_text(run_beaten(provider))

    def test_the_csv_has_every_column_a_bom_and_crlf(self):
        data = report.to_csv(run_beaten())
        assert data.startswith("﻿".encode("utf-8")) and b"\r\n" in data
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8").lstrip("﻿"), newline="")))
        assert list(rows[0]) == list(report.CSV_COLUMNS) and [r["symbol"] for r in rows] == ["GOOD", "BAD"]
        assert rows[0]["conditions_met"] == "5" and rows[0]["e_status"] == "PASS" and rows[0]["sector_driven"] == "yes"
        assert rows[1]["c_status"] == "FAIL" and "operating cash flow" in rows[1]["c_detail"]

    def test_csv_text_from_the_web_cannot_run_as_a_formula(self):
        result = run_beaten()
        result.rows[0].name = "=cmd|' /C calc'!A0"
        result.rows[0].conditions["c"].detail = "+1+1"
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(report.to_csv(result).decode("utf-8").lstrip("﻿"), newline="")))
        assert rows[0]["name"].startswith("'=") and rows[0]["c_detail"].startswith("'+")

    def test_json_has_the_funnel_and_rows(self):
        data = report.to_json(run_beaten())
        assert data["recipe"] == "beaten-down" and data["treasury_yield"] == 4.8 and data["market"]["vix"] == 31.0
        assert data["funnel"][0] == {"step": "Stocks to look at", "count": 5} and data["rows"][0]["symbol"] == "GOOD"
        json.dumps(data)                                                     # everything in it is plain JSON
