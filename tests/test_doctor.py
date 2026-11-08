"""The doctor: plain-words status of the setup, offline here with fake network replies and fake AI models."""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from tradingagents import doctor as dr
from tradingagents.default_config import build_default_config

KEY = "sk-ant-REALLOOKINGKEY1234567890"
FRED = "fredkey1234567890abcdef"
QUIVER = "quivertoken1234567890"
ALPHA = "alphakey1234567890"


class FakeHttp:
    """Answers by URL fragment; remembers every call. An Exception value is raised."""

    def __init__(self, routes=None, default=200):
        self.routes, self.default, self.calls = routes or {}, default, []

    def __call__(self, url, headers=None, params=None, timeout=12, max_bytes=4000):
        self.calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        for fragment, answer in self.routes.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer if isinstance(answer, dr.Reply) else dr.Reply(answer, "")
        return dr.Reply(self.default, "{}")


class FakeLLM:
    def __init__(self, structured=True, error=None, structured_error=None, tokens=12):
        self.structured, self.error, self.structured_error, self.tokens = structured, error, structured_error, tokens

    def invoke(self, prompt):
        if self.error:
            raise self.error
        return SimpleNamespace(content="OK", usage_metadata={"total_tokens": self.tokens})

    def with_structured_output(self, schema):
        if not self.structured:
            raise NotImplementedError
        outer = self

        class Bound:
            def invoke(self, prompt):
                if outer.structured_error:
                    raise outer.structured_error
                return schema(ok=True)

        return Bound()


def make_ctx(tmp_path, env=None, http=None, llm=None, **over):
    cfg = build_default_config()
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "memory" / "log.md"), llm_provider="anthropic",
               quick_think_llm="claude-haiku-5-5", deep_think_llm="claude-sonnet-5",
               quick_think_provider=None, deep_think_provider=None, temperature=None, research_passes=5)
    full_env = {"ANTHROPIC_API_KEY": KEY, "FRED_API_KEY": FRED, "QUIVER_API_KEY": QUIVER, "ALPHA_VANTAGE_API_KEY": ALPHA,
                "SEC_EDGAR_USER_AGENT": "Test Person test@example.org"}
    full_env.update(env or {})
    cfg.update(over.pop("config", {}))
    return dr.Context(env=full_env, config=cfg, http_get=http or FakeHttp(), dotenv_path=over.pop("dotenv_path", "/x/.env"),
                      make_llm=lambda tier: llm or FakeLLM(), yahoo_rows=over.pop("yahoo_rows", lambda: 5), **over)


def run(ctx, **kw):
    return dr.run_doctor(ctx=ctx, **kw)


def by_id(report, id_):
    return next(r for r in report.results if r.id == id_)


@pytest.mark.unit
class TestScrub:
    def test_secrets_in_the_environment_and_in_common_shapes_are_hidden(self):
        env = {"ANTHROPIC_API_KEY": KEY, "OTHER": "visible-value-here"}
        text = dr.scrub(f"failed with {KEY} at https://x.test/q?api_key=abc123&y=1 header Bearer abcdefghijklmnop and sk-abcdefghij1234 end", env)
        assert KEY not in text and "abc123" not in text and "abcdefghijklmnop" not in text and "sk-abcdefghij1234" not in text
        assert text.count("[hidden]") >= 4

    def test_it_puts_the_text_on_one_line_and_cuts_it(self):
        assert "\n" not in dr.scrub("a\nb\nc")
        assert len(dr.scrub("x" * 1000)) == 300 and len(dr.scrub("x" * 1000, limit=50)) == 50

    @pytest.mark.parametrize("value, is_set", [("sk-real-looking-123456", True), ("", False), ("   ", False), ("your_api_key_here", False),
                                               ("placeholder", False), ("changeme", False), ("<your key>", False), (None, False)])
    def test_example_text_does_not_count_as_a_key(self, value, is_set):
        assert dr.key_is_set({"K": value} if value is not None else {}, "K") is is_set


