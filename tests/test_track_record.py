"""The analyst track-record study: grade and firm cleaning, scoring a call from the day after it, price targets, and the
walk-forward test, on made-up data where the right answer is known. No network."""

import pickle

import numpy as np
import pandas as pd
import pytest

from tradingagents.research import analyst_study as study
from tradingagents.research import track_record as tr

COLUMNS = ["date", "Firm", "ToGrade", "FromGrade", "Action", "priceTargetAction", "currentPriceTarget", "priorPriceTarget"]


def history(rows):
    """A rating-history frame from (date, firm, action, to_grade, target_action, target) tuples."""
    return pd.DataFrame([(pd.Timestamp(d), f, g, "", a, ta, float(t), 0.0) for d, f, a, g, ta, t in rows], columns=COLUMNS)


def flat_prices(days, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=days)
    frame = pd.DataFrame({"adj": 100.0, "close": 100.0, "high": 101.0, "low": 99.0}, index=idx)
    return frame


def make_prices(symbols, start="2013-06-03", end="2021-12-31", seed=1):
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    out = {}
    for sym in [*symbols, "SPY"]:
        r = rng.normal(0.0003, 0.008 if sym == "SPY" else 0.015, len(idx))
        adj = 100 * np.exp(np.cumsum(r))
        out[sym] = pd.DataFrame({"adj": adj, "close": adj, "high": adj * 1.01, "low": adj * 0.99}, index=idx)
    return out


@pytest.mark.unit
class TestGradesAndFirms:
    @pytest.mark.parametrize("grade, expected", [
        ("Buy", 1), ("Strong Buy", 1), ("Outperform", 1), ("Overweight", 1), ("Positive", 1), (" buy ", 1),
        ("Hold", 0), ("Neutral", 0), ("Equal-Weight", 0), ("Market Perform", 0), ("Sector Weight", 0),
        ("Sell", -1), ("Underperform", -1), ("Underweight", -1), ("Reduce", -1), ("Negative", -1),
        ("", None), (None, None), ("Gibberish", None), (float("nan"), None),
    ])
    def test_grade_direction(self, grade, expected):
        assert tr.grade_direction(grade) == expected

    @pytest.mark.parametrize("a, b", [
        ("JP Morgan", "JPMorgan"), ("B of A Securities", "BofA Securities"), ("Cowen & Co.", "TD Cowen"),
        ("Evercore ISI Group", "evercore"), ("Morgan Stanley", "morgan  stanley "), ("Wells Fargo Securities", "Wells Fargo"),
    ])
    def test_spellings_of_one_firm_match(self, a, b):
        assert tr.firm_key(a) == tr.firm_key(b)

    def test_different_firms_stay_different(self):
        assert len({tr.firm_key(n) for n in ("Goldman Sachs", "Morgan Stanley", "UBS", "Jefferies", "Needham")}) == 5

    def test_nonsense_names_do_not_raise(self):
        assert tr.firm_key(None) == "" and tr.firm_key("&&&") == ""


@pytest.mark.unit
class TestRatingCalls:
    def test_only_real_calls_are_kept(self):
        h = history([
            ("2020-03-02", "JP Morgan", "up", "Overweight", "Raises", 100),
            ("2020-03-03", "Barclays", "down", "Underweight", "Lowers", 50),
            ("2020-03-04", "UBS", "init", "Buy", "Announces", 90),
            ("2020-03-05", "Citi", "init", "Sell", "Announces", 40),
            ("2020-03-06", "Wolfe", "init", "Peer Perform", "", 0),        # a neutral first rating says nothing
            ("2020-03-09", "Needham", "main", "Buy", "Raises", 110),       # maintains: nothing new
            ("2020-03-10", "Baird", "reit", "Buy", "Maintains", 110),
            ("2020-03-02", "JPMorgan", "up", "Overweight", "Raises", 100),  # the same call, spelled differently
        ])
        calls = tr.build_rating_calls({"AAA": h})
        assert sorted(zip(calls["firm"], calls["direction"], calls["kind"])) == [
            ("barclays", -1, "downgrade"), ("citi", -1, "initiate"), ("jpmorgan", 1, "upgrade"), ("ubs", 1, "initiate")]

    def test_empty_or_missing_histories_give_an_empty_frame(self):
        assert tr.build_rating_calls({}).empty
        assert tr.build_rating_calls({"A": pd.DataFrame(columns=COLUMNS), "B": None}).empty


