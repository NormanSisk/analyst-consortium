"""Tests for the valuation-context vendor (a local addition): the price against the company's own
P/E history, similar-size peers and the Treasury yield, with Yahoo and FRED stubbed out."""

from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import valuation as val

NOW = pd.Timestamp.now().normalize()
TODAY = NOW.strftime("%Y-%m-%d")


def _quarters(count: int, eps: float = 1.0) -> pd.Series:
    """A reported-EPS series, one report every 90 days ending 20 days ago."""
    dates = [NOW - pd.Timedelta(days=20 + 90 * k) for k in reversed(range(count))]
    return pd.Series([eps] * count, index=pd.DatetimeIndex(dates))


def _closes(start: float = 20.0, end: float = 40.0) -> pd.Series:
    days = pd.bdate_range(end=NOW, periods=2600)
    return pd.Series(pd.Series(range(len(days))).mul((end - start) / len(days)).add(start).to_numpy(), index=days)


@pytest.mark.unit
class TestReadings:
    def test_pe_is_the_close_over_the_last_four_reported_quarters(self):
        eps = _quarters(8, eps=2.0)
        closes = pd.Series(80.0, index=pd.bdate_range(end=NOW, periods=1500))
        readings = val.pe_readings(eps, closes)
        assert len(readings) == 5                      # the first three reports lack four quarters behind them
        assert readings.iloc[-1] == pytest.approx(10.0)

    def test_a_loss_over_four_quarters_and_near_zero_earnings_are_left_out(self):
        eps = pd.Series([-1.0, -1.0, -1.0, -1.0, 0.001, 0.001, 0.001, 0.001, 1.0, 1.0],
                        index=_quarters(10).index)
        closes = pd.Series(50.0, index=pd.bdate_range(end=NOW, periods=1500))
        readings = val.pe_readings(eps, closes)
        # Windows of -4 to -1 are losses and 0.004 is near zero (12,500x); the last two stay.
        assert list(readings.round(2)) == [round(50.0 / 1.003, 2), round(50.0 / 2.002, 2)]

    def test_the_current_pe_is_placed_among_the_last_ten_years(self):
        readings = pd.Series([10.0 + i for i in range(20)], index=_quarters(20).index)
        lines = val.history_lines(readings, 25.0, NOW)
        assert "low 10.0x, median 19.5x, high 29.0x" in lines[0]
        assert "higher than 75% of those readings" in lines[0]
        assert lines[1].startswith("Over the last 5 years: median ")

    def test_too_few_readings_say_so_instead_of_giving_a_range(self):
        readings = pd.Series([10.0] * 5, index=_quarters(5).index)
        assert "Not enough history" in val.history_lines(readings, 12.0, NOW)[0]


@pytest.mark.unit
class TestImpliedGrowth:
    def test_the_yield_the_gap_to_the_bond_and_the_implied_growth(self):
        lines = val.implied_growth_lines(15.0, 300.0, "forward earnings", (4.9, "2026-11-02"))
        text = "\n".join(lines)
        assert "Earnings yield on forward earnings: 5.0%" in text
        assert "10-year Treasury yield: 4.90% (2026-11-02). The earnings yield is 0.1 percentage points above it." in text
        assert "needing 8% a year -> 3.0%; needing 9% a year -> 4.0%; needing 10% a year -> 5.0%" in text
        assert "negative figure" not in text

    def test_a_high_earnings_yield_gets_the_negative_growth_explanation(self):
        text = "\n".join(val.implied_growth_lines(0.37, 2.08, "forward earnings", None))
        assert "needing 8% a year -> -9.8%" in text and "negative figure means the price already assumes earnings will fall" in text
        assert "Treasury" not in text

    def test_the_bond_yield_skips_missing_observations_and_survives_an_outage(self, monkeypatch):
        monkeypatch.setattr(val.fred, "_request", lambda path, params: {"observations": [
            {"date": "2026-11-03", "value": "."}, {"date": "2026-11-02", "value": "4.91"}]})
        assert val._ten_year_yield() == (4.91, "2026-11-02")

        def down(path, params):
            raise VendorUnavailableError("FRED is down")

        monkeypatch.setattr(val.fred, "_request", down)
        assert val._ten_year_yield() is None


