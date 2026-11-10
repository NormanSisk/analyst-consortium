"""The custom search: its catalog of filters, the plain-words checking of a request, what Yahoo is asked, and the search itself.

No network: the picker runs against the made-up markets in tests/picker_world.py."""

import csv
import io
import json
import math

import pytest

from tests import picker_world as w
from tradingagents.dataflows.vendors.yahoo import picker_data as pdata
from tradingagents.picker import custom as cu, ledger, quicklook, report
from tradingagents.picker.settings import PickerSettings
from tradingagents.picker.stage_a import price_metrics

S = PickerSettings()


def query(**changes):
    return cu.from_request(changes)


def rng(**pairs):
    """{"ranges": {...}} from keyword pairs such as market_cap=(2, None)."""
    return {"ranges": {k: {"min": lo, "max": hi} for k, (lo, hi) in pairs.items()}}


@pytest.mark.unit
class TestTheCatalog:
    def test_every_filter_is_described_and_consistent(self):
        ids = [c.id for c in cu.CRITERIA] + [f.id for f in cu.FLAGS]
        assert len(ids) == len(set(ids))
        groups = {g for g, _ in cu.GROUPS}
        for c in cu.CRITERIA:
            assert c.group in groups and c.label and c.unit and c.help, c.id
            assert c.sides in (("min", "max"), ("min",), ("max",)) and c.where in ("yahoo", "quote", "price") and c.low < c.high
            assert (c.where != "yahoo") == (c.field == ""), c.id               # only the screener's filters name a Yahoo field
            assert c.default_min is None or c.low <= c.default_min <= c.high
        for f in cu.FLAGS:
            assert f.group in groups and f.label and f.help and (f.where != "yahoo" or f.field)

    def test_the_sectors_match_the_data_layers(self):
        assert cu.SECTORS == pdata.SECTORS

    def test_the_page_is_given_the_whole_catalog_in_a_form_it_can_send_as_json(self):
        spec = cu.spec_for_page()
        json.dumps(spec)
        assert [c["id"] for c in spec["criteria"]] == [c.id for c in cu.CRITERIA] and [f["id"] for f in spec["flags"]] == [f.id for f in cu.FLAGS]
        assert [s["id"] for s in spec["sorts"]] == list(cu.SORT_IDS) and spec["sectors"] == list(cu.SECTORS)
        assert spec["limits"]["show"] == {"usual": 15, "min": 1, "max": 500, "label": "How many to show"}
        defaults = {c["id"]: c["default_min"] for c in spec["criteria"] if c["default_min"] is not None}
        assert defaults == {"market_cap": 2, "avg_volume": 500}                  # the usual starting point; both can be cleared