@pytest.mark.unit
class TestOutcomes:
    def test_a_call_is_scored_from_the_close_of_the_next_trading_day(self):
        """A stock that jumps 10 percent the day after the call has not 'earned' that jump for the caller."""
        frame = flat_prices(120)
        frame.iloc[41:, :] = frame.iloc[41:, :] * 1.10               # the jump arrives on day 41
        call_day = frame.index[40]
        calls = tr.build_rating_calls({"AAA": history([(call_day, "UBS", "up", "Buy", "Raises", 100)])})
        out = tr.attach_outcomes(calls, {"AAA": frame}, flat_prices(120)["adj"], horizon=21)
        assert len(out) == 1 and out["entry"].iloc[0] == frame.index[41]
        assert out["excess"].iloc[0] == pytest.approx(0.0, abs=1e-12)

    def test_a_call_on_a_weekend_enters_on_monday(self):
        frame = flat_prices(120)
        saturday = pd.Timestamp("2020-02-01")
        assert saturday.weekday() == 5
        calls = tr.build_rating_calls({"AAA": history([(saturday, "UBS", "down", "Sell", "Lowers", 50)])})
        out = tr.attach_outcomes(calls, {"AAA": frame}, flat_prices(120)["adj"], horizon=21)
        assert out["entry"].iloc[0] == pd.Timestamp("2020-02-03")

    def test_excess_return_is_against_the_market_and_signed_by_direction(self):
        idx = pd.bdate_range("2020-01-01", periods=200)
        stock = pd.DataFrame({"adj": 100 * 1.001 ** np.arange(200)}, index=idx)
        market = pd.Series(100 * 1.0004 ** np.arange(200), index=idx)
        calls = tr.build_rating_calls({"AAA": history([(idx[10], "UBS", "up", "Buy", "Raises", 1), (idx[10], "Citi", "down", "Sell", "Lowers", 1)])})
        out = tr.attach_outcomes(calls, {"AAA": stock}, market, horizon=21)
        expected = 1.001 ** 21 - 1.0004 ** 21
        assert out["excess"].tolist() == [pytest.approx(expected)] * 2
        by_firm = out.set_index("firm")
        assert by_firm.loc["ubs", "signed"] == pytest.approx(expected) and by_firm.loc["citi", "signed"] == pytest.approx(-expected)

    def test_timing_only_gives_no_credit_for_a_stock_that_simply_rose(self):
        idx = pd.bdate_range("2020-01-01", periods=200)
        stock = pd.DataFrame({"adj": 100 * 1.001 ** np.arange(200)}, index=idx)          # rises steadily, every day alike
        market = pd.Series(100.0, index=idx)
        calls = tr.build_rating_calls({"AAA": history([(idx[30], "UBS", "up", "Buy", "Raises", 1)])})
        out = tr.attach_outcomes(calls, {"AAA": stock}, market, horizon=21)
        assert out["excess"].iloc[0] > 0.02 and out["timing"].iloc[0] == pytest.approx(0.0, abs=1e-9)

    def test_calls_without_prices_or_without_a_finished_window_are_left_out(self):
        frame = flat_prices(60)
        calls = tr.build_rating_calls({"AAA": history([(frame.index[50], "UBS", "up", "Buy", "Raises", 1)]),
                                       "ZZZ": history([(frame.index[5], "UBS", "up", "Buy", "Raises", 1)])})
        out = tr.attach_outcomes(calls, {"AAA": frame}, frame["adj"], horizon=21)
        assert out.empty and {"signed", "signed_timing"} <= set(out.columns)


