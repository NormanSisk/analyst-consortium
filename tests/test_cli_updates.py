"""What's new reaches the person at a terminal: a note when a newer release of this project exists.

The check runs at most daily in a background thread that only writes a cache; the
note shown is read from that cache, so a run never waits on the network, and
unattended runs contact no server at all. Only this project's GitHub releases are asked.
"""

import io
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

import cli.selections as cli_selections
import cli.updates as updates
from tradingagents import project


class _Tty(io.StringIO):
    def isatty(self):
        return True


# --- who sees it -----------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("stdin_tty, stdout_tty, ci, opt_out, expected", [
    (True, True, None, None, True),
    (False, True, None, None, False),
    (True, False, None, None, False),
    (True, True, "true", None, False),
    (True, True, None, "1", False),
])
def test_only_a_person_at_a_terminal_is_told(monkeypatch, stdin_tty, stdout_tty, ci, opt_out, expected):
    monkeypatch.setattr("sys.stdin", _Tty() if stdin_tty else io.StringIO())
    monkeypatch.setattr("sys.stdout", _Tty() if stdout_tty else io.StringIO())
    for name, value in (("CI", ci), ("TRADINGAGENTS_NO_UPDATE_CHECK", opt_out)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert updates.enabled() is expected


ENV = {
    "TRADINGAGENTS_LLM_PROVIDER": "openai",
    "TRADINGAGENTS_DEEP_THINK_LLM": "gpt-6-sol",
    "TRADINGAGENTS_QUICK_THINK_LLM": "gpt-6-luna",
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
}


@pytest.mark.unit
def test_an_unattended_run_contacts_no_server(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())      # no terminal
    calls = []
    monkeypatch.setattr(updates.requests, "get", lambda *a, **k: calls.append(a))
    started = []
    monkeypatch.setattr(updates.threading, "Thread", lambda *a, **k: started.append(1))
    with mock.patch.dict(os.environ, ENV, clear=False), \
         mock.patch.object(cli_selections, "ensure_api_key"):
        cli_selections.get_user_selections({"ticker": "AAPL", "date": "2026-09-01", "analysts": "market"})
    assert calls == [] and started == []


# --- what it says -----------------------------------------------------------------

LATEST = {"version": "1.2.0", "url": f"{project.HOME_URL}/releases/tag/v1.2.0",
          "summary": "A results viewer and a faster stock search."}


@pytest.mark.unit
@pytest.mark.parametrize("running, shown", [
    ("1.1.0", True), ("1.2.0.dev0", True),     # a development build of the release is older than it
    ("1.2.0", False), ("1.3.0.dev0", False), ("not-a-version", False),
])
def test_a_newer_release_is_announced_once_in_the_notice(running, shown):
    lines = updates.messages({"latest": LATEST}, running=running, upgrade=lambda: None)
    assert any("Analyst Consortium 1.2.0 is available" in line for line in lines) is shown
    if shown:
        text = "\n".join(lines)
        assert f"you have {running}" in text and LATEST["url"] in text and LATEST["summary"] in text


@pytest.mark.unit
def test_the_notice_names_the_upgrade_command():
    lines = updates.messages({"latest": LATEST}, running="1.1.0", upgrade=lambda: "git -C /src pull && pip install /src")
    assert "Upgrade: git -C /src pull && pip install /src" in "\n".join(lines)


@pytest.mark.unit
def test_remote_text_cannot_style_or_break_the_terminal():
    body = "[bold red]Grab it[/bold red] now\x1b[2J with `--html`, a very long line " + "x" * 400 + "\n\nSecond paragraph."
    summary = updates.summary_of(body)
    assert "\n" not in summary and "Second paragraph" not in summary and len(summary) <= 160
    lines = updates.messages({"latest": {**LATEST, "summary": summary}}, running="1.1.0", upgrade=lambda: None)
    shown = "\n".join(lines)
    assert "[bold red]Grab it" in shown and "\x1b" not in shown   # shown as plain text, controls stripped


@pytest.mark.unit
def test_remote_text_cannot_carry_c1_or_direction_controls():
    latest = {**LATEST, "summary": "A\x9b2Jb\u202eevil", "url": f"{LATEST['url']}\x9b2J\u200b"}
    lines = updates.messages({"latest": latest}, running="1.1.0", upgrade=lambda: None)
    assert lines[0].endswith("A 2Jbevil")            # the C1 control reads as a space, the override is dropped
    assert lines[1] == f"{LATEST['url']} 2J"


@pytest.mark.unit
def test_the_note_is_shown_in_a_panel_as_plain_text():
    console = Console(record=True, width=120, color_system=None)
    cache = {"latest": {**LATEST, "summary": "[b]bold[/b] :warning:"}}
    with mock.patch.object(updates, "read_cache", lambda: cache), mock.patch("tradingagents.__version__", "1.1.0"):
        updates.show(console)
    text = console.export_text()
    assert "What's new" in text and "[b]bold[/b] :warning:" in text and LATEST["url"] in text


@pytest.mark.unit
def test_notices_from_an_older_cache_are_dropped_and_never_shown(tmp_path, monkeypatch):
    # Before the fork the check also fetched notices from the original project's server.
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text(json.dumps({"checked_at": 1.0, "latest": LATEST,
                                      "notices": [{"id": "n", "message": "A notice from elsewhere"}]}))
    monkeypatch.setattr(updates, "CACHE", cache_file)
    assert updates.read_cache() == {"checked_at": 1.0, "latest": LATEST}
    out = Console(file=io.StringIO(), width=200)
    with mock.patch("tradingagents.__version__", "1.2.0"):
        updates.show(out)
    assert "elsewhere" not in out.file.getvalue()


# --- how it is checked ---------------------------------------------------------------

@pytest.mark.unit
def test_a_check_is_due_once_a_day():
    now = 1_000_000.0
    assert updates.due({}, now)
    assert not updates.due({"checked_at": now - 3600}, now)
    assert updates.due({"checked_at": now - 25 * 3600}, now)


def _response(payload, status=200):
    return SimpleNamespace(status_code=status, ok=status < 400, json=lambda: payload)


@pytest.mark.unit
def test_a_check_records_the_latest_release(tmp_path, monkeypatch):
    cache_file = tmp_path / "update_check.json"
    monkeypatch.setattr(updates, "CACHE", cache_file)

    def get(url, timeout):
        assert timeout <= 3
        return _response({"tag_name": "v1.2.0", "html_url": LATEST["url"],
                          "body": "A results viewer and a faster stock search.\n\n## Added"})

    monkeypatch.setattr(updates.requests, "get", get)
    updates.refresh(now=5000.0)
    cache = json.loads(cache_file.read_text())
    assert cache["checked_at"] == 5000.0
    assert cache["latest"] == {"version": "1.2.0", "url": LATEST["url"], "summary": LATEST["summary"]}
    assert "notices" not in cache


@pytest.mark.unit
def test_a_check_asks_only_this_projects_github_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "CACHE", tmp_path / "update_check.json")
    asked = []
    monkeypatch.setattr(updates.requests, "get", lambda url, timeout: asked.append(url) or _response({}, 404))
    updates.refresh(now=5000.0)
    assert asked == [f"https://api.github.com/repos/{project.OWNER}/{project.REPO}/releases/latest"]
    assert not any("tauric" in url.lower() for url in asked)


