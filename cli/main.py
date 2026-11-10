import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
    rerun: bool | None = typer.Option(
        None, "--rerun/--no-rerun",
        help="When the call is borderline, run two more analyses without asking (or never ask). "
        "Without either flag the question is asked at the keyboard only.",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html,
                 "rerun": rerun}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


# The plans `login` and `logout` take: the providers signed in to, not keyed.
Plan = StrEnum("Plan", {name: name for name in SIGNED_IN_PROVIDERS})


@app.command()
def login(plan: Annotated[Plan, typer.Argument(help="The plan to sign in with")]):
    """Sign in with your ChatGPT Plus or Pro plan, for the chatgpt provider."""
    from tradingagents.llm_clients import chatgpt_auth

    try:
        # One unwrapped line, so the sign-in address can be copied whole.
        email = chatgpt_auth.sign_in(
            say=lambda line: console.print(line, soft_wrap=True, markup=False, highlight=False))
    except chatgpt_auth.ChatGPTAuthError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Signed in to ChatGPT as {escape(email)}.[/green]")


@app.command()
def logout(plan: Annotated[Plan, typer.Argument(help="The plan to sign out of")]):
    """Sign out of your ChatGPT plan: revoke this machine's access and forget it."""
    from tradingagents.llm_clients import chatgpt_auth

    outcome = chatgpt_auth.sign_out()
    if outcome == "revoked":
        console.print("Signed out of ChatGPT.")
    elif outcome == "forgotten":
        console.print("[yellow]Signed out here, but OpenAI could not be reached to revoke the sign-in; "
                      "it stays valid at OpenAI until it expires.[/yellow]")
    else:
        console.print("Not signed in to ChatGPT.")


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON, for programs")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Skip every check that needs the internet")] = False,
    no_ai_call: Annotated[bool, typer.Option("--no-ai-call", help="Skip the live test calls to the AI provider")] = False,
    group: Annotated[list[str] | None, typer.Option(
        "--group", help="Check only this group (repeatable): environment, ai, data, settings")] = None,
):
    """Check your setup in plain words: folders, certificates, AI keys and models, data sources, settings.

    Changes nothing and never prints a key. Exits with 1 when something required is missing or broken.
    """
    from tradingagents.doctor import GROUPS, format_json, format_text, run_doctor

    unknown = [g for g in group or [] if g not in GROUPS]
    if unknown:
        raise typer.BadParameter(f"unknown group {', '.join(unknown)}; choose from {', '.join(GROUPS)}", param_hint="--group")
    report = run_doctor(offline=offline, live_ai=not no_ai_call, groups=group)
    typer.echo(format_json(report) if json_output else format_text(report))
    raise typer.Exit(code=0 if report.ready else 1)


@app.command()
def ui(
    port: Annotated[int, typer.Option("--port", help="Port to listen on (the next free one is used if it is busy)")] = 8765,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open the page in your browser")] = False,
):
    """Open the setup screen in your browser: check the setup, set the AI connection and keys.

    It listens on this computer only. Needs the extra libraries: pip install "tradingagents[ui]"
    """
    try:
        from tradingagents.ui.server import run_server
    except ImportError:
        typer.echo('The setup screen needs two extra libraries. Install them with:  pip install "tradingagents[ui]"', err=True)
        raise typer.Exit(code=1) from None
    run_server(port=port, open_browser=not no_browser)


@app.command()
def shortcut(
    where: Annotated[str, typer.Option("--where", help="desktop (the default), start-menu, or here (the current folder)")] = "desktop",
    folder: Annotated[Path | None, typer.Option("--folder", help="Put it in this folder instead")] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Remove the shortcut this command made")] = False,
):
    """Put a launcher on your desktop, so a double-click opens Analyst Consortium without typing anything.

    It runs the same `tradingagents ui`, from the folder that holds your .env, so your keys, settings and results stay where they are.
    It changes no program files: you can still edit the program, and your edits show the next time it starts. Windows gets a real
    shortcut with the app's icon; macOS gets a .command file and Linux a .desktop entry (written from those systems' documentation,
    not tried on them yet).
    """
    from tradingagents import launcher

    if where not in launcher.WHERES:
        raise typer.BadParameter(f"choose from {', '.join(launcher.WHERES)}", param_hint="--where")
    try:
        if remove:
            gone = launcher.remove(where, folder)
            typer.echo(f"Removed {gone}" if gone else "There was no shortcut there to remove.")
            return
        made = launcher.create(where, folder)
    except launcher.ShortcutError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Created: {made.path}")
    typer.echo(f"Double-click it to open Analyst Consortium. It starts from {made.start}, where your .env is, so your keys and data stay put.")
    typer.echo("If you move the project folder or its Python environment, run this command again. Remove it with:  tradingagents shortcut --remove")
    for note in made.notes:
        typer.echo(f"Note: {note}")


pick_app = typer.Typer(
    name="pick", no_args_is_help=True,
    help="Find stocks worth analyzing: a free, quick check in code before any paid analysis.",
)
app.add_typer(pick_app, name="pick")


def _pick_folder():
    from tradingagents.picker.folders import picker_folder

    return picker_folder()


