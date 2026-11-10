"""Tests for the IBKR market-statistics vendor (a local addition): worked-out figures, flags, and the helper."""

import json
import subprocess

import pytest

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import ibkr_market_stats as stats

KOPN = {
    "last": {"price": 3.04, "ts": 1794010689, "halted": False, "is_close": False},
    "change": {"change": 0.29, "change_pct": 10.55},
    "volume": {"volume": 4459092.14404},
    "avg-90d-usd-volume": {"volume": 11334576},
    "misc-statistics": {"high_13w": 5.525000095367432, "high_26w": 6.610000133514404, "high_52w": 6.610000133514404,
                        "low_13w": 2.6500000953674316, "low_26w": 2.6500000953674316, "low_52w": 1.809999942779541,
                        "open_52w": 2.7000999450683594},
    "historical-vol": {"daily_pct": 0.055511, "annual_pct": 0.881209806171039},
    "implied-vol-underlying": {"daily_iv": 0.0783732810886073, "annual_iv": 1.2441372671556987, "is_valid": True},
    "implied-volatility-percentile": {"high_13w": 0.9538461565971375, "high_26w": 0.787401556968689,
                                      "high_52w": 0.7768924236297607},
    "year-to-date-change": {"change": 0.7000000858306885, "change_pct": 29.91},
    "top-status": {"status": "FROZEN"},
}
GRML = {
    "last": {"price": 3.85, "ts": 1794012117}, "change": {"change": 0.28, "change_pct": 7.84},
    "volume": {"volume": 518658.238907}, "avg-90d-usd-volume": {"volume": 40409221},
    "misc-statistics": {"high_13w": 18.21, "high_26w": 23.78, "high_52w": 36.99, "low_13w": 2.82, "low_26w": 2.82,
                        "low_52w": 2.82, "open_52w": 23.5},
    "historical-vol": {"annual_pct": 1.7296832022200135},
    "implied-vol-underlying": {"annual_iv": 5.518830190984577, "is_valid": True},
    "year-to-date-change": {"change_pct": -73.36}, "top-status": {"status": "FROZEN"},
}
NVDA = {
    "last": {"price": 188.7, "ts": 1794013199}, "volume": {"volume": 1.80293588785681e8},
    "avg-90d-usd-volume": {"volume": 22143752356},
    "misc-statistics": {"high_13w": 243.37, "high_26w": 243.37, "high_52w": 243.37, "low_13w": 181.46,
                        "low_26w": 181.46, "low_52w": 164.08, "open_52w": 184.66},
    "historical-vol": {"annual_pct": 0.38462027619313055},
    "implied-vol-underlying": {"annual_iv": 0.555982943777447, "is_valid": True},
    "implied-volatility-percentile": {"high_13w": 0.9846, "high_26w": 0.9921, "high_52w": 0.9960},
    "top-status": {"status": "FROZEN"},
}
CONTRACT = {"contract_id": 271009, "symbol": "KOPN", "description": "KOPIN CORP", "exchange": "NASDAQ"}


def _flags(text: str) -> list[str]:
    section = text.split("## Flags to state in the report\n")[1]
    return [line[2:] for line in section.splitlines() if line.startswith("- ")]


