"""The run form's engine: checking a request, the command and environment for a run, reading its progress and result
from the files it writes, and the one-at-a-time queue. No real analysis and no network: a fake child stands in."""

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from tradingagents.ui import runs as rn

TODAY = date(2026, 11, 8)
SUMMARY = "Hold existing exposure; do not add before the report."
DECISION = (
    "**Rating**: Hold\n\n**Executive Summary**: " + SUMMARY + "\n\n**Investment Thesis**: Because.\n\n"
    "**Price Target**: not provided\n\n**Conviction Score**: 48 of 100 (Hold band 41-59)"
)


def request(**changes):
    base = {"tickers": ["nvda"], "date": "2026-11-08", "analysts": ["news", "market"], "depth": 1, "passes": 5,
            "language": "English", "holdings": None, "selloff": True, "scorecard": True}
    base.update(changes)
    return base


def clean(raw, books=None):
    return rn.clean_request(raw, today=TODAY, holdings=books or {})


def write_run(results, ticker="NVDA", day="2026-11-08", decision=DECISION, rating="Hold", panel=True, report=True):
    """Leave behind what a finished run leaves: the state log, usage, per-step reports and a saved report folder."""
    logs = results / ticker / "TradingAgentsStrategy_logs"
    logs.mkdir(parents=True, exist_ok=True)
    state = {"final_trade_decision": decision, "final_rating": rating,
             "structured_fallbacks": {"Trader": {"calls": 1, "retries": 0, "fallbacks": 1}}}
    if panel:
        state["research_panel"] = {"passes": 5, "requested": 5, "ratings": ["Hold"] * 4 + ["Underweight"], "median": "Hold",
                                   "agreement": 0.8, "label": "strong", "range": ["Underweight", "Hold"],
                                   "summary": "5 independent passes: Hold 4, Underweight 1.", "extra": "dropped"}
    (logs / f"full_states_log_{day}.json").write_text(json.dumps(state), encoding="utf-8")
    run = results / ticker / day
    (run / "reports").mkdir(parents=True, exist_ok=True)
    (run / "usage.json").write_text(json.dumps({"estimated_cost_usd": 0.38, "seconds": 351, "llm_calls": 29}), encoding="utf-8")
    if report:
        (results / "reports" / f"{ticker}_20261108_101010").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------------------------- the request

