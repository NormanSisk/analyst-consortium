"""An optional HTML copy of the report, for readers who do not open Markdown (#1419).

It is one self-contained file: no script, no remote asset, nothing a report's
text can inject; the Markdown report is unchanged.
"""

import re

import pytest

from tradingagents.reporting import write_report_tree

SETTINGS = {"version": "1.2.3", "llm_provider": "openai", "deep_think_llm": "gpt-6-sol",
            "quick_think_llm": "gpt-6-luna", "analysts": ["market", "news"], "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1, "data_vendors": {"core_stock_apis": "yfinance"}}


def _state(**overrides):
    state = {"trade_date": "2026-09-26",
             "market_report": "# Market view\n\nPrice | Level\n--- | ---\nClose | 181.2\n",
             "news_report": "News text.",
             "final_trade_decision": "**Rating**: Overweight\n\nAdd on weakness."}
    state.update(overrides)
    return state


def _html(tmp_path, **overrides):
    write_report_tree(_state(**overrides), "NVDA", tmp_path, settings=SETTINGS, html=True)
    return (tmp_path / "complete_report.html").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_html_page_is_written_unless_declined(tmp_path):
    write_report_tree(_state(), "NVDA", tmp_path / "plain", settings=SETTINGS, html=False)
    assert not (tmp_path / "plain" / "complete_report.html").exists()
    write_report_tree(_state(), "NVDA", tmp_path / "default", settings=SETTINGS)
    assert (tmp_path / "default" / "complete_report.html").exists()

    page = _html(tmp_path / "html")
    assert "<h1" in page and "NVDA" in page and "Overweight" in page and "2026-09-26" in page
    assert '<table>' in page and "181.2" in page


@pytest.mark.unit
def test_the_markdown_report_is_the_same_with_or_without_html(tmp_path):
    write_report_tree(_state(), "NVDA", tmp_path / "a", settings=SETTINGS, html=False)
    write_report_tree(_state(), "NVDA", tmp_path / "b", settings=SETTINGS)
    def undated(path):
        return re.sub(r"- Generated: .*\n", "", path.read_text(encoding="utf-8"))

    assert undated(tmp_path / "a" / "complete_report.md") == undated(tmp_path / "b" / "complete_report.md")


@pytest.mark.unit
def test_the_page_loads_nothing_and_runs_nothing(tmp_path):
    page = _html(tmp_path)
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "<script" not in page.lower()
    assert not re.search(r'<(img|link|iframe|object|embed)\b', page, re.IGNORECASE)


@pytest.mark.unit
def test_report_text_cannot_inject_markup_or_fetch_anything(tmp_path):
    hostile = ("<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n"
               "![pixel](https://tracker.example/p.png) [x](javascript:alert(1))")
    page = _html(tmp_path, news_report=hostile)
    assert "<script>alert" not in page and "<img" not in page.lower()
    assert "tracker.example/p.png" not in re.findall(r'(?:src|href)="([^"]*)"', page)
    assert 'href="javascript:' not in page


@pytest.mark.unit
def test_header_values_are_escaped(tmp_path):
    write_report_tree(_state(), "<b>NV</b>", tmp_path, settings={**SETTINGS, "deep_think_llm": "<i>m</i>"}, html=True)
    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    assert "<b>NV</b>" not in page and "&lt;b&gt;NV&lt;/b&gt;" in page and "<i>m</i>" not in page


@pytest.mark.unit
def test_an_agent_s_own_headings_sit_below_its_heading(tmp_path):
    page = _html(tmp_path)
    assert page.count("<h1") == 1                     # only the report's title
    assert re.search(r"<h4[^>]*>Market view</h4>", page)


def _fields(fragment):
    return re.findall(r'<span class="field">([^<]*)</span>', fragment)


@pytest.mark.unit
def test_run_details_show_each_value_as_its_own_field(tmp_path):
    settings = {**SETTINGS, "deep_think_provider": "anthropic", "deep_think_llm": "claude-opus-5-5",
                "data_vendors": {"core_stock_apis": "yfinance", "fundamental_data": "sec_edgar,yfinance"}}
    write_report_tree(_state(), "NVDA", tmp_path, settings=settings, html=True)
    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    header = page[page.index("<header"):page.index("</header>")]
    for value in ("anthropic", "claude-opus-5-5", "openai", "gpt-6-luna", "market", "news",
                  "sec_edgar", "yfinance"):
        assert value in _fields(header)
    # Vendors are named by what they serve, and a chain keeps its order.
    assert "Prices" in header and "Fundamentals" in header and "fundamental_data" not in header
    assert header.index(">sec_edgar<") < header.rindex(">yfinance<")


@pytest.mark.unit
def test_the_contents_name_each_section_once_and_list_its_agents_beneath(tmp_path):
    page = _html(tmp_path)
    nav = page[page.index('<nav class="contents"'):page.index("</nav>")]
    assert "<ol" not in nav                     # no list numbers beside the sections' own I-V
    assert re.search(r'<a href="#s1"><span class="num">I</span>\s*<span class="label">Analyst Team Reports</span></a>', nav)
    assert re.search(r'<a href="#s1-1"><span class="bar"></span>\s*<span class="label">Market Analyst</span></a>', nav)


@pytest.mark.unit
def test_the_section_rail_opens_on_hover_and_keyboard_focus(tmp_path):
    page = _html(tmp_path)
    css = page[page.index("<style>"):page.index("</style>")]
    # Wide screens: a rail centred on the left edge whose names are hidden from
    # sight only (still read by screen readers) until it is pointed at or focused.
    assert "top: 50%" in css and "translateY(-50%)" in css
    assert ".contents:hover .label" in css and ".contents:focus-within .label" in css
    assert "display: none" not in css.split(".contents .label", 1)[1].split("}", 1)[0]


@pytest.mark.unit
def test_the_sections_stay_within_reach(tmp_path):
    page = _html(tmp_path)
    nav = page[page.index('<nav class="contents"'):page.index("</nav>")]
    assert "<h2" not in nav                                         # the list needs no heading
    assert 'id="top"' in page and 'href="#top"' in page             # a way back on small screens
    assert ".contents { position: fixed;" in page                   # beside the text on wide ones


@pytest.mark.unit
def test_the_page_heading_is_the_ticker_and_its_rating(tmp_path):
    page = _html(tmp_path)
    heading = re.search(r"<h1>(.*?)</h1>", page).group(1)
    assert re.sub(r"<[^>]+>", "", heading).split() == ["NVDA", "Overweight"]


@pytest.mark.unit
def test_the_header_bears_the_programs_own_name_and_says_what_it_is_built_on(tmp_path):
    page = _html(tmp_path)
    header = page[page.index("<header"):page.index("</header>")]
    assert '<p class="brand">Analyst Consortium</p>' in header
    assert re.search(r'<p class="built-on">Built on <a href="https://github.com/TauricResearch/TradingAgents"[^>]*>TradingAgents</a></p>', header)
    assert header.index('class="brand"') < header.index('class="built-on"') < header.index("<h1")      # the name leads, then the ticker
    assert "Analyst Consortium report</title>" in page and "TradingAgents report" not in page


@pytest.mark.unit
def test_the_makers_logo_and_name_are_not_in_the_header_and_the_page_draws_no_pictures(tmp_path):
    page = _html(tmp_path)
    header = page[page.index("<header"):page.index("</header>")]
    assert "<svg" not in page and "tauric.ai" not in page                      # their logo is their brand, not ours
    assert "Tauric" not in re.sub(r"<[^>]+>", "", header)                      # the name a reader sees (the link may still point at their repository)
    assert "logo" not in header.lower()


@pytest.mark.unit
def test_the_version_line_names_the_program_and_the_engine_it_is_built_on(tmp_path):
    page = _html(tmp_path)
    assert "Analyst Consortium, built on TradingAgents 1.2.3" in page[page.index("<header"):page.index("</header>")]


@pytest.mark.unit
def test_the_page_credits_its_maker_in_a_line_of_text(tmp_path):
    page = _html(tmp_path)
    footer = page[page.index('<footer class="credit">'):page.index("</footer>")]
    # The Apache-2.0 credit stays, in words and links: the project it is built on, its maker and the license.
    assert re.sub(r"<[^>]+>", "", footer).split() == ["Built", "on", "TradingAgents", "by", "Tauric", "Research", "(Apache-2.0)"]
    assert 'href="https://github.com/TauricResearch/TradingAgents"' in footer
    assert 'href="https://github.com/TauricResearch"' in footer and "tauric.ai" not in footer
    assert "<svg" not in footer and 'class="brand"' in footer
    assert page.index("</main>") < page.index('<footer class="credit">')


# ---------------------------------------------------------------- pages and orphaned headings
def _print_css(page):
    css = page[page.index("<style>"):page.index("</style>")]
    return css[css.index("@media print"):]


@pytest.mark.unit
def test_no_heading_level_may_end_a_printed_page(tmp_path):
    css = _print_css(_html(tmp_path))
    # every level, including the agents' own headings (h5, h6), and the lines that introduce a list or table
    assert "h1, h2, h3, h4, h5, h6, main .keep-next { break-after: avoid; }" in css
    assert "main h3, main h4 { break-after: avoid; }" not in css                 # the old rule covered only two levels
    assert "main h2 { break-before: page;" in css                                 # each section still starts a page


@pytest.mark.unit
def test_tables_and_other_blocks_are_kept_whole_with_headers_that_repeat(tmp_path):
    css = _print_css(_html(tmp_path))
    assert "table { break-inside: avoid; }" in css and "tr, pre, blockquote { break-inside: avoid; }" in css
    assert "thead { display: table-header-group; }" in css and "orphans: 3; widows: 3;" in css


@pytest.mark.unit
@pytest.mark.parametrize("markdown, marked", [
    ("**Summary Table**", True),                                  # one bold line used as a heading
    ("Consensus (as of 2026-11-08):", True),                      # introduces a table or list
    ("Reference levels, with `code` in it:", True),
    ("**Risks:**", True),
    ("**Note:** the stock fell on its own.", False),              # a sentence with a bold lead-in is ordinary text
    ("**One** then **two**", False),
    ("A normal sentence that ends with a full stop.", False),
    ("A question we ask: why? Because.", False),
])
def test_lines_that_introduce_what_follows_are_marked_to_stay_with_it(tmp_path, markdown, marked):
    page = _html(tmp_path, news_report=f"{markdown}\n\nPrice | Level\n--- | ---\nClose | 1\n")
    paragraph = re.search(r"<p( class=\"keep-next\")?>([^\n]*)</p>", page[page.index("<main>"):])
    assert bool(paragraph.group(1)) is marked, paragraph.group(0)


@pytest.mark.unit
def test_marked_lines_carry_only_the_class_the_program_adds(tmp_path):
    page = _html(tmp_path, news_report='**Summary Table**\n\n<p class="x" onclick="y()">hostile:</p>')
    assert '<p class="keep-next"><strong>Summary Table</strong></p>' in page
    assert not re.search(r"<[^>]*onclick", page) and '<p class="x"' not in page            # hostile markup stays escaped text
