"""The README is Analyst Consortium's own front page, and these tests keep its words true.

It makes claims a reader will act on: commands to type, files to open, keys and switches to set, a trademark position, and what
we have and have not shown. Each is checked against the program or the repository, so the page cannot quietly drift from them."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGES = (ROOT / "CHANGES-FROM-UPSTREAM.md").read_text(encoding="utf-8")
NOTICE = re.sub(r"\s+", " ", (ROOT / "NOTICE").read_text(encoding="utf-8"))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces turned into hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    return {slug(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)}


def code_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL)


def program_text() -> str:
    parts = [p.read_text(encoding="utf-8", errors="ignore") for folder in ("tradingagents", "cli") for p in (ROOT / folder).rglob("*.py")]
    return "\n".join(parts) + (ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.mark.unit
class TestWhoItIsAndWhoItCredits:
    def test_it_says_what_it_is_early_and_points_to_the_changes_page(self):
        assert "(CHANGES-FROM-UPSTREAM.md)" in README[:1200] and "modified version of" in README[:1200]
        assert "Analyst Consortium" in README[:600]

    def test_it_carries_the_disclaimers_and_the_attribution(self):
        flat = re.sub(r"\s+", " ", README)
        for needed in ("not financial advice", "never places a trade", "not affiliated with or endorsed by Tauric Research",
                       "does not use their logo", "Apache License 2.0", "https://github.com/TauricResearch/TradingAgents",
                       "Yijia Xiao, Edward Sun, Di Luo and Wei Wang", "arxiv.org/abs/2412.20138", "Norman R. Sisk"):
            assert needed in flat, needed

    def test_it_no_longer_shows_the_originals_logo_pictures_or_badges(self):
        for gone in ("tauric-logo", "TauricResearch.png", "assets/schema.png", "assets/analyst.png", "assets/cli/", "shields.io",
                     "discord.com", "trendshift", "readme-i18n", "x.com/TauricResearch"):
            assert gone not in README, gone
        assert not (ROOT / "tradingagents" / "assets" / "tauric-logo.svg").exists() and not (ROOT / "assets" / "TauricResearch.png").exists()

    def test_it_makes_no_claim_that_the_changes_make_money(self):
        flat = re.sub(r"\s+", " ", README)
        assert "We have not shown that it makes money" in flat and "That it makes money" in flat
        assert not re.search(r"\b(outperform|beats the market|guarantee)", flat, re.IGNORECASE)


@pytest.mark.unit
class TestTheTrademarkPosition:
    FILES = ("tradingagents/assets/analyst-consortium-logo.svg", "tradingagents/assets/analyst-consortium-emblem.svg",
             "assets/analyst-consortium-logo-on-white.svg")

    def test_the_readme_and_the_notice_both_claim_the_logo_as_a_trademark_of_the_author(self):
        flat = re.sub(r"\s+", " ", README)
        assert "logo is a trademark (TM) of Norman R. Sisk" in flat and "claimed by use and not registered" in flat
        assert "logo is a trademark (TM) of Norman R. Sisk" in NOTICE and "claimed by use and not registered" in NOTICE

    def test_both_say_the_license_does_not_cover_the_logo_and_what_you_may_and_may_not_do(self):
        flat = re.sub(r"\s+", " ", README)
        for text in (flat, NOTICE):
            assert "does not cover" in text and "grants no right to use it" in text
            assert "replace the logo with your own" in text or "replace the logo with yours" in text
            assert "unmodified copy" in text.lower()

    def test_every_logo_file_they_name_exists(self):
        for path in self.FILES:
            assert (ROOT / path).is_file(), path
            assert path in README, path
        for path in self.FILES[:2]:
            assert path in NOTICE, path


@pytest.mark.unit
class TestLinksAndPictures:
    def test_every_picture_it_shows_exists(self):
        sources = re.findall(r'<img[^>]*\ssrc="([^"]+)"', README) + re.findall(r"!\[[^\]]*\]\(([^)\s]+)", README)
        assert sources, "the logo should be shown"
        for src in sources:
            assert not src.startswith("http"), f"no remote pictures: {src}"
            assert (ROOT / src).is_file(), src

    def test_the_picture_is_the_logo_on_white_with_a_description(self):
        assert re.search(r'<img src="assets/analyst-consortium-logo-on-white\.svg"[^>]*alt="[^"]{20,}"', README)

    def test_every_local_link_goes_somewhere_real(self):
        for target in re.findall(r"\]\((?!https?:|#)([^)\s]+)\)", README):
            path, _, fragment = target.partition("#")
            assert (ROOT / path).exists(), target
            if fragment and path.endswith(".md"):
                assert fragment in anchors((ROOT / path).read_text(encoding="utf-8")), target

    def test_every_link_within_the_page_matches_a_heading(self):
        have = anchors(README)
        for fragment in re.findall(r"\]\(#([^)\s]+)\)", README):
            assert fragment in have, fragment


@pytest.mark.unit
class TestWhatItTellsYouToType:
    def test_every_command_in_its_code_blocks_exists(self):
        from typer.testing import CliRunner

        from cli.main import app

        commands = set()
        for block in code_blocks(README):
            for line in block.splitlines():
                match = re.match(r"tradingagents\s+(?!-)([a-z-]+)(?:\s+(?!-)([a-z-]+))?", line.strip())
                if match:
                    commands.add(tuple(w for w in match.groups() if w))
        assert {("doctor",), ("ui",), ("shortcut",), ("backtest",), ("login",)} <= {c[:1] for c in commands}
        runner = CliRunner()
        for command in sorted(commands):
            words = list(command)
            if words[0] == "pick":
                assert runner.invoke(app, [*words[:2], "--help"]).exit_code == 0, command
            else:
                assert runner.invoke(app, [words[0], "--help"]).exit_code == 0, command

    def test_the_flags_it_shows_for_the_bare_command_exist(self):
        from typer.testing import CliRunner

        from cli.main import app

        out = CliRunner().invoke(app, ["--help"]).output
        for flag in ("--ticker", "--date", "--analysts", "--save", "--no-show", "--no-rerun", "--portfolio"):
            assert flag in re.sub(r"\s+", "", out) or flag in out, flag

    def test_the_install_commands_use_extras_the_project_defines(self):
        import tomllib

        extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
        named = set(re.findall(r'pip install -e "\.\[(\w+)\]"', README))
        assert {"ui", "bedrock", "dev"} <= named and named <= set(extras)
        assert 'pip install -e ".[ui]"' in README and "python -m venv .venv" in README

    def test_the_python_it_needs_is_the_one_the_project_requires(self):
        assert "Python 3.11 or later" in README and 'requires-python = ">=3.11"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_the_python_example_uses_real_names(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        example = next(b for b in code_blocks(README) if "TradingAgentsGraph" in b)
        for key in re.findall(r'config\["(\w+)"\]', example):
            assert key in DEFAULT_CONFIG, key
        assert hasattr(TradingAgentsGraph, "propagate") and hasattr(TradingAgentsGraph, "save_reports")
        assert DEFAULT_CONFIG["research_passes"] == 5 and "our default is 5" in example

    def test_the_numbers_it_states_are_the_real_ones(self):
        from tradingagents.agents.schemas import SCORE_BANDS
        from tradingagents.ui import runs

        stated = {name: tuple(map(int, rng)) for name, *rng in re.findall(r"(Sell|Underweight|Hold|Overweight|Buy) (\d+)-(\d+)", README)}
        assert stated == SCORE_BANDS
        assert runs.MAX_TICKERS == 10 and "up to ten stocks" in README
        assert "5, 21 and 63 trading sessions" in README


@pytest.mark.unit
class TestKeysSwitchesAndFolders:
    def test_every_key_and_switch_it_names_is_really_in_the_program(self):
        sources = program_text()
        named = set(re.findall(r"\b(TRADINGAGENTS_[A-Z_]+|[A-Z][A-Z_]*_API_KEY|SEC_EDGAR_USER_AGENT)\b", README))
        assert {"TRADINGAGENTS_RESEARCH_PASSES", "TRADINGAGENTS_NO_UPDATE_CHECK", "QUIVER_API_KEY", "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY",
                "TYPESAFE_API_KEY", "SEC_EDGAR_USER_AGENT", "TRADINGAGENTS_RESULTS_DIR"} <= named
        for name in named - {"TRADINGAGENTS_"}:
            assert name in sources, name

    def test_the_private_things_it_says_are_kept_out_of_the_repository_really_are(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("/data/", "my_book*.json", "*.pem", ".env"):
            assert re.search(rf"^{re.escape(pattern)}\s*$", ignore, re.MULTILINE), pattern

    def test_the_page_and_promo_it_describes_are_where_it_says(self):
        html = (ROOT / "tradingagents" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        assert "tradingagents/ui/static/index.html" in README and "From the maker of this app" in html and "aeroplanimetry.com" in html
        assert "Setup and settings" in html and "READY TO RUN" in (ROOT / "tradingagents" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
        assert "127.0.0.1" in README

    def test_the_example_env_file_it_tells_you_to_copy_exists(self):
        assert ".env.example" in README and (ROOT / ".env.example").is_file()