@pytest.mark.unit
class TestCleanRequest:
    def test_a_good_request_is_normalized(self):
        out = clean(request(tickers=[" nvda ", "MU", "NVDA", "0700.hk"], analysts=["news", "market", "market"]))
        assert out["tickers"] == ["NVDA", "MU", "0700.HK"]                    # upper case, no repeats, order kept
        assert out["analysts"] == ["market", "news"]                          # the program's own order
        assert out["date"] == "2026-11-08" and out["depth"] == 1 and out["passes"] == 5

    @pytest.mark.parametrize("tickers", [[], [""], ["  "], "NVDA", None])
    def test_a_symbol_is_required(self, tickers):
        with pytest.raises(rn.RequestError, match="at least one stock symbol"):
            clean(request(tickers=tickers))

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "NV DA", "x" * 40, "AAPL;rm", "..\\x"])
    def test_a_symbol_that_could_leave_the_results_folder_is_refused(self, bad):
        with pytest.raises(rn.RequestError, match="not a symbol"):
            clean(request(tickers=[bad]))

    def test_too_many_symbols(self):
        with pytest.raises(rn.RequestError, match="Up to 10"):
            clean(request(tickers=[f"A{i}" for i in range(11)]))

    @pytest.mark.parametrize("day", ["", "yesterday", "2026-13-01", "2026-11-08T10:00", "11/08/2026"])
    def test_a_date_must_be_a_real_day(self, day):
        with pytest.raises(rn.RequestError, match="Pick a date"):
            clean(request(date=day))

    def test_the_future_is_refused_and_the_past_is_allowed(self):
        with pytest.raises(rn.RequestError, match="future"):
            clean(request(date="2026-11-09"))
        assert clean(request(date="2026-11-01"))["date"] == "2026-11-01"

    @pytest.mark.parametrize("analysts", [[], ["bogus"], ["market", "bogus"], "market", None])
    def test_analysts_must_be_known_and_at_least_one(self, analysts):
        with pytest.raises(rn.RequestError, match="analyst"):
            clean(request(analysts=analysts))

    @pytest.mark.parametrize("depth", [0, 2, 4, 6, True, "1", None])
    def test_depth_is_one_of_the_three(self, depth):
        with pytest.raises(rn.RequestError, match="Shallow, Medium or Deep"):
            clean(request(depth=depth))

    @pytest.mark.parametrize("passes", [0, 16, -1, True, "5", 5.0, None])
    def test_passes_is_a_whole_number_in_range(self, passes):
        with pytest.raises(rn.RequestError, match="whole number"):
            clean(request(passes=passes))

    @pytest.mark.parametrize("language", ["English\nTRADINGAGENTS_X=1", "a" * 41, "Eng;lish", "", "Turkish 2", "-x"])
    def test_a_language_cannot_carry_anything_but_a_name(self, language):
        if language == "":
            assert clean(request(language=language))["language"] == "English"      # empty means the default
            return
        with pytest.raises(rn.RequestError, match="language"):
            clean(request(language=language))

    @pytest.mark.parametrize("language", ["Turkish", "Brazilian Portuguese", "Türkçe", "Swiss-German", "中文"])
    def test_a_custom_language_name_is_allowed(self, language):
        assert clean(request(language=language))["language"] == language

    def test_a_holdings_file_must_be_one_that_was_offered(self, tmp_path):
        book = tmp_path / "my_book.json"
        assert clean(request(holdings="my_book.json"), {"my_book.json": book})["holdings"] == "my_book.json"
        for name in ("other.json", "../my_book.json", str(book)):
            with pytest.raises(rn.RequestError, match="not one of the files offered"):
                clean(request(holdings=name), {"my_book.json": book})
        assert clean(request(holdings=None))["holdings"] is None and clean(request(holdings=""))["holdings"] is None

    def test_the_crypto_suffixes_match_the_command_lines(self):
        prompts = pytest.importorskip("cli.prompts")
        assert tuple(rn.CRYPTO_SUFFIXES) == tuple(prompts.CRYPTO_SUFFIXES)


# ---------------------------------------------------------------------------------------------- the command

def make_job(**changes):
    values = dict(id="20261108-101010-abcd", ticker="NVDA", date="2026-11-08", analysts=["market", "news"], depth=3,
                  passes=7, language="Spanish", holdings=None, selloff=True, scorecard=True, provider="anthropic",
                  quick="claude-haiku-5-5", deep="claude-sonnet-5")
    values.update(changes)
    return rn.Job(**values)