def skill_world(skilled=0, random=0, anti=0, per_year=15, seed=3, horizon=21):
    """Firms whose calls follow the future (skilled), ignore it (random) or oppose it (anti), over 2014 to 2020."""
    prices = make_prices(["AAA", "BBB", "CCC"], seed=seed)
    bench = prices["SPY"]["adj"]
    idx = bench.index
    rng = np.random.default_rng(seed + 100)
    rows = {s: [] for s in ("AAA", "BBB", "CCC")}
    firms = [(f"Skilled{i}", 1) for i in range(skilled)] + [(f"Random{i}", 0) for i in range(random)] + [(f"Anti{i}", -1) for i in range(anti)]
    for firm, mode in firms:
        for year in range(2014, 2021):
            positions = np.flatnonzero(idx.year == year)[:-horizon - 3]
            for _ in range(per_year):
                symbol = ("AAA", "BBB", "CCC")[rng.integers(3)]
                i = int(rng.choice(positions))
                adj = prices[symbol]["adj"]
                future = (adj.iloc[i + 1 + horizon] / adj.iloc[i + 1] - 1) - (bench.iloc[i + 1 + horizon] / bench.iloc[i + 1] - 1)
                direction = int(np.sign(future)) * mode if mode else int(rng.choice([-1, 1]))
                direction = direction or 1
                rows[symbol].append((idx[i], firm, "up" if direction > 0 else "down", "Buy" if direction > 0 else "Sell", "", 0))
    return prices, {s: history(r) for s, r in rows.items()}


@pytest.mark.unit
class TestInformationAndPersistence:
    def test_calls_that_follow_the_future_show_a_positive_spread_every_year(self):
        prices, hist = skill_world(skilled=6)
        out = tr.attach_outcomes(tr.build_rating_calls(hist), prices, prices["SPY"]["adj"], 21)
        info = tr.information_spread(out)
        assert info["bull_mean"] > 0 > info["bear_mean"] and info["positive_years"] == len(info["years"]) >= 6

    def test_calls_made_at_random_show_no_reliable_spread(self):
        prices, hist = skill_world(random=8)
        out = tr.attach_outcomes(tr.build_rating_calls(hist), prices, prices["SPY"]["adj"], 21)
        assert abs(tr.information_spread(out)["mean_spread"]) < 0.02

    def test_the_walk_forward_test_finds_lasting_skill(self):
        prices, hist = skill_world(skilled=4, random=4, anti=4)
        out = tr.attach_outcomes(tr.build_rating_calls(hist), prices, prices["SPY"]["adj"], 21)
        wf = tr.walk_forward(out)
        assert len(wf["years"]) >= 4 and wf["positive_years"] == len(wf["years"])
        assert wf["mean_spread"] > 0.05 and wf["t_stat"] > 3

    def test_the_walk_forward_test_sees_nothing_where_there_is_no_skill(self):
        prices, hist = skill_world(random=12)
        out = tr.attach_outcomes(tr.build_rating_calls(hist), prices, prices["SPY"]["adj"], 21)
        wf = tr.walk_forward(out)
        assert wf["years"] and abs(wf["mean_spread"]) < 0.03

    def test_firm_skill_ranks_skilled_above_anti(self):
        prices, hist = skill_world(skilled=3, anti=3)
        out = tr.attach_outcomes(tr.build_rating_calls(hist), prices, prices["SPY"]["adj"], 21)
        table = tr.firm_skill(out, min_calls=20)
        assert set(table.index[:3]) == {"skilled0", "skilled1", "skilled2"} and set(table.index[-3:]) == {"anti0", "anti1", "anti2"}
        assert table["hit_rate"].iloc[0] > 0.9 > 0.1 > table["hit_rate"].iloc[-1] or table["hit_rate"].iloc[-1] < 0.2

    @staticmethod
    def outcome_rows(firm, mean, year, n=12, exit_year=None):
        entry = pd.Timestamp(year=year, month=3, day=1)
        exit_ = pd.Timestamp(year=exit_year or year, month=4, day=1)
        return [{"firm": firm, "entry": entry, "exit": exit_, "signed_timing": mean, "signed": mean, "direction": 1, "excess": mean}] * n

    def make(self, prior_means, now_means, prior_exit_year=None):
        rows = []
        for k, (p, c) in enumerate(zip(prior_means, now_means)):
            rows += self.outcome_rows(f"F{k}", p, 2017, exit_year=prior_exit_year)
            rows += self.outcome_rows(f"F{k}", c, 2018)
        return pd.DataFrame(rows)

    def test_a_ranking_that_holds_gives_a_positive_spread(self):
        means = [0.05, 0.05, 0.05, 0.0, 0.0, 0.0, -0.05, -0.05, -0.05]
        wf = tr.walk_forward(self.make(means, means), first_year=2018)
        assert [r["year"] for r in wf["years"]] == [2018] and wf["years"][0]["spread"] == pytest.approx(0.10)

    def test_a_ranking_that_reverses_gives_a_negative_spread(self):
        means = [0.05, 0.05, 0.05, 0.0, 0.0, 0.0, -0.05, -0.05, -0.05]
        wf = tr.walk_forward(self.make(means, list(reversed(means))), first_year=2018)
        assert wf["years"][0]["spread"] == pytest.approx(-0.10)

    def test_outcomes_not_yet_known_when_the_year_began_are_never_used_for_the_ranking(self):
        """Calls entered in 2017 that finish in 2018 are not known on 1 January 2018, so no firm can be ranked."""
        means = [0.05, 0.05, 0.05, 0.0, 0.0, 0.0, -0.05, -0.05, -0.05]
        wf = tr.walk_forward(self.make(means, means, prior_exit_year=2018), first_year=2018)
        assert wf["years"] == [] and wf["mean_spread"] is None and wf["t_stat"] is None

    def test_too_few_firms_or_calls_gives_no_year(self):
        assert tr.walk_forward(self.make([0.1, 0.0], [0.1, 0.0]), first_year=2018)["years"] == []

    def test_t_stat_needs_three_numbers_and_some_spread(self):
        assert tr._tstat([0.1, 0.2]) is None and tr._tstat([0.1, 0.1, 0.1]) is None and tr._tstat([0.1, 0.2, 0.3]) > 0


