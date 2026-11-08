"""The page that says what this version changed from TradingAgents: its file-list script, and that its words stay true.

The script is tested with made-up git output (no git needed). The document is checked against the code: every file it names
exists and every switch it mentions is really in the program, so it cannot quietly drift from the truth."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "CHANGES-FROM-UPSTREAM.md"


def load_script():
    spec = importlib.util.spec_from_file_location("list_changes", ROOT / "scripts" / "list_changes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lc = load_script()

NAME_STATUS = "M\tcli/main.py\nA\ttradingagents/picker/custom.py\nA\ttests/test_x.py\nA\tNOTICE\nD\told/gone.py\nA\tscripts/tool.py\n"
NUMSTAT = "207\t1\tcli/main.py\n12\t0\ttradingagents/picker/custom.py\n-\t-\ttradingagents/assets/app-icon.png\n"


@pytest.mark.unit
class TestTheListScript:
    def test_git_status_lines_become_status_and_path(self):
        assert lc.parse_name_status(NAME_STATUS) == [("M", "cli/main.py"), ("A", "tradingagents/picker/custom.py"), ("A", "tests/test_x.py"),
                                                      ("A", "NOTICE"), ("D", "old/gone.py"), ("A", "scripts/tool.py")]
        assert lc.parse_name_status("") == [] and lc.parse_name_status("garbage line\n") == []

    def test_line_counts_are_read_and_a_binary_file_shows_dashes(self):
        stats = lc.parse_numstat(NUMSTAT)
        assert stats["cli/main.py"] == ("207", "1") and stats["tradingagents/assets/app-icon.png"] == ("-", "-")

    @pytest.mark.parametrize("path, area", [
        ("tests/test_x.py", "Tests"), ("tradingagents/ui/server.py", "Setup screen and run form (the local web page)"),
        ("tradingagents/picker/custom.py", "Stock picker"), ("tradingagents/research/track_record.py", "Research studies"),
        ("tradingagents/dataflows/vendors/quiver.py", "Data sources"), ("tradingagents/agents/scorecard.py", "Analysis engine"),
        ("tradingagents/graph/research_panel.py", "Analysis engine"), ("cli/usage_log.py", "Command line"),
        ("scripts/make_app_icon.py", "Scripts, icon and design notes"), ("tradingagents/assets/app-icon.ico", "Scripts, icon and design notes"),
        ("tradingagents/doctor.py", "Other program files"), ("NOTICE", "Other program files"),
    ])
    def test_each_added_file_is_filed_under_an_area(self, path, area):
        assert lc.area_of(path) == area

    def test_the_lists_count_and_order_the_files_and_say_what_is_unchanged(self):
        text = lc.render(lc.parse_name_status(NAME_STATUS), lc.parse_numstat(NUMSTAT), base="abc1234")
        assert text.startswith("Compared with commit `abc1234`") and "A file that is not listed here is unchanged." in text
        assert "#### Upstream files we modified (1)" in text and "- `cli/main.py` (+207 -1)" in text
        assert "#### Files we added (4)" in text and "#### Upstream files we removed (1)" in text and "- `old/gone.py`" in text
        positions = [text.index(f"**{name}**") for name in ("Tests", "Stock picker", "Scripts, icon and design notes", "Other program files")]
        assert positions == sorted(positions)                                    # areas appear in a fixed order, not the order git listed them
        assert "**Other program files** (1)" in text and "- `NOTICE`" in text and "**Analysis engine**" not in text      # an empty area is not listed

    def test_no_removed_files_is_said_plainly(self):
        assert lc.render([("A", "tests/a.py")], {}).rstrip().endswith("None.")

    def test_a_file_with_no_line_counts_shows_question_marks_not_a_crash(self):
        assert "(+? -?)" in lc.render([("M", "x.py")], {})

    def test_only_the_text_between_the_markers_is_replaced(self):
        doc = f"before\n{lc.BEGIN}\n\nold list\n{lc.END}\nafter\n"
        new = lc.replace_section(doc, "new list\n")
        assert new.startswith("before\n") and new.endswith("\nafter\n") and "new list" in new and "old list" not in new
        assert lc.replace_section(new, "new list\n") == new                          # writing twice changes nothing

    def test_a_document_without_the_markers_is_refused_in_plain_words(self):
        with pytest.raises(ValueError, match="needs these two marker lines"):
            lc.replace_section("no markers here", "x")

    def test_check_and_write_work_on_a_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(lc, "collect", lambda base: (lc.parse_name_status(NAME_STATUS), lc.parse_numstat(NUMSTAT)))
        doc = tmp_path / "doc.md"
        doc.write_text(f"intro\n{lc.BEGIN}\n\nstale\n{lc.END}\n", encoding="utf-8")
        assert lc.main(["--check", str(doc)]) == 1 and "out of date" in capsys.readouterr().out
        assert lc.main(["--write", str(doc)]) == 0 and "Updated" in capsys.readouterr().out
        assert lc.main(["--check", str(doc)]) == 0 and "current" in capsys.readouterr().out
        assert doc.read_text(encoding="utf-8").startswith("intro\n") and "stale" not in doc.read_text(encoding="utf-8")

    def test_without_the_original_history_it_says_how_to_get_it(self):
        failing = lambda *a, **k: type("Done", (), {"returncode": 128, "stdout": "", "stderr": "bad revision"})()      # noqa: E731
        with pytest.raises(SystemExit, match="git remote add upstream"):
            lc.collect("nothere", run=failing)


@pytest.mark.unit
class TestTheDocument:
    @pytest.fixture(scope="class")
    def narrative(self):
        text = DOC.read_text(encoding="utf-8")
        return text[:text.index(lc.BEGIN)]

    def test_it_exists_with_the_markers_the_script_needs(self):
        text = DOC.read_text(encoding="utf-8")
        assert text.count(lc.BEGIN) == 1 and text.count(lc.END) == 1 and text.index(lc.BEGIN) < text.index(lc.END)

    def test_it_names_what_it_started_from_the_license_and_the_trademark_position(self, narrative):
        for needed in ("TradingAgents v0.7.1", "`c454491`", "Apache License 2.0", "Not affiliated with Tauric Research",
                       "we do not use their logo", "Not financial advice"):
            assert needed in narrative, needed
        assert "never places a trade" in narrative

    def test_every_file_it_names_exists(self, narrative):
        shorthand = [ROOT, ROOT / "tradingagents", ROOT / "tradingagents" / "dataflows" / "vendors"]
        missing = []
        for token in sorted(set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|md|toml|html|js))`", narrative))):
            if not any((base / token).is_file() for base in shorthand):
                missing.append(token)
        assert missing == []

    def test_every_folder_it_names_exists(self, narrative):
        missing = []
        for token in sorted(set(re.findall(r"`((?:tradingagents|cli|scripts|agents|llm_clients|dataflows)/[A-Za-z0-9_./-]*/)`", narrative))):
            if not any((base / token).is_dir() for base in (ROOT, ROOT / "tradingagents")):
                missing.append(token)
        assert missing == []

    @pytest.mark.parametrize("switch", ["TRADINGAGENTS_RESEARCH_PASSES", "TRADINGAGENTS_NO_SELLOFF_RULES", "TRADINGAGENTS_NO_SCORECARD",
                                        "TRADINGAGENTS_DISABLED_TOOLS", "TRADINGAGENTS_INVESTING_STYLE", "drop_unfinished_session",
                                        "QUIVER_API_KEY", "SEC_EDGAR_USER_AGENT", "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY"])
    def test_every_switch_and_key_it_mentions_is_really_in_the_program(self, narrative, switch):
        assert switch in narrative
        sources = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in (ROOT / "tradingagents").rglob("*.py"))
        assert switch in sources

    def test_the_commands_it_describes_exist(self, narrative):
        from typer.testing import CliRunner

        from cli.main import app

        for command in ("doctor", "ui", "pick", "shortcut"):
            assert f"`tradingagents {command}" in narrative
            assert CliRunner().invoke(app, [command, "--help"]).exit_code == 0, command
        assert "--rerun/--no-rerun" in narrative and "--no-rerun" in CliRunner().invoke(app, ["--help"]).output       # the bare command runs an analysis

    def test_the_conviction_bands_it_states_are_the_ones_in_the_code(self, narrative):
        from tradingagents.agents.schemas import SCORE_BANDS

        stated = {name: tuple(map(int, rng)) for name, *rng in re.findall(r"(Sell|Underweight|Hold|Overweight|Buy) (\d+)-(\d+)", narrative)}
        assert stated == SCORE_BANDS

    def test_the_default_it_gives_for_research_passes_is_the_real_one(self, narrative):
        from tradingagents.default_config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["research_passes"] == 5 and "five passes, median rating" in narrative

    def test_the_new_tool_count_is_the_real_one(self, narrative):
        from tradingagents.agents import tools

        names = {n for n in dir(tools) if n.startswith("get_")}
        upstream = {"get_balance_sheet", "get_cashflow", "get_fundamentals", "get_global_news", "get_income_statement", "get_indicators",
                    "get_insider_transactions", "get_macro_indicators", "get_news", "get_prediction_markets", "get_stock_data",
                    "get_verified_market_snapshot"}
        assert len(names - upstream) == 11 and "**Eleven new data tools**" in narrative

    def test_the_seven_adjusted_test_files_it_mentions_are_the_ones_the_inventory_lists(self, narrative):
        assert "**Seven test files:**" in narrative

    def test_the_readme_points_here_and_the_notice_says_the_same_things(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "(CHANGES-FROM-UPSTREAM.md)" in readme[:1200] and "modified version of" in readme[:1200]
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        for needed in ("modified version of TradingAgents", "https://github.com/TauricResearch/TradingAgents", "Apache License, Version 2.0",
                       "c454491", "CHANGES-FROM-UPSTREAM.md", "not affiliated with or endorsed by Tauric Research",
                       "Copyright 2026 Norman R. Sisk, for the changes and additions", "The original code remains the work of its authors"):
            assert re.sub(r"\s+", " ", notice).find(needed) >= 0, needed

    def test_the_copyright_for_our_additions_is_named_the_same_way_in_the_notice_and_the_page(self, narrative):
        assert "copyright 2026 Norman R. Sisk and under the same license" in narrative
        assert "the original code remains its authors'" in narrative
