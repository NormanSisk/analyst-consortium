"""Tests for the Quiver vendor: Congress trades and Form 4 insider transactions."""

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import router
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.vendors import quiver

TODAY = pd.Timestamp(get_current_date())
TOKEN = "test-token-123"


def _day(offset: int) -> str:
    return (TODAY - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


@pytest.fixture(autouse=True)
def _forget_refusals():
    quiver._REFUSED.clear()
    yield
    quiver._REFUSED.clear()


@pytest.fixture
def api(monkeypatch):
    """Fake Quiver: maps a path to a response and records every call."""
    monkeypatch.setenv(quiver.KEY_ENV, TOKEN)
    calls, answers = [], {}

    def fake_get(url, params=None, headers=None, timeout=None):
        path = url.replace(quiver.API_BASE + "/", "")
        calls.append({"path": path, "params": params, "headers": headers})
        answer = answers.get(path, _Response(200, []))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(quiver.requests, "get", fake_get)
    return type("Api", (), {"calls": calls, "answers": answers})()


def _congress(filed_ago, traded_ago, who="Nancy Pelosi", kind="Purchase", rng="$500,001 - $1,000,000", ticker="BE"):
    return {"Representative": who, "ReportDate": _day(filed_ago), "TransactionDate": _day(traded_ago),
            "Ticker": ticker, "Transaction": kind, "Range": rng, "House": "Representatives", "Party": "D"}


@pytest.mark.unit
class TestRequests:
    def test_a_missing_token_is_a_configuration_error(self, monkeypatch):
        monkeypatch.delenv(quiver.KEY_ENV, raising=False)
        with pytest.raises(VendorNotConfiguredError):
            quiver.get_congress_trades("AAPL")

    def test_the_token_goes_in_a_header_and_never_in_the_url(self, api):
        quiver.get_congress_trades("AAPL")
        call = api.calls[0]
        assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in call["path"] and TOKEN not in str(call["params"])

    @pytest.mark.parametrize("status, error", [
        (401, VendorNotConfiguredError), (403, quiver.QuiverPlanError), (429, VendorUnavailableError),
        (500, VendorUnavailableError)])
    def test_http_failures_map_to_router_errors(self, api, status, error):
        api.answers["historical/congresstrading/AAPL"] = _Response(status)
        with pytest.raises(error):
            quiver.get_congress_trades("AAPL")

    def test_a_network_failure_is_unavailable_and_does_not_quote_the_request(self, api):
        api.answers["historical/congresstrading/AAPL"] = requests.ConnectionError(f"failed for token {TOKEN}")
        with pytest.raises(VendorUnavailableError) as raised:
            quiver.get_congress_trades("AAPL")
        assert TOKEN not in str(raised.value)

    def test_a_ticker_with_no_record_reads_as_no_trades_not_an_error(self, api):
        api.answers["historical/congresstrading/AAPL"] = _Response(404)
        assert "No disclosed trades" in quiver.get_congress_trades("AAPL")

    def test_non_us_symbols_make_no_request(self, api):
        out = quiver.get_congress_trades("7203.T")
        assert "US-listed" in out and not api.calls


@pytest.mark.unit
class TestCongress:
    def test_window_lag_and_summary(self, api):
        api.answers["historical/congresstrading/BE"] = _Response(200, [
            _congress(20, 44),                                      # 24 days late, newest
            _congress(60, 90, who="Kevin Hern", kind="Sale (Full)", rng="$15,001 - $50,000"),  # 30 late
            _congress(400, 430),                                    # too old
        ])
        out = quiver.get_congress_trades("BE")
        assert "Disclosures: 2, of which 1 purchase (at least $500K) and 1 sale (at least $15K)" in out
        assert "Typical delay between trade and filing: 27 days (longest 30)" in out
        assert "Newest filing" in out and "20 days before" in out
        assert "Nancy Pelosi (D-House)" in out and "Kevin Hern" in out
        assert _day(400) not in out

    def test_a_dated_run_sees_only_what_was_public_then(self, api):
        api.answers["historical/congresstrading/BE"] = _Response(200, [
            _congress(100, 130), _congress(20, 50)])
        as_of = _day(60)                                           # the 20-days-ago filing is still in the future
        out = quiver.get_congress_trades("BE", as_of)
        assert _day(100) in out
        assert _day(20) not in out

    def test_both_alphabet_classes_are_queried(self, api):
        api.answers["historical/congresstrading/GOOG"] = _Response(200, [_congress(5, 30, ticker="GOOG")])
        api.answers["historical/congresstrading/GOOGL"] = _Response(200, [_congress(9, 20, ticker="GOOGL")])
        out = quiver.get_congress_trades("GOOG")
        assert [c["path"] for c in api.calls] == ["historical/congresstrading/GOOG", "historical/congresstrading/GOOGL"]
        assert "Disclosures: 2" in out and "GOOG / GOOGL" in out
        assert "[GOOG]" in out and "[GOOGL]" in out       # each row says which share class traded

    def test_field_names_in_other_cases_are_understood(self, api):
        api.answers["historical/congresstrading/BE"] = _Response(200, [{
            "representative": "Nancy Pelosi", "report_date": _day(10), "transaction_date": _day(30),
            "transaction": "Purchase", "range": "$1,001 - $15,000", "party": "D"}])
        assert "Disclosures: 1" in quiver.get_congress_trades("BE")


def _insider(traded_ago, code, shares, price, filed_ago=None, who="DOE JANE", title="CFO", **extra):
    row = {"ticker": "AAPL", "date": _day(traded_ago), "file_date": _day(filed_ago if filed_ago is not None else traded_ago - 2),
           "owner": who, "transaction_type": code, "acquired_disposed": "D", "shares": shares, "price": price,
           "shares_owned_after": 1000.0, "officer_title": title, "is_director": False, "is_officer": True}
    row.update(extra)
    return row


@pytest.mark.unit
class TestInsiders:
    def test_sales_are_totalled_and_routine_filings_only_counted(self, api):
        api.answers["live/insiders"] = _Response(200, [
            _insider(10, "S", 1000, 250.0),
            _insider(12, "S", 500, 240.0, who="ROE RICHARD"),
            _insider(11, "F", 300, 250.0),
            _insider(40, "A", 2000, 0.0),
            _insider(300, "S", 9000, 100.0),                        # outside the 120-day window
        ])
        out = quiver.get_insider_transactions("AAPL")
        assert "Open-market sales: 2, about $370K by 2 insiders" in out
        assert "Open-market purchases: 0" in out
        assert "1 x shares withheld for taxes" in out and "1 x award or grant" in out
        assert "9,000" not in out
        assert "Doe Jane" in out

    def test_alphabet_filings_are_asked_for_under_googl(self, api):
        api.answers["live/insiders"] = _Response(200, [_insider(5, "S", 82, 284.0, who="ARNOLD FRANCES")])
        out = quiver.get_insider_transactions("GOOG")
        assert api.calls[0]["params"]["ticker"] == "GOOGL"
        assert "Open-market sales: 1" in out

    def test_a_plan_without_the_live_endpoint_falls_back_to_the_ticker_endpoint(self, api):
        api.answers["live/insiders"] = _Response(403)
        api.answers["mobile/stock/AAPL/insiders"] = _Response(200, {"trades": [_insider(5, "P", 100, 200.0)]})
        out = quiver.get_insider_transactions("AAPL")
        assert [c["path"] for c in api.calls] == ["live/insiders", "mobile/stock/AAPL/insiders"]
        assert "Open-market purchases: 1, about $20K" in out

    def test_a_refusal_by_every_endpoint_is_unavailable_so_the_router_moves_on(self, api):
        api.answers["live/insiders"] = _Response(403)
        api.answers["mobile/stock/AAPL/insiders"] = _Response(403)
        with pytest.raises(VendorUnavailableError):
            quiver.get_insider_transactions("AAPL")

    def test_a_dated_run_uses_filing_dates_and_withholds_undated_records(self, api):
        as_of = _day(60)
        api.answers["live/insiders"] = _Response(200, [
            _insider(70, "S", 100, 50.0, filed_ago=65),             # public by the run date
            _insider(75, "S", 100, 50.0, filed_ago=30),             # filed later: not yet public
        ])
        out = quiver.get_insider_transactions("AAPL", as_of)
        assert "Open-market sales: 1" in out

        undated = _insider(70, "S", 100, 50.0)
        del undated["file_date"]
        api.answers["live/insiders"] = _Response(200, [undated])
        assert "cannot show which trades were public" in quiver.get_insider_transactions("AAPL", as_of)

    def test_an_empty_window_says_so(self, api):
        api.answers["live/insiders"] = _Response(200, [])
        assert "No Form 4 filings" in quiver.get_insider_transactions("AAPL")


def _real_insider(traded_ago, filed_ago, kind="Sale", shares=1000, price=100.0, who="SEEGER LAUREEN", title="Chief Legal Officer"):
    """A record in the shape Quiver's ticker endpoint actually returns."""
    traded = (TODAY - pd.Timedelta(days=traded_ago)).strftime("%b %d, %Y")
    filed = (TODAY - pd.Timedelta(days=filed_ago)).strftime("%Y-%m-%dT17:12:05")
    return {"owner_name": who, "transaction_type": kind, "shares": shares, "price_per_share": price,
            "shares_after": 5000, "transaction_date": traded, "file_date": filed, "title": title, "pct_change": -0.5}


@pytest.mark.unit
class TestRealResponseShapes:
    def test_the_ticker_endpoint_shape_is_read(self, api):
        api.answers["live/insiders"] = _Response(403)
        api.answers["mobile/stock/AXP/insiders"] = _Response(200, {
            "insider_trades": [_real_insider(2, 1, shares=69860, price=328.17),
                               _real_insider(9, 8, "Purchase", 100, 50.0, who="DOE JANE", title=None)],
            "insider_sentiment": [{"quarter": "2026Q3", "sentiment": -5.0}]})
        out = quiver.get_insider_transactions("AXP")
        assert "Open-market sales: 1, about $22.9M by 1 insider." in out
        assert "Open-market purchases: 1, about $5K by 1 insider." in out
        assert "Seeger Laureen" in out and "Chief Legal Officer" in out
        assert "open-market sales and purchases only" in out

    def test_a_refused_endpoint_is_not_asked_again(self, api):
        api.answers["live/insiders"] = _Response(403)
        api.answers["mobile/stock/AXP/insiders"] = _Response(200, {"insider_trades": []})
        quiver.get_insider_transactions("AXP")
        quiver.get_insider_transactions("AXP")
        assert [c["path"] for c in api.calls].count("live/insiders") == 1

    def test_paging_stops_once_filings_are_older_than_the_window(self, api, monkeypatch):
        monkeypatch.setattr(quiver, "PAGE_SIZE", 2)
        api.answers["live/insiders"] = _Response(403)
        recent = [_real_insider(3, 2), _real_insider(4, 3)]
        old = [_real_insider(400, 399), _real_insider(401, 400)]
        pages = {1: recent, 2: old, 3: old}
        paths = []

        def fake_get(url, params=None, headers=None, timeout=None):
            path = url.replace(quiver.API_BASE + "/", "")
            paths.append((path, (params or {}).get("page")))
            if path == "live/insiders":
                return _Response(403)
            return _Response(200, {"insider_trades": pages[params["page"]]})

        monkeypatch.setattr(quiver.requests, "get", fake_get)
        quiver.get_insider_transactions("AXP")
        pages_asked = [p for path, p in paths if path.startswith("mobile")]
        assert pages_asked == [1, 2]        # page 2 reached filings from over a year ago; page 3 never asked


@pytest.mark.unit
class TestCongressDetails:
    def _record(self, **extra):
        row = {"Representative": "Nancy Pelosi", "ReportDate": _day(20), "TransactionDate": _day(44),
               "Ticker": "BE", "Transaction": "Purchase", "Range": "$500,001 - $1,000,000",
               "House": "Representatives", "Party": "Democratic", "TickerType": "ST", "PriceChange": 27.8}
        row.update(extra)
        return row

    def test_options_party_chamber_and_price_move_are_shown(self, api):
        api.answers["historical/congresstrading/BE"] = _Response(200, [
            self._record(TickerType="OP", Description="PURCHASED 100 CALL OPTIONS WITH A STRIKE PRICE OF $100."),
            self._record()])
        out = quiver.get_congress_trades("BE")
        assert "Nancy Pelosi (D-House)" in out
        assert "Purchase (options)" in out
        assert "1 row involves option contracts, not shares." in out
        assert "Stock move since trade" in out and "+27.8%" in out
        assert "PURCHASED 100 CALL OPTIONS" in out

    def test_a_dated_run_leaves_out_the_price_move_that_would_leak_the_future(self, api):
        api.answers["historical/congresstrading/BE"] = _Response(200, [self._record(ReportDate=_day(100), TransactionDate=_day(130))])
        out = quiver.get_congress_trades("BE", _day(60))
        assert "Stock move since trade" not in out and "+27.8%" not in out
        assert _day(100) in out


@pytest.mark.unit
class TestRouting:
    def test_the_vendor_is_registered_for_both_methods(self):
        assert "quiver" in router.VENDOR_METHODS["get_insider_transactions"]
        assert "quiver" in router.VENDOR_METHODS["get_congress_trades"]
        assert router.get_category_for_method("get_congress_trades") == "congress_data"

    def test_without_a_token_insiders_fall_back_to_yahoo_and_congress_degrades(self, monkeypatch):
        monkeypatch.delenv(quiver.KEY_ENV, raising=False)
        monkeypatch.setitem(router.VENDOR_METHODS["get_insider_transactions"], "yfinance", lambda *a, **k: "yahoo rows")
        assert router.route_to_vendor("get_insider_transactions", "AAPL", None) == "yahoo rows"
        out = router.route_to_vendor("get_congress_trades", "AAPL", None)
        assert out.startswith("DATA_UNAVAILABLE") and "QUIVER_API_KEY" in out