@pytest.mark.unit
class TestHealthyRun:
    def test_everything_working_is_ready_and_no_key_appears_anywhere(self, tmp_path):
        http = FakeHttp({"alphavantage": dr.Reply(200, '{"Global Quote": {}}')})
        report = run(make_ctx(tmp_path, http=http))
        assert report.ready and report.counts[dr.NOT_WORKING] == 0
        everything = dr.format_text(report) + dr.format_json(report)
        for secret in (KEY, FRED, QUIVER, ALPHA):
            assert secret not in everything
        assert {r.group for r in report.results} == set(dr.GROUPS)

    def test_the_keys_are_sent_only_where_they_belong(self, tmp_path):
        http = FakeHttp({"alphavantage": dr.Reply(200, "{}")})
        run(make_ctx(tmp_path, http=http))
        fred = next(c for c in http.calls if "stlouisfed.org/fred/series" in c["url"])
        assert fred["params"]["api_key"] == FRED
        quiver = next(c for c in http.calls if "quiverquant" in c["url"])
        assert quiver["headers"]["Authorization"] == f"Bearer {QUIVER}"
        assert all(KEY not in json.dumps(c) for c in http.calls)          # the AI key never goes to a data source

    def test_a_check_that_breaks_is_reported_and_the_others_still_run(self, tmp_path, monkeypatch):
        def boom(ctx):
            raise RuntimeError(f"exploded with {KEY}")
        monkeypatch.setattr(dr, "check_python", boom)
        report = run(make_ctx(tmp_path))
        broken = by_id(report, "env.python")
        assert broken.status == dr.NOT_WORKING and "The check itself failed" in broken.detail and KEY not in broken.detail
        assert by_id(report, "data.yahoo").status == dr.OK


@pytest.mark.unit
class TestEnvironment:
    def test_unwritable_folders_stop_the_run(self, tmp_path):
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        report = run(make_ctx(tmp_path, config={"results_dir": str(blocker / "inside")}))
        r = by_id(report, "env.folders")
        assert r.status == dr.NOT_WORKING and r.required and not report.ready and "TRADINGAGENTS_RESULTS_DIR" in r.fix

    def test_no_dotenv_is_fine_when_the_keys_come_from_the_environment(self, tmp_path):
        assert by_id(run(make_ctx(tmp_path, dotenv_path=None)), "env.dotenv").status == dr.OK
        r = by_id(run(make_ctx(tmp_path, dotenv_path=None, env={"ANTHROPIC_API_KEY": ""})), "env.dotenv")
        assert r.status == dr.NEEDS_SETUP and ".env.example" in r.fix

    def test_a_certificate_bundle_that_does_not_exist_is_a_problem(self, tmp_path):
        good = tmp_path / "bundle.pem"
        good.write_text("x")
        assert by_id(run(make_ctx(tmp_path)), "env.certificates").status == dr.OFF
        assert by_id(run(make_ctx(tmp_path, env={"REQUESTS_CA_BUNDLE": str(good)})), "env.certificates").status == dr.OK
        bad = by_id(run(make_ctx(tmp_path, env={"SSL_CERT_FILE": str(tmp_path / "missing.pem")})), "env.certificates")
        assert bad.status == dr.NOT_WORKING and "SSL_CERT_FILE" in bad.detail

    def test_an_antivirus_certificate_failure_is_explained(self, tmp_path):
        http = FakeHttp({"api.stlouisfed.org/fred/": dr.CertificateProblem()})
        r = by_id(run(make_ctx(tmp_path, http=http)), "env.network")
        assert r.status == dr.NOT_WORKING and r.required
        assert "antivirus" in r.fix and "REQUESTS_CA_BUNDLE" in r.fix

    def test_no_connection_is_reported(self, tmp_path):
        r = by_id(run(make_ctx(tmp_path, http=FakeHttp({"api.stlouisfed.org/fred/": dr.NetworkProblem("no connection")}))), "env.network")
        assert r.status == dr.NOT_WORKING and "no connection" in r.detail

    def test_offline_mode_skips_every_network_check_and_says_the_check_is_partial(self, tmp_path):
        ctx = make_ctx(tmp_path, network=False, live_ai=False)
        report = run(ctx)
        assert by_id(report, "env.network").status == dr.SKIPPED and by_id(report, "data.yahoo").status == dr.SKIPPED
        assert report.skipped_some and report.ready
        assert "not a full check" in dr.format_text(report)


