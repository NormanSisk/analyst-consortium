"""Reading a list of stocks, and the `tradingagents pick` command, with a stand-in data provider (no network)."""

import csv
import io
import json

import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from tests import picker_world as w
from tradingagents.picker import lists


@pytest.mark.unit
class TestLists:
    def test_clean_symbol(self):
        assert lists.clean_symbol("nvda") == "NVDA" and lists.clean_symbol(" 'amd' ") == "AMD"
        assert lists.clean_symbol("") is None and lists.clean_symbol(None) is None and lists.clean_symbol("../x") is None

    @pytest.mark.parametrize("text, expected", [
        ("NVDA\nmu\n\nTSM\nNVDA", ["NVDA", "MU", "TSM"]),
        ("NVDA, MU, TSM", ["NVDA", "MU", "TSM"]),
        ("NVDA MU  TSM", ["NVDA", "MU", "TSM"]),
        ("Symbol,Name,Exchange\nNVDA,Nvidia,NASDAQ\nMU,Micron,NASDAQ", ["NVDA", "MU"]),
        ("Ticker;Price\nAAPL;1\nMSFT;2", ["AAPL", "MSFT"]),
        ("symbol\tqty\nAAPL\t5\nMSFT\t6", ["AAPL", "MSFT"]),
        ("NVDA,Nvidia Corp\nMU,Micron Technology", ["NVDA", "MU"]),
        ("﻿NVDA\nMU", ["NVDA", "MU"]),
        ("", []), ("   \n\n", []),
    ])
    def test_the_shapes_a_file_can_take(self, text, expected):
        assert lists.parse_symbols(text)[0] == expected

    def test_what_cannot_be_a_symbol_is_skipped_and_reported_not_guessed(self):
        symbols, skipped = lists.parse_symbols("NVDA ../x MU")
        assert symbols == ["NVDA", "MU"] and skipped == ["../x"]

    def test_files(self, tmp_path):
        good = tmp_path / "watch.csv"
        good.write_bytes("﻿Symbol,Name\nNVDA,Nvidia\nMU,Micron\n".encode("utf-8"))
        assert lists.read_symbols_file(good) == (["NVDA", "MU"], [])
        with pytest.raises(ValueError, match="could not read"):
            lists.read_symbols_file(tmp_path / "missing.csv")
        big = tmp_path / "big.txt"
        big.write_bytes(b"A\n" * 600_000)
        with pytest.raises(ValueError, match="larger than 1 MB"):
            lists.read_symbols_file(big)


@pytest.fixture
def pick(monkeypatch, tmp_path):
    provider = w.pullback_world()
    monkeypatch.setattr("tradingagents.picker.providers.YahooProvider", lambda: provider)
    monkeypatch.setattr(cli_main, "_pick_folder", lambda: tmp_path / "picker")

    def invoke(*args):
        return CliRunner().invoke(cli_main.app, ["pick", *args])

    return invoke, provider, tmp_path


