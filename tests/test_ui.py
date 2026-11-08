"""The local setup screen: the settings writer, the server's guards, and that no secret ever leaves it."""

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


def client(dotenv, port=8765, **kw):
    app = server.create_app(TOKEN, dotenv, port)
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