@pytest.mark.unit
class TestCommandAndEnvironment:
    def test_the_command_answers_every_question_and_never_uses_a_shell(self):
        command = rn.build_command(make_job(), "python", None)
        assert isinstance(command, list) and command[0] == "python" and command[1] == "-c"
        assert command[3:] == ["--ticker", "NVDA", "--date", "2026-11-08", "--analysts", "market,news",
                               "--save", "--no-show", "--html", "--no-rerun"]

    def test_a_holdings_file_is_passed_as_its_own_argument(self, tmp_path):
        book = tmp_path / "my book.json"
        command = rn.build_command(make_job(holdings="my book.json"), "python", book)
        assert command[-2:] == ["--portfolio", str(book)]

    def test_the_environment_carries_the_form_choices(self):
        env = rn.build_env(make_job(), {"KEEP": "yes", "TRADINGAGENTS_NO_SELLOFF_RULES": "1", "TRADINGAGENTS_NO_SCORECARD": "1"})
        assert env["KEEP"] == "yes"
        assert env["TRADINGAGENTS_OUTPUT_LANGUAGE"] == "Spanish"
        assert env["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] == env["TRADINGAGENTS_MAX_RISK_ROUNDS"] == "3"
        assert env["TRADINGAGENTS_RESEARCH_PASSES"] == "7"
        assert (env["TRADINGAGENTS_LLM_PROVIDER"], env["TRADINGAGENTS_QUICK_THINK_LLM"], env["TRADINGAGENTS_DEEP_THINK_LLM"]) == \
            ("anthropic", "claude-haiku-5-5", "claude-sonnet-5")
        assert env["PYTHONUTF8"] == "1"
        assert "TRADINGAGENTS_NO_SELLOFF_RULES" not in env and "TRADINGAGENTS_NO_SCORECARD" not in env   # switched on

    def test_switching_a_feature_off_sets_its_variable(self):
        env = rn.build_env(make_job(selloff=False, scorecard=False), {})
        assert env["TRADINGAGENTS_NO_SELLOFF_RULES"] == "1" and env["TRADINGAGENTS_NO_SCORECARD"] == "1"

    def test_the_callers_environment_is_not_changed(self):
        base = {"A": "1"}
        rn.build_env(make_job(), base)
        assert base == {"A": "1"}

    def test_the_unattended_command_line_would_ask_nothing(self):
        """Every variable the command line demands when no terminal is attached is in the environment built here."""
        selections = pytest.importorskip("cli.selections")
        command = rn.build_command(make_job(), "python", None)
        flags = {"ticker": command[command.index("--ticker") + 1], "date": "d", "analysts": "a", "save": True, "show": False}
        saved = {k: v for k, v in os.environ.items()}
        try:
            os.environ.update(rn.build_env(make_job(), {}))
            assert selections.unattended_gaps(flags) == []
        finally:
            for name in list(os.environ):
                if name.startswith("TRADINGAGENTS_") and name not in saved:
                    del os.environ[name]
            os.environ.update(saved)


@pytest.mark.unit
class TestEstimate:
    def test_the_measured_pair_has_a_price(self):
        one, five = (rn.estimate("anthropic", "claude-haiku-5-5", "claude-sonnet-5", n) for n in (1, 5))
        assert one["usd"] == 0.13 and one["minutes"] == 5 and one["measured"]
        assert five["usd"] == pytest.approx(0.4) and five["minutes"] == 6

    def test_other_models_have_no_invented_price(self):
        for provider, quick, deep in (("openai", "gpt-x", "gpt-y"), ("anthropic", "claude-opus-5", "claude-opus-5")):
            out = rn.estimate(provider, quick, deep, 5)
            assert out["usd"] is None and not out["measured"] and out["minutes"] == 6


# ---------------------------------------------------------------------------------------------- reading a run

@pytest.mark.unit
class TestProgress:
    def setup_method(self):
        self.job = make_job(analysts=["market", "news"])
        self.job.started = time.time() - 5

    def states(self, tmp_path):
        out = rn.read_progress(tmp_path, self.job, time.time())
        return [s["state"] for s in out["stages"]], out

    def put(self, tmp_path, name, text="x"):
        path = tmp_path / "reports" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_nothing_written_yet_means_the_first_step_is_under_way(self, tmp_path):
        states, out = self.states(tmp_path)
        assert states == ["now", "waiting", "waiting", "waiting", "waiting"] and out["step"] == 1 and out["steps"] == 5
        assert out["latest"] is None

    def test_reports_move_it_along(self, tmp_path):
        self.put(tmp_path, "market_report.md")
        states, out = self.states(tmp_path)
        assert states[0] == "now" and out["stages"][0]["note"] == "1 of 2 reports filed"
        self.put(tmp_path, "news_report.md")
        states, _ = self.states(tmp_path)
        assert states == ["done", "now", "waiting", "waiting", "waiting"]
        self.put(tmp_path, "investment_plan.md", "### Bull\n...\n### Research Manager Decision\nHold")
        self.put(tmp_path, "trader_investment_plan.md")
        states, _ = self.states(tmp_path)
        assert states == ["done", "done", "done", "now", "waiting"]
        self.put(tmp_path, "final_trade_decision.md", "### Aggressive Analyst Analysis\n...\n### Portfolio Manager Decision\n...")
        assert self.states(tmp_path)[0] == ["done", "done", "done", "done", "now"]

    def test_a_partly_written_debate_is_not_a_finished_one(self, tmp_path):
        self.put(tmp_path, "investment_plan.md", "### Bull Researcher Analysis\n...")
        assert self.states(tmp_path)[0][1] != "done"

    def test_a_later_step_finishing_closes_the_earlier_ones(self, tmp_path):
        self.put(tmp_path, "trader_investment_plan.md")        # the analysts' files are missing, the trader's is not
        assert self.states(tmp_path)[0][:3] == ["done", "done", "done"]

    def test_files_left_by_an_earlier_run_the_same_day_are_ignored(self, tmp_path):
        old = self.put(tmp_path, "market_report.md")
        os.utime(old, (self.job.started - 600, self.job.started - 600))
        states, out = self.states(tmp_path)
        assert states[0] == "now" and out["stages"][0]["note"] == "0 of 2 reports filed"

    def test_finished_marks_everything_done(self, tmp_path):
        out = rn.read_progress(tmp_path, self.job, time.time(), finished=True)
        assert [s["state"] for s in out["stages"]] == ["done"] * 5 and out["step"] == 5

    def test_the_latest_activity_line_hides_secrets_and_says_how_old_it_is(self, tmp_path):
        log = tmp_path / "message_tool.log"
        log.write_text("12:00 [Tool Call] get_news(ticker=NVDA)\n12:01 [Tool Call] fetch(api_key=SECRETVALUE123)\n", encoding="utf-8")
        out = rn.read_progress(tmp_path, self.job, time.time())
        assert "SECRETVALUE123" not in json.dumps(out) and "fetch" in out["latest"]["text"]
        assert out["latest"]["seconds_ago"] >= 0


@pytest.mark.unit
class TestResult:
    def test_a_finished_run_is_read_back(self, tmp_path):
        since = time.time() - 5
        write_run(tmp_path)
        out = rn.read_result(tmp_path, "NVDA", "2026-11-08", since)
        assert out["rating"] == "Hold" and out["score"] == 48 and out["review"] is False
        assert out["summary"] == SUMMARY
        assert out["borderline"] is False and "Not borderline" in out["borderline_text"]
        assert out["panel"]["ratings"][-1] == "Underweight" and out["panel"]["label"] == "strong" and "extra" not in out["panel"]
        assert out["cost_usd"] == 0.38 and out["seconds"] == 351 and out["llm_calls"] == 29
        assert out["fallbacks"] == 1
        assert out["report_dir"] and out["report_dir"].endswith("NVDA_20261108_101010")

    def test_a_score_near_a_line_is_flagged_borderline(self, tmp_path):
        write_run(tmp_path, decision=DECISION.replace("48 of 100 (Hold band 41-59)", "42 of 100 (Hold band 41-59)"))
        out = rn.read_result(tmp_path, "NVDA", "2026-11-08", time.time() - 5)
        assert out["borderline"] is True and "Borderline call" in out["borderline_text"]

    def test_a_run_without_a_panel_or_report_folder(self, tmp_path):
        write_run(tmp_path, panel=False, report=False)
        out = rn.read_result(tmp_path, "NVDA", "2026-11-08", time.time() - 5)
        assert out["panel"] is None and out["report_dir"] is None

    def test_an_unreadable_decision_is_marked_for_review_not_given_a_rating(self, tmp_path):
        write_run(tmp_path, decision="No call could be made.", rating="")
        out = rn.read_result(tmp_path, "NVDA", "2026-11-08", time.time() - 5)
        assert out["review"] is True and out["borderline"] is None and out["score"] is None

    def test_an_earlier_runs_log_is_not_this_runs_result(self, tmp_path):
        write_run(tmp_path)
        assert rn.read_result(tmp_path, "NVDA", "2026-11-08", time.time() + 60) is None

    def test_missing_or_broken_files_give_none(self, tmp_path):
        assert rn.read_result(tmp_path, "NVDA", "2026-11-08", 0) is None
        write_run(tmp_path)
        (tmp_path / "NVDA" / "TradingAgentsStrategy_logs" / "full_states_log_2026-11-08.json").write_text("{broken", encoding="utf-8")
        assert rn.read_result(tmp_path, "NVDA", "2026-11-08", 0) is None

    def test_the_console_tail_hides_keys(self, tmp_path):
        log = tmp_path / "console.txt"
        log.write_text("\n".join(["line"] * 20 + ["Traceback", "Error with sk-ant-ABCDEFGHIJKLMNOP1234 inside"]), encoding="utf-8")
        tail = rn.console_tail(log)
        assert "sk-ant-ABCDEFGH" not in tail and "[hidden]" in tail and tail.count("\n") == 5
        assert rn.console_tail(tmp_path / "missing.txt") == ""


@pytest.mark.unit
class TestFindHoldings:
    def test_only_files_that_read_as_a_book_are_offered(self, tmp_path):
        (tmp_path / "my_book.json").write_text(json.dumps(
            {"cash": 100.0, "currency": "USD", "positions": [{"ticker": "NVDA", "quantity": 3}], "profile": "long term"}), encoding="utf-8")
        (tmp_path / "random.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")
        (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
        (tmp_path / "broken.json").write_text("{nope", encoding="utf-8")
        (tmp_path / "bad_book.json").write_text(json.dumps({"positions": [{"ticker": "X"}]}), encoding="utf-8")
        found = rn.find_holdings([tmp_path, tmp_path / "missing"])
        assert list(found) == ["my_book.json"]
        book = found["my_book.json"]
        assert (book["positions"], book["cash"], book["currency"], book["profile"]) == (1, 100.0, "USD", True)
        assert book["path"] == tmp_path / "my_book.json"


# ---------------------------------------------------------------------------------------------- the queue

class FakeProc:
    def __init__(self, code=0, hold=False):
        self.pid, self.returncode, self.code, self.hold, self.killed = 4242, None, code, hold, False

    def poll(self):
        if self.killed:
            self.returncode = 1
            return 1
        if self.hold:
            return None
        self.returncode = self.code
        return self.code


class Rig:
    """A manager whose children are fakes: each launch is recorded, and a hook decides what the 'run' leaves behind."""

    def __init__(self, tmp_path, books=None, behave=None):
        self.results = tmp_path / "logs"
        self.books = dict(books or {})
        self.launches, self.procs = [], []
        self.behave = behave or (lambda job_args, rig: FakeProc(0))
        self.manager = rn.RunManager(
            self.results, holdings=lambda: dict(self.books), launcher=self.launch, python="python", cwd=None,
            base_env=lambda: {"BASE": "1"}, sleep=lambda s: time.sleep(0.005), poll_seconds=0.005,
            killer=lambda proc: setattr(proc, "killed", True))

    def launch(self, command, env, cwd, console):
        args = {"command": command, "env": env, "console": console}
        self.launches.append(args)
        proc = self.behave(args, self)
        self.procs.append(proc)
        return proc

    def submit(self, **changes):
        return self.manager.submit(clean(request(**changes), self.books), "anthropic", "claude-haiku-5-5", "claude-sonnet-5")

    def status(self, job):
        return self.manager.get(job.id)["status"]

    def wait(self, check, seconds=5.0):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.01)
        raise AssertionError("timed out waiting for the queue")


def succeeding(args, rig):
    command = args["command"]
    ticker = command[command.index("--ticker") + 1]
    day = command[command.index("--date") + 1]
    write_run(rig.results, ticker=ticker, day=day)
    return FakeProc(0)


@pytest.fixture
def rig(tmp_path):
    made = []

    def build(**kw):
        r = Rig(tmp_path, **kw)
        made.append(r)
        return r

    yield build
    for r in made:
        r.manager.shutdown()


@pytest.mark.unit
class TestQueue:
    def test_symbols_run_one_after_another_and_finish_with_a_result(self, rig):
        r = rig(behave=succeeding)
        first, second = r.submit(tickers=["NVDA", "MU"])
        r.wait(lambda: r.status(second) == "done")
        assert [c["command"][c["command"].index("--ticker") + 1] for c in r.launches] == ["NVDA", "MU"]
        got = r.manager.get(first.id)
        assert got["status"] == "done" and got["result"]["rating"] == "Hold" and got["error"] == ""
        assert got["elapsed"] is not None and got["expected_minutes"] == 6
        assert r.launches[0]["env"]["BASE"] == "1" and r.launches[0]["env"]["TRADINGAGENTS_RESEARCH_PASSES"] == "5"

    def test_a_second_run_does_not_start_while_the_first_is_running(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        first, second = r.submit(tickers=["NVDA", "MU"])
        r.wait(lambda: r.status(first) == "running")
        time.sleep(0.1)
        assert len(r.launches) == 1 and r.status(second) == "waiting"
        listing = {j["id"]: j for j in r.manager.listing()}
        assert listing[second.id]["place"] == 1 and listing[first.id]["place"] is None
        assert listing[first.id]["progress"]["steps"] == 5          # a running job reports its steps
        assert listing[second.id]["progress"] is None

    def test_the_listing_is_newest_first(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        first, second = r.submit(tickers=["NVDA", "MU"])
        assert [j["ticker"] for j in r.manager.listing()] == ["MU", "NVDA"]

    def test_a_run_that_fails_reports_why_and_the_queue_goes_on(self, rig):
        def behave(args, rig_):
            ticker = args["command"][args["command"].index("--ticker") + 1]
            if ticker == "BAD":
                args["console"].write_text("Traceback\nValueError: no data for sk-ant-ABCDEFGHIJKLMNOP1234\n", encoding="utf-8")
                return FakeProc(1)
            return succeeding(args, rig_)

        r = rig(behave=behave)
        bad, good = r.submit(tickers=["BAD", "NVDA"])
        r.wait(lambda: r.status(good) == "done")
        got = r.manager.get(bad.id)
        assert got["status"] == "failed" and "exit code 1" in got["error"] and got["result"] is None
        assert "no data for" in got["detail"] and "sk-ant-ABCDEFGH" not in got["detail"]

    def test_a_clean_exit_with_no_decision_is_a_failure(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0))
        job, = r.submit()
        r.wait(lambda: r.status(job) == "failed")
        assert "without writing a decision" in r.manager.get(job.id)["error"]

    def test_stopping_a_waiting_analysis_means_it_never_starts(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        first, second = r.submit(tickers=["NVDA", "MU"])
        r.wait(lambda: r.status(first) == "running")
        assert r.manager.cancel(second.id) is True
        assert r.status(second) == "stopped" and "before it started" in r.manager.get(second.id)["error"]
        assert r.manager.cancel(second.id) is False                  # already over
        r.manager.cancel(first.id)
        r.wait(lambda: r.status(first) == "stopped")
        assert len(r.launches) == 1

    def test_stopping_a_running_analysis_ends_its_process(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        job, = r.submit()
        r.wait(lambda: r.status(job) == "running")
        assert r.manager.cancel(job.id) is True
        r.wait(lambda: r.status(job) == "stopped")
        assert r.procs[0].killed and r.manager.get(job.id)["error"] == "Stopped by you."

    def test_an_unknown_job_cannot_be_stopped(self, rig):
        r = rig()
        assert r.manager.cancel("20261108-101010-abcd") is False

    def test_the_same_symbol_and_day_cannot_be_queued_twice(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        r.submit(tickers=["NVDA"])
        with pytest.raises(rn.RequestError, match="already waiting or running"):
            r.submit(tickers=["MU", "NVDA"])
        assert len(r.manager.listing()) == 1                          # and nothing from the refused request was queued
        r.submit(tickers=["NVDA"], date="2026-11-07")                 # another day is a different analysis

    def test_the_queue_has_a_limit(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        for i in range(3):
            r.submit(tickers=[f"A{i}{j}" for j in range(10)])
        with pytest.raises(rn.RequestError, match="Too many"):
            r.submit(tickers=["ZZZ"])

    def test_crypto_loses_the_fundamentals_analyst_with_a_note(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        stock, coin = r.submit(tickers=["NVDA", "BTC-USD"], analysts=["market", "fundamentals"])
        assert stock.analysts == ["market", "fundamentals"] and stock.notes == []
        assert coin.analysts == ["market"] and "fundamentals analyst was left out" in coin.notes[0]

    def test_a_crypto_request_with_nothing_left_queues_nothing(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        with pytest.raises(rn.RequestError, match="cryptocurrency"):
            r.submit(tickers=["NVDA", "BTC-USD"], analysts=["fundamentals"])
        assert r.manager.listing() == []

    def test_a_launch_that_raises_fails_that_run_only(self, rig):
        calls = []

        def behave(args, rig_):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("no such program")
            return succeeding(args, rig_)

        r = rig(behave=behave)
        a, b = r.submit(tickers=["NVDA", "MU"])
        r.wait(lambda: r.status(b) == "done")
        assert r.status(a) == "failed" and "Could not start" in r.manager.get(a.id)["error"]

    def test_a_holdings_file_goes_to_the_command_and_a_vanished_one_fails_the_run(self, rig, tmp_path):
        book = tmp_path / "my_book.json"
        r = rig(books={"my_book.json": book}, behave=succeeding)
        job, = r.submit(holdings="my_book.json")
        r.wait(lambda: r.status(job) == "done")
        assert r.launches[0]["command"][-2:] == ["--portfolio", str(book)]
        gone = rig(books={}, behave=succeeding)
        gone.manager._holdings = lambda: {}
        request_ = clean(request(holdings="my_book.json"), {"my_book.json": book})
        job, = gone.manager.submit(request_, "anthropic", "claude-haiku-5-5", "claude-sonnet-5")
        gone.wait(lambda: gone.status(job) == "failed")
        assert "no longer there" in gone.manager.get(job.id)["error"] and gone.launches == []

    def test_history_survives_a_restart_and_an_unfinished_run_is_marked_stopped(self, rig, tmp_path):
        r = rig(behave=succeeding)
        done, = r.submit(tickers=["NVDA"])
        r.wait(lambda: r.status(done) == "done")
        hold = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        hold.results = r.results
        pending, = hold.submit(tickers=["MU"])
        hold.wait(lambda: hold.status(pending) == "running")
        again = rn.RunManager(r.results, launcher=lambda *a: FakeProc(0, hold=True), start_worker=False)
        by_id = {j["id"]: j for j in again.listing()}
        assert by_id[done.id]["status"] == "done" and by_id[done.id]["result"]["rating"] == "Hold"
        assert by_id[pending.id]["status"] == "stopped" and "closed before" in by_id[pending.id]["error"]

    def test_only_the_newest_kept_runs_are_loaded(self, tmp_path):
        for i in range(5):
            folder = tmp_path / "ui-runs" / f"20261108-10101{i}-abc{i}"
            folder.mkdir(parents=True)
            job = make_job(id=folder.name, status="done")
            (folder / "job.json").write_text(json.dumps(job.to_dict()), encoding="utf-8")
        (tmp_path / "ui-runs" / "not-a-job").mkdir()
        (tmp_path / "ui-runs" / "20261108-101019-abcd").mkdir()           # a folder with no job.json
        manager = rn.RunManager(tmp_path, keep=3, start_worker=False)
        assert [j["id"] for j in manager.listing()] == ["20261108-101014-abc4", "20261108-101013-abc3", "20261108-101012-abc2"][:len(manager.listing())]

    def test_shutdown_ends_a_running_analysis(self, rig):
        r = rig(behave=lambda a, rig_: FakeProc(0, hold=True))
        job, = r.submit()
        r.wait(lambda: r.status(job) == "running")
        r.manager.shutdown()
        assert r.procs[0].killed
        assert r.manager.get(job.id)["status"] == "stopped"

    def test_a_job_file_is_json_a_person_could_read(self, rig):
        r = rig(behave=succeeding)
        job, = r.submit()
        r.wait(lambda: r.status(job) == "done")
        saved = json.loads((r.results / "ui-runs" / job.id / "job.json").read_text(encoding="utf-8"))
        assert saved["ticker"] == "NVDA" and saved["status"] == "done" and saved["result"]["rating"] == "Hold"
        assert "cancel_requested" not in saved


@pytest.mark.unit
class TestRealProcess:
    """One real child, to prove the pieces that fakes cannot: no terminal, output to a file, and ending it."""

    def test_a_child_gets_no_terminal_and_writes_to_the_console_file(self, tmp_path):
        import sys

        console = tmp_path / "console.txt"
        code = "import sys; print('tty', sys.stdin.isatty() if sys.stdin else 'none'); print('hello')"
        proc = rn.launch([sys.executable, "-c", code], dict(os.environ), None, console)
        assert proc.wait(timeout=60) == 0
        text = console.read_text(encoding="utf-8")
        assert "hello" in text and "tty False" in text           # a closed pipe, not the null device, which says True on Windows

    def test_kill_tree_ends_a_running_child(self, tmp_path):
        import sys

        proc = rn.launch([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), None, tmp_path / "c.txt")
        assert proc.poll() is None
        rn.kill_tree(proc)
        assert proc.wait(timeout=30) is not None
        rn.kill_tree(proc)                                     # a second call on a finished child is harmless