@pytest.mark.unit
class TestTargets:
    def prices(self, slope=0.2, days=400):
        idx = pd.bdate_range("2020-01-01", periods=days)
        level = 100 + slope * np.arange(days)
        return {"AAA": pd.DataFrame({"adj": level, "close": level, "high": level + 1, "low": level - 1}, index=idx)}, idx

    def events(self, rows):
        return tr.build_target_events({"AAA": history([(d, f, "main", "Buy", a, t) for d, f, a, t in rows])})

    def test_only_new_or_changed_targets_are_events(self):
        ev = self.events([("2020-01-02", "UBS", "Raises", 120), ("2020-01-03", "Citi", "Maintains", 130), ("2020-01-06", "Baird", "Announces", 90),
                          ("2020-01-07", "Jefferies", "Lowers", 0), ("2020-01-08", "Wolfe", "", 100), ("2020-01-09", "Barclays", "Lowers", 80)])
        assert sorted(ev["firm"]) == ["baird", "barclays", "ubs"]

    def test_a_target_is_judged_against_the_next_252_trading_days(self):
        prices, idx = self.prices(slope=0.2)                                   # 100 rising to about 150 over 252 days
        ev = self.events([(idx[0], "UBS", "Raises", 130), (idx[0], "Citi", "Raises", 160)])
        out = tr.target_outcomes(ev, prices).set_index("firm")
        assert out.loc["ubs", "hit_any"] and out.loc["ubs", "hit_end"] and not out.loc["citi", "hit_any"] and not out.loc["citi", "hit_end"]
        assert out.loc["ubs", "implied"] == pytest.approx(0.30) and out.loc["ubs", "realized"] == pytest.approx(50.4 / 100)
        assert out.loc["ubs", "optimism"] == pytest.approx(0.30 - 0.504) and out.loc["ubs", "abs_err"] == pytest.approx(0.204)

    def test_a_target_reached_early_but_not_at_the_end_is_counted_as_such(self):
        idx = pd.bdate_range("2020-01-01", periods=400)
        level = np.concatenate([np.linspace(100, 140, 100), np.linspace(140, 90, 300)])
        prices = {"AAA": pd.DataFrame({"adj": level, "close": level, "high": level + 1, "low": level - 1}, index=idx)}
        out = tr.target_outcomes(self.events([(idx[0], "UBS", "Raises", 135)]), prices)
        assert bool(out["hit_any"].iloc[0]) and not bool(out["hit_end"].iloc[0])

    def test_a_target_below_the_price_is_reached_when_the_stock_falls_to_it(self):
        idx = pd.bdate_range("2020-01-01", periods=400)
        level = np.linspace(100, 60, 400)
        prices = {"AAA": pd.DataFrame({"adj": level, "close": level, "high": level + 1, "low": level - 1}, index=idx)}
        out = tr.target_outcomes(self.events([(idx[0], "UBS", "Lowers", 80), (idx[0], "Citi", "Lowers", 50)]), prices).set_index("firm")
        assert out.loc["ubs", "hit_any"] and out.loc["ubs", "hit_end"] and not out.loc["citi", "hit_any"]
        assert out.loc["ubs", "implied"] == pytest.approx(-0.20)

    def test_a_target_is_put_in_todays_share_units_using_the_splits_that_came_after_it(self):
        """Yahoo's prices are in today's shares but an old target is in the shares of its day."""
        prices, idx = self.prices()
        ev = self.events([(idx[0], "UBS", "Raises", 240)])               # 240 in old shares is 120 after a later 2-for-1 split
        splits = {"AAA": pd.Series([2.0], index=[idx[200]])}
        out = tr.target_outcomes(ev, prices, splits=splits)
        assert out["target"].iloc[0] == pytest.approx(120) and out["implied"].iloc[0] == pytest.approx(0.20)
        assert out.attrs["split_adjusted"] == 1 and out.attrs["excluded"] == 0
        unadjusted = tr.target_outcomes(ev, prices)                       # what goes wrong without it: a plausible-looking +140 percent
        assert unadjusted["implied"].iloc[0] == pytest.approx(1.40) and unadjusted.attrs["split_adjusted"] == 0

    def test_a_split_before_the_target_changes_nothing_and_several_later_splits_multiply(self):
        prices, idx = self.prices()
        ev = self.events([(idx[100], "UBS", "Raises", 125)])
        before = tr.target_outcomes(ev, prices, splits={"AAA": pd.Series([4.0], index=[idx[50]])})
        assert before["target"].iloc[0] == pytest.approx(125) and before.attrs["split_adjusted"] == 0
        later = tr.target_outcomes(self.events([(idx[10], "UBS", "Raises", 300)]), prices,
                                   splits={"AAA": pd.Series([2.0, 1.5], index=[idx[150], idx[250]])})
        assert later["target"].iloc[0] == pytest.approx(100)             # 300 / (2 * 1.5)

    def test_split_factor(self):
        splits = pd.Series([2.0, 3.0], index=pd.to_datetime(["2021-06-01", "2023-06-01"]))
        assert tr.split_factor(splits, pd.Timestamp("2020-01-01")) == 6.0
        assert tr.split_factor(splits, pd.Timestamp("2022-01-01")) == 3.0
        assert tr.split_factor(splits, pd.Timestamp("2023-06-01")) == 1.0          # the split day itself is not "after"
        assert tr.split_factor(splits, pd.Timestamp("2024-01-01")) == 1.0
        assert tr.split_factor(None, pd.Timestamp("2020-01-01")) == 1.0 and tr.split_factor(pd.Series(dtype=float), pd.Timestamp("2020-01-01")) == 1.0

    def test_a_target_whose_year_has_not_finished_is_left_out(self):
        prices, idx = self.prices(days=300)
        assert tr.target_outcomes(self.events([(idx[100], "UBS", "Raises", 130)]), prices).empty

    def test_a_probable_split_or_units_mismatch_is_dropped_and_counted(self):
        prices, idx = self.prices()
        out = tr.target_outcomes(self.events([(idx[0], "UBS", "Raises", 1500), (idx[0], "Citi", "Lowers", 10), (idx[0], "Baird", "Raises", 120)]), prices)
        assert list(out["firm"]) == ["baird"] and out.attrs["excluded"] == 2

    def test_a_target_dated_on_a_weekend_uses_the_last_close_before_it(self):
        prices, idx = self.prices()
        friday = next(i for i, day in enumerate(idx) if day.weekday() == 4 and i > 5)
        out = tr.target_outcomes(self.events([(idx[friday] + pd.Timedelta(days=1), "UBS", "Raises", 130)]), prices)
        assert out["start"].iloc[0] == pytest.approx(float(prices["AAA"]["close"].iloc[friday]))

    def test_the_miss_is_measured_against_other_firms_on_the_same_stock_and_quarter(self):
        prices, idx = self.prices()
        ev = self.events([(idx[0], "UBS", "Raises", 120), (idx[0], "Citi", "Raises", 130), (idx[0], "Baird", "Raises", 140), (idx[0], "Wolfe", "Raises", 125)])
        out = tr.target_outcomes(ev, prices).set_index("firm")
        assert out["rel_abs_err"].mean() == pytest.approx(0.0, abs=1e-12)
        assert out.loc["baird", "rel_abs_err"] < 0 < out.loc["ubs", "rel_abs_err"]           # the 140 target missed least, the 120 most
        lone = tr.target_outcomes(self.events([(idx[0], "UBS", "Raises", 120), (idx[0], "Citi", "Raises", 130)]), prices)
        assert lone["rel_abs_err"].isna().all()                                              # fewer than three firms: no comparison

    def test_summary_and_firm_tables(self):
        prices, idx = self.prices()
        ev = self.events([(idx[d], f"F{k}", "Raises", 125 + k) for d in (0, 1, 2) for k in range(4)])
        out = tr.target_outcomes(ev, prices)
        summary = tr.target_summary(out)
        assert summary["n"] == 12 and 0 <= summary["hit_end"] <= 1 and summary["excluded"] == 0
        assert tr.target_summary(out.iloc[0:0]) == {"n": 0}
        table = tr.firm_target_skill(out, min_events=3)
        assert len(table) == 4 and table["rel_abs_err"].is_monotonic_increasing

    def test_persistence_needs_enough_firms_and_finds_a_lasting_difference(self):
        rows = []
        for k in range(12):
            for year in (2016, 2022):
                for _ in range(12):
                    rows.append({"firm": f"F{k}", "date": pd.Timestamp(year=year, month=6, day=1), "rel_abs_err": k / 100.0})
        persist = tr.target_persistence(pd.DataFrame(rows), split="2019-01-01")
        assert persist["firms"] == 12 and persist["spearman"] == pytest.approx(1.0)
        assert persist["best_third_later"] < persist["worst_third_later"]
        assert tr.target_persistence(pd.DataFrame(rows[:30]), split="2019-01-01")["spearman"] is None