@pytest.mark.unit
class TestAI:
    def test_a_missing_key_needs_setup_and_the_live_test_is_skipped(self, tmp_path):
        report = run(make_ctx(tmp_path, env={"ANTHROPIC_API_KEY": ""}))
        key = by_id(report, "ai.key.anthropic")
        assert key.status == dr.NEEDS_SETUP and key.required and "ANTHROPIC_API_KEY" in key.fix and not report.ready
        assert by_id(report, "ai.quick.call").status == dr.SKIPPED

    def test_example_text_in_the_key_is_called_out(self, tmp_path):
        r = by_id(run(make_ctx(tmp_path, env={"ANTHROPIC_API_KEY": "your_api_key_here"})), "ai.key.anthropic")
        assert r.status == dr.NEEDS_SETUP and "example text" in r.detail

    def test_one_key_check_per_provider_when_the_tiers_differ(self, tmp_path):
        report = run(make_ctx(tmp_path, env={"OPENAI_API_KEY": "sk-openai-1234567890abc"},
                              config={"deep_think_provider": "openai", "deep_think_llm": "gpt-6-sol"}))
        ids = {r.id for r in report.results if r.id.startswith("ai.key.")}
        assert ids == {"ai.key.anthropic", "ai.key.openai"}

    def test_a_provider_that_needs_no_key_is_fine(self, tmp_path):
        report = run(make_ctx(tmp_path, config={"llm_provider": "ollama", "quick_think_llm": "llama3", "deep_think_llm": "llama3"}))
        assert by_id(report, "ai.key.ollama").status == dr.OK

    def test_a_signed_in_provider_is_checked_by_its_sign_in(self, tmp_path, monkeypatch):
        from tradingagents.llm_clients import chatgpt_auth
        monkeypatch.setattr(chatgpt_auth, "signed_in_as", lambda: None)
        cfg = {"llm_provider": "chatgpt", "quick_think_llm": "gpt-x", "deep_think_llm": "gpt-x"}
        r = by_id(run(make_ctx(tmp_path, config=cfg)), "ai.key.chatgpt")
        assert r.status == dr.NEEDS_SETUP and "tradingagents login chatgpt" in r.fix
        monkeypatch.setattr(chatgpt_auth, "signed_in_as", lambda: "me@example.org")
        assert by_id(run(make_ctx(tmp_path, config=cfg)), "ai.key.chatgpt").status == dr.OK

    def test_a_claude_5_model_with_a_temperature_set_is_a_known_failure(self, tmp_path):
        r = by_id(run(make_ctx(tmp_path, config={"temperature": 0.2})), "ai.deep.model")
        assert r.status == dr.NOT_WORKING and "temperature" in r.detail and "TRADINGAGENTS_TEMPERATURE" in r.fix

    def test_a_model_name_the_app_does_not_list_is_a_note_not_a_blocker(self, tmp_path):
        report = run(make_ctx(tmp_path, config={"quick_think_llm": "claude-brand-new-9"}))
        r = by_id(report, "ai.quick.model")
        assert r.status == dr.OK and "not in the app's list" in r.detail and report.ready

    def test_the_live_call_reports_time_and_tokens(self, tmp_path):
        r = by_id(run(make_ctx(tmp_path, llm=FakeLLM(tokens=33))), "ai.quick.call")
        assert r.status == dr.OK and "claude-haiku-5-5" in r.detail and "33 tokens" in r.detail

    @pytest.mark.parametrize("error, words", [
        (type("AuthenticationError", (Exception,), {"status_code": 401})("bad key"), "refused the key"),
        (type("RateLimitError", (Exception,), {"status_code": 429})("slow down"), "rate limit or quota"),
        (type("NotFoundError", (Exception,), {"status_code": 404})("no such model"), "does not know this model name"),
        (type("APITimeoutError", (Exception,), {})("Request timed out."), "did not answer in time"),
        (type("BadRequestError", (Exception,), {"status_code": 400})("temperature is not supported"), "rejects a temperature"),
    ])
    def test_a_failed_live_call_is_explained_in_plain_words(self, tmp_path, error, words):
        r = by_id(run(make_ctx(tmp_path, llm=FakeLLM(error=error))), "ai.deep.call")
        assert r.status == dr.NOT_WORKING and r.required and words in r.detail

    def test_an_error_that_quotes_the_key_never_shows_it(self, tmp_path):
        r = by_id(run(make_ctx(tmp_path, llm=FakeLLM(error=RuntimeError(f"weird failure for key {KEY}")))), "ai.quick.call")
        assert r.status == dr.NOT_WORKING and KEY not in r.detail and "[hidden]" in r.detail

    def test_structured_answers_are_tested_separately(self, tmp_path):
        assert by_id(run(make_ctx(tmp_path)), "ai.quick.structured").status == dr.OK
        no_support = by_id(run(make_ctx(tmp_path, llm=FakeLLM(structured=False))), "ai.quick.structured")
        assert no_support.status == dr.NEEDS_SETUP and "plain text" in no_support.detail and not no_support.required
        failing = by_id(run(make_ctx(tmp_path, llm=FakeLLM(structured_error=ValueError("bad")))), "ai.quick.structured")
        assert failing.status == dr.NOT_WORKING and "retry once" in failing.fix

    def test_no_live_call_when_it_is_switched_off(self, tmp_path):
        report = run(make_ctx(tmp_path, live_ai=False))
        assert by_id(report, "ai.quick.call").status == dr.SKIPPED and by_id(report, "ai.deep.structured").status == dr.SKIPPED


