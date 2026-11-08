"""The Find stocks panel: the background search manager, its endpoints, and the page's files.

No network and no cost: the picker runs against the made-up markets in tests/picker_world.py, and the page is checked as
text (its ids, that it uses no inline code, and that every id the script reaches for exists)."""

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from tests import picker_world as w  # noqa: E402
from tradingagents.picker import ledger, quicklook  # noqa: E402
from tradingagents.ui import picker_jobs as pj  # noqa: E402

_REAL_CONNECT, _REAL_CONNECT_EX, _REAL_GETADDRINFO = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def _loopback_only(_no_network, monkeypatch):
    """The suite blocks the network; the in-process test client needs this computer's own loopback and nothing else."""
    def ok(address):
        return isinstance(address, tuple) and bool(address) and address[0] in _LOOPBACK

    def refuse(address):
        raise OSError(f"test tried to reach the network: {address}")

    monkeypatch.setattr(socket.socket, "connect", lambda self, a: _REAL_CONNECT(self, a) if ok(a) else refuse(a))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, a: _REAL_CONNECT_EX(self, a) if ok(a) else refuse(a))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: _REAL_GETADDRINFO(host, *a, **k) if host in _LOOPBACK else refuse((host,)))


def wait_done(manager, job_id, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        job = manager.get(job_id)
        if job["state"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("the search did not finish")


def manager(tmp_path, provider=None, **kw):
    return pj.PickerManager(tmp_path / "picker", provider if provider is not None else w.pullback_world(), workers=1, **kw)


@pytest.mark.unit
class TestOptions:
    def test_the_page_is_offered_three_recipes_and_the_few_settings_it_may_change(self, tmp_path):
        o = manager(tmp_path).options()
        assert [r["id"] for r in o["recipes"]] == ["beaten-down", "list", "custom"] and all(r["label"] and r["help"] for r in o["recipes"])
        names = {s["name"]: s for s in o["settings"]}
        assert set(names) == {"below_high", "min_market_cap", "min_avg_volume", "universe_limit", "top"}
        assert names["below_high"]["default"] == 0.20 and names["below_high"]["scale"] == 100
        assert names["min_market_cap"]["default"] == 2e9 and names["top"]["default"] == 15
        assert all(s["for"] == ["beaten-down"] for s in o["settings"])        # none of them means anything for a named list
        assert o["busy"] is False and o["latest"] is None and o["ledger_path"].endswith("picks.csv")

    def test_the_options_carry_the_catalog_the_custom_form_is_drawn_from(self, tmp_path):
        from tradingagents.picker import custom as cu

        assert manager(tmp_path).options()["custom"] == cu.spec_for_page()

    def test_a_manager_without_a_folder_has_no_log_to_name(self):
        m = pj.PickerManager(None, w.pullback_world())
        assert m.options()["ledger_path"] is None and m.options()["log"] == {"path": None, "stocks": 0, "lists": 0, "first": None, "last": None}

    def test_the_options_say_what_the_picks_log_holds(self, tmp_path):
        from tests.test_picker_cli import write_record

        m = manager(tmp_path)
        assert m.options()["log"]["stocks"] == 0 and m.options()["log"]["first"] is None
        write_record(tmp_path / "picker", ("GOOD", "BAD"), sessions_ago=40)
        write_record(tmp_path / "picker", ("GOOD",), sessions_ago=10)                  # rewrites the file: one list of one stock
        log = m.options()["log"]
        assert log["stocks"] == 1 and log["lists"] == 1 and log["first"] == log["last"] and log["path"].endswith("picks.csv")


@pytest.mark.unit
class TestSearching:
    def test_a_list_search_checks_every_stock_named_and_shows_them_all(self, tmp_path):
        m = manager(tmp_path)
        job = wait_done(m, m.start("list", "GOOD OWN FLAT SHORT BAD")["id"])
        assert job["state"] == "done" and job["error"] == ""
        result = job["result"]
        symbols = [r["symbol"] for r in result["rows"]]
        assert "GOOD" in symbols and "SHORT" not in symbols             # too little history for SHORT
        assert result["top"] == 5 and result["recipe"] == "list" and set(result["condition_titles"]) == set("abcde")
        assert any("Checking 5 stocks" in x for x in job["messages"])
        assert m.options()["latest"] == job["id"]

    def test_a_market_search_follows_the_funnel_and_logs_its_steps(self, tmp_path):
        m = manager(tmp_path)
        job = wait_done(m, m.start("beaten-down")["id"])
        assert job["state"] == "done"
        assert "GOOD" in [r["symbol"] for r in job["result"]["rows"]]
        assert dict((f["step"], f["count"]) for f in job["result"]["funnel"])["Stocks to look at"] == 5
        assert any("Screening the market" in x for x in job["messages"])

    def test_the_settings_the_page_sends_reach_the_search(self, tmp_path):
        m = manager(tmp_path)
        job = wait_done(m, m.start("beaten-down", overrides={"below_high": 0.9})["id"])
        assert job["result"]["rows"] == [] and job["result"]["top"] == 15

    def test_the_top_stocks_are_recorded_in_the_picks_log_unless_told_not_to(self, tmp_path):
        m = manager(tmp_path)
        job = wait_done(m, m.start("list", "GOOD BAD")["id"])
        saved = ledger.read_picks(tmp_path / "picker" / "picks.csv")
        assert job["recorded"] == len(saved) == len(job["result"]["rows"]) and job["ledger_path"].endswith("picks.csv")
        quiet = wait_done(m, m.start("list", "GOOD BAD", record=False)["id"])
        assert quiet["recorded"] == 0 and quiet["ledger_path"] is None
        assert len(ledger.read_picks(tmp_path / "picker" / "picks.csv")) == len(saved)

    def test_a_csv_of_all_the_stocks_is_ready_when_the_search_is_done(self, tmp_path):
        m = manager(tmp_path)
        job = wait_done(m, m.start("list", "GOOD FLAT")["id"])
        data = m.csv(job["id"])
        assert data.startswith("﻿rank,symbol,name".encode("utf-8")) and b"GOOD" in data
        assert m.csv("p20990101-000000-abcd") is None

    def test_symbols_that_cannot_be_used_are_reported_not_hidden(self, tmp_path):
        m = manager(tmp_path)
        started = m.start("list", "GOOD ../etc/passwd")
        assert any("Skipped" in x for x in started["messages"]) and started["skipped"]
        wait_done(m, started["id"])

    def test_only_the_last_few_searches_are_kept(self, tmp_path):
        m = manager(tmp_path, keep=2)
        ids = [wait_done(m, m.start("list", "GOOD")["id"])["id"] for _ in range(3)]
        assert m.get(ids[0]) is None and m.get(ids[1]) and m.get(ids[2])


@pytest.mark.unit
class TestCustomSearching:
    def custom_manager(self, tmp_path, **kw):
        from tests.test_picker_custom import world

        return manager(tmp_path, world(), **kw)

    def test_a_custom_search_runs_with_its_filters_and_shows_what_was_asked(self, tmp_path):
        m = self.custom_manager(tmp_path)
        started = m.start("custom", custom_request={"ranges": {"forward_pe": {"min": None, "max": 15}}, "show": 10})
        assert started["recipe"] == "custom"
        job = wait_done(m, started["id"])
        result = job["result"]
        assert job["state"] == "done" and result["recipe"] == "custom" and result["top"] == 10
        assert sorted(r["symbol"] for r in result["rows"]) == ["GOOD", "OWN"] and result["filters"][1].startswith("Forward P/E: at most 15")
        assert result["rows"][0]["analyst_rating"] == 1.8 and result["funnel"][0]["step"] == "Stocks matching your filters at Yahoo"
        assert any("Searching Yahoo's free screener" in x for x in job["messages"])

    def test_a_custom_search_is_recorded_with_its_filters_and_saved_as_csv(self, tmp_path):
        m = self.custom_manager(tmp_path)
        job = wait_done(m, m.start("custom", custom_request={"flags": ["market_explains"]})["id"])
        saved = ledger.read_picks(tmp_path / "picker" / "picks.csv")
        assert job["recorded"] == len(saved) == 2 and {r["recipe"] for r in saved} == {"custom"}
        assert json.loads(saved[0]["settings"])["custom"]["flags"] == ["market_explains"]
        assert b"analyst_rating" in m.csv(job["id"])

    def test_an_empty_custom_search_is_an_answer_and_is_not_recorded(self, tmp_path):
        provider = w.pullback_world()
        provider._universe = []
        m = manager(tmp_path, provider)
        job = wait_done(m, m.start("custom", custom_request={})["id"])
        assert job["state"] == "done" and job["result"]["rows"] == [] and job["recorded"] == 0

    @pytest.mark.parametrize("request_, words", [
        ({"ranges": {"wizard": {"min": 1}}}, "unknown filter"),
        ({"ranges": {"beta": {"min": 3, "max": 1}}}, "above the largest"),
        ({"sectors": ["Crypto"]}, "unknown sector"),
        ({"sort": "luck"}, "Choose one of the ways"),
        ({"show": 0}, "How many to show"),
    ])
    def test_a_bad_custom_request_is_refused_in_plain_words_and_starts_nothing(self, tmp_path, request_, words):
        m = self.custom_manager(tmp_path)
        with pytest.raises(ValueError, match=words):
            m.start("custom", custom_request=request_)
        assert m.options()["latest"] is None and not m.busy()

    def test_the_count_asks_yahoo_once_with_the_same_terms_as_a_search(self, tmp_path):
        provider = w.pullback_world()
        provider._matches = 412
        m = manager(tmp_path, provider)
        assert m.count({"ranges": {"market_cap": {"min": 2, "max": None}}, "sectors": ["Energy"]}) == {"matches": 412}
        terms, sectors = provider.count_calls[0]
        assert ("gte", "intradaymarketcap", 2e9) in terms and sectors == ["Energy"] and provider.closes_calls == []
        with pytest.raises(ValueError, match="unknown filter"):
            m.count({"ranges": {"wizard": {"min": 1}}})


@pytest.mark.unit
class TestOneAtATime:
    def test_a_second_search_is_refused_until_the_first_ends(self, tmp_path):
        release = threading.Event()

        def slow(recipe, provider, settings, **kw):
            kw["log"]("working")
            release.wait(5)
            return quicklook.run(recipe, provider, settings, **kw)

        m = manager(tmp_path, run=slow)
        first = m.start("list", "GOOD")
        assert m.busy() and m.options()["busy"] is True and first["state"] == "running"
        with pytest.raises(pj.SearchBusy, match="already running"):
            m.start("list", "FLAT")
        release.set()
        assert wait_done(m, first["id"])["state"] == "done" and not m.busy()
        assert wait_done(m, m.start("list", "FLAT")["id"])["state"] == "done"


@pytest.mark.unit
class TestBadRequests:
    @pytest.mark.parametrize("args, words", [
        (("stocks",), "Choose Beaten-down"),
        (("list", ""), "at least one stock symbol"),
        (("list", "!!! ???"), "at least one stock symbol"),
        (("list", " ".join(f"S{i}" for i in range(301))), "at most 300"),
        (("list", "A" * (pj.MAX_TEXT + 1)), "too much text"),
        (("beaten-down", "", {"below_high": 5}), "below_high"),
        (("beaten-down", "", {"wizard": 1}), "unknown picker setting"),
        (("beaten-down", "", {"top": 0}), "top"),
    ])
    def test_the_message_is_plain_and_nothing_starts(self, tmp_path, args, words):
        m = manager(tmp_path)
        with pytest.raises(ValueError, match=words):
            m.start(*args)
        assert m.options()["latest"] is None and not m.busy()

    def test_a_search_that_cannot_run_says_why_in_plain_words(self, tmp_path):
        def refuse(*a, **k):
            raise quicklook.PickerError("could not get S&P 500 prices from Yahoo; try again")

        m = manager(tmp_path, run=refuse)
        job = wait_done(m, m.start("beaten-down")["id"])
        assert job["state"] == "failed" and "S&P 500 prices" in job["error"] and job["result"] is None

    def test_an_unexpected_failure_shows_no_traceback(self, tmp_path):
        def explode(*a, **k):
            raise KeyError("secret-internal-detail")

        m = manager(tmp_path, run=explode)
        job = wait_done(m, m.start("beaten-down")["id"])
        assert job["state"] == "failed" and "KeyError" in job["error"] and "secret-internal-detail" not in job["error"]


# ---------------------------------------------------------------- the endpoints
from fastapi.testclient import TestClient  # noqa: E402

from tradingagents.ui import server  # noqa: E402

TOKEN = "test-session-token-1234567890"
HEADERS = {"X-Council-Token": TOKEN}


def api(tmp_path, provider=None, **kw):
    app = server.create_app(TOKEN, tmp_path / ".env", 8765, picker=manager(tmp_path, provider, **kw))
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.mark.unit
class TestEndpoints:
    def test_every_endpoint_needs_the_token(self, tmp_path):
        c = api(tmp_path)
        assert c.get("/api/picker/options").status_code == 401
        assert c.post("/api/picker/run", json={"recipe": "list", "text": "GOOD"}).status_code == 401
        assert c.get("/api/picker/jobs/p20261108-101010-abcd").status_code == 401
        assert c.post("/api/picker/export", json={"id": "p20261108-101010-abcd"}).status_code == 401
        assert c.get("/api/picker/options", headers=HEADERS).status_code == 200

    def test_a_search_from_another_site_is_refused(self, tmp_path):
        c = api(tmp_path)
        r = c.post("/api/picker/run", json={"recipe": "list", "text": "GOOD"}, headers={**HEADERS, "Origin": "https://evil.example.com"})
        assert r.status_code == 403 and c.app.state.picker.options()["latest"] is None

    def test_a_search_is_started_followed_and_saved(self, tmp_path):
        c = api(tmp_path)
        r = c.post("/api/picker/run", headers=HEADERS, json={"recipe": "list", "text": "GOOD FLAT BAD", "settings": {}, "record": False})
        assert r.status_code == 202 and r.json()["state"] in ("running", "done")
        job_id = r.json()["id"]
        wait_done(c.app.state.picker, job_id)
        body = c.get(f"/api/picker/jobs/{job_id}", headers=HEADERS).json()
        assert body["state"] == "done" and body["result"]["rows"] and body["result"]["rows"][0]["conditions_met"] >= 0
        assert c.get("/api/picker/options", headers=HEADERS).json()["latest"] == job_id
        csv = c.post("/api/picker/export", headers=HEADERS, json={"id": job_id})
        assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
        assert "stocks.csv" in csv.headers["content-disposition"] and b"GOOD" in csv.content

    def test_the_settings_are_sent_in_the_pickers_own_units(self, tmp_path):
        c = api(tmp_path)
        r = c.post("/api/picker/run", headers=HEADERS, json={"recipe": "beaten-down", "settings": {"below_high": 0.9}})
        assert r.status_code == 202
        assert wait_done(c.app.state.picker, r.json()["id"])["result"]["rows"] == []

    @pytest.mark.parametrize("body, words", [
        ({"recipe": "wizard"}, "Choose Beaten-down"),
        ({"recipe": "list", "text": ""}, "at least one stock symbol"),
        ({"recipe": "list", "text": "GOOD", "settings": {"below_high": 7}}, "below_high"),
    ])
    def test_a_bad_request_is_a_plain_422_and_starts_nothing(self, tmp_path, body, words):
        c = api(tmp_path)
        r = c.post("/api/picker/run", headers=HEADERS, json=body)
        assert r.status_code == 422 and words in r.json()["detail"] and c.app.state.picker.options()["latest"] is None

    @pytest.mark.parametrize("body", [
        {"recipe": "list", "text": "GOOD", "settings": {"carry": 1}},          # not one of the settings the page may change
        {"recipe": "list", "text": "GOOD", "settings": {"top": "many"}},
        {"recipe": "list", "text": ["GOOD"]},
        {"text": "GOOD"},
    ])
    def test_a_malformed_body_is_refused(self, tmp_path, body):
        c = api(tmp_path)
        assert c.post("/api/picker/run", headers=HEADERS, json=body).status_code == 422
        assert c.app.state.picker.options()["latest"] is None

    def test_a_busy_picker_answers_409_with_a_plain_sentence(self, tmp_path):
        release = threading.Event()

        def slow(recipe, provider, settings, **kw):
            release.wait(5)
            return quicklook.run(recipe, provider, settings, **kw)

        c = api(tmp_path, run=slow)
        assert c.post("/api/picker/run", headers=HEADERS, json={"recipe": "list", "text": "GOOD"}).status_code == 202
        again = c.post("/api/picker/run", headers=HEADERS, json={"recipe": "list", "text": "FLAT"})
        assert again.status_code == 409 and "already running" in again.json()["detail"]
        release.set()
        wait_done(c.app.state.picker, c.app.state.picker.options()["latest"])

    @pytest.mark.parametrize("job_id", ["nope", "../../etc/passwd", "p20261108-101010-zzzz", "p20261108-101010-abcd"])
    def test_an_unknown_search_is_a_404_for_both_reading_and_saving(self, tmp_path, job_id):
        c = api(tmp_path)
        assert c.get(f"/api/picker/jobs/{job_id}", headers=HEADERS).status_code in (404, 405)
        r = c.post("/api/picker/export", headers=HEADERS, json={"id": job_id})
        assert r.status_code == 404 and "no finished search" in r.json()["detail"]

    def test_a_search_that_has_not_finished_cannot_be_saved(self, tmp_path):
        release = threading.Event()

        def slow(recipe, provider, settings, **kw):
            release.wait(5)
            return quicklook.run(recipe, provider, settings, **kw)

        c = api(tmp_path, run=slow)
        job_id = c.post("/api/picker/run", headers=HEADERS, json={"recipe": "list", "text": "GOOD"}).json()["id"]
        assert c.post("/api/picker/export", headers=HEADERS, json={"id": job_id}).status_code == 404
        release.set()
        wait_done(c.app.state.picker, job_id)

    def test_the_manager_is_made_only_when_first_needed(self, tmp_path, monkeypatch):
        made, real = [], pj.PickerManager
        monkeypatch.setattr(server.picker_jobs, "PickerManager", lambda folder: made.append(folder) or real(None, w.pullback_world()))
        c = TestClient(server.create_app(TOKEN, tmp_path / ".env", 8765), base_url="http://127.0.0.1:8765")
        assert c.get("/api/state", headers=HEADERS).status_code == 200 and made == []
        assert c.get("/api/picker/options", headers=HEADERS).status_code == 200 and len(made) == 1
        assert c.get("/api/picker/options", headers=HEADERS).status_code == 200 and len(made) == 1


@pytest.mark.unit
class TestCustomEndpoints:
    def custom_api(self, tmp_path, matches=0):
        from tests.test_picker_custom import world

        provider = world()
        provider._matches = matches
        return api(tmp_path, provider)

    def test_a_custom_search_is_started_and_followed_like_any_other(self, tmp_path):
        c = self.custom_api(tmp_path)
        r = c.post("/api/picker/run", headers=HEADERS, json={"recipe": "custom", "record": False, "custom": {
            "ranges": {"forward_pe": {"min": None, "max": 15}}, "flags": [], "sectors": [], "otc": False, "exclude": "", "min_conditions": None,
            "sort": "conditions", "examine": None, "check": None, "show": None}})
        assert r.status_code == 202
        job = wait_done(c.app.state.picker, r.json()["id"])
        assert sorted(x["symbol"] for x in c.get(f"/api/picker/jobs/{r.json()['id']}", headers=HEADERS).json()["result"]["rows"]) == ["GOOD", "OWN"]
        assert job["state"] == "done"

    def test_a_custom_search_needs_its_filters_object_to_be_well_formed(self, tmp_path):
        c = self.custom_api(tmp_path)
        for bad in ({"wizard": 1}, {"ranges": {"beta": {"mean": 2}}}, {"ranges": {"beta": {"min": "tall"}}}, {"flags": "profitable"}, {"min_conditions": "all"}):
            assert c.post("/api/picker/run", headers=HEADERS, json={"recipe": "custom", "custom": bad}).status_code == 422
        assert c.app.state.picker.options()["latest"] is None

    def test_a_filter_that_makes_no_sense_is_a_plain_422(self, tmp_path):
        r = self.custom_api(tmp_path).post("/api/picker/run", headers=HEADERS, json={"recipe": "custom", "custom": {"ranges": {"beta": {"min": 3, "max": 1}}}})
        assert r.status_code == 422 and "above the largest" in r.json()["detail"]

    def test_the_count_endpoint_needs_the_token_and_answers_with_a_number(self, tmp_path):
        c = self.custom_api(tmp_path, matches=77)
        assert c.post("/api/picker/count", json={"custom": {}}).status_code == 401
        assert c.post("/api/picker/count", json={"custom": {}}, headers={**HEADERS, "Origin": "https://evil.example.com"}).status_code == 403
        r = c.post("/api/picker/count", headers=HEADERS, json={"custom": {"ranges": {"market_cap": {"min": 2}}}})
        assert r.status_code == 200 and r.json() == {"matches": 77}
        assert c.post("/api/picker/count", headers=HEADERS, json={}).json() == {"matches": 77}               # no filters is a valid question

    def test_a_bad_count_request_is_a_plain_422(self, tmp_path):
        c = self.custom_api(tmp_path)
        r = c.post("/api/picker/count", headers=HEADERS, json={"custom": {"ranges": {"wizard": {"min": 1}}}})
        assert r.status_code == 422 and "unknown filter" in r.json()["detail"]
        assert c.post("/api/picker/count", headers=HEADERS, json={"custom": {"wizard": 1}}).status_code == 422

    def test_yahoo_being_down_is_a_502_with_no_traceback(self, tmp_path):
        provider = w.pullback_world()
        provider.count = lambda terms, sectors: (_ for _ in ()).throw(OSError("secret-internal-detail"))
        r = api(tmp_path, provider).post("/api/picker/count", headers=HEADERS, json={"custom": {}})
        assert r.status_code == 502 and "OSError" in r.json()["detail"] and "secret-internal-detail" not in r.text


@pytest.mark.unit
class TestScoreboardEndpoints:
    def test_both_endpoints_need_the_token(self, tmp_path):
        c = api(tmp_path)
        assert c.post("/api/picker/scoreboard").status_code == 401 and c.post("/api/picker/scoreboard/csv").status_code == 401
        assert c.post("/api/picker/scoreboard", headers={**HEADERS, "Origin": "https://evil.example.com"}).status_code == 403

    def test_an_empty_log_is_answered_in_words_and_needs_no_download(self, tmp_path):
        provider = w.pullback_world()
        c = api(tmp_path, provider)
        body = c.post("/api/picker/scoreboard", headers=HEADERS).json()
        assert "Nothing is recorded yet" in body["verdict"] and body["picks"] == [] and provider.closes_calls == []

    def test_the_log_is_measured_and_saved_as_csv(self, tmp_path):
        from tests.test_picker_cli import write_record

        write_record(tmp_path / "picker", ("GOOD", "BAD"), sessions_ago=70)
        c = api(tmp_path)
        assert c.post("/api/picker/scoreboard/csv", headers=HEADERS).status_code == 404         # nothing worked out yet
        body = c.post("/api/picker/scoreboard", headers=HEADERS).json()
        assert [p["symbol"] for p in body["picks"]] == ["GOOD", "BAD"] and body["recipes"][0]["stats"]["63"]["n"] == 2
        assert body["horizons"] == [5, 21, 63] and body["verdict"] and "S&P 500" in body["yardstick"]
        csv = c.post("/api/picker/scoreboard/csv", headers=HEADERS)
        assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
        assert "scoreboard.csv" in csv.headers["content-disposition"] and b"GOOD" in csv.content and csv.content.startswith(b"\xef\xbb\xbf")

    def test_no_market_prices_is_a_502_with_a_plain_sentence(self, tmp_path):
        from tests.test_picker_cli import write_record

        write_record(tmp_path / "picker", ("GOOD",), sessions_ago=40)
        provider = w.pullback_world()
        del provider._closes["^GSPC"]
        r = api(tmp_path, provider).post("/api/picker/scoreboard", headers=HEADERS)
        assert r.status_code == 502 and "S&P 500 prices" in r.json()["detail"]

    def test_an_unexpected_failure_shows_no_traceback(self, tmp_path):
        from tests.test_picker_cli import write_record

        write_record(tmp_path / "picker", ("GOOD",), sessions_ago=40)
        provider = w.pullback_world()
        provider.closes = lambda symbols: (_ for _ in ()).throw(KeyError("secret-internal-detail"))
        r = api(tmp_path, provider).post("/api/picker/scoreboard", headers=HEADERS)
        assert r.status_code == 502 and "KeyError" in r.json()["detail"] and "secret-internal-detail" not in r.text

    def test_a_second_scoreboard_while_one_is_running_is_refused(self, tmp_path):
        from tests.test_picker_cli import write_record

        write_record(tmp_path / "picker", ("GOOD",), sessions_ago=40)
        release, inside = threading.Event(), threading.Event()
        provider = w.pullback_world()
        real = provider.closes

        def slow(symbols):
            inside.set()
            release.wait(5)
            return real(symbols)

        provider.closes = slow
        c = api(tmp_path, provider)
        first = threading.Thread(target=lambda: c.post("/api/picker/scoreboard", headers=HEADERS))
        first.start()
        assert inside.wait(5)
        again = c.post("/api/picker/scoreboard", headers=HEADERS)
        assert again.status_code == 409 and "already being worked out" in again.json()["detail"]
        release.set()
        first.join(10)


# ---------------------------------------------------------------- the page's files
STATIC = Path(server.STATIC)
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "picker.js").read_text(encoding="utf-8")
BOARD = (STATIC / "board.js").read_text(encoding="utf-8")
CUSTOM = (STATIC / "custom.js").read_text(encoding="utf-8")
HTML_IDS = set(re.findall(r'\bid="([^"]+)"', HTML))
# ids that the scripts write into the page themselves, after a search or a scoreboard
MADE_BY_SCRIPT = {"f-results-h", "f-top", "f-clear", "f-send", "f-sendmsg", "f-save", "f-save-result", "f-save-error"}
MADE_BY_BOARD = {"b-results-h", "b-save", "b-save-result", "b-save-error"}
MADE_BY_CUSTOM = {"c-otc", "c-exclude", "c-minc", "c-sort"}


@pytest.mark.unit
class TestThePage:
    def test_the_panel_and_its_script_are_in_the_page(self):
        assert '<script src="/static/picker.js">' in HTML and HTML.index("runs.js") < HTML.index("picker.js")     # it uses runs.js's helpers
        for needed in ("find", "find-form", "f-recipes", "f-text", "f-file", "f-settings", "f-start", "f-record", "f-error", "f-progress", "f-results", "f-announce"):
            assert needed in HTML_IDS, needed
        assert HTML.index('id="find"') < HTML.index('id="run"')                     # find first, then run

    def test_every_id_the_script_reaches_for_exists(self):
        wanted = set(re.findall(r'\$\("([a-z0-9-]+)"\)', SCRIPT))
        assert wanted - HTML_IDS - MADE_BY_SCRIPT == set()
        for made in MADE_BY_SCRIPT - {"f-results-h"}:
            assert f'id="{made}"' in SCRIPT, made

    def test_the_scoreboard_script_is_in_the_page_and_every_id_it_reaches_for_exists(self):
        assert '<script src="/static/board.js">' in HTML and HTML.index("picker.js") < HTML.index("board.js")
        for needed in ("board", "b-h", "b-hint", "b-run", "b-status", "b-error", "b-results"):
            assert needed in HTML_IDS, needed
        wanted = set(re.findall(r'\$\("([a-z0-9-]+)"\)', BOARD))
        assert wanted - HTML_IDS - MADE_BY_BOARD == set()
        for made in MADE_BY_BOARD - {"b-results-h"}:
            assert f'id="{made}"' in BOARD, made
        assert '"/api/picker/scoreboard"' in BOARD and '"/api/picker/scoreboard/csv"' in BOARD and "saveFile(" in BOARD

    def test_the_custom_form_script_is_in_the_page_and_every_id_it_reaches_for_exists(self):
        assert '<script src="/static/custom.js">' in HTML and HTML.index("picker.js") < HTML.index("custom.js") < HTML.index("board.js")
        for needed in ("f-custom-block", "f-more", "c-groups", "c-count", "c-clear", "c-reset", "c-count-text"):
            assert needed in HTML_IDS, needed
        assert set(re.findall(r'\$\("([a-z0-9-]+)"\)', CUSTOM)) - HTML_IDS - MADE_BY_CUSTOM - {"cg-which"} == set()
        for made in MADE_BY_CUSTOM:
            assert f'id="{made}"' in CUSTOM, made
        assert 'id="cg-${esc(g.id)}"' in CUSTOM                            # the group the form opens first is made from the catalog
        for hook in ("fillCustom", "collectCustom"):                       # what picker.js calls into custom.js
            assert f"function {hook}(" in CUSTOM and f"{hook}(" in SCRIPT, hook
        assert '"/api/picker/count"' in CUSTOM

    def test_the_custom_form_escapes_what_the_server_describes_and_the_script_stays_clean(self):
        assert set(re.findall(r"\$\{([a-z]+\.[a-z_]+)\}", CUSTOM)) == set()        # every field from the catalog goes through esc()
        for name in ("c.label", "c.unit", "c.help", "f.label", "f.help", "s)", "g.label", "g.id"):
            assert f"esc({name}" in CUSTOM, name
        assert " style=" not in CUSTOM and "eval(" not in CUSTOM and "document.write" not in CUSTOM and "localStorage" not in CUSTOM
        assert 'class="side"' not in CUSTOM                                  # "side" is the left column's class; reusing it restyled every box
        assert "X-Council-Token" not in CUSTOM and TOKEN not in CUSTOM

    def test_a_custom_search_shows_what_was_asked_and_its_extra_facts(self):
        assert "r.filters" in SCRIPT and "esc(f)" in SCRIPT and "r.analyst_rating" in SCRIPT and "r.trailing_pe" in SCRIPT
        assert 'id="f-more"' in HTML and "$(\"f-more\").hidden" in SCRIPT                  # "More settings" only where some apply

    def test_what_the_scoreboard_shows_is_escaped_and_the_script_stays_clean(self):
        raw = re.findall(r"\$\{([a-z]\.[a-z_]+)\}", BOARD)
        assert set(raw) <= {"p.met", "s.n", "s.beat"}                           # whole numbers the server made
        for name in ("d.verdict", "d.yardstick", "p.symbol", "p.name", "p.shown", "b.shown", "r.label", "label"):
            assert f"esc({name}" in BOARD, name
        assert " style=" not in BOARD and "eval(" not in BOARD and "document.write" not in BOARD and "localStorage" not in BOARD
        assert "X-Council-Token" not in BOARD and TOKEN not in BOARD

    def test_the_save_flow_is_shared_with_the_run_panel_and_its_messages_exist(self):
        runs = (STATIC / "runs.js").read_text(encoding="utf-8")
        assert "async function saveFile(kind, ids, baseName, source)" in runs and 'out: "export-result", err: "export-error"' in runs
        assert {"export-result", "export-error"} <= HTML_IDS
        assert '"/api/picker/export"' in SCRIPT and "saveFile(" in SCRIPT

    def test_no_inline_code_and_every_label_is_matched(self):
        assert " style=" not in HTML and not re.search(r"\son[a-z]+=", HTML) and "<script>" not in HTML
        for target in re.findall(r'<label for="([^"]+)"', HTML):
            assert target in HTML_IDS, target
        assert " style=" not in SCRIPT and "onclick" not in SCRIPT

    def test_aria_references_point_at_real_ids(self):
        assert "aria-labeledby" not in HTML                                          # the misspelling that screen readers ignore
        for attr in ("aria-labelledby", "aria-describedby"):
            for ref in re.findall(rf'{attr}="([^"]+)"', HTML):
                for one in ref.split():
                    assert one in HTML_IDS, (attr, one)

    def test_what_the_search_found_is_always_escaped_before_it_goes_into_the_page(self):
        # a field of a stock (r.) or of the search (j.) is put into HTML only through esc(), except whole numbers the server made
        raw = re.findall(r"\$\{(r\.[a-z_0-9]+|j\.[a-z_]+|f\.[a-z]+)\}", SCRIPT)
        assert set(raw) <= {"r.rank", "r.conditions_met", "f.count"}
        for name in ("r.symbol", "r.name", "place", "titles[c]", "f[0]", "f[1]", "m)"):       # f[1] carries the data-trouble note
            assert f"esc({name}" in SCRIPT, name

    def test_the_script_does_not_hold_the_token_or_use_forbidden_calls(self, tmp_path):
        c = api(tmp_path)
        js = c.get("/static/picker.js")
        assert js.status_code == 200 and "X-Council-Token" not in js.text and TOKEN not in js.text
        assert "eval(" not in SCRIPT and "document.write" not in SCRIPT and "localStorage" not in SCRIPT