@pytest.mark.unit
class TestCheckingARequest:
    def test_an_empty_request_is_a_search_with_no_filters(self):
        q = cu.from_request(None)
        assert q == cu.from_request({}) and q.ranges == {} and q.flags == () and q.sectors == () and not q.otc
        assert (q.examine, q.check, q.show, q.sort, q.min_conditions) == (800, 60, 15, "conditions", None)

    def test_a_good_request_is_kept_in_the_units_shown(self):
        q = query(**rng(market_cap=(2, 50), forward_pe=(None, 15)), flags=["profitable", "profitable"], sectors=["Energy"], otc=True,
                  exclude="tsla, X ;nvda", min_conditions=3, sort="forward_pe", examine=300, check=40, show=10)
        assert q.ranges == {"market_cap": (2.0, 50.0), "forward_pe": (None, 15.0)} and q.flags == ("profitable",) and q.sectors == ("Energy",)
        assert q.otc and q.exclude == ("TSLA", "X", "NVDA") and q.min_conditions == 3 and q.sort == "forward_pe"
        assert (q.examine, q.check, q.show) == (300, 40, 10)

    def test_a_blank_filter_means_no_filter(self):
        assert query(**rng(market_cap=(None, None), beta=("", ""))).ranges == {}

    @pytest.mark.parametrize("request_, words", [
        ({"ranges": {"wizard": {"min": 1}}}, "unknown filter: wizard"),
        ({"ranges": {"dividend_yield": {"max": 3}}}, "takes only a smallest number"),
        ({"ranges": {"peg": {"min": 1}}}, "takes only a largest number"),
        ({"ranges": {"market_cap": {"min": -1}}}, "type a number from 0 to 20000"),
        ({"ranges": {"analyst_rating": {"max": 9}}}, "type a number from 1 to 5"),
        ({"ranges": {"beta": {"min": 3, "max": 1}}}, r"the smallest number \(3\) is above the largest \(1\)"),
        ({"ranges": {"beta": {"min": True}}}, "type a number"),
        ({"ranges": {"beta": {"min": "high"}}}, "type a number"),
        ({"ranges": {"beta": {"min": math.nan}}}, "type a number"),
        ({"ranges": {"beta": {"max": math.inf}}}, "type a number"),
        ({"flags": ["lucky"]}, "unknown filter: lucky"),
        ({"sectors": ["Crypto"]}, "unknown sector: Crypto"),
        ({"exclude": "TSLA ../etc"}, r"Could not read these as stock symbols to leave out: \.\./etc"),
        ({"min_conditions": 6}, "choose a number from 1 to 5"),
        ({"min_conditions": 0}, "choose a number from 1 to 5"),
        ({"min_conditions": True}, "choose a number from 1 to 5"),
        ({"sort": "luck"}, "Choose one of the ways to order"),
        ({"examine": 5}, "How many stocks to look at: type a whole number from 10 to 3000"),
        ({"check": 500}, "How many to check in depth: type a whole number from 1 to 200"),
        ({"show": 1.5}, "How many to show: type a whole number from 1 to 500"),
    ])
    def test_a_bad_request_is_refused_in_plain_words(self, request_, words):
        with pytest.raises(ValueError, match=words):
            cu.from_request(request_)

    def test_too_long_a_leave_out_list_is_refused(self):
        with pytest.raises(ValueError, match="at most 200"):
            query(exclude=" ".join(f"S{i}" for i in range(201)))


@pytest.mark.unit
class TestWhatYahooIsAsked:
    def test_with_no_filters_it_asks_for_us_stocks_on_the_big_exchanges(self):
        assert cu.yahoo_terms(query()) == [("eq", "region", "us"), ("is-in", "exchange", "NMS", "NYQ", "ASE", "NGM", "NCM")]

    def test_over_the_counter_listings_are_asked_for_only_when_chosen(self):
        assert cu.yahoo_terms(query(otc=True)) == [("eq", "region", "us")]

    def test_each_filter_becomes_a_term_in_yahoos_units(self):
        terms = cu.yahoo_terms(query(**rng(market_cap=(2, None), avg_volume=(500, None), price=(None, 30), beta=(0.5, 1.5), roe=(15, None),
                                             debt_equity=(None, 80)), flags=["profitable", "positive_fcf"]))
        assert terms[2:] == [("gte", "intradaymarketcap", 2e9), ("gte", "avgdailyvol3m", 5e5), ("lte", "intradayprice", 30.0),
                             ("btwn", "beta", 0.5, 1.5), ("gte", "returnonequity.lasttwelvemonths", 15.0),
                             ("lte", "totaldebtequity.lasttwelvemonths", 80.0),
                             ("gt", "netincomeis.lasttwelvemonths", 0), ("gt", "leveredfreecashflow.lasttwelvemonths", 0)]

    def test_the_filters_checked_here_are_not_sent_to_yahoo(self):
        terms = cu.yahoo_terms(query(**rng(forward_pe=(None, 15), analyst_rating=(None, 2), below_high=(20, None), move_63d=(None, -5)),
                                      flags=["market_explains"]))
        assert terms == cu.yahoo_terms(query())

    def test_numbers_carry_no_floating_point_noise(self):
        assert cu.yahoo_terms(query(**rng(market_cap=(0.3, 7.7))))[2] == ("btwn", "intradaymarketcap", 3e8, 7.7e9)