@pytest.mark.unit
class TestDataSources:
    def test_yahoo_is_required_and_its_failure_blocks_readiness(self, tmp_path):
        def down():
            raise ConnectionError("blocked")
        report = run(make_ctx(tmp_path, yahoo_rows=down))
        r = by_id(report, "data.yahoo")
        assert r.status == dr.NOT_WORKING and r.required and not report.ready and "ConnectionError" in r.detail
        assert by_id(run(make_ctx(tmp_path, yahoo_rows=lambda: 0)), "data.yahoo").status == dr.NOT_WORKING

    def test_sec_wants_a_real_contact_address(self, tmp_path):
        placeholder = by_id(run(make_ctx(tmp_path, env={"SEC_EDGAR_USER_AGENT": ""})), "data.sec")
        assert placeholder.status == dr.NEEDS_SETUP and "SEC_EDGAR_USER_AGENT" in placeholder.fix and not placeholder.required
        assert by_id(run(make_ctx(tmp_path)), "data.sec").status == dr.OK
        refused = by_id(run(make_ctx(tmp_path, http=FakeHttp({"data.sec.gov": 403}))), "data.sec")
        assert refused.status == dr.NOT_WORKING and "identify themselves" in refused.detail

    def test_sec_is_probed_with_the_users_own_contact(self, tmp_path):
        http = FakeHttp()
        run(make_ctx(tmp_path, http=http))
        sec = next(c for c in http.calls if "data.sec.gov" in c["url"])
        assert sec["headers"]["User-Agent"] == "Test Person test@example.org"

    def test_fred_needs_its_free_key_and_a_rejected_key_is_not_leaked(self, tmp_path):
        assert by_id(run(make_ctx(tmp_path, env={"FRED_API_KEY": ""})), "data.fred").status == dr.NEEDS_SETUP
        assert by_id(run(make_ctx(tmp_path)), "data.fred").status == dr.OK
        rejected = by_id(run(make_ctx(tmp_path, http=FakeHttp({"stlouisfed.org/fred/series": 400}))), "data.fred")
        assert rejected.status == dr.NOT_WORKING and "FRED_API_KEY" in rejected.fix and FRED not in rejected.detail

    def test_finra_looks_back_over_recent_business_days(self, tmp_path):
        from datetime import datetime
        # a Monday: the days tried are the previous Friday, Thursday, ... and weekends are skipped
        ctx = make_ctx(tmp_path, http=FakeHttp({"CNMSshvol": 404}), now=lambda: datetime(2026, 11, 9, 12, 0))
        assert by_id(run(ctx), "data.finra").status == dr.NOT_WORKING
        urls = [c["url"] for c in ctx.http_get.calls if "CNMSshvol" in c["url"]]
        assert len(urls) == 6 and urls[0].endswith("CNMSshvol20261106.txt") and not any("20261107" in u or "20261108" in u for u in urls)
        assert by_id(run(make_ctx(tmp_path)), "data.finra").status == dr.OK

    def test_quiver_statuses(self, tmp_path):
        assert by_id(run(make_ctx(tmp_path, env={"QUIVER_API_KEY": ""})), "data.quiver").status == dr.NEEDS_SETUP
        full = by_id(run(make_ctx(tmp_path)), "data.quiver")
        assert full.status == dr.OK and "Congressional trades: available" in full.detail and "Insider filings: available" in full.detail
        rejected = by_id(run(make_ctx(tmp_path, http=FakeHttp({"quiverquant": 401}))), "data.quiver")
        assert rejected.status == dr.NOT_WORKING and "QUIVER_API_KEY" in rejected.fix
        assert by_id(run(make_ctx(tmp_path, http=FakeHttp({"quiverquant": 500}))), "data.quiver").status == dr.NOT_WORKING

    def test_a_quiver_plan_without_insiders_is_fine_not_a_nag(self, tmp_path):
        # the real case: the token works and congressional trades are there, the insider dataset is not in the plan
        http = FakeHttp({"live/insiders": 403, "historical/congresstrading": 200})
        r = by_id(run(make_ctx(tmp_path, http=http)), "data.quiver")
        assert r.status == dr.OK and "Congressional trades: available" in r.detail
        assert "not in your plan, so insider data comes from Yahoo" in r.detail and not r.fix

    def test_no_trades_for_the_test_symbol_does_not_mean_a_bad_token(self, tmp_path):
        http = FakeHttp({"historical/congresstrading": 404})
        assert by_id(run(make_ctx(tmp_path, http=http)), "data.quiver").status == dr.OK

    def test_alpha_vantage_statuses(self, tmp_path):
        assert by_id(run(make_ctx(tmp_path, env={"ALPHA_VANTAGE_API_KEY": ""})), "data.alphavantage").status == dr.NEEDS_SETUP
        ok = FakeHttp({"alphavantage": dr.Reply(200, '{"Global Quote": {"01. symbol": "IBM"}}')})
        assert by_id(run(make_ctx(tmp_path, http=ok)), "data.alphavantage").status == dr.OK
        bad = FakeHttp({"alphavantage": dr.Reply(200, '{"Error Message": "Invalid API call."}')})
        assert by_id(run(make_ctx(tmp_path, http=bad)), "data.alphavantage").status == dr.NOT_WORKING
        limit = FakeHttp({"alphavantage": dr.Reply(200, '{"Information": "rate limit is 25 requests per day"}')})
        assert by_id(run(make_ctx(tmp_path, http=limit)), "data.alphavantage").status == dr.NEEDS_SETUP

    def test_the_keyless_sources_are_probed_and_an_outage_is_optional(self, tmp_path):
        http = FakeHttp({"gamma-api.polymarket": 500, "api.fda.gov": 503})
        report = run(make_ctx(tmp_path, http=http))
        assert by_id(report, "data.polymarket").status == dr.NOT_WORKING and by_id(report, "data.openfda").status == dr.NOT_WORKING
        assert by_id(report, "data.usaspending").status == dr.OK
        assert report.ready      # none of them is required

    def test_a_rate_limit_is_explained_as_a_wait_not_a_breakage(self, tmp_path):
        r = by_id(run(make_ctx(tmp_path, http=FakeHttp({"reddit.com": 429}))), "data.reddit")
        assert r.status == dr.NOT_WORKING and "limiting requests" in r.detail and "Wait a few minutes" in r.fix and not r.required

    def test_the_ibkr_helper_is_off_unless_configured(self, tmp_path):
        assert by_id(run(make_ctx(tmp_path)), "data.ibkr").status == dr.OFF
        assert by_id(run(make_ctx(tmp_path, env={"IBKR_HELPER_DIR": str(tmp_path)})), "data.ibkr").status == dr.OK
        gone = by_id(run(make_ctx(tmp_path, env={"IBKR_HELPER_DIR": str(tmp_path / "nowhere")})), "data.ibkr")
        assert gone.status == dr.NOT_WORKING

    def test_an_optional_source_that_needs_setup_says_what_it_adds(self, tmp_path):
        text = dr.format_text(run(make_ctx(tmp_path, env={"QUIVER_API_KEY": ""})))
        assert "Adds: congressional trades and insider filings." in text