@pytest.mark.unit
class TestReport:
    def test_price_ranges_and_year_figures_are_worked_out_against_the_last_price(self):
        text = stats.build_report("KOPN", CONTRACT, KOPN)
        assert text.startswith("# Market statistics for KOPN (IBKR, present-day figures)")
        assert "IBKR's figures for KOPIN CORP (NASDAQ). Quote status: the market was closed, so these are the last " \
               "session's figures." in text
        assert "Last price 3.04 at 2026-11-07 00:18 UTC; +0.29 (+10.6%) from the prior close, as IBKR reports it." in text
        assert "52-week range 1.81 to 6.61: the last price is 54% below the high and 68% above the low." in text
        assert "13-week range 2.65 to 5.525: the last price is 45% below the high and 15% above the low." in text
        assert "so the price is +12.6% over the year" in text and "Year to date: +29.9%" in text

    def test_volatility_is_given_both_ways_with_the_implied_moves_as_arithmetic(self):
        text = stats.build_report("KOPN", CONTRACT, KOPN)
        assert "Options-implied volatility, annualized: 124%." in text
        assert "Realized volatility over the last 30 days, annualized: 88%." in text
        assert "Implied volatility is 1.4 times realized volatility." in text
        assert "13 weeks 95%, 26 weeks 79%, 52 weeks 78%" in text
        assert "as arithmetic and not a forecast, a target or a limit: about ±18% over 5 trading days and ±36% over 21." in text

    def test_volume_is_compared_with_the_ninety_day_average(self):
        text = stats.build_report("KOPN", CONTRACT, KOPN)
        assert "90-day average dollar volume: $11,334,576 a day." in text
        assert "The latest session traded 4,459,092 shares" in text and "1.2 times that average." in text

    def test_a_steady_high_volatility_stock_gets_only_the_high_volatility_flag(self):
        flags = _flags(stats.build_report("KOPN", CONTRACT, KOPN))
        assert len(flags) == 1 and flags[0].startswith("Implied volatility is 124% a year, 100% or more")
        assert "can come from a thin options market" in flags[0]

    def test_an_extreme_small_stock_is_flagged_three_ways_and_has_no_percentile_line(self):
        text = stats.build_report("GRML", {"description": "GREENLAND MINES", "exchange": "NASDAQ"}, GRML)
        flags = _flags(text)
        assert any(f.startswith("Implied volatility is 552% a year") for f in flags)
        assert "Implied volatility is 3.2 times realized volatility: the options market prices more movement" in " ".join(flags)
        assert any("0.05 times the 90-day average; a few very large sessions can dominate that average." in f for f in flags)
        assert "Implied-volatility percentile" not in text

    def test_an_implied_move_beyond_one_hundred_percent_is_explained_and_a_normal_one_is_not(self):
        extreme = stats.build_report("GRML", {}, GRML)
        assert "±78% over 5 trading days and ±159% over 21. A figure above 100% is only the arithmetic" in extreme
        assert "A figure above 100%" not in stats.build_report("KOPN", CONTRACT, KOPN)

    def test_an_implied_volatility_at_the_top_of_its_year_is_flagged(self):
        flags = _flags(stats.build_report("NVDA", {"description": "NVIDIA CORP"}, NVDA))
        assert flags == ["Implied volatility is at the 100th percentile of its 52-week range: high against its own history."]

    def test_a_low_percentile_and_implied_below_realized_are_flagged(self):
        snapshot = json.loads(json.dumps(NVDA))
        snapshot["implied-volatility-percentile"]["high_52w"] = 0.05
        snapshot["implied-vol-underlying"]["annual_iv"] = 0.20
        flags = _flags(stats.build_report("NVDA", {}, snapshot))
        assert any("only 0.5 times realized volatility" in f and "less movement" in f for f in flags)
        assert any("5th percentile" in f and "low against its own history" in f for f in flags)

    def test_a_price_at_the_edge_of_its_year_is_flagged(self):
        high = json.loads(json.dumps(NVDA))
        high["last"]["price"] = 240.0
        assert any("within 5% of its 52-week high (243.37)" in f for f in _flags(stats.build_report("NVDA", {}, high)))
        low = json.loads(json.dumps(NVDA))
        low["last"]["price"] = 166.0
        assert any("within 5% of its 52-week low (164.08)" in f for f in _flags(stats.build_report("NVDA", {}, low)))

    def test_missing_implied_volatility_is_said_not_guessed(self):
        snapshot = json.loads(json.dumps(KOPN))
        snapshot["implied-vol-underlying"] = {"daily_iv": 0.0, "annual_iv": 0.0, "is_valid": False}
        text = stats.build_report("KOPN", CONTRACT, snapshot)
        assert "No valid options-implied volatility is available" in text
        assert "standard deviation" not in text and "times realized volatility" not in text

    def test_the_quote_status_is_named(self):
        live = json.loads(json.dumps(KOPN))
        live["top-status"]["status"] = "REALTIME"
        assert "Quote status: a real-time quote." in stats.build_report("KOPN", CONTRACT, live)
        live["top-status"]["status"] = "DELAYED"
        assert "Quote status: a delayed quote." in stats.build_report("KOPN", CONTRACT, live)

    def test_a_snapshot_with_no_price_is_no_data(self):
        with pytest.raises(NoMarketDataError):
            stats.build_report("KOPN", CONTRACT, {"top-status": {"status": "REJECT"}})