@pytest.mark.unit
class TestFiltersCheckedHere:
    def record(self, **extra):
        return {**w.record("GOOD", forward_pe=12.0), "analyst_rating": 2.0, **extra}

    @pytest.mark.parametrize("pair, expected", [((None, 15), True), ((None, 12), True), ((None, 11.9), False), ((12, None), True),
                                                ((12.1, None), False), ((10, 14), True)])
    def test_forward_pe_limits_are_inclusive(self, pair, expected):
        assert cu.passes_quote(self.record(), query(**rng(forward_pe=pair))) is expected

    def test_a_stock_with_no_number_does_not_pass_a_filter_that_was_set(self):
        assert cu.passes_quote(self.record(forward_pe=None), query(**rng(forward_pe=(None, 50)))) is False
        assert cu.passes_quote(self.record(analyst_rating=None), query(**rng(analyst_rating=(None, 3)))) is False
        assert cu.passes_quote(self.record(forward_pe=None, analyst_rating=None), query()) is True           # no filter, no test

    def test_the_analyst_rating_is_a_largest_number(self):
        assert cu.passes_quote(self.record(), query(**rng(analyst_rating=(None, 2.5)))) is True
        assert cu.passes_quote(self.record(), query(**rng(analyst_rating=(None, 1.5)))) is False

    def test_stocks_to_leave_out_are_left_out(self):
        assert cu.passes_quote(self.record(), query(exclude="GOOD")) is False
        assert cu.passes_quote(self.record(), query(exclude="BAD")) is True

    @pytest.fixture(scope="class")
    def metrics(self):
        closes = w.pullback_world().closes(["GOOD", "OWN", "FLAT", "^GSPC"])
        return {s: price_metrics(closes[s], closes["^GSPC"]) for s in ("GOOD", "OWN", "FLAT")}

    def test_how_far_below_the_high_can_be_a_smallest_or_a_largest(self, metrics):
        good = metrics["GOOD"]
        depth = -good.off_high * 100
        assert depth > 20
        assert cu.passes_price(good, query(**rng(below_high=(20, None))), S) is True
        assert cu.passes_price(good, query(**rng(below_high=(depth + 1, None))), S) is False
        assert cu.passes_price(good, query(**rng(below_high=(None, 10))), S) is False
        assert cu.passes_price(metrics["FLAT"], query(**rng(below_high=(None, 10))), S) is True

    def test_price_changes_are_in_percent(self, metrics):
        good = metrics["GOOD"]
        move = good.w63.stock * 100
        assert move < -10
        assert cu.passes_price(good, query(**rng(move_63d=(None, -10))), S) is True
        assert cu.passes_price(good, query(**rng(move_63d=(move + 1, None))), S) is False

    def test_the_market_explains_the_fall_uses_the_analysis_rule(self, metrics):
        flag = query(flags=["market_explains"])
        assert cu.passes_price(metrics["GOOD"], flag, S) is True              # a fall the market explains
        assert cu.passes_price(metrics["OWN"], flag, S) is False              # a stock that fell on its own
        assert cu.passes_price(metrics["FLAT"], flag, S) is False             # nothing to explain
        assert cu.passes_price(metrics["OWN"], query(), S) is True            # no filter, no test


@pytest.mark.unit
class TestOrder:
    def test_stocks_without_a_number_always_come_last(self):
        recs = [{"symbol": "A", "market_cap": 5, "forward_pe": 20.0}, {"symbol": "B", "market_cap": 9, "forward_pe": None},
                {"symbol": "C", "market_cap": 7, "forward_pe": 8.0}, {"symbol": "D", "market_cap": None, "forward_pe": -3.0}]
        m = type("M", (), {"off_high": -0.2, "w63": type("W", (), {"stock": 0.1})()})()

        def by(sort):
            return [r["symbol"] for r in sorted(recs, key=lambda r: cu.stage_key(sort, r, m))]

        assert by("market_cap") == ["B", "C", "A", "D"] == by("conditions")
        assert by("forward_pe") == ["C", "A", "B", "D"]                       # a negative P/E is no P/E

    def test_the_final_order_follows_the_choice_and_breaks_ties_by_conditions_met(self):
        def row(met, cap, off, move, pe):
            return type("R", (), {"met": met, "score": 0.5, "market_cap": cap, "facts": type("F", (), {"forward_pe": pe})(),
                                  "metrics": type("M", (), {"off_high": off, "w63": type("W", (), {"stock": move})()})()})()

        rows = {"a": row(5, 1e9, -0.1, 0.2, 30), "b": row(3, 9e9, -0.5, -0.3, 8), "c": row(4, 5e9, -0.3, 0.0, 15)}

        def order(sort):
            return [k for k, _ in sorted(rows.items(), key=lambda kv: cu.final_key(sort, kv[1]))]

        assert order("conditions") == ["a", "c", "b"] and order("market_cap") == ["b", "c", "a"] and order("below_high") == ["b", "c", "a"]
        assert order("forward_pe") == ["b", "c", "a"] and order("move_63d") == ["a", "c", "b"]