@pytest.mark.unit
class TestSettings:
    @pytest.mark.parametrize("passes, status, words", [(1, dr.OK, "original single debate"), (5, dr.OK, "median"),
                                                       (4, dr.NEEDS_SETUP, "odd number")])
    def test_research_passes_are_explained_with_their_cost(self, tmp_path, passes, status, words):
        r = by_id(run(make_ctx(tmp_path, config={"research_passes": passes})), "settings.passes")
        assert r.status == status and words in (r.detail + r.fix)

    def test_switches_are_reported_in_words(self, tmp_path):
        report = run(make_ctx(tmp_path, env={"TRADINGAGENTS_NO_SELLOFF_RULES": "1", "TRADINGAGENTS_DISABLED_TOOLS": "get_filing_events, get_public_records"}))
        assert by_id(report, "settings.selloff").status == dr.OFF
        assert by_id(report, "settings.scorecard").status == dr.OK
        tools = by_id(report, "settings.tools")
        assert tools.status == dr.OFF and "get_filing_events, get_public_records" in tools.detail


@pytest.mark.unit
class TestReport:
    def test_an_optional_failure_does_not_block_but_a_required_one_does(self, tmp_path):
        optional = run(make_ctx(tmp_path, http=FakeHttp({"gamma-api.polymarket": 500})))
        assert optional.ready and optional.counts[dr.NOT_WORKING] >= 1
        assert not run(make_ctx(tmp_path, env={"ANTHROPIC_API_KEY": ""})).ready

    def test_the_text_puts_a_status_word_first_on_every_line_and_a_fix_under_a_problem(self, tmp_path):
        text = dr.format_text(run(make_ctx(tmp_path, env={"ANTHROPIC_API_KEY": "", "QUIVER_API_KEY": ""})))
        assert "READY TO RUN: no" in text and "(REQUIRED)" in text and "To fix: Add ANTHROPIC_API_KEY" in text
        body = [l for l in text.splitlines() if l.startswith("  [")]
        assert body and all(l[3:].split("]")[0] in dr.LABEL.values() for l in body)

    def test_the_json_carries_the_same_facts(self, tmp_path):
        data = json.loads(dr.format_json(run(make_ctx(tmp_path))))
        assert data["ready"] is True and set(data["counts"]) == set(dr.LABEL)
        first = data["results"][0]
        assert {"id", "group", "title", "status", "detail", "fix", "required"} <= set(first)

    def test_a_group_can_be_picked(self, tmp_path):
        report = run(make_ctx(tmp_path), groups=["settings"])
        assert {r.group for r in report.results} == {"settings"}