def _industry(rows):
    frame = pd.DataFrame(rows, columns=["symbol", "name", "market weight"]).set_index("symbol")
    return frame


def _info(cap_b, forward_pe=15.0, trailing_pe=20.0, name=None):
    return {"shortName": name, "marketCap": cap_b * 1e9, "forwardPE": forward_pe, "trailingPE": trailing_pe,
            "priceToSalesTrailing12Months": 5.0, "enterpriseToEbitda": 12.0, "revenueGrowth": 0.1, "profitMargins": 0.2}


class FakeYahoo:
    """Stands in for the yfinance module inside valuation."""

    def __init__(self, infos, industry, earnings=None, prices=None, estimates=None):
        self.infos, self._industry, self._earnings, self._prices = infos, industry, earnings, prices
        self._estimates = estimates
        outer = self

        class Ticker:
            def __init__(self, symbol):
                self.symbol = symbol

            @property
            def info(self):
                return outer.infos.get(self.symbol, {})

            @property
            def earnings_estimate(self):
                return outer._estimates

            def get_earnings_dates(self, limit=None):
                return outer._earnings

            def history(self, period=None, auto_adjust=None):
                return outer._prices

        class Industry:
            def __init__(self, key):
                self.top_companies = outer._industry

        self.Ticker, self.Industry = Ticker, Industry


@pytest.mark.unit
class TestPeers:
    def test_peers_nearest_in_size_are_shown_with_a_median_only_from_similar_ones(self, monkeypatch):
        industry = _industry([("AAA", "Alpha Inc", 0.5), ("BBB", "Beta Inc", 0.2), ("CCC", "Gamma Inc", 0.15),
                              ("DDD", "Delta Inc", 0.1), ("EEE", "Epsilon Inc", 0.03), ("FFF", "Zeta Inc", 0.02)])
        infos = {
            "TGT": {**_info(100, forward_pe=18.0, name="Target Corp"), "industryKey": "widgets"},
            "AAA": _info(300, forward_pe=40.0), "BBB": _info(120, forward_pe=14.0), "CCC": _info(80, forward_pe=12.0),
            "DDD": _info(60, forward_pe=20.0), "EEE": _info(10, forward_pe=-5.0), "FFF": _info(5, forward_pe=9.0),
        }
        monkeypatch.setattr(val, "yf", FakeYahoo(infos, industry))
        lines, figures = val._peer_section("TGT", infos["TGT"])
        text = "\n".join(lines)
        assert "TGT (this stock)" in text and "(1.20x)" in text           # BBB, as a multiple of the target's size
        assert figures["similar_peers"] == 4                              # AAA is 3x, BBB 1.2x, CCC 0.8x, DDD 0.6x
        assert figures["peer_forward_pe"] == 17.0                         # median of 40, 14, 12, 20 -> (14 + 20) / 2
        assert "Median of the similar-size peers" in text
        assert "4 of the 5 are within 0.25 to 4 times" in text

    def test_a_company_with_no_similar_peer_gets_no_median_and_no_reference_level(self, monkeypatch):
        industry = _industry([("BIG", "Big Corp", 0.6), ("BIG2", "Big Two", 0.3), ("BIG3", "Big Three", 0.1)])
        infos = {"TGT": {**_info(0.3, name="Tiny Co"), "industryKey": "widgets"},
                 "BIG": _info(50), "BIG2": _info(30), "BIG3": _info(10)}
        monkeypatch.setattr(val, "yf", FakeYahoo(infos, industry))
        lines, figures = val._peer_section("TGT", infos["TGT"])
        text = "\n".join(lines)
        assert "Median of the similar-size peers" not in text
        assert "here too few do" in text and figures["peer_forward_pe"] is None and figures["similar_peers"] == 0
        levels = "\n".join(val.reference_lines(
            2.0, {"forwardEps": 0.4}, {"own_median_pe": 8.0, "trailing_eps": 0.4}, figures))
        assert "peer-based level is not worked out: 0 peer(s) of similar size" in levels
        assert "peers' median" not in levels

    def test_the_target_itself_and_its_other_share_class_are_not_its_own_peers(self, monkeypatch):
        industry = _industry([("GOOG", "Alphabet Inc.", 0.6), ("GOOGL", "Alphabet Inc.", 0.3), ("META", "Meta", 0.1)])
        infos = {"GOOG": {**_info(3400, name="Alphabet Inc."), "industryKey": "web"}, "GOOGL": _info(3400), "META": _info(1500, name="Meta")}
        monkeypatch.setattr(val, "yf", FakeYahoo(infos, industry))
        lines, _ = val._peer_section("GOOG", infos["GOOG"])
        text = "\n".join(lines)
        assert "META Meta" in text and "GOOGL" not in text

    def test_a_security_with_no_industry_has_no_peers(self):
        lines, figures = val._peer_section("FUND", {"marketCap": 1e9})
        assert "no industry" in lines[0] and figures == {}


