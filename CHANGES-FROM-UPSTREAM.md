# What changed from TradingAgents

**Analyst Consortium** is a modified version of [TradingAgents](https://github.com/TauricResearch/TradingAgents) by Tauric Research. This page says what we changed and what we added, so that anyone can tell the original from our work. It is also the notice that the Apache License 2.0 (section 4) asks a modified version to carry.

- **Started from:** TradingAgents v0.7.1, commit `c454491` (2026-10-27).
- **License:** unchanged, Apache License 2.0. Our changes and additions are copyright 2026 Norman R. Sisk and under the same license; the original code remains its authors'. See `LICENSE` and `NOTICE`.
- **Not affiliated with Tauric Research.** "TradingAgents" and "Tauric Research" are the names of that project and its authors. We use them only to say where this came from, and we do not use their logo.
- **The logo is a trademark.** The Analyst Consortium logo is a trademark (TM) of Norman R. Sisk, claimed by use and not registered, and its artwork is copyright 2026 Norman R. Sisk, all rights reserved. The Apache License does not cover the logo and grants no right to use it; `NOTICE` and the README say what you may and may not do with it.
- **Not financial advice.** This is a research tool. It never places a trade, and nothing it says is a recommendation to buy or sell. We have not shown that any of these changes improves investment results (see "What we measured" below).

## The short version

The original idea is intact: analysts gather data, a bull and a bear debate, a trader proposes, a risk team argues, and a portfolio manager decides. We kept that and added:

1. **A sharper research stage.** The debate runs several times and the median wins; the bull, the bear and the manager read one shared fact sheet worked out in code; a market-wide selloff is told apart from a company in trouble; every call gets a 0 to 100 conviction score.
2. **Eleven new data tools** for the analysts: earnings, trends, valuation, ownership and short interest, options and volatility, market context, SEC filing events, what management said, macro conditions, public records and Congress trades. The existing insider-trades tool also gained a new source.
3. **A local web page** (`tradingagents ui`) to check the setup, set the AI connection and keys, start analyses, watch the analysts work, and save reports as HTML, CSV and PDF.
4. **A free stock picker** (no AI calls) with three ways to look, a record of every list it showed, and a scoreboard that compares those lists with the S&P 500 later.
5. **Command-line additions:** `doctor`, `ui`, `pick`, `shortcut`, an offer of extra runs when a call is borderline, and a usage and cost log.
6. **Our own name and look** on the reports, with the original credited by name and link.

## See the exact difference yourself

```bash
git remote add upstream https://github.com/TauricResearch/TradingAgents.git
git fetch upstream
git diff c454491 HEAD --stat          # every file that differs
python scripts/list_changes.py        # the same lists as the end of this page
python scripts/list_changes.py --check CHANGES-FROM-UPSTREAM.md   # is this page's list current?
```

## What behaves differently from the original, by default

| What | Original | Here | To get the original behavior |
| --- | --- | --- | --- |
| Research debate | one pass | five passes, median rating | `TRADINGAGENTS_RESEARCH_PASSES=1` |
| Selloff rules in the prompts | none | on | `TRADINGAGENTS_NO_SELLOFF_RULES=1` |
| Shared fact sheet (scorecard) | none | on | `TRADINGAGENTS_NO_SCORECARD=1` |
| The new data tools | none | offered to the analysts | `TRADINGAGENTS_DISABLED_TOOLS=name1,name2` |
| Investing style | none | standard (growth is a choice) | leave `TRADINGAGENTS_INVESTING_STYLE` unset |
| Decision fields | as upstream | adds a 0 to 100 conviction score; the optional price target is shown only if the thesis states the same number | none needed |
| Insider trades | Yahoo | Quiver first when `QUIVER_API_KEY` is set, else Yahoo | leave the key unset |
| Today's price bar | partial bar could read as a finished day | an unfinished session is left out and the report says so | `drop_unfinished_session: False` in the config |
| Report | analysts, debate, plan, risk, decision | adds Research Panel and Scorecard sections, our header, a print layout that keeps headings with their content | none needed |
| After a borderline call | nothing | offers two more analyses and a combined verdict | `--no-rerun` |

Five passes cost about five times the debate. With Anthropic's Haiku and Sonnet models we measured about $0.40 and six minutes per stock at five passes, and the run form shows an estimate before you start. The version number in the code is still 0.7.1, because our changes are not versioned separately yet.

## What we added

### 1. The analysis engine

- **Research panel** (`tradingagents/graph/research_panel.py`). Runs the whole bull, bear and Research Manager debate several independent times and passes on the median rating, with how many passes agreed. On borderline stocks a single pass flips between neighboring ratings about as often as a coin; a median of an odd number of passes does not. A call decided by the narrowest majority is labeled borderline and the summary states the range.
- **Scorecard** (`agents/scorecard.py`). A fact sheet worked out in code from the data tools (not by a model), handed to the bull, the bear and the Research Manager so they argue from the same figures.
- **Selloff rules** (`agents/selloff.py`). Prompts that help tell a market-driven selloff from a company in trouble, with a five-condition test for a real deal: the market is in a drawdown, the market explains most of the stock's fall, the company has not deteriorated, it passes a survival test, and its valuation is not stretched. The aim is not to penalize a stock for panic that hit everything.
- **Conviction score and borderline note** (`agents/schemas.py`, `agents/conviction.py`). Each rating owns a band of a 0 to 100 score (Sell 0-20, Underweight 21-40, Hold 41-59, Overweight 60-79, Buy 80-100). A score near the edge of its band says the call is close to the next rating and what would tip it. The price target is shown only when the thesis states the same number.
- **Investing style** (`agents/style.py`). A run-time choice between a standard and a growth lens. It changes how the agents weigh the same facts, never the facts, and the report says which was used.
- **Portfolio context** (`tradingagents/portfolio.py`). Optional free-text investor profile, and with no position held the five ratings are restated as advice about opening a position, so the same view keeps the same rating whether or not a portfolio was supplied.
- **Structured-output safety** (`agents/structured.py`). A malformed structured answer is retried once, and each agent's calls, retries and fallbacks to plain text are counted and written into the run's log.
- **Analyst prompt rules** (the market, news and fundamentals analysts). Rules about quoting figures, valuation yardsticks, trends and what management said, plus an on/off switch for each new tool (`agents/analysts/toolset.py`).

### 2. More data for the analysts

Eleven new tools the analysts can call (and a new source, Quiver, for the existing insider-trades tool); the modules are under `tradingagents/dataflows/vendors/`. "Key" means a free or paid API key in your `.env`; without it that source is skipped. `tradingagents doctor` shows which keys are set.

- **Earnings facts, trend facts and valuation context** (Yahoo). Earnings history and expectations; each quarterly figure seen two ways with rule-based flags; the price set against the company's own history, its peers and the bond yield. No key.
- **Ownership and short interest** (Yahoo, with FINRA's short-sale data). Who owns the stock and how heavily it is sold short, with the reporting lag stated. No key.
- **Market statistics** (Yahoo options and prices; Interactive Brokers is an optional alternative). Volatility, options positioning, volume and price range. No key for Yahoo.
- **Market context** (Yahoo prices). The market's state and the stock's beta, relative volatility and how much of its move the market explains.
- **Filing events** (SEC EDGAR). What the company has filed that its statements do not show yet: 8-K items, offerings, 13D and 13G holders, share counts. No key; set `SEC_EDGAR_USER_AGENT` to your own name and address as the SEC asks.
- **Company statements** (SEC EDGAR; Alpha Vantage for call transcripts). What management said: the earnings call, the release and the MD&A. The call needs `ALPHA_VANTAGE_API_KEY`.
- **Macro backdrop** (FRED). Credit spreads, financial conditions and the next data releases. Needs a free `FRED_API_KEY`.
- **Public records.** Federal contract awards (USAspending), and for health-care companies clinical studies (ClinicalTrials.gov) and FDA recalls. No key.
- **Congress trades** (Quiver). Needs `QUIVER_API_KEY`. Quiver is also a new first source for the existing insider-trades tool, with Yahoo as the fallback.

We also fixed some existing Yahoo data behavior (see "Upstream files we modified"): an unfinished trading session no longer reads as a finished day, insider filings are limited to a recent window so an old trade is not given the wrong year, company news falls back to Yahoo search when the news feed fails, and the fundamentals and price snapshots now carry arithmetic worked out in code (distances, drawdowns, moving-average state, volume ratios) instead of leaving it to a model.

### 3. The local web page

`tradingagents ui` starts a small web server that answers only on your own computer, and opens it in your browser. It is Python (FastAPI) serving plain HTML, CSS and JavaScript from `tradingagents/ui/static/`.

- **Setup check and settings.** Runs `tradingagents doctor` (below) and edits the settings. It is the first thing in the main column, behind a labeled rollup button ("Setup and settings"): open until setup is complete, closed afterward but always one click away. Keys are written to your `.env` and are never sent back to the page; it learns only whether one is set.
- **Run form.** Start analyses for up to ten stocks, choose analysts, depth, research passes, investing style and language, see a cost estimate first, follow progress, and stop one.
- **Live view.** What each analyst says and asks for while it works, with an on/off switch.
- **Reports shortcut.** A button in the left column opens the folder of saved reports in the file manager (Explorer, Finder or the desktop's own) and shows where the folder is. The server uses its own configured path; the page sends none.
- **Clearing the list.** Finished, failed and stopped analyses can be taken off the page's list one at a time or all together, after a confirmation. Only the list and its small record (`ui-runs` under the results folder) are deleted; saved reports, run logs and the decision memory log stay.
- **Reports.** The HTML report opens when a run finishes, and any finished analysis can be saved as CSV or PDF. Browsers that offer a Save As window let you choose the place; others download the file. (PDF needs Microsoft Edge or Google Chrome installed; it is made from the HTML report.)
- **Designed to be readable.** Every status is a symbol and a word (never color alone), text and background pairs checked for WCAG AAA contrast (at least 7:1), three color themes including high contrast, two text sizes (the last choice is remembered), and no sideways scrolling.
- **Safe by design.** It listens on `127.0.0.1` only, checks the `Host` and `Origin` of every request, requires a random per-session token on every call, and sends a strict content-security policy, so other web sites open in your browser cannot reach it.
- **A small promotion.** The side column has a "From the maker of this app" block, in that website's dark blue with white text, with a button linking to AeroPlanimetry (https://aeroplanimetry.com), a mapping website owned by the author. It is one marked block in `tradingagents/ui/static/index.html`; delete it if you do not want it.
- **The logo.** The symbol of the Analyst Consortium logo (`tradingagents/assets/analyst-consortium-logo.svg` is the author's full logo; `tradingagents/assets/analyst-consortium-emblem.svg` is the symbol alone, made from it by `scripts/make_logo_emblem.py`) appears to the left of the name in the page's heading and in the header of every report. On the dark themes it sits on a white chip, because the symbol has dark navy lines. For the same reason the README shows `assets/analyst-consortium-logo-on-white.svg`, the full logo with a white card drawn behind it, made by the same script; nothing else in it is changed. The logo is the author's trademark (see the top of this page).
- **One copy at a time, and a shortcut.** Starting it again while it is open just opens the page that is running. `tradingagents shortcut` puts a launcher on your desktop (Windows `.lnk`, macOS `.command`, Linux `.desktop`). The macOS and Linux launchers are written from those systems' documentation and have not been tried on them. The app icon is drawn by `scripts/make_app_icon.py` with no outside artwork.

### 4. The stock picker

A free, quick look in code before any paid analysis. It uses Yahoo's free screener and prices, makes no AI calls, and checks each stock against the five deal conditions above. Three ways to look, on the page under "Find stocks" and on the command line as `tradingagents pick`:

- **Beaten-down quality.** The largest US stocks that are well below their highs where the market explains the fall, checked against the five conditions.
- **Your own list.** Type symbols or load a CSV or text file and get the same checks on each.
- **Custom search** (page only). About 25 optional filters (size, price action, valuation, growth, profits, debt, ownership, analyst rating), with a count of matches before you search. A blank filter means no filter.

Each result is a card with the five conditions shown as PASS, FAIL or UNKNOWN and the numbers behind them, and you can tick stocks and send them to the run form for the full analysis. Every list shown can be recorded in a picks log, and `tradingagents pick scoreboard` (or the page) later compares each recorded stock with the S&P 500 at 5, 21 and 63 trading sessions. It says "too early" until there is enough to look at, and never calls a result proof.

### 5. Research studies

`python -m tradingagents.research.analyst_study` runs a free study of Yahoo's analyst rating and price-target history, asking whether a firm's past accuracy predicts its future accuracy. In our run (77 stocks) it did not, so analyst ratings stay context in the analysis and are not weighted by track record.

### 6. The command line

`tradingagents doctor` checks the setup in plain words (folders, certificates, AI keys and models, data sources, settings) and exits non-zero when something required is missing. `tradingagents ui`, `tradingagents pick ...` and `tradingagents shortcut` are described above. The bare `tradingagents` command (which runs an analysis) gained `--rerun/--no-rerun`, and every finished run writes token totals and an estimated cost to a usage log.

### 7. Reports

The HTML report (and the PDF made from it) carries the symbol of the Analyst Consortium logo and the name, with a "Built on TradingAgents" line, and credits Tauric Research by name and link in its footer. The symbol is drawn inside the page, so a report is still one self-contained file that loads nothing. The original project's logo is not used. Print layout keeps every heading, and every line that introduces a list or table, on the same page as what follows it, and keeps table rows and code blocks whole.

## Upstream files we modified

All of these are marked in the file lists at the end. In short:

- **Analysts** (`agents/analysts/` market, news and fundamentals): the new tools, the prompt rules, the on/off switch.
- **Researchers, managers and risk debaters** (`agents/researchers/`, `agents/managers/`, `agents/risk_mgmt/`): their prompts include the scorecard, the selloff rules and the style lens; the portfolio manager also sees the research panel's summary.
- **`agents/schemas.py`, `agents/structured.py`, `agents/state.py`, `agents/tools.py`:** the conviction score and target guard, the retry and counts, two new state fields, the new tool wrappers.
- **`graph/setup.py`, `graph/trading_graph.py`:** the research panel and scorecard nodes, a hook that reports which agent is speaking (for the live view), and the new settings recorded with each run.
- **`dataflows/router.py`, `default_config.py`:** register the new data categories and vendors and set their defaults.
- **`dataflows/vendors/yahoo/` fundamentals, market, news, ohlcv, snapshot:** the data fixes and computed facts described above.
- **`portfolio.py`, `reporting.py`, `report_html.py`:** the profile and no-position reading, the new report sections, our header and the print layout.
- **`cli/main.py`, `cli/run.py`:** the new commands and flags, the live event log, the usage log and the offer of extra runs.
- **`cli/display.py`, `cli/selections.py`, `cli/static/welcome.txt`:** the command line's welcome banners and lettering show our name, and credit the original by name and link ("Built on TradingAgents by Tauric Research (Apache-2.0)") instead of claiming its copyright line.
- **`cli/updates.py`, `dataflows/vendors/reddit.py`, `dataflows/vendors/stocktwits.py`, `.github/`:** pointed at this project instead of the original. The start-up update check asks this project's GitHub releases once a day and no longer contacts the original's announcements server (the notices feature went with it); the user-agent that Reddit and StockTwits see names this project; the issue links and the release-assets note use this project's address. `tradingagents/project.py` holds that address, and a test checks that every place agrees.
- **`pyproject.toml`:** a description and project links that name this project (with a link back to the original), an optional `ui` extra (`pip install "tradingagents[ui]"`) and the packaged icon and web files. **`.gitignore`:** keeps private local data out (see below). **`README.md`:** rewritten as Analyst Consortium's own front page: what it is, how to install and use it, what it connects to, what we have not shown, the logo's trademark position, and credit to the original. Its text is ours (apart from the citation the original asks for), and it shows none of the original's badges or pictures.
- **Seven test files:** adjusted to match the changes.

## What we did not change

The AI provider clients (`llm_clients/`), the decision memory log, checkpoint and resume, backtesting, the original agents' roles and their order in the graph (we added nodes, we did not remove or reorder any), the original data vendors other than the Yahoo fixes above, and upstream's own documentation and pictures (`CHANGELOG.md` is still their release history, and their pictures are still in `assets/`). We removed only the two files that were nothing but their logo, `tradingagents/assets/tauric-logo.svg` and `assets/TauricResearch.png`, which nothing used once the README was rewritten.

## What we measured, and what we have not shown

- **Selloff rules.** An A/B test of about three dozen runs on three stocks (NVDA, MU, ASML) with the rules on and off: the control stocks held their ratings and no real deal was found among them. This shows the rules did not wreck the ratings; it does not show they improve them.
- **New data tools.** A 30-run on/off test: switching the new tools on did not change any rating on the stocks tried (the same stocks split the same way), but the tools are used correctly and change what the plans cite, such as short interest and the credit backdrop.
- **Research panel.** Single runs of a borderline stock do flip between neighboring ratings; the panel makes the median stable. That is a consistency gain, not a claim about accuracy.
- **Analyst track records.** No persistence of a firm's accuracy in our 77-stock study.
- **Stock picker.** Spot checks against the full analysis (AMD, NVDA, ASML) agreed on which conditions fail. It is more generous on some stocks (for example TSM) because it compares with a different peer set.

We have not tested whether any of this makes money. Ratings are an AI's reading of public data on one day.

## Private data stays private

Your keys (`.env`), holdings file, run logs and reports, the picks log, caches and certificate bundles live in your own folders and are excluded from the repository by `.gitignore` (`/data/`, `.env`, `my_book*.json`, `*.pem`).

## The full file lists

<!-- inventory:begin (written by scripts/list_changes.py; do not edit by hand) -->

Compared with commit `c454491` (TradingAgents v0.7.1). A file that is not listed here is unchanged.

#### Upstream files we modified (50)

- `.dockerignore` (+6 -0)
- `.env.example` (+1 -1)
- `.github/ISSUE_TEMPLATE/bug-report.yml` (+1 -1)
- `.github/ISSUE_TEMPLATE/config.yml` (+3 -3)
- `.github/workflows/release-assets.yml` (+1 -1)
- `.gitignore` (+6 -0)
- `README.md` (+120 -321)
- `cli/display.py` (+4 -3)
- `cli/main.py` (+208 -2)
- `cli/run.py` (+37 -5)
- `cli/selections.py` (+5 -4)
- `cli/static/welcome.txt` (+11 -6)
- `cli/updates.py` (+18 -56)
- `pyproject.toml` (+12 -2)
- `tests/conftest.py` (+20 -0)
- `tests/test_cli_commands.py` (+2 -1)
- `tests/test_cli_headless.py` (+1 -1)
- `tests/test_cli_updates.py` (+68 -105)
- `tests/test_graph_end_to_end.py` (+52 -3)
- `tests/test_memory_log.py` (+2 -2)
- `tests/test_report_html.py` (+79 -16)
- `tests/test_tool_date_enforcement.py` (+5 -0)
- `tradingagents/agents/analysts/fundamentals_analyst.py` (+33 -3)
- `tradingagents/agents/analysts/market_analyst.py` (+21 -4)
- `tradingagents/agents/analysts/news_analyst.py` (+15 -3)
- `tradingagents/agents/managers/portfolio_manager.py` (+11 -2)
- `tradingagents/agents/managers/research_manager.py` (+5 -2)
- `tradingagents/agents/researchers/bear_researcher.py` (+5 -2)
- `tradingagents/agents/researchers/bull_researcher.py` (+5 -2)
- `tradingagents/agents/risk_mgmt/aggressive_debator.py` (+3 -1)
- `tradingagents/agents/risk_mgmt/conservative_debator.py` (+3 -1)
- `tradingagents/agents/risk_mgmt/neutral_debator.py` (+3 -1)
- `tradingagents/agents/schemas.py` (+95 -3)
- `tradingagents/agents/state.py` (+2 -0)
- `tradingagents/agents/structured.py` (+88 -14)
- `tradingagents/agents/tools.py` (+226 -0)
- `tradingagents/dataflows/router.py` (+120 -2)
- `tradingagents/dataflows/vendors/reddit.py` (+2 -1)
- `tradingagents/dataflows/vendors/stocktwits.py` (+2 -1)
- `tradingagents/dataflows/vendors/yahoo/fundamentals.py` (+79 -3)
- `tradingagents/dataflows/vendors/yahoo/market.py` (+20 -2)
- `tradingagents/dataflows/vendors/yahoo/news.py` (+15 -2)
- `tradingagents/dataflows/vendors/yahoo/ohlcv.py` (+46 -0)
- `tradingagents/dataflows/vendors/yahoo/snapshot.py` (+98 -0)
- `tradingagents/default_config.py` (+22 -0)
- `tradingagents/graph/setup.py` (+34 -14)
- `tradingagents/graph/trading_graph.py` (+19 -1)
- `tradingagents/portfolio.py` (+33 -1)
- `tradingagents/report_html.py` (+53 -19)
- `tradingagents/reporting.py` (+5 -0)

#### Files we added (117)

**Tests** (45)

- `tests/fixtures/scorecard_nvda.json`
- `tests/picker_world.py`
- `tests/test_changes_document.py`
- `tests/test_company_statements.py`
- `tests/test_conviction_note.py`
- `tests/test_conviction_score.py`
- `tests/test_doctor.py`
- `tests/test_estimates_section.py`
- `tests/test_finra_short.py`
- `tests/test_fred_backdrop.py`
- `tests/test_ibkr_market_stats.py`
- `tests/test_launcher.py`
- `tests/test_live_events.py`
- `tests/test_local_accuracy_changes.py`
- `tests/test_logo.py`
- `tests/test_market_context.py`
- `tests/test_ownership.py`
- `tests/test_picker.py`
- `tests/test_picker_cli.py`
- `tests/test_picker_custom.py`
- `tests/test_picker_data.py`
- `tests/test_picker_scoreboard.py`
- `tests/test_portfolio_profile.py`
- `tests/test_price_target_guard.py`
- `tests/test_project_address.py`
- `tests/test_public_records.py`
- `tests/test_quiver_vendor.py`
- `tests/test_readme.py`
- `tests/test_rerun.py`
- `tests/test_research_panel.py`
- `tests/test_scorecard.py`
- `tests/test_sec_filing_events.py`
- `tests/test_selloff_rules.py`
- `tests/test_structured_fallback_counts.py`
- `tests/test_style.py`
- `tests/test_toolset.py`
- `tests/test_track_record.py`
- `tests/test_trends.py`
- `tests/test_ui.py`
- `tests/test_ui_exports.py`
- `tests/test_ui_picker.py`
- `tests/test_ui_runs.py`
- `tests/test_usage_log.py`
- `tests/test_valuation.py`
- `tests/test_yahoo_market_stats.py`

**Setup screen and run form (the local web page)** (15)

- `tradingagents/ui/__init__.py`
- `tradingagents/ui/exports.py`
- `tradingagents/ui/picker_jobs.py`
- `tradingagents/ui/runs.py`
- `tradingagents/ui/server.py`
- `tradingagents/ui/settings_store.py`
- `tradingagents/ui/static/app.css`
- `tradingagents/ui/static/app.js`
- `tradingagents/ui/static/bg-market-dark.svg`
- `tradingagents/ui/static/bg-market-light.svg`
- `tradingagents/ui/static/board.js`
- `tradingagents/ui/static/custom.js`
- `tradingagents/ui/static/index.html`
- `tradingagents/ui/static/picker.js`
- `tradingagents/ui/static/runs.js`

**Stock picker** (14)

- `tradingagents/picker/__init__.py`
- `tradingagents/picker/cache.py`
- `tradingagents/picker/conditions.py`
- `tradingagents/picker/custom.py`
- `tradingagents/picker/facts.py`
- `tradingagents/picker/folders.py`
- `tradingagents/picker/ledger.py`
- `tradingagents/picker/lists.py`
- `tradingagents/picker/providers.py`
- `tradingagents/picker/quicklook.py`
- `tradingagents/picker/report.py`
- `tradingagents/picker/scoreboard.py`
- `tradingagents/picker/settings.py`
- `tradingagents/picker/stage_a.py`

**Research studies** (3)

- `tradingagents/research/__init__.py`
- `tradingagents/research/analyst_study.py`
- `tradingagents/research/track_record.py`

**Data sources** (16)

- `tradingagents/dataflows/vendors/company_statements.py`
- `tradingagents/dataflows/vendors/finra_short.py`
- `tradingagents/dataflows/vendors/fred_backdrop.py`
- `tradingagents/dataflows/vendors/ibkr_market_stats.py`
- `tradingagents/dataflows/vendors/public_records.py`
- `tradingagents/dataflows/vendors/quiver.py`
- `tradingagents/dataflows/vendors/sec_filing_events.py`
- `tradingagents/dataflows/vendors/yahoo/analyst_history.py`
- `tradingagents/dataflows/vendors/yahoo/earnings.py`
- `tradingagents/dataflows/vendors/yahoo/health.py`
- `tradingagents/dataflows/vendors/yahoo/market_context.py`
- `tradingagents/dataflows/vendors/yahoo/market_stats.py`
- `tradingagents/dataflows/vendors/yahoo/ownership.py`
- `tradingagents/dataflows/vendors/yahoo/picker_data.py`
- `tradingagents/dataflows/vendors/yahoo/trends.py`
- `tradingagents/dataflows/vendors/yahoo/valuation.py`

**Analysis engine** (6)

- `tradingagents/agents/analysts/toolset.py`
- `tradingagents/agents/conviction.py`
- `tradingagents/agents/scorecard.py`
- `tradingagents/agents/selloff.py`
- `tradingagents/agents/style.py`
- `tradingagents/graph/research_panel.py`

**Command line** (2)

- `cli/rerun.py`
- `cli/usage_log.py`

**Scripts, icon and design notes** (8)

- `docs/mockups/setup-screen.html`
- `scripts/list_changes.py`
- `scripts/make_app_icon.py`
- `scripts/make_logo_emblem.py`
- `tradingagents/assets/analyst-consortium-emblem.svg`
- `tradingagents/assets/analyst-consortium-logo.svg`
- `tradingagents/assets/app-icon.ico`
- `tradingagents/assets/app-icon.png`

**Other program files** (8)

- `CHANGES-FROM-UPSTREAM.md`
- `NOTICE`
- `assets/analyst-consortium-logo-on-white.svg`
- `tradingagents/csv_safe.py`
- `tradingagents/doctor.py`
- `tradingagents/launcher.py`
- `tradingagents/live_events.py`
- `tradingagents/project.py`

#### Upstream files we removed (2)

- `assets/TauricResearch.png`
- `tradingagents/assets/tauric-logo.svg`

<!-- inventory:end -->
