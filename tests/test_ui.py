"""The local setup screen: the settings writer, the server's guards, and that no secret ever leaves it."""

import io
import json
import os
import re
import socket
import stat
import sys

import pytest

pytest.importorskip("fastapi")

# The suite blocks every network connection. The in-process test client (and Windows' event loop, which builds its
# wake-up pipe from a loopback socket pair) needs connections to THIS computer, so they are allowed here; anything
# else is still refused.
_REAL_CONNECT, _REAL_CONNECT_EX, _REAL_GETADDRINFO = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _is_loopback(address) -> bool:
    return isinstance(address, tuple) and bool(address) and address[0] in _LOOPBACK


@pytest.fixture(autouse=True)
def _loopback_only(_no_network, monkeypatch):
    def refuse(address):
        raise OSError(f"test tried to reach the network: {address}")

    monkeypatch.setattr(socket.socket, "connect", lambda self, a: _REAL_CONNECT(self, a) if _is_loopback(a) else refuse(a))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, a: _REAL_CONNECT_EX(self, a) if _is_loopback(a) else refuse(a))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: _REAL_GETADDRINFO(host, *a, **k) if host in _LOOPBACK else refuse((host,)))
from fastapi.testclient import TestClient  # noqa: E402

from tradingagents import doctor as dr  # noqa: E402
from tradingagents.ui import server, settings_store as store  # noqa: E402

TOKEN = "test-session-token-1234567890"
KEY = "sk-ant-THISISASECRETKEY1234567890"
HEADERS = {"X-Council-Token": TOKEN}


@pytest.fixture
def env(monkeypatch):
    """A private environment dict, so a test never touches the real one."""
    return {}


@pytest.fixture
def dotenv(tmp_path):
    return tmp_path / ".env"


def client(dotenv, port=8765, runs=None, **kw):
    app = server.create_app(TOKEN, dotenv, port, runs=runs)
    return TestClient(app, base_url=f"http://127.0.0.1:{port}", **kw)


@pytest.mark.unit
class TestValidate:
    def setup_method(self):
        self.reg = store.registry()

    @pytest.mark.parametrize("name, value, expected", [
        ("TRADINGAGENTS_LLM_PROVIDER", " Anthropic ", "anthropic"),
        ("TRADINGAGENTS_RESEARCH_PASSES", "5", "5"),
        ("TRADINGAGENTS_QUICK_THINK_LLM", "claude-haiku-5-5", "claude-haiku-5-5"),
        ("SEC_EDGAR_USER_AGENT", "Jane Doe jane@example.org", "Jane Doe jane@example.org"),
        ("TRADINGAGENTS_NO_SCORECARD", "1", "1"),
        ("FRED_API_KEY", "abcdefgh12345678", "abcdefgh12345678"),
    ])
    def test_good_values_are_normalized(self, name, value, expected):
        assert store.validate(self.reg[name], value) == expected

    @pytest.mark.parametrize("name, value", [
        ("TRADINGAGENTS_LLM_PROVIDER", "not-a-provider"),
        ("TRADINGAGENTS_RESEARCH_PASSES", "0"), ("TRADINGAGENTS_RESEARCH_PASSES", "16"), ("TRADINGAGENTS_RESEARCH_PASSES", "five"),
        ("TRADINGAGENTS_QUICK_THINK_LLM", "bad name with spaces"), ("TRADINGAGENTS_QUICK_THINK_LLM", "x\nINJECTED=1"),
        ("SEC_EDGAR_USER_AGENT", "ab"), ("SEC_EDGAR_USER_AGENT", "line one\nline two"),
        ("FRED_API_KEY", "short"), ("FRED_API_KEY", "has a space inside 12345"), ("FRED_API_KEY", "key\nINJECTED=1abcdefgh"),
        ("TRADINGAGENTS_NO_SCORECARD", "yes"),
    ])
    def test_bad_values_are_refused_without_echoing_them(self, name, value):
        with pytest.raises(ValueError) as caught:
            store.validate(self.reg[name], value)
        assert value.strip() not in str(caught.value) or len(value.strip()) < 3

    def test_only_listed_names_can_be_written(self, dotenv, env):
        for bad in ("PATH", "TRADINGAGENTS_RESULTS_DIR", "LD_PRELOAD", "REQUESTS_CA_BUNDLE", "anthropic_api_key"):
            with pytest.raises(ValueError, match="not a setting"):
                store.save({bad: "value-1234567890"}, dotenv, env)
        assert not dotenv.exists() and env == {}


