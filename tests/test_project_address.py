"""This project's web address is named in one place, and everything that uses it agrees.

The original project (TradingAgents by Tauric Research) is credited and linked in the
README, the notices, the report footer and the banners, but nothing here asks its servers
or sends users to its issue tracker.
"""

import re
import tomllib
from pathlib import Path

import pytest

from tradingagents import project
from tradingagents.dataflows.vendors import reddit, stocktwits

ROOT = Path(__file__).resolve().parent.parent
HOME = "https://github.com/NormanSisk/analyst-consortium"
UPSTREAM = "TauricResearch"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_address_is_built_from_one_owner_and_one_name():
    assert project.HOME_URL == HOME
    assert f"{HOME}/issues" == project.ISSUES_URL and f"{HOME}/discussions" == project.DISCUSSIONS_URL
    assert f"{HOME}/security/advisories/new" == project.SECURITY_REPORT_URL
    assert project.LATEST_RELEASE_API_URL == "https://api.github.com/repos/NormanSisk/analyst-consortium/releases/latest"
    assert f"https://github.com/{UPSTREAM}/TradingAgents" == project.UPSTREAM_URL


@pytest.mark.unit
def test_the_issue_links_name_this_project():
    config = read(".github/ISSUE_TEMPLATE/config.yml")
    assert project.DISCUSSIONS_URL in config and project.SECURITY_REPORT_URL in config
    assert project.ISSUES_URL in read(".github/ISSUE_TEMPLATE/bug-report.yml")
    assert f"{project.HOME_URL}/releases/download/" in read(".github/workflows/release-assets.yml")


@pytest.mark.unit
def test_the_readme_tells_people_to_clone_this_project():
    readme = read("README.md")
    assert f"git clone {project.HOME_URL}.git" in readme and f"cd {project.REPO}" in readme


@pytest.mark.unit
def test_the_tests_can_be_run_by_hand_from_the_actions_tab_as_well_as_on_a_push_to_main():
    ci = read(".github/workflows/ci.yml")
    assert "workflow_dispatch:" in ci and "branches: [main," in ci and "pull_request:" in ci
    # A failed test is repeated where people who are not signed in can read it.
    assert "if: failure()" in ci and "::error title=test failed::" in ci and "GITHUB_STEP_SUMMARY" in ci


@pytest.mark.unit
def test_nothing_under_github_points_at_the_original():
    for path in (ROOT / ".github").rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".yaml", ".md"}:
            assert UPSTREAM not in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


@pytest.mark.unit
def test_the_update_check_asks_this_project_and_nobody_else():
    import cli.updates as updates

    assert updates.RELEASES_URL == project.LATEST_RELEASE_API_URL
    assert not hasattr(updates, "NOTICES_URL")
    source = read("cli/updates.py")
    assert "tauric" not in source.lower()


@pytest.mark.unit
def test_no_program_file_contacts_the_originals_servers():
    # Credit links (banners, the report footer) are fine; a hostname of the original's is not.
    for folder in ("cli", "tradingagents"):
        for path in (ROOT / folder).rglob("*.py"):
            assert "tauric.ai" not in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


@pytest.mark.unit
def test_reddit_and_stocktwits_are_told_who_is_calling():
    for module in (reddit, stocktwits):
        assert module._UA == project.USER_AGENT
    assert project.HOME_URL in project.USER_AGENT and UPSTREAM not in project.USER_AGENT
    assert project.USER_AGENT.isascii() and "\n" not in project.USER_AGENT


@pytest.mark.unit
def test_the_command_line_banners_name_this_project_and_credit_the_original():
    welcome = read("cli/static/welcome.txt")
    assert "TradingAgents" not in welcome and len(welcome.splitlines()) <= 14      # the lettering is ours, and fits a terminal
    assert max(len(line) for line in welcome.splitlines()) <= 70
    for relative in ("cli/selections.py", "cli/display.py"):
        source = read(relative)
        assert "Welcome to Analyst Consortium" in source and "Welcome to TradingAgents" not in source
        assert "UPSTREAM_URL" in source and "Tauric Research (Apache-2.0)" in source
        assert "Built by [Tauric" not in source and "© [Tauric" not in source        # not claimed as the original's own work
    assert "Analyst Consortium" in read("cli/main.py")


@pytest.mark.unit
def test_the_package_description_and_links_name_this_project():
    meta = tomllib.loads(read("pyproject.toml"))["project"]
    assert meta["description"].startswith("Analyst Consortium")
    assert "Multi-Agents LLM Financial Trading Framework" not in meta["description"]
    assert meta["urls"]["Homepage"] == project.HOME_URL and meta["urls"]["Issues"] == project.ISSUES_URL
    assert meta["urls"]["Original project (TradingAgents)"] == project.UPSTREAM_URL


@pytest.mark.unit
def test_every_address_this_project_names_agrees_with_the_one_place():
    # Any github.com/NormanSisk/<name> link in the files people read must use the project's own name.
    pattern = re.compile(r"github\.com/NormanSisk/([A-Za-z0-9._-]+)")
    for relative in ("README.md", "CHANGES-FROM-UPSTREAM.md", "NOTICE", ".env.example", "pyproject.toml",
                     ".github/ISSUE_TEMPLATE/config.yml", ".github/ISSUE_TEMPLATE/bug-report.yml",
                     ".github/workflows/release-assets.yml"):
        for name in pattern.findall(read(relative)):
            assert name.removesuffix(".git") == project.REPO, (relative, name)