@pytest.mark.unit
def test_a_failed_check_keeps_what_was_known_and_waits_a_day(tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text(json.dumps({"checked_at": 1.0, "latest": LATEST}))
    monkeypatch.setattr(updates, "CACHE", cache_file)

    def get(url, timeout):
        raise updates.requests.ConnectionError("offline")

    monkeypatch.setattr(updates.requests, "get", get)
    updates.refresh(now=9000.0)                          # never raises
    cache = json.loads(cache_file.read_text())
    assert cache["checked_at"] == 9000.0 and cache["latest"] == LATEST
    assert capsys.readouterr() == ("", "")               # the check never prints


@pytest.mark.unit
def test_no_release_published_yet_is_no_news(tmp_path, monkeypatch):
    # GitHub answers 404 until the first release exists; that must not replace what was known.
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text(json.dumps({"checked_at": 1.0, "latest": LATEST}))
    monkeypatch.setattr(updates, "CACHE", cache_file)
    for answer in (_response({"message": "Not Found"}, 404), _response({"message": "no tag"}), _response(["x"])):
        monkeypatch.setattr(updates.requests, "get", lambda url, timeout, answer=answer: answer)
        updates.refresh(now=9000.0)
        assert json.loads(cache_file.read_text())["latest"] == LATEST


@pytest.mark.unit
def test_an_unreadable_cache_is_no_news(tmp_path, monkeypatch):
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text("{not json")
    monkeypatch.setattr(updates, "CACHE", cache_file)
    assert updates.read_cache() == {}


# --- the upgrade command for how it was installed ----------------------------------

def _dist(direct=None, installer="pip"):
    files = {"direct_url.json": json.dumps(direct) if direct else None, "INSTALLER": installer}
    return SimpleNamespace(read_text=lambda name: files.get(name))


GIT_URL = f"{project.HOME_URL}.git"


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="the upgrade commands are POSIX shell")
def test_the_upgrade_command_follows_the_install(tmp_path, monkeypatch):
    clone = tmp_path / "analyst-consortium"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.delenv("TRADINGAGENTS_IN_DOCKER", raising=False)
    monkeypatch.setattr(updates.sys, "executable", "/venv/bin/python")
    url = clone.as_uri()

    editable = updates.upgrade_command(_dist({"url": url, "dir_info": {"editable": True}}))
    assert editable == f"git -C {clone} pull && /venv/bin/python -m pip install -e {clone}"
    local = updates.upgrade_command(_dist({"url": url, "dir_info": {}}, installer="uv"))
    assert local == f"git -C {clone} pull && uv pip install --python /venv/bin/python {clone}"
    vcs = updates.upgrade_command(_dist({"url": GIT_URL, "vcs_info": {"vcs": "git", "commit_id": "abc"}}))
    assert vcs == f"/venv/bin/python -m pip install -U git+{GIT_URL}"