@pytest.mark.unit
class TestSayingWhatWasAsked:
    def test_the_lines_name_each_filter_with_its_units(self):
        lines = cu.describe(query(**rng(market_cap=(2, 50), roe=(15, None), peg=(None, 1)), flags=["profitable"], sectors=["Energy", "Utilities"],
                                  exclude="TSLA", min_conditions=4, sort="below_high"))
        assert lines[0] == "US stocks on Nasdaq, NYSE and NYSE American" and lines[1] == "Sectors: Energy, Utilities"
        assert "Market value: between 2 and 50 (billion dollars)" in lines and "Return on equity: at least 15 (percent a year)" in lines
        assert "PEG ratio: at most 1 (P/E divided by growth)" in lines and "Profitable" in lines and "Left out: TSLA" in lines
        assert "At least 4 of the five deal conditions met" in lines and lines[-1] == "Order: Furthest below its 52-week high first"

    def test_a_search_with_no_filters_says_so(self):
        assert cu.describe(query()) == ["US stocks on Nasdaq, NYSE and NYSE American", "Order: Most of the five deal conditions met first"]
        assert "over-the-counter" in cu.describe(query(otc=True))[0]

    def test_the_summary_for_the_log_is_plain_json(self):
        s = cu.summary(query(**rng(market_cap=(2, None)), flags=["profitable"], min_conditions=3))
        assert json.loads(json.dumps(s)) == {"ranges": {"market_cap": [2.0, None]}, "flags": ["profitable"], "sectors": [], "otc": False,
                                             "min_conditions": 3, "sort": "conditions"}


# ---------------------------------------------------------------- the search itself
def world(**caps) -> w.FakeProvider:
    """The pullback world with a screener answer that carries the extra facts a custom search reads."""
    provider = w.pullback_world()
    details = {"GOOD": (12.0, 1.8, 9e10), "OWN": (14.0, 2.6, 8e10), "FLAT": (16.0, 3.1, 7e10), "SHORT": (18.0, None, 6e10), "BAD": (20.0, 4.2, 5e10)}
    provider._universe = [{**w.record(s, forward_pe=pe), "analyst_rating": rating, "market_cap": caps.get(s, cap), "trailing_pe": pe + 3,
                           "dividend_yield": 1.5, "price_to_book": 2.2} for s, (pe, rating, cap) in details.items()]
    for s, (_, _, cap) in details.items():                          # the company profile carries the same size as the screener's answer
        if s in provider._raws:
            provider._raws[s]["info"]["marketCap"] = caps.get(s, cap)
    return provider


def search(provider=None, **changes):
    return quicklook.run("custom", provider or world(), S, query=cu.from_request(changes))