def study_inputs():
    prices, hist = skill_world(skilled=3, random=3, anti=3, per_year=10)
    for k, row in enumerate(hist["AAA"].itertuples(index=False)):            # give some calls a price target
        if k % 3 == 0:
            hist["AAA"].loc[hist["AAA"].index[k], ["priceTargetAction", "currentPriceTarget"]] = ["Raises", 100 * (1 + (k % 7) / 10)]
    return hist, prices


@pytest.mark.unit
class TestWholeStudyAndReport:
    def test_run_and_render(self):
        hist, prices = study_inputs()
        result = tr.run_study(hist, prices, "SPY", horizons=(21, 63))
        assert result.stocks == 3 and result.rating_calls > 0 and set(result.information) == {21, 63}
        report = tr.render_report(result, universe=3)
        for heading in ("## 1. Do rating changes carry any information?", "## 2. Does skill last?", "## 3. Firms with the best and worst record",
                        "## 4. Price targets", "## 5. Limits"):
            assert heading in report
        assert "survivors" in report and "luck" in report and "| 21 trading days |" in report and "| 63 trading days |" in report

    def test_the_report_says_how_many_targets_were_adjusted_for_splits(self):
        hist, prices = study_inputs()
        splits = {"AAA": pd.Series([2.0], index=[pd.Timestamp("2021-01-04")])}
        report = tr.render_report(tr.run_study(hist, prices, "SPY", horizons=(21,), splits=splits), universe=3)
        assert "adjusted for stock splits that came after them" in report

    def test_a_study_with_no_price_targets_says_so(self):
        prices, hist = skill_world(random=6, per_year=6)
        result = tr.run_study(hist, prices, "SPY", horizons=(21,))
        assert "No price targets could be scored." in tr.render_report(result, universe=3)

    def test_numbers_are_formatted_safely(self):
        assert tr._pct(None) == "n/a" and tr._pct(float("nan")) == "n/a" and tr._pct(0.0123) == "+1.2%" and tr._num(None) == "n/a"