@pytest.mark.unit
class TestSave:
    def test_values_go_to_the_file_and_the_running_environment(self, dotenv, env):
        out = store.save({"TRADINGAGENTS_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-openai-1234567890abc"}, dotenv, env)
        assert out["changed"] == ["OPENAI_API_KEY", "TRADINGAGENTS_LLM_PROVIDER"]
        text = dotenv.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY" in text and "TRADINGAGENTS_LLM_PROVIDER" in text
        assert env["OPENAI_API_KEY"] == "sk-openai-1234567890abc" and env["TRADINGAGENTS_LLM_PROVIDER"] == "openai"
        assert "sk-openai-1234567890abc" not in json.dumps(out)           # the result never carries a value

    def test_existing_lines_are_kept_and_a_value_can_be_replaced_or_removed(self, dotenv, env):
        dotenv.write_text("KEEP_ME=1\n# a comment\nTRADINGAGENTS_NO_SCORECARD=1\n", encoding="utf-8")
        env["TRADINGAGENTS_NO_SCORECARD"] = "1"
        store.save({"TRADINGAGENTS_NO_SCORECARD": None, "TRADINGAGENTS_RESEARCH_PASSES": "3"}, dotenv, env)
        text = dotenv.read_text(encoding="utf-8")
        assert "KEEP_ME=1" in text and "# a comment" in text and "NO_SCORECARD" not in text and "RESEARCH_PASSES" in text
        assert "TRADINGAGENTS_NO_SCORECARD" not in env

    def test_one_bad_value_writes_nothing_at_all(self, dotenv, env):
        with pytest.raises(ValueError):
            store.save({"TRADINGAGENTS_RESEARCH_PASSES": "5", "FRED_API_KEY": "short"}, dotenv, env)
        assert not dotenv.exists() and env == {}

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows does not keep POSIX file modes")
    def test_a_new_file_is_owner_only(self, dotenv, env):
        store.save({"TRADINGAGENTS_RESEARCH_PASSES": "5"}, dotenv, env)
        assert stat.S_IMODE(os.stat(dotenv).st_mode) == 0o600

    def test_current_shows_ordinary_values_and_only_whether_a_secret_is_set(self):
        env = {"TRADINGAGENTS_RESEARCH_PASSES": "5", "FRED_API_KEY": "fredsecret1234567", "ANTHROPIC_API_KEY": ""}
        cur = store.current(env)
        assert cur["values"] == {"TRADINGAGENTS_RESEARCH_PASSES": "5"}
        assert cur["secrets"]["FRED_API_KEY"] is True and cur["secrets"]["ANTHROPIC_API_KEY"] is False
        assert "fredsecret1234567" not in json.dumps(cur)


@pytest.mark.unit
class TestGuards:
    def test_the_page_carries_the_token_and_a_strict_policy(self, dotenv):
        r = client(dotenv).get("/")
        assert r.status_code == 200 and TOKEN in r.text and "__SESSION_TOKEN__" not in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"] and "'unsafe-inline'" not in r.headers["content-security-policy"]
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"

    def test_the_page_uses_no_inline_scripts_or_styles(self):
        from pathlib import Path
        html = (Path(server.STATIC) / "index.html").read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", html) and " style=" not in html and not re.search(r"\son[a-z]+=", html)

    def test_the_api_needs_the_token(self, dotenv):
        c = client(dotenv)
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", headers={"X-Council-Token": "wrong"}).status_code == 401
        assert c.post("/api/settings", json={"updates": {}}).status_code == 401
        assert c.post("/api/doctor", json={}).status_code == 401
        assert c.get("/api/state", headers=HEADERS).status_code == 200

    @pytest.mark.parametrize("host", ["evil.example.com", "127.0.0.1.evil.com", "192.168.1.5:8765", "localhost.evil.com"])
    def test_a_request_for_another_host_is_refused(self, dotenv, host):
        r = client(dotenv).get("/", headers={"Host": host})
        assert r.status_code == 400

    def test_a_request_for_the_wrong_port_is_refused(self, dotenv):
        assert client(dotenv).get("/", headers={"Host": "127.0.0.1:9999"}).status_code == 400
        assert client(dotenv).get("/", headers={"Host": "localhost:8765"}).status_code == 200

    def test_changes_from_another_origin_are_refused_even_with_the_token(self, dotenv):
        c = client(dotenv)
        evil = c.post("/api/settings", json={"updates": {"TRADINGAGENTS_RESEARCH_PASSES": "5"}},
                      headers={**HEADERS, "Origin": "https://evil.example.com"})
        assert evil.status_code == 403 and not dotenv.exists()
        same = c.post("/api/settings", json={"updates": {"TRADINGAGENTS_RESEARCH_PASSES": "5"}},
                      headers={**HEADERS, "Origin": "http://127.0.0.1:8765"})
        assert same.status_code == 200

    def test_the_api_docs_are_not_exposed(self, dotenv):
        c = client(dotenv)
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path, headers=HEADERS).status_code == 404