@pytest.mark.unit
class TestTheSearch:
    def test_what_is_asked_of_yahoo_and_the_funnel_of_a_search_with_no_filters(self):
        provider = world()
        result = search(provider)
        terms, sectors, limit = provider.screen_calls[0]
        assert terms == cu.yahoo_terms(cu.from_request({})) and sectors == list(cu.SECTORS) and limit == 800
        assert dict(result.funnel) == {"Stocks matching your filters at Yahoo": 5, "With enough price history": 4, "Fundamentals checked": 4,
                                       "Meeting at least 4 of 5 conditions": 4, "Meeting all 5": 1}      # no price filter: OWN and FLAT are kept too
        assert result.rows[0].symbol == "GOOD" and {r.symbol for r in result.rows} == {"GOOD", "OWN", "FLAT", "BAD"} and result.recipe == "custom"
        assert result.filters[0].startswith("US stocks") and result.query["sort"] == "conditions"

    def test_chosen_sectors_and_the_number_to_look_at_reach_yahoo(self):
        provider = world()
        search(provider, sectors=["Energy"], examine=30)
        assert provider.screen_calls[0][1:] == (["Energy"], 30)

    def test_the_screener_filters_checked_here_cut_the_list_and_the_funnel_says_so(self):
        result = search(**rng(forward_pe=(None, 15)))
        assert [step for step, _ in result.funnel][:2] == ["Stocks matching your filters at Yahoo", "After your forward P/E, analyst rating and leave-out filters"]
        assert result.funnel[0][1] == 5 and result.funnel[1][1] == 2 and sorted(r.symbol for r in result.rows) == ["GOOD", "OWN"]

    def test_the_analyst_rating_and_leave_outs(self):
        rated = search(**rng(analyst_rating=(None, 3)))
        assert sorted(r.symbol for r in rated.rows) == ["GOOD", "OWN"]               # FLAT has 3.1, BAD 4.2, SHORT has none
        left = search(exclude="GOOD")
        assert "GOOD" not in [r.symbol for r in left.rows] and left.funnel[1] == ("After your forward P/E, analyst rating and leave-out filters", 4)

    def test_price_filters_use_the_same_numbers_as_the_analysis(self):
        deep = search(**rng(below_high=(20, None)))
        assert dict(deep.funnel)["After your price filters"] == 3 and sorted(r.symbol for r in deep.rows) == ["BAD", "GOOD", "OWN"]
        explained = search(flags=["market_explains"])
        assert sorted(r.symbol for r in explained.rows) == ["BAD", "GOOD"] and dict(explained.funnel)["After your price filters"] == 2
        assert [r.symbol for r in search(**rng(below_high=(None, 10))).rows] == ["FLAT"]

    def test_a_minimum_number_of_conditions_keeps_only_those_stocks(self):
        result = search(min_conditions=5)
        assert [r.symbol for r in result.rows] == ["GOOD"]
        assert dict(result.funnel)["Meeting at least 5 of 5 conditions"] == 1 and dict(result.funnel)["Fundamentals checked"] == 4

    def test_the_results_are_ordered_as_chosen(self):
        assert [r.symbol for r in search(world(OWN=9.5e10), sort="market_cap").rows][0] == "OWN"
        assert [r.symbol for r in search(sort="forward_pe").rows] == ["GOOD", "OWN", "FLAT", "BAD"]
        deepest = [r.symbol for r in search(sort="below_high").rows]
        assert deepest[-1] == "FLAT" and set(deepest) == {"GOOD", "OWN", "FLAT", "BAD"}

    def test_only_as_many_as_asked_are_checked_in_depth_and_the_search_says_so(self):
        provider = world()
        result = search(provider, check=1)
        assert len(result.rows) == 1 and result.rows[0].symbol == "GOOD"             # the largest first
        assert any("only the 1 that come first in your chosen order" in x for x in result.warnings)
        assert provider.fundamentals_calls == ["GOOD"]

    def test_what_the_screener_returned_is_shown_with_each_stock(self):
        result = search()
        assert result.rows[0].extras == {"trailing_pe": 15.0, "dividend_yield": 1.5, "price_to_book": 2.2, "analyst_rating": 1.8}
        data = report.to_rows(result)[0]
        assert (data["trailing_pe"], data["dividend_yield_pct"], data["price_to_book"], data["analyst_rating"]) == (15.0, 1.5, 2.2, 1.8)

    def test_nothing_matching_is_an_answer_not_an_error(self):
        provider = world()
        provider._universe = []
        result = search(provider)
        assert result.rows == [] and result.funnel[0] == ("Stocks matching your filters at Yahoo", 0)
        assert provider.closes_calls == [["^GSPC", "^VIX"]]                            # only the market was downloaded
        assert "No stocks to show. Loosen one of your filters" in report.render_text(result)

    def test_a_search_without_its_filters_is_refused_in_plain_words(self):
        with pytest.raises(quicklook.PickerError, match="needs its filters"):
            quicklook.run("custom", world(), S)

    def test_the_other_recipes_are_unchanged(self):
        assert quicklook.RECIPES == ("beaten-down", "list", "custom")
        assert dict(quicklook.run("beaten-down", w.pullback_world(), S).funnel)["Stocks to look at"] == 5

    def test_the_report_names_the_search_and_what_was_asked(self):
        result = search(**rng(forward_pe=(None, 15)))
        text = report.render_text(result)
        assert "YOUR SEARCH" in text and "WHAT YOU ASKED FOR" in text and "Forward P/E: at most 15" in text and "in the order you chose" in text
        data = report.to_json(result)
        assert data["recipe"] == "custom" and data["filters"][1].startswith("Forward P/E")
        rows = list(csv.DictReader(io.StringIO(report.to_csv(search()).decode("utf-8-sig"))))
        assert list(rows[0]) == list(report.CSV_COLUMNS) and rows[0]["analyst_rating"] == "1.8" and rows[0]["trailing_pe"] == "15.0"

    def test_the_picks_log_keeps_the_filters_that_found_the_stocks(self, tmp_path):
        result = search(**rng(forward_pe=(None, 15)), flags=["market_explains"])
        ledger.append_picks(tmp_path / "picks.csv", result)
        saved = ledger.read_picks(tmp_path / "picks.csv")
        assert saved and saved[0]["recipe"] == "custom"
        kept = json.loads(saved[0]["settings"])["custom"]
        assert kept["ranges"] == {"forward_pe": [None, 15.0]} and kept["flags"] == ["market_explains"]