@pytest.mark.unit
class TestPickCommand:
    def test_the_help_names_both_recipes(self):
        out = CliRunner().invoke(cli_main.app, ["pick", "--help"]).output
        assert "beaten-down" in out and "list" in out

    def test_beaten_down_prints_the_answer_and_messages_go_to_the_other_stream(self, pick):
        invoke, _, tmp_path = pick
        result = invoke("beaten-down", "--no-log")
        assert result.exit_code == 0
        assert "BEATEN-DOWN QUALITY" in result.stdout and "GOOD" in result.stdout and "5 of 5" in result.stdout
        assert "Screening the market" in result.stderr and "Screening the market" not in result.stdout
        assert not (tmp_path / "picker" / "picks.csv").exists()

    def test_the_list_is_recorded_unless_told_not_to(self, pick):
        invoke, _, tmp_path = pick
        result = invoke("beaten-down")
        assert result.exit_code == 0 and "Recorded the top 2" in result.stderr
        rows = list(csv.DictReader(open(tmp_path / "picker" / "picks.csv", newline="", encoding="utf-8")))
        assert [r["symbol"] for r in rows] == ["GOOD", "BAD"] and rows[0]["recipe"] == "beaten-down"

    def test_todays_downloads_are_saved_for_the_next_run(self, pick):
        invoke, provider, tmp_path = pick
        invoke("beaten-down", "--no-log")
        assert list((tmp_path / "picker" / "cache").rglob("GOOD.json"))
        invoke("beaten-down", "--no-log")
        assert provider.fundamentals_calls.count("GOOD") == 1                      # the second run read it from disk

    def test_csv_and_json_outputs(self, pick):
        invoke, _, tmp_path = pick
        target = tmp_path / "all.csv"
        result = invoke("beaten-down", "--no-log", "--csv", str(target))
        assert result.exit_code == 0 and "Saved all 2 stocks" in result.stderr
        rows = list(csv.DictReader(io.StringIO(target.read_bytes().decode("utf-8").lstrip("﻿"), newline="")))
        assert [r["symbol"] for r in rows] == ["GOOD", "BAD"]
        data = json.loads(invoke("beaten-down", "--no-log", "--json").stdout)
        assert data["recipe"] == "beaten-down" and data["rows"][0]["symbol"] == "GOOD"

    def test_top_and_details_limit_the_text(self, pick):
        invoke, _, _ = pick
        out = invoke("beaten-down", "--no-log", "--top", "1", "--details", "1").stdout
        assert "THE TOP 1" in out and "BAD" not in out

    def test_an_empty_answer_is_not_an_error_and_is_not_recorded(self, pick):
        invoke, _, tmp_path = pick
        result = invoke("beaten-down", "--below-high", "0.9")
        assert result.exit_code == 0 and "No stocks to show" in result.stdout
        assert not (tmp_path / "picker" / "picks.csv").exists()

    def test_a_setting_out_of_range_is_refused_in_plain_words(self, pick):
        invoke, provider, _ = pick
        result = invoke("beaten-down", "--below-high", "5")
        assert result.exit_code == 1 and "Error:" in result.stderr and "below_high" in result.stderr
        assert provider.closes_calls == []

    def test_a_data_problem_ends_with_a_message_and_exit_code_1(self, pick):
        invoke, provider, _ = pick
        del provider._closes["^GSPC"]
        result = invoke("beaten-down", "--no-log")
        assert result.exit_code == 1 and "could not get S&P 500 prices" in result.stderr

    def test_a_named_list(self, pick):
        invoke, _, _ = pick
        result = invoke("list", "good", "FLAT", "--no-log")
        assert result.exit_code == 0 and "YOUR LIST" in result.stdout and "GOOD" in result.stdout and "FLAT" in result.stdout

    def test_a_list_from_a_file_with_skipped_entries(self, pick, tmp_path):
        invoke, _, _ = pick
        path = tmp_path / "mine.csv"
        path.write_text("symbol\nGOOD\n../evil\nBAD\n", encoding="utf-8")
        result = invoke("list", "--file", str(path), "--no-log")
        assert result.exit_code == 0 and "Skipped (not usable as symbols): ../evil" in result.stderr and "BAD" in result.stdout

    def test_typed_symbols_and_a_file_are_combined_without_repeats(self, pick, tmp_path):
        invoke, provider, _ = pick
        path = tmp_path / "mine.txt"
        path.write_text("GOOD\nBAD\n", encoding="utf-8")
        result = invoke("list", "GOOD", "--file", str(path), "--no-log", "--json")
        assert [r["symbol"] for r in json.loads(result.stdout)["rows"]].count("GOOD") == 1

    def test_list_errors(self, pick, tmp_path):
        invoke, _, _ = pick
        none = invoke("list")
        assert none.exit_code == 1 and "name at least one stock" in none.stderr
        missing = invoke("list", "--file", str(tmp_path / "nope.csv"))
        assert missing.exit_code == 1 and "could not read" in missing.stderr
        unusable = invoke("list", "../x")
        assert unusable.exit_code == 1 and "Skipped" in unusable.stderr

    def test_the_picker_makes_no_ai_calls_and_opens_no_windows(self, pick, monkeypatch):
        """The suite blocks sockets and browser windows; a run that completes proves neither was needed."""
        invoke, _, _ = pick
        assert invoke("beaten-down", "--no-log").exit_code == 0