@pytest.mark.unit
class TestApi:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for name in list(store.registry()):
            monkeypatch.delenv(name, raising=False)

    def test_state_lists_providers_models_and_never_a_secret(self, dotenv, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        body = client(dotenv).get("/api/state", headers=HEADERS).json()
        assert KEY not in json.dumps(body) and body["settings"]["secrets"]["ANTHROPIC_API_KEY"] is True
        ids = [p["id"] for p in body["choices"]["providers"]]
        assert "anthropic" in ids and "ollama" in ids
        anth = next(p for p in body["choices"]["providers"] if p["id"] == "anthropic")
        assert anth["key_env"] == "ANTHROPIC_API_KEY" and not anth["sign_in"]
        assert next(p for p in body["choices"]["providers"] if p["id"] == "ollama")["key_env"] is None
        assert body["choices"]["passes"] == [1, 3, 5, 7, 9] and body["effective"]["passes"] >= 1
        assert body["dotenv"]["exists"] is False

    def test_saving_a_key_writes_it_and_the_reply_never_contains_it(self, dotenv, monkeypatch):
        c = client(dotenv)
        r = c.post("/api/settings", headers=HEADERS, json={"updates": {
            "ANTHROPIC_API_KEY": KEY, "TRADINGAGENTS_QUICK_THINK_LLM": "claude-haiku-5-5", "TRADINGAGENTS_NO_SELLOFF_RULES": "1"}})
        assert r.status_code == 200 and KEY not in r.text
        assert sorted(r.json()["changed"]) == ["ANTHROPIC_API_KEY", "TRADINGAGENTS_NO_SELLOFF_RULES", "TRADINGAGENTS_QUICK_THINK_LLM"]
        assert KEY in dotenv.read_text(encoding="utf-8")
        state = r.json()["state"]
        assert state["settings"]["secrets"]["ANTHROPIC_API_KEY"] is True and KEY not in json.dumps(state)
        assert state["settings"]["values"]["TRADINGAGENTS_NO_SELLOFF_RULES"] == "1"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def test_bad_input_is_a_clear_422_and_writes_nothing(self, dotenv):
        c = client(dotenv)
        r = c.post("/api/settings", headers=HEADERS, json={"updates": {"TRADINGAGENTS_RESEARCH_PASSES": "99"}})
        assert r.status_code == 422 and "whole number" in r.json()["detail"] and not dotenv.exists()
        r = c.post("/api/settings", headers=HEADERS, json={"updates": {"PATH": "x"}})
        assert r.status_code == 422 and "not a setting" in r.json()["detail"]
        assert c.post("/api/settings", headers=HEADERS, json={"updates": "nope"}).status_code == 422

    def test_the_doctor_endpoint_runs_the_checks_and_passes_the_options(self, dotenv, monkeypatch):
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return dr.Report([dr.Result("env.python", "environment", "Python", dr.OK, "fine", required=True)], "now",
                             {s: 0 for s in dr.LABEL} | {dr.OK: 1}, True, False)
        monkeypatch.setattr(dr, "run_doctor", fake)
        r = client(dotenv).post("/api/doctor", headers=HEADERS, json={"live_ai": False, "ids": ["env.python"]})
        assert r.status_code == 200 and r.json()["ready"] is True and r.json()["results"][0]["id"] == "env.python"
        assert seen == {"offline": False, "live_ai": False, "ids": ["env.python"]}

    def test_the_background_tiles_are_valid_images_served_from_this_server(self, dotenv):
        import xml.etree.ElementTree as ET
        c = client(dotenv)
        for name in ("bg-market-light.svg", "bg-market-dark.svg"):
            r = c.get(f"/static/{name}")
            assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
            root = ET.fromstring(r.text)                          # well-formed
            assert root.tag.endswith("svg") and "<script" not in r.text and "http://" not in r.text.replace("http://www.w3.org/2000/svg", "")

    def test_the_decoration_never_applies_to_high_contrast_and_can_be_switched_off(self):
        from pathlib import Path
        css = (Path(server.STATIC) / "app.css").read_text(encoding="utf-8")
        html = (Path(server.STATIC) / "index.html").read_text(encoding="utf-8")
        js = (Path(server.STATIC) / "app.js").read_text(encoding="utf-8")
        contrast = re.search(r'\[data-theme="contrast"\]\s*\{(.*?)\}', css, re.S).group(1)
        assert "--page-img:none" in contrast and "--page-grad:none" in contrast
        assert 'html[data-theme="contrast"] body::before { display:none; }' in css
        assert 'html[data-bg="market"] body' in css                      # the picture shows only in market mode
        assert 'data-bg="plain"' in html and 'data-bg="market"' in html and 'setBg(' in js
        assert "http" not in css.replace("http://www.w3.org", "")        # no remote images or fonts

    def test_the_static_files_are_served(self, dotenv):
        c = client(dotenv)
        assert "council-token" in c.get("/").text
        js = c.get("/static/app.js")
        assert js.status_code == 200 and "X-Council-Token" in js.text
        assert c.get("/static/app.css").status_code == 200
        assert c.get("/static/../server.py").status_code in (400, 404)


class _HeldProc:
    """A stand-in child that never finishes by itself and ends when it is killed."""

    pid, returncode, killed = 4242, None, False

    def poll(self):
        if self.killed:
            self.returncode = 1
            return 1
        return None


@pytest.mark.unit
class TestRunsApi:
    """The run form's endpoints, with a queue whose children are fakes (no analysis, no network, no cost)."""

    @pytest.fixture
    def rig(self, tmp_path, monkeypatch):
        from tradingagents.ui import runs as rn

        for name in list(store.registry()):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "claude-haiku-5-5")
        monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", "claude-sonnet-5")
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "my_book.json").write_text(json.dumps({"cash": 1000, "currency": "USD", "profile": "patient",
                                                          "positions": [{"ticker": "NVDA", "quantity": 2}]}), encoding="utf-8")
        monkeypatch.setattr(server, "holdings_folders", lambda cfg: [folder])
        launches, procs = [], []

        def launcher(command, env, cwd, console):
            launches.append(command)
            procs.append(_HeldProc())
            return procs[-1]

        manager = rn.RunManager(tmp_path / "logs", holdings=lambda: {k: v["path"] for k, v in rn.find_holdings([folder]).items()},
                                launcher=launcher, python="python", poll_seconds=0.005, sleep=lambda s: __import__("time").sleep(0.005),
                                killer=lambda p: setattr(p, "killed", True))
        yield manager, launches, procs
        manager.shutdown()

    def body(self, **changes):
        base = {"tickers": ["NVDA"], "date": server.date.today().isoformat(), "analysts": ["market", "news"],
                "depth": 1, "passes": 5, "language": "English", "holdings": None, "selloff": True, "scorecard": True}
        base.update(changes)
        return base

    def test_the_endpoints_need_the_token(self, dotenv, rig):
        c = client(dotenv, runs=rig[0])
        assert c.get("/api/run-options").status_code == 401
        assert c.get("/api/runs").status_code == 401
        assert c.post("/api/runs", json=self.body()).status_code == 401
        assert c.post("/api/runs/20261108-101010-abcd/cancel").status_code == 401
        assert rig[1] == []

    def test_starting_from_another_origin_is_refused(self, dotenv, rig):
        c = client(dotenv, runs=rig[0])
        r = c.post("/api/runs", json=self.body(), headers={**HEADERS, "Origin": "https://evil.example.com"})
        assert r.status_code == 403 and rig[1] == []

    def test_the_options_describe_the_form_and_the_settings_in_force(self, dotenv, rig):
        body = client(dotenv, runs=rig[0]).get("/api/run-options", headers=HEADERS).json()
        assert body["today"] == server.date.today().isoformat() and body["max_tickers"] == 10
        assert [a["id"] for a in body["analysts"]] == ["market", "social", "news", "fundamentals"]
        assert [d["value"] for d in body["depths"]] == [1, 3, 5] and "English" in body["languages"]
        assert body["defaults"]["depth"] == 1 and body["defaults"]["selloff"] is True and body["defaults"]["scorecard"] is True
        assert body["provider"] == {"id": "anthropic", "quick": "claude-haiku-5-5", "deep": "claude-sonnet-5",
                                    "key_env": "ANTHROPIC_API_KEY", "key_set": True, "sign_in": False}
        assert body["costs"]["1"]["usd"] == 0.13 and body["costs"]["5"]["measured"] is True
        assert [h["id"] for h in body["holdings"]] == ["my_book.json"] and body["holdings"][0]["positions"] == 1
        assert KEY not in json.dumps(body)
        assert "path" not in body["holdings"][0]                      # the page learns a name, never a path

    def test_the_defaults_follow_the_saved_settings(self, dotenv, rig, monkeypatch):
        monkeypatch.setenv("TRADINGAGENTS_RESEARCH_PASSES", "11")
        monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "3")
        monkeypatch.setenv("TRADINGAGENTS_NO_SCORECARD", "1")
        body = client(dotenv, runs=rig[0]).get("/api/run-options", headers=HEADERS).json()
        assert body["defaults"]["passes"] == 11 and 11 in body["passes"] and body["defaults"]["depth"] == 3
        assert body["defaults"]["scorecard"] is False

    def test_a_started_analysis_is_listed_and_launched_with_the_form_choices(self, dotenv, rig):
        manager, launches, _ = rig
        c = client(dotenv, runs=manager)
        r = c.post("/api/runs", headers=HEADERS, json=self.body(tickers=["nvda", "MU"], holdings="my_book.json"))
        assert r.status_code == 202 and len(r.json()["started"]) == 2
        end = __import__("time").time() + 5
        while not launches and __import__("time").time() < end:
            __import__("time").sleep(0.01)
        listing = c.get("/api/runs", headers=HEADERS).json()["runs"]
        assert [j["ticker"] for j in listing] == ["MU", "NVDA"] and listing[1]["status"] == "running" and listing[0]["status"] == "waiting"
        assert listing[0]["place"] == 1 and listing[1]["progress"]["steps"] == 5
        assert launches[0][launches[0].index("--ticker") + 1] == "NVDA" and "--portfolio" in launches[0]
        assert listing[1]["holdings"] == "my_book.json" and "api_key" not in json.dumps(listing).lower()

    def test_a_running_analysis_can_be_stopped(self, dotenv, rig):
        manager, launches, procs = rig
        c = client(dotenv, runs=manager)
        job = c.post("/api/runs", headers=HEADERS, json=self.body()).json()["started"][0]
        end = __import__("time").time() + 5
        while not launches and __import__("time").time() < end:
            __import__("time").sleep(0.01)
        assert c.post(f"/api/runs/{job}/cancel", headers=HEADERS).json() == {"stopped": job}
        end = __import__("time").time() + 5
        while manager.get(job)["status"] != "stopped" and __import__("time").time() < end:
            __import__("time").sleep(0.01)
        assert manager.get(job)["status"] == "stopped" and procs[0].killed
        assert c.post(f"/api/runs/{job}/cancel", headers=HEADERS).status_code == 404        # already over

    @pytest.mark.parametrize("job_id", ["nope", "../../etc", "20261108-101010-zzzz", "20261108-101010-abcd"])
    def test_stopping_something_that_is_not_there_is_a_404(self, dotenv, rig, job_id):
        assert client(dotenv, runs=rig[0]).post(f"/api/runs/{job_id}/cancel", headers=HEADERS).status_code in (404, 405)

    @pytest.mark.parametrize("changes, words", [
        ({"tickers": ["../x"]}, "not a symbol"),
        ({"tickers": []}, "at least one"),
        ({"date": "2999-01-01"}, "future"),
        ({"date": "soon"}, "Pick a date"),
        ({"analysts": []}, "at least one analyst"),
        ({"analysts": ["wizard"]}, "not recognized"),
        ({"depth": 2}, "Shallow, Medium or Deep"),
        ({"passes": 99}, "whole number"),
        ({"language": "X=1\nY"}, "language"),
        ({"holdings": "other.json"}, "not one of the files offered"),
        ({"holdings": "C:\\Users\\x\\secrets.json"}, "not one of the files offered"),
    ])
    def test_a_bad_request_is_a_clear_422_and_starts_nothing(self, dotenv, rig, changes, words):
        manager, launches, _ = rig
        r = client(dotenv, runs=manager).post("/api/runs", headers=HEADERS, json=self.body(**changes))
        assert r.status_code == 422 and words in r.json()["detail"]
        assert manager.listing() == [] and launches == []

    def test_a_malformed_body_is_refused_without_an_echo(self, dotenv, rig):
        c = client(dotenv, runs=rig[0])
        for bad in ({"tickers": "NVDA"}, {**self.body(), "depth": "deep"}, {**self.body(), "tickers": ["A"] * 50}, {}):
            assert c.post("/api/runs", headers=HEADERS, json=bad).status_code == 422
        assert rig[0].listing() == []

    def test_no_saved_key_stops_the_start_before_anything_is_queued(self, dotenv, rig, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = client(dotenv, runs=rig[0]).post("/api/runs", headers=HEADERS, json=self.body())
        assert r.status_code == 422 and "No key is saved" in r.json()["detail"] and rig[0].listing() == []

    def test_the_same_analysis_cannot_be_started_twice(self, dotenv, rig):
        c = client(dotenv, runs=rig[0])
        assert c.post("/api/runs", headers=HEADERS, json=self.body()).status_code == 202
        again = c.post("/api/runs", headers=HEADERS, json=self.body())
        assert again.status_code == 422 and "already waiting or running" in again.json()["detail"]

    def test_the_run_scripts_and_markup_are_served_and_use_no_inline_code(self, dotenv, rig):
        from pathlib import Path

        c = client(dotenv, runs=rig[0])
        js = c.get("/static/runs.js")
        assert js.status_code == 200 and "/api/runs" in js.text and "X-Council-Token" not in js.text   # the token stays in app.js
        html = (Path(server.STATIC) / "index.html").read_text(encoding="utf-8")
        assert '<script src="/static/runs.js">' in html and 'id="run-form"' in html and 'id="start"' in html
        assert " style=" not in html and not re.search(r"\son[a-z]+=", html)

    def test_the_manager_is_made_only_when_first_needed_and_closed_with_the_app(self, dotenv, monkeypatch):
        made = []
        monkeypatch.setattr(server.run_tools, "RunManager", lambda *a, **k: made.append(1) or type("M", (), {
            "listing": lambda s: [], "busy": lambda s: False, "shutdown": lambda s: made.append("closed")})())
        c = client(dotenv)
        assert c.get("/api/state", headers=HEADERS).status_code == 200 and made == []
        assert c.get("/api/runs", headers=HEADERS).json() == {"runs": []} and made == [1]
        with TestClient(c.app, base_url="http://127.0.0.1:8765"):
            pass
        assert made == [1, "closed"]


@pytest.mark.unit
class TestLiveAndFilesApi:
    """The live view, opening a report, and the CSV and PDF downloads."""

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        import time

        from tests.test_ui_runs import FakeProc, write_run
        from tradingagents.ui import runs as rn

        for name in list(store.registry()):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "claude-haiku-5-5")
        monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", "claude-sonnet-5")
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setattr(server, "holdings_folders", lambda cfg: [tmp_path / "none"])
        opened, state = [], {"hold": False}

        def launcher(command, env, cwd, console):
            if state["hold"]:
                return FakeProc(0, hold=True)
            write_run(tmp_path / "logs", ticker=command[command.index("--ticker") + 1], day=command[command.index("--date") + 1])
            return FakeProc(0)

        manager = rn.RunManager(tmp_path / "logs", launcher=launcher, python="python", poll_seconds=0.005,
                                sleep=lambda s: time.sleep(0.005), killer=lambda p: setattr(p, "killed", True),
                                opener=lambda p: opened.append(p) or True)
        yield manager, opened, state, tmp_path
        manager.shutdown()

    def body(self, **changes):
        base = {"tickers": ["NVDA"], "date": server.date.today().isoformat(), "analysts": ["market"], "depth": 1, "passes": 5,
                "language": "English", "holdings": None, "selloff": True, "scorecard": True, "open_report": True}
        base.update(changes)
        return base

    def start(self, c, manager, **changes):
        r = c.post("/api/runs", headers=HEADERS, json=self.body(**changes))
        assert r.status_code == 202, r.text
        return r.json()["started"]

    def wait_done(self, manager, ids):
        import time

        end = time.time() + 5
        while time.time() < end and any(manager.get(i)["status"] != "done" for i in ids):
            time.sleep(0.01)
        assert all(manager.get(i)["status"] == "done" for i in ids)

    def test_every_new_endpoint_needs_the_token(self, dotenv, setup):
        c = client(dotenv, runs=setup[0])
        job = "20261108-101010-abcd"
        assert c.get(f"/api/runs/{job}/live").status_code == 401
        assert c.post(f"/api/runs/{job}/open-report").status_code == 401
        assert c.post("/api/export", json={"format": "csv", "ids": []}).status_code == 401

    def test_the_live_view_follows_a_running_analysis(self, dotenv, setup):
        import time

        from tradingagents.live_events import FILE_NAME, EventLog

        manager, _, state, tmp_path = setup
        state["hold"] = True
        c = client(dotenv, runs=manager)
        job, = self.start(c, manager)
        end = time.time() + 5
        while manager.get(job)["status"] != "running" and time.time() < end:
            time.sleep(0.01)
        run_dir = tmp_path / "logs" / "NVDA" / server.date.today().isoformat()
        log = EventLog(run_dir / FILE_NAME)
        log.record("Market Analyst", "Agent", "Looking at the price history.")
        log.record("Market Analyst", "Tool Call", "get_stock_data(start_date=2025-11-08)")
        (run_dir / "reports").mkdir(parents=True, exist_ok=True)
        (run_dir / "reports" / "market_report.md").write_text("The market report.", encoding="utf-8")

        first = c.get(f"/api/runs/{job}/live", headers=HEADERS).json()
        assert first["status"] == "running" and [e["kind"] for e in first["events"]] == ["thought", "request"]
        assert first["events"][0]["source"] == "Market Analyst" and first["next"] > 0
        assert {a["id"]: a["state"] for a in first["agents"]}["market"] == "done"
        assert first["sections"][0]["text"] == "The market report."

        log.record("Market Analyst", "Data", "250 rows of prices")
        rev = first["sections"][0]["rev"]
        again = c.get(f"/api/runs/{job}/live", headers=HEADERS, params={"after": first["next"], "have": f"market:{rev}"}).json()
        assert [e["kind"] for e in again["events"]] == ["data"] and again["sections"][0]["text"] is None

    @pytest.mark.parametrize("job_id", ["nope", "../../x", "20261108-101010-abcd"])
    def test_the_live_view_of_something_unknown_is_a_404(self, dotenv, setup, job_id):
        assert client(dotenv, runs=setup[0]).get(f"/api/runs/{job_id}/live", headers=HEADERS).status_code == 404

    def test_odd_query_values_are_refused_or_ignored(self, dotenv, setup):
        manager, _, state, _ = setup
        state["hold"] = True
        c = client(dotenv, runs=manager)
        job, = self.start(c, manager)
        assert c.get(f"/api/runs/{job}/live", headers=HEADERS, params={"after": -1}).status_code == 404
        assert c.get(f"/api/runs/{job}/live", headers=HEADERS, params={"after": "x"}).status_code == 422
        junk = "a:b,../../etc:1-2,market:zz-1,x" + ",y:1-1" * 50
        assert c.get(f"/api/runs/{job}/live", headers=HEADERS, params={"have": junk}).status_code == 200

    def test_a_finished_analysis_opens_its_report_by_itself_and_on_request(self, dotenv, setup):
        manager, opened, _, _ = setup
        c = client(dotenv, runs=manager)
        job, = self.start(c, manager)
        self.wait_done(manager, [job])
        import time

        end = time.time() + 5
        while not opened and time.time() < end:
            time.sleep(0.01)
        assert len(opened) == 1 and opened[0].name == "complete_report.html"
        assert c.post(f"/api/runs/{job}/open-report", headers=HEADERS).json() == {"opened": True} and len(opened) == 2

    def test_opening_a_report_that_does_not_exist_is_a_404(self, dotenv, setup):
        manager, opened, state, _ = setup
        state["hold"] = True
        c = client(dotenv, runs=manager)
        job, = self.start(c, manager)
        assert c.post(f"/api/runs/{job}/open-report", headers=HEADERS).status_code == 404
        assert c.post("/api/runs/bad/open-report", headers=HEADERS).status_code == 404 and opened == []

    def test_all_finished_analyses_come_back_as_one_csv(self, dotenv, setup):
        manager, _, _, _ = setup
        c = client(dotenv, runs=manager)
        ids = self.start(c, manager, tickers=["NVDA", "MU"], open_report=False)
        self.wait_done(manager, ids)
        r = c.post("/api/export", headers=HEADERS, json={"format": "csv", "ids": []})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
        rows = list(__import__("csv").DictReader(io.StringIO(r.content.decode("utf-8").lstrip("﻿"), newline="")))
        assert [row["ticker"] for row in rows] == ["NVDA", "MU"] and rows[0]["rating"] == "Hold" and rows[0]["conviction_score"] == "48"
        one = c.post("/api/export", headers=HEADERS, json={"format": "csv", "ids": [ids[1]]})
        assert [row["ticker"] for row in __import__("csv").DictReader(io.StringIO(one.content.decode("utf-8").lstrip("﻿"), newline=""))] == ["MU"]

    def test_a_pdf_is_made_for_one_analysis(self, dotenv, setup, monkeypatch):
        manager, _, _, _ = setup
        c = client(dotenv, runs=manager)
        asked = []
        monkeypatch.setattr(server.exports, "html_to_pdf", lambda page, *a, **k: asked.append(page) or b"%PDF-1.4 made")
        ids = self.start(c, manager, tickers=["NVDA", "MU"], open_report=False)
        self.wait_done(manager, ids)
        r = c.post("/api/export", headers=HEADERS, json={"format": "pdf", "ids": [ids[0]]})
        assert r.status_code == 200 and r.content == b"%PDF-1.4 made" and r.headers["content-type"] == "application/pdf"
        assert asked and asked[0].name == "complete_report.html"
        many = c.post("/api/export", headers=HEADERS, json={"format": "pdf", "ids": ids})
        assert many.status_code == 422 and "one analysis at a time" in many.json()["detail"]
        assert c.post("/api/export", headers=HEADERS, json={"format": "pdf", "ids": []}).status_code == 422

    def test_a_pdf_that_cannot_be_made_says_why(self, dotenv, setup, monkeypatch):
        manager, _, _, _ = setup
        c = client(dotenv, runs=manager)

        def broken(page, *a, **k):
            raise server.exports.ExportError("Making a PDF needs Chrome, Edge or Chromium on this computer.")

        monkeypatch.setattr(server.exports, "html_to_pdf", broken)
        job, = self.start(c, manager, open_report=False)
        self.wait_done(manager, [job])
        r = c.post("/api/export", headers=HEADERS, json={"format": "pdf", "ids": [job]})
        assert r.status_code == 422 and "needs Chrome, Edge or Chromium" in r.json()["detail"]

    @pytest.mark.parametrize("body, words", [
        ({"format": "docx", "ids": []}, "Choose CSV or PDF"),
        ({"format": "csv", "ids": ["../../x"]}, "Choose CSV or PDF"),
        ({"format": "csv", "ids": ["20261108-101010-abcd"]}, "no finished analysis"),
        ({"format": "csv", "ids": []}, "no finished analysis"),
        ({"format": "pdf", "ids": ["20261108-101010-abcd"]}, "no finished analysis"),
    ])
    def test_a_bad_export_request_is_a_clear_422(self, dotenv, setup, body, words):
        r = client(dotenv, runs=setup[0]).post("/api/export", headers=HEADERS, json=body)
        assert r.status_code == 422 and words in r.json()["detail"]

    def test_a_running_analysis_cannot_be_exported(self, dotenv, setup):
        manager, _, state, _ = setup
        state["hold"] = True
        c = client(dotenv, runs=manager)
        job, = self.start(c, manager)
        assert c.post("/api/export", headers=HEADERS, json={"format": "csv", "ids": [job]}).status_code == 422

    def test_the_page_offers_the_report_buttons_the_live_view_and_the_save_dialog(self):
        from pathlib import Path

        html = (Path(server.STATIC) / "index.html").read_text(encoding="utf-8")
        js = (Path(server.STATIC) / "runs.js").read_text(encoding="utf-8")
        assert 'id="r-open"' in html and 'id="save-all-csv"' in html and 'id="export-result"' in html and 'id="export-error"' in html
        css = (Path(server.STATIC) / "app.css").read_text(encoding="utf-8")
        assert 'id="live-toggle"' in html and 'html[data-live="off"] .live-slot' in css       # the live view's on/off switch
        assert 'j.status === "waiting" || !liveOn()' in js and "if (!liveOn()) return;" in js     # it stops loading and polling
        assert 'pref("ac-live"' in js                                                          # and the choice is remembered
        for needed in ("showSaveFilePicker", "/api/export", "/live?after=", "/open-report", "data-save=", "data-open-report=", "Watch the analysts live"):
            assert needed in js
        assert "apiBlob" in (Path(server.STATIC) / "app.js").read_text(encoding="utf-8")