@pytest.mark.unit
class TestRunner:
    def fake_fetchers(self, monkeypatch):
        hist, prices = study_inputs()
        calls = {"ratings": [], "prices": 0}
        monkeypatch.setattr(study, "fetch_rating_history", lambda s: calls["ratings"].append(s) or hist[s])
        monkeypatch.setattr(study, "fetch_split_history", lambda s: pd.Series(dtype=float))
        monkeypatch.setattr(study, "fetch_price_history", lambda symbols, start: calls.__setitem__("prices", calls["prices"] + 1) or prices)
        return calls

    def test_the_download_is_saved_and_reused(self, tmp_path, monkeypatch):
        calls = self.fake_fetchers(monkeypatch)
        study.load_data(["AAA", "BBB", "CCC"], tmp_path, "2011-06-01", refresh=False)
        assert calls["ratings"] == ["AAA", "BBB", "CCC"] and (tmp_path / "data.pkl").exists()
        study.load_data(["AAA", "BBB"], tmp_path, "2011-06-01", refresh=False)           # a subset comes from the saved copy
        assert len(calls["ratings"]) == 3 and calls["prices"] == 1
        study.load_data(["AAA", "BBB", "CCC"], tmp_path, "2011-06-01", refresh=True)     # asked to download again
        assert len(calls["ratings"]) == 6
        study.load_data(["AAA", "BBB", "CCC"], tmp_path, "2012-01-01", refresh=False)    # a different start needs new prices
        assert calls["prices"] == 3

    def test_a_stock_with_no_history_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        hist, prices = study_inputs()

        def fetch(symbol):
            if symbol == "BBB":
                raise OSError("no data")
            return hist[symbol]

        monkeypatch.setattr(study, "fetch_rating_history", fetch)
        monkeypatch.setattr(study, "fetch_split_history", lambda s: pd.Series(dtype=float))
        monkeypatch.setattr(study, "fetch_price_history", lambda symbols, start: prices)
        histories, _, splits = study.load_data(["AAA", "BBB", "CCC"], tmp_path, "2011-06-01", refresh=True)
        assert sorted(histories) == ["AAA", "CCC"] and sorted(splits) == ["AAA", "CCC"]

    def test_a_saved_copy_from_before_splits_were_kept_is_downloaded_again(self, tmp_path, monkeypatch):
        calls = self.fake_fetchers(monkeypatch)
        (tmp_path / "data.pkl").write_bytes(pickle.dumps({"histories": {"AAA": None, "BBB": None, "CCC": None}, "prices": {}, "start": "2011-06-01"}))
        _, _, splits = study.load_data(["AAA", "BBB", "CCC"], tmp_path, "2011-06-01", refresh=False)
        assert calls["ratings"] == ["AAA", "BBB", "CCC"] and sorted(splits) == ["AAA", "BBB", "CCC"]

    def test_main_writes_the_report_and_the_tables(self, tmp_path, monkeypatch, capsys):
        self.fake_fetchers(monkeypatch)
        assert study.main(["--tickers", "aaa", "bbb", "ccc", "--out", str(tmp_path)]) == 0
        assert "Do some analysts do better" in (tmp_path / "report.md").read_text(encoding="utf-8")
        names = {p.name for p in tmp_path.iterdir()}
        assert {"report.md", "rating_calls_21d.csv", "rating_calls_63d.csv", "firm_rating_skill_21d.csv", "price_targets.csv",
                "firm_target_skill.csv", "raw"} <= names
        assert "Do some analysts do better" in capsys.readouterr().out

    def test_main_without_benchmark_prices_stops_with_a_message(self, tmp_path, monkeypatch, capsys):
        hist, prices = study_inputs()
        monkeypatch.setattr(study, "fetch_rating_history", lambda s: hist[s])
        monkeypatch.setattr(study, "fetch_split_history", lambda s: pd.Series(dtype=float))
        monkeypatch.setattr(study, "fetch_price_history", lambda symbols, start: {k: v for k, v in prices.items() if k != "SPY"})
        assert study.main(["--tickers", "AAA", "--out", str(tmp_path)]) == 1
        assert "No prices for SPY" in capsys.readouterr().err

    def test_the_default_universe_is_large_cap_names_and_has_no_repeats(self):
        assert len(study.UNIVERSE) == len(set(study.UNIVERSE)) >= 70 and {"NVDA", "TSM", "AMD"} <= set(study.UNIVERSE)

    def test_the_saved_copy_is_only_read_from_our_own_folder_layout(self, tmp_path):
        assert (study.default_output_dir().name, study.default_output_dir().parent.name) == ("analyst-track-record", "research")
        raw = tmp_path / "data.pkl"
        raw.write_bytes(pickle.dumps({"histories": {}, "prices": {}, "splits": {}, "start": "x"}))
        histories, _, splits = study.load_data([], tmp_path, "x", refresh=False)
        assert histories == {} and splits == {}