@pytest.mark.unit
class TestReferenceLevels:
    def test_levels_are_arithmetic_from_the_multiples_and_say_they_are_not_targets(self):
        lines = val.reference_lines(
            100.0, {"forwardEps": 8.0},
            {"own_median_pe": 20.0, "trailing_eps": 6.0}, {"peer_forward_pe": 15.0, "similar_peers": 4})
        text = "\n".join(lines)
        assert "not forecasts or targets" in text
        assert "own 10-year median trailing P/E of 20.0x on trailing reported EPS of 6.00: 120.00 (+20% from the price)" in text
        assert "median forward P/E of 15.0x on its next-fiscal-year EPS estimate of 8.00: 120.00 (+20% from the price)" in text

    def test_with_nothing_to_multiply_there_is_no_section(self):
        assert val.reference_lines(100.0, {}, {}, {}) == []


def _estimate_frame(this_year=(9.31, 9.01, 10.10, 51), next_year=(15.91, 12.85, 18.75, 52)):
    rows = {"0y": this_year, "+1y": next_year}
    return pd.DataFrame({key: dict(zip(("avg", "low", "high", "numberOfAnalysts"), values)) for key, values in rows.items()}).T


@pytest.mark.unit
class TestTwoYearEstimates:
    LABELS = ("this fiscal year (ending Jan 2027)", "next fiscal year (ending Jan 2028)")

    def test_both_fiscal_years_are_shown_with_their_yields_against_the_treasury(self):
        frame = _estimate_frame()
        parsed = {name: {"eps": float(frame.loc[key, "avg"]), "low": float(frame.loc[key, "low"]),
                         "high": float(frame.loc[key, "high"]), "analysts": float(frame.loc[key, "numberOfAnalysts"])}
                  for key, name in (("0y", "this"), ("+1y", "next"))}
        text = "\n".join(val.estimate_lines(204.41, parsed, self.LABELS, (4.91, "2026-11-02")))
        assert "Yahoo's 'forward EPS' and forward P/E are the NEXT fiscal year's estimate" in text
        assert "- this fiscal year (ending Jan 2027): EPS 9.31 from 51 analysts (range 9.01 to 10.10); P/E 22.0x; earnings yield 4.6%." in text
        assert "- next fiscal year (ending Jan 2028): EPS 15.91 from 52 analysts (range 12.85 to 18.75); P/E 12.8x; earnings yield 7.8%." in text
        assert "0.4 percentage points below it on this year's estimate and 2.9 percentage points above it on next year's estimate" in text

    def test_the_growth_is_worked_from_this_year_and_the_other_year_is_compared(self):
        parsed = {"this": {"eps": 9.31, "low": None, "high": None, "analysts": None},
                  "next": {"eps": 15.91, "low": None, "high": None, "analysts": None}}
        text = "\n".join(val.estimate_lines(204.41, parsed, self.LABELS, None))
        assert "(worked from this fiscal year (ending Jan 2027))" in text
        assert "needing 8% a year -> 3.4%; needing 9% a year -> 4.4%; needing 10% a year -> 5.4%" in text
        assert "Worked from next fiscal year (ending Jan 2028) instead, those growth figures would be about 3.2 points lower." in text
        assert "Treasury" not in text

    def test_a_loss_year_is_named_and_two_loss_years_define_nothing(self):
        losses = {"this": {"eps": -4.08, "low": None, "high": None, "analysts": 9.0},
                  "next": {"eps": -3.14, "low": None, "high": None, "analysts": 9.0}}
        text = "\n".join(val.estimate_lines(28.9, losses, self.LABELS, (4.9, "d")))
        assert "EPS -4.08 from 9 analysts; a loss, so no P/E or earnings yield." in text
        assert "Earnings are not positive in either year" in text and "Treasury" not in text

    def test_the_fiscal_year_labels_come_from_the_next_fiscal_year_end(self):
        stamp = int(pd.Timestamp("2027-01-31").timestamp())
        assert val._fiscal_labels({"nextFiscalYearEnd": stamp}) == self.LABELS
        assert val._fiscal_labels({}) == ("this fiscal year", "next fiscal year")

    def test_the_estimates_are_read_from_yahoos_table_and_missing_ones_give_none(self, monkeypatch):
        monkeypatch.setattr(val, "yf", FakeYahoo({}, _industry([("A", "A", 1.0)]), estimates=_estimate_frame()))
        parsed = val._estimates("TGT")
        assert parsed["this"]["eps"] == 9.31 and parsed["next"]["analysts"] == 52.0
        monkeypatch.setattr(val, "yf", FakeYahoo({}, _industry([("A", "A", 1.0)]), estimates=None))
        assert val._estimates("TGT") is None

    def test_the_section_falls_back_to_yahoos_forward_eps_and_names_what_it_is(self, monkeypatch):
        monkeypatch.setattr(val, "yf", FakeYahoo({}, _industry([("A", "A", 1.0)]), estimates=None))
        monkeypatch.setattr(val, "_ten_year_yield", lambda: (4.9, "2026-11-02"))
        text = "\n".join(val._implied_section("TGT", 40.0, {"forwardEps": 5.0, "currency": "USD"}))
        assert "Earnings yield on Yahoo's forward earnings (normally the next fiscal year's estimate): 12.5%" in text

    def test_the_whole_report_carries_the_two_year_table_when_yahoo_has_the_estimates(self, monkeypatch):
        _fake_world(monkeypatch, estimates=_estimate_frame((5.0, 4.8, 5.3, 20), (8.0, 7.0, 9.0, 21)))
        out = val.get_valuation_context("TGT", TODAY)
        assert "Analyst consensus EPS for two fiscal years" in out and "EPS 5.00 from 20 analysts" in out
        assert "forward P/E 8.0x (on the next fiscal year's estimate)" in out