@pytest.mark.unit
class TestServerHelpers:
    def test_a_free_port_is_found_past_a_busy_one(self):
        import socket
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0)); busy.listen(1)
            taken = busy.getsockname()[1]
            assert server.find_free_port(taken, 5) != taken

    def test_the_server_listens_only_on_the_loopback_address(self):
        assert server.HOST == "127.0.0.1"
        import inspect
        assert 'host=HOST' in inspect.getsource(server.run_server) and '"0.0.0.0"' not in inspect.getsource(server)


@pytest.mark.unit
class TestCommand:
    def test_the_ui_command_exists_and_passes_its_options(self, monkeypatch):
        from typer.testing import CliRunner

        from cli.main import app
        calls = {}
        monkeypatch.setattr(server, "run_server", lambda port=None, open_browser=True, dotenv_path=None: calls.update(port=port, open=open_browser))
        result = CliRunner().invoke(app, ["ui", "--port", "9001", "--no-browser"])
        assert result.exit_code == 0 and calls == {"port": 9001, "open": False}

    def test_the_doctor_can_check_a_single_item(self):
        from tradingagents.default_config import build_default_config
        ctx = dr.Context(env={}, config=build_default_config(), network=False, live_ai=False)
        report = dr.run_doctor(ctx=ctx, ids=["settings.selloff"])
        assert [r.id for r in report.results] == ["settings.selloff"]
