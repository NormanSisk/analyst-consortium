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
    assert project.ISSUES_URL == f"{HOME}/issues" and project.DISCUSSIONS_URL == f"{HOME}/discussions"
    assert project.SECURITY_REPORT_URL == f"{HOME}/security/advisories/new"
    assert project.LATEST_RELEASE_API_URL == "https://api.github.com/repos/NormanSisk/analyst-consortium/releases/latest"
    assert project.UPSTREAM_URL == f"https://github.com/{UPSTREAM}/TradingAgents"


@pytest.mark.unit
def test_the_issue_links_name_this_project():
    config = read(".github/ISSUE_TEMPLATE/config.yml")
    assert project.DISCUSSIONS_URL in config and project.SECURITY_REPORT_URL in config
    assert project.ISSUES_URL in read(".github/ISSUE_TEMPLATE/bug-report.yml")
    assert f"{project.HOME_URL}/releases/download/" in read(".github/workflows/release-assets.yml")


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
            assert name == project.REPO, (relative, name)