def _fake_world(monkeypatch, *, info=None, industry=None, earnings=True, bond=(4.9, "2026-11-02"), estimates=None):
    info = info or {"currentPrice": 40.0, "currency": "USD", "industryKey": "widgets", "shortName": "Target Corp",
                    "marketCap": 100e9, "trailingPE": 10.0, "forwardPE": 8.0, "forwardEps": 5.0, "trailingEps": 4.0,
                    "pegRatio": 1.2}
    reports = _quarters(24)
    earn = pd.DataFrame({"EPS Estimate": 1.0, "Reported EPS": reports.to_numpy()},
                        index=reports.index.tz_localize("America/New_York"))
    prices = pd.DataFrame({"Close": _closes().to_numpy()}, index=_closes().index.tz_localize("America/New_York"))
    peers = industry if industry is not None else _industry([("TGT", "Target Corp", 0.5), ("P1", "Peer One", 0.2), ("P2", "Peer Two", 0.1)])
    infos = {"TGT": info, "P1": _info(90, forward_pe=12.0), "P2": _info(150, forward_pe=14.0)}
    monkeypatch.setattr(val, "yf", FakeYahoo(infos, peers, earn if earnings else None, prices, estimates))
    monkeypatch.setattr(val, "_ten_year_yield", lambda: bond)