@pytest.mark.unit
class TestEntryPoint:
    def test_a_run_dated_in_the_past_is_withheld(self):
        out = stats.get_market_statistics("GOOG", "2020-01-02")
        assert "withheld for this date" in out and "Point-in-time as of: 2020-01-02" in out

    def test_an_answer_from_the_helper_becomes_the_report(self, monkeypatch):
        monkeypatch.setattr(stats, "_run_helper", lambda symbol: {"status": "ok", "contract": CONTRACT, "snapshot": KOPN})
        assert "## Volatility" in stats.get_market_statistics("kopn", None)

    def test_an_unknown_symbol_is_no_data_and_a_lapsed_sign_in_is_an_outage_with_the_fix(self, monkeypatch):
        monkeypatch.setattr(stats, "_run_helper", lambda symbol: {"status": "not_found"})
        with pytest.raises(NoMarketDataError):
            stats.get_market_statistics("ZZZZ", None)
        monkeypatch.setattr(stats, "_run_helper", lambda symbol: {"status": "needs_sign_in"})
        with pytest.raises(VendorUnavailableError, match=r"sign in again with .*probe\.py"):
            stats.get_market_statistics("KOPN", None)
        monkeypatch.setattr(stats, "_run_helper", lambda symbol: {"status": "unavailable", "reason": "ReadError"})
        with pytest.raises(VendorUnavailableError, match="ReadError"):
            stats.get_market_statistics("KOPN", None)

    def test_symbols_that_are_not_plain_stocks_never_reach_the_helper(self, monkeypatch):
        monkeypatch.setattr(stats, "_run_helper", lambda symbol: pytest.fail("the helper should not run"))
        for symbol in ("GC=F", "^GSPC", "BTC-USD", "9992.HK", "EURUSD=X"):
            with pytest.raises(NoMarketDataError):
                stats.get_market_statistics(symbol, None)

    def test_a_lapsed_sign_in_degrades_to_a_sentence_through_the_router(self, monkeypatch):
        from tradingagents.dataflows import router

        def lapsed(*args, **kwargs):
            raise VendorUnavailableError("the IBKR sign-in has lapsed")

        monkeypatch.setitem(router.VENDOR_METHODS["get_market_statistics"], "ibkr", lapsed)
        monkeypatch.setattr(router, "get_vendor", lambda category, method=None: "ibkr")       # Yahoo is the default now
        out = router.route_to_vendor("get_market_statistics", "KOPN", None)
        assert out.startswith("DATA_UNAVAILABLE: no configured vendor could serve get_market_statistics")
        assert "the IBKR sign-in has lapsed" in out and "do not estimate or fabricate values" in out

    def test_the_router_still_knows_the_ibkr_vendor_though_yahoo_is_the_default(self):
        from tradingagents.dataflows import router

        assert router.get_category_for_method("get_market_statistics") == "market_statistics"
        assert "market_statistics" in router.OPTIONAL_CATEGORIES
        assert "ibkr" in router.VENDOR_METHODS["get_market_statistics"]


@pytest.mark.unit
class TestHelperProcess:
    @pytest.fixture
    def installed(self, tmp_path, monkeypatch):
        (tmp_path / "venv" / "Scripts").mkdir(parents=True)
        (tmp_path / "venv" / "Scripts" / "python.exe").write_text("")
        (tmp_path / "fetch_stats.py").write_text("")
        monkeypatch.setenv(stats.HELPER_DIR_ENV, str(tmp_path))
        return tmp_path

    def test_a_missing_helper_is_a_configuration_problem(self, tmp_path, monkeypatch):
        monkeypatch.setenv(stats.HELPER_DIR_ENV, str(tmp_path / "nowhere"))
        with pytest.raises(VendorNotConfiguredError, match="IBKR_HELPER_DIR"):
            stats._run_helper("KOPN")

    def test_the_helper_runs_in_its_own_folder_without_trace_output_and_its_json_is_read(self, installed, monkeypatch):
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(command=command, **kwargs)
            return subprocess.CompletedProcess(command, 0, stdout='{"status": "not_found"}', stderr="noise")

        monkeypatch.setenv("IBKR_TRACE", "1")
        monkeypatch.setattr(stats.subprocess, "run", fake_run)
        assert stats._run_helper("KOPN") == {"status": "not_found"}
        assert seen["command"][-1] == "KOPN" and seen["command"][1].endswith("fetch_stats.py")
        assert seen["cwd"] == str(installed) and "IBKR_TRACE" not in seen["env"]
        assert seen["timeout"] == stats.HELPER_SECONDS and seen["capture_output"] is True

    def test_a_timeout_a_failed_start_and_garbage_output_are_outages(self, installed, monkeypatch):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        def cannot_start(command, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(stats.subprocess, "run", timeout)
        with pytest.raises(VendorUnavailableError, match="took longer"):
            stats._run_helper("KOPN")
        monkeypatch.setattr(stats.subprocess, "run", cannot_start)
        with pytest.raises(VendorUnavailableError, match="could not start"):
            stats._run_helper("KOPN")
        for output in ("", "Traceback...", "[1, 2]"):
            monkeypatch.setattr(stats.subprocess, "run",
                                lambda command, _o=output, **kw: subprocess.CompletedProcess(command, 1, stdout=_o))
            with pytest.raises(VendorUnavailableError, match="unreadable"):
                stats._run_helper("KOPN")