# ---------------------------------------------------------------- the data layer
@pytest.mark.unit
class TestYahooPieces:
    def test_neutral_terms_become_a_yahoo_query(self):
        q = pdata._custom_query([("eq", "region", "us"), ("is-in", "exchange", "NMS", "NYQ"), ("gte", "intradaymarketcap", 2e9),
                                 ("btwn", "beta", 0.5, 1.5)]).to_dict()
        text = str(q)
        assert q["operator"].upper() == "AND" and "region" in text and "NMS" in text and "intradaymarketcap" in text and "beta" in text

    def test_a_single_term_is_not_wrapped_in_an_and(self):
        assert pdata._custom_query([("eq", "region", "us")]).to_dict()["operator"].upper() == "EQ"

    @pytest.mark.parametrize("bad", [("approximately", "beta", 1), ("btwn", "beta", 1), ("is-in", "sector"), ("gte", "beta")])
    def test_a_term_it_cannot_translate_is_refused(self, bad):
        with pytest.raises(ValueError, match="cannot turn"):
            pdata._custom_query([("eq", "region", "us"), bad])

    def test_the_analyst_rating_is_the_number_in_yahoos_words(self):
        assert pdata._rating("1.8 - Buy") == 1.8 and pdata._rating("4.2 - Underperform") == 4.2
        assert pdata._rating(None) is None and pdata._rating("") is None and pdata._rating("n/a") is None

    def test_one_query_per_sector_and_the_largest_overall_are_kept(self, monkeypatch):
        seen = []

        def fake(query, **k):
            text = str(query.to_dict())
            seen.append(text)
            name = "ENRG" if "Energy" in text else "TECH"
            return {"quotes": [{"symbol": name, "quoteType": "EQUITY", "marketCap": 9e9 if name == "ENRG" else 3e9,
                                "averageAnalystRating": "2.0 - Buy"}], "total": 1}

        monkeypatch.setattr(pdata.yf, "screen", fake)
        out = pdata.screen_custom([("eq", "region", "us"), ("gte", "intradaymarketcap", 2e9)], ["Technology", "Energy"], 1)
        assert len(seen) == 2 and [r["symbol"] for r in out] == ["ENRG"] and out[0]["sector"] == "Energy" and out[0]["analyst_rating"] == 2.0

    def test_the_count_is_yahoos_total_summed_over_the_sectors_asked_for(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "screen", lambda query, **k: {"quotes": [], "total": 7 if "Energy" in str(query.to_dict()) else 5})
        assert pdata.count_matches([("eq", "region", "us"), ("gte", "beta", 1)], []) == 5
        assert pdata.count_matches([("eq", "region", "us"), ("gte", "beta", 1)], ["Energy", "Utilities"]) == 12
        monkeypatch.setattr(pdata.yf, "screen", lambda query, **k: None)
        assert pdata.count_matches([("eq", "region", "us")], []) == 0