@pytest.mark.unit
def test_no_command_is_guessed_without_a_clone(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_IN_DOCKER", raising=False)
    gone = (tmp_path / "deleted-clone").as_uri()
    assert updates.upgrade_command(_dist({"url": gone, "dir_info": {}})) is None
    assert updates.upgrade_command(_dist(None)) is None
    monkeypatch.setenv("TRADINGAGENTS_IN_DOCKER", "1")
    assert updates.upgrade_command(_dist(None)) == "git pull && docker compose build (on the host)"


@pytest.mark.unit
def test_whats_new_never_waits_for_the_person(tmp_path, monkeypatch):
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text(json.dumps({"latest": LATEST}))
    monkeypatch.setattr(updates, "CACHE", cache_file)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("asked for input")))
    out = Console(file=io.StringIO(), width=200)
    with mock.patch("tradingagents.__version__", "1.1.0"):
        updates.show(out)
    assert "1.2.0 is available" in out.file.getvalue()


@pytest.mark.unit
@pytest.mark.parametrize("stored", [
    {"latest": "x"}, {"latest": ["0.9.0"]}, {"checked_at": "today"}, [],
])
def test_a_cache_of_the_wrong_shape_is_no_news(tmp_path, monkeypatch, stored):
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text(json.dumps(stored))
    monkeypatch.setattr(updates, "CACHE", cache_file)
    assert updates.messages(updates.read_cache(), running="1.1.0", upgrade=lambda: None) == []
    assert updates.due(updates.read_cache(), 1_000_000.0)


@pytest.mark.unit
def test_a_check_dated_in_the_future_is_due():
    assert updates.due({"checked_at": 2_000_000.0}, 1_000_000.0)


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="the upgrade commands are POSIX shell")
def test_a_uv_git_install_is_upgraded_with_uv(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_IN_DOCKER", raising=False)
    monkeypatch.setattr(updates.sys, "executable", "/venv/bin/python")
    vcs = updates.upgrade_command(_dist({"url": GIT_URL, "vcs_info": {"vcs": "git"}}, installer="uv"))
    assert vcs == f"uv pip install --python /venv/bin/python -U git+{GIT_URL}"


@pytest.mark.unit
def test_no_command_is_offered_on_windows(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_IN_DOCKER", raising=False)
    monkeypatch.setattr(updates.os, "name", "nt")
    assert updates.upgrade_command(_dist({"url": GIT_URL, "vcs_info": {"vcs": "git"}})) is None


@pytest.mark.unit
def test_the_upgrade_command_is_worked_out_only_for_a_newer_release():
    asked = []
    updates.messages({"latest": LATEST}, running=LATEST["version"], upgrade=lambda: asked.append(1))
    assert asked == []