@pytest.mark.unit
class TestRealHttp:
    def _patch(self, monkeypatch, exc):
        import requests

        def get(*a, **k):
            raise exc
        monkeypatch.setattr(requests, "get", get)

    def test_errors_become_plain_types_that_never_contain_the_url_or_key(self, monkeypatch):
        import requests
        url = "https://api.example.test/q"
        secret = "api_key=SUPERSECRETVALUE"
        self._patch(monkeypatch, requests.exceptions.SSLError(f"bad cert for {url}?{secret}"))
        with pytest.raises(dr.CertificateProblem):
            dr.real_http_get(url, params={"api_key": "SUPERSECRETVALUE"})
        for exc, kind in ((requests.exceptions.ConnectTimeout(secret), "timed out"), (requests.exceptions.ConnectionError(secret), "no connection")):
            self._patch(monkeypatch, exc)
            with pytest.raises(dr.NetworkProblem) as caught:
                dr.real_http_get(url)
            assert kind in caught.value.kind and "SUPERSECRET" not in str(caught.value)


@pytest.mark.unit
class TestCommand:
    def _app(self):
        from cli.main import app
        return app

    def test_offline_json_run(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890abcdef")
        result = CliRunner().invoke(self._app(), ["doctor", "--offline", "--json"])
        data = json.loads(result.output)
        assert result.exit_code == (0 if data["ready"] else 1)
        assert data["skipped_some"] is True and "sk-test-1234567890abcdef" not in result.output

    def test_missing_keys_exit_with_one(self, monkeypatch):
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.setenv(name, "")
        result = CliRunner().invoke(self._app(), ["doctor", "--offline"])
        assert result.exit_code == 1 and "READY TO RUN: no" in result.output

    def test_an_unknown_group_is_refused(self):
        result = CliRunner().invoke(self._app(), ["doctor", "--offline", "--group", "nonsense"])
        assert result.exit_code == 2