def _finish_pick(recipe, symbols, overrides, top, details, csv_path, json_output, refresh, no_log, workers):
    """Run the quick look, print it, and save the CSV and the ledger row when asked."""
    import json as _json
    from datetime import date

    from tradingagents.picker import ledger, quicklook, report
    from tradingagents.picker.cache import FundamentalsCache
    from tradingagents.picker.providers import YahooProvider
    from tradingagents.picker.settings import PickerSettings

    try:
        settings = PickerSettings.with_overrides(top=top, **overrides)
        folder = _pick_folder()
        result = quicklook.run(recipe, YahooProvider(), settings, symbols=symbols,
                               cache=FundamentalsCache(folder / "cache", date.today()), workers=workers, refresh=refresh,
                               log=lambda message: typer.echo(message, err=True))
    except (ValueError, quicklook.PickerError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if json_output:
        typer.echo(_json.dumps(report.to_json(result), indent=2))
    else:
        typer.echo(report.render_text(result, settings.top, details))
    if csv_path:
        csv_path.write_bytes(report.to_csv(result))
        typer.echo(f"Saved all {len(result.rows)} stocks to {csv_path}", err=True)
    if not no_log and result.rows:
        added = ledger.append_picks(folder / "picks.csv", result, settings.top)
        typer.echo(f"Recorded the top {added} in {folder / 'picks.csv'} (use --no-log to skip).", err=True)


@pick_app.command("beaten-down")
def pick_beaten_down(
    below_high: Annotated[float | None, typer.Option("--below-high", help="At least this far below the 52-week high, as a fraction (0.20 = 20%)")] = None,
    min_cap: Annotated[float | None, typer.Option("--min-cap", help="Smallest market value in dollars (default 2 billion)")] = None,
    min_volume: Annotated[float | None, typer.Option("--min-volume", help="Smallest average daily volume in shares (default 500,000)")] = None,
    universe: Annotated[int | None, typer.Option("--universe", help="Screen the largest this many stocks (default 800)")] = None,
    top: Annotated[int | None, typer.Option("--top", help="How many to show (default 15)")] = None,
    details: Annotated[int, typer.Option("--details", help="How many get a full write-up")] = 3,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Also save every stock checked to this CSV file")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON instead of text, for programs")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Download the fundamentals again instead of using today's saved copy")] = False,
    no_log: Annotated[bool, typer.Option("--no-log", help="Do not add this list to the picks record")] = False,
    workers: Annotated[int, typer.Option("--workers", help="Downloads at the same time")] = 6,
):
    """Beaten-down quality: stocks well below their highs where the market explains the fall, then checked against the
    analysis's five deal conditions. Free and takes under a minute; it spends nothing on AI.
    """
    _finish_pick("beaten-down", None, {"below_high": below_high, "min_market_cap": min_cap, "min_avg_volume": min_volume,
                                       "universe_limit": universe}, top, details, csv_path, json_output, refresh, no_log, workers)


@pick_app.command("list")
def pick_list(
    tickers: Annotated[list[str] | None, typer.Argument(help="Stock symbols, for example NVDA MU TSM")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A CSV or text file of symbols (a column called symbol or ticker, or one per line)")] = None,
    top: Annotated[int | None, typer.Option("--top", help="How many to show (default 15)")] = None,
    details: Annotated[int, typer.Option("--details", help="How many get a full write-up")] = 3,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Also save every stock checked to this CSV file")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON instead of text, for programs")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Download the fundamentals again instead of using today's saved copy")] = False,
    no_log: Annotated[bool, typer.Option("--no-log", help="Do not add this list to the picks record")] = False,
    workers: Annotated[int, typer.Option("--workers", help="Downloads at the same time")] = 6,
):
    """Check stocks you name (typed here, or from a file) the same way, and rank them. Free; no AI calls."""
    from tradingagents.picker.lists import clean_symbol, read_symbols_file

    symbols: list[str] = []
    skipped: list[str] = []
    for text in tickers or []:
        symbol = clean_symbol(text)
        (symbols if symbol else skipped).append(symbol or text)
    if file is not None:
        try:
            found, bad = read_symbols_file(file)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None
        symbols += [s for s in found if s not in symbols]
        skipped += bad
    if skipped:
        typer.echo(f"Skipped (not usable as symbols): {', '.join(skipped[:10])}{' ...' if len(skipped) > 10 else ''}", err=True)
    if not symbols:
        typer.echo("Error: name at least one stock, for example: tradingagents pick list NVDA MU TSM", err=True)
        raise typer.Exit(code=1)
    _finish_pick("list", symbols, {}, top, details, csv_path, json_output, refresh, no_log, workers)


@pick_app.command("scoreboard")
def pick_scoreboard(
    log_file: Annotated[Path | None, typer.Option("--log", help="The picks record to read (default: data/picker/picks.csv)")] = None,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Also save every recorded stock with its outcomes to this CSV file")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON instead of text, for programs")] = False,
):
    """How the recorded lists did: each stock's price when it was shown against its price later, and against the S&P 500
    over the same days. Free: it only downloads prices. It is facts and counts, never advice.
    """
    import json as _json

    from tradingagents.picker import ledger, quicklook, scoreboard
    from tradingagents.picker.providers import YahooProvider

    path = log_file or _pick_folder() / "picks.csv"
    try:
        board = scoreboard.build(ledger.read_picks(path), YahooProvider(), log=lambda message: typer.echo(message, err=True))
    except quicklook.PickerError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(_json.dumps(scoreboard.to_json(board), indent=2) if json_output else scoreboard.render_text(board))
    if csv_path:
        csv_path.write_bytes(scoreboard.to_csv(board))
        typer.echo(f"Saved {len(board.picks)} stocks to {csv_path}", err=True)


if __name__ == "__main__":
    app()