@pytest.mark.unit
class TestEntryPoint:
    def test_a_run_dated_in_the_past_is_withheld(self):
        out = val.get_valuation_context("GOOG", "2020-01-02")
        assert "Withheld for 2020-01-02" in out and "present-day values" in out

    def test_every_section_is_served_in_order_with_the_framing(self, monkeypatch):
        _fake_world(monkeypatch)
        out = val.get_valuation_context("TGT", TODAY)
        order = ["## Where the price stands", "## Against its own history", "## Against its peers",
                 "## What the price implies", "## Reference price levels"]
        assert [out.index(h) for h in order] == sorted(out.index(h) for h in order)
        assert "none of them is a fair-value estimate" in out
        assert "Now: 10.0x (price 40.00 over the last four reported quarters' EPS of 4.00)" in out
        assert "10-year Treasury yield: 4.90%" in out

    def test_one_failing_section_does_not_take_the_others_down(self, monkeypatch):
        _fake_world(monkeypatch, earnings=False)        # no earnings history: the history section says so
        out = val.get_valuation_context("TGT", TODAY)
        assert "No reported-earnings history is available" in out and "## What the price implies" in out

    def test_losses_mean_no_pe_history_and_no_earnings_yield(self, monkeypatch):
        info = {"currentPrice": 40.0, "currency": "USD", "industryKey": "widgets", "shortName": "Target Corp",
                "marketCap": 100e9, "forwardEps": -1.0, "trailingEps": -2.0}
        _fake_world(monkeypatch, info=info)
        out = val.get_valuation_context("TGT", TODAY)
        assert "Earnings are not positive" in out

    def test_without_a_quote_or_with_most_sections_broken_it_reports_the_vendor_as_unavailable(self, monkeypatch):
        _fake_world(monkeypatch)
        monkeypatch.setattr(val, "_history_section", lambda *a: (_ for _ in ()).throw(VendorUnavailableError("down")))
        monkeypatch.setattr(val, "_peer_section", lambda *a: (_ for _ in ()).throw(VendorUnavailableError("down")))
        monkeypatch.setattr(val, "_implied_section", lambda *a: (_ for _ in ()).throw(VendorUnavailableError("down")))
        with pytest.raises(VendorUnavailableError):
            val.get_valuation_context("TGT", TODAY)

    def test_the_fundamentals_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import fundamentals_analyst
        from tradingagents.dataflows import router

        assert "get_valuation_context" in [t.name for t in fundamentals_analyst.TOOLS]
        assert router.get_category_for_method("get_valuation_context") == "fundamental_data"
        assert "yfinance" in router.VENDOR_METHODS["get_valuation_context"]


@pytest.mark.unit
def test_the_portfolio_manager_is_told_not_to_copy_the_street_target():
    from tradingagents.agents.schemas import PortfolioDecision

    description = PortfolioDecision.model_fields["price_target"].description
    assert "Do not copy the Wall Street mean" in description and "otherwise leave it empty" in description
    assert "A stop, support or resistance level is not a target" in description


_ = SimpleNamespace  # kept for readability of the stubs above
