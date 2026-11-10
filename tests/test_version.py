"""The package version has one source, tradingagents.__version__, which the build reads."""

import re
from pathlib import Path

import pytest

import tradingagents

PYPROJECT = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_build_reads_the_version_from_the_package():
    assert re.search(r'^dynamic = \["version"\]', PYPROJECT, re.M)
    assert 'version = {attr = "tradingagents.__version__"}' in PYPROJECT
    assert not re.search(r'^version = "', PYPROJECT, re.M)
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+|rc\d+)?", tradingagents.__version__)


@pytest.mark.unit
def test_the_newest_release_notes_are_for_the_version_in_the_code():
    notes = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    newest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", notes, re.M)
    assert newest and newest.group(1) == tradingagents.__version__
    assert "Analyst Consortium" in notes.split("## [", 1)[0]          # the top of the file says whose notes these are
    assert notes.index("## [0.8.0]") < notes.index("## [0.7.1]")      # ours first, the original's history below


@pytest.mark.unit
def test_the_release_workflow_checks_the_tag_against_the_version_and_the_packaged_files():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-assets.yml").read_text(encoding="utf-8")
    assert 'if [ "v$version" != "$TAG" ]' in workflow
    for packaged in ("welcome.txt", "index.html", "analyst-consortium-emblem.svg"):
        assert packaged in workflow
        assert any(Path(__file__).resolve().parents[1].rglob(packaged))          # each is really in the repository
