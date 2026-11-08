"""Run the analyst track-record study on Yahoo data (a local addition).

    python -m tradingagents.research.analyst_study               # about 77 large US-listed stocks
    python -m tradingagents.research.analyst_study --tickers NVDA AMD TSM
    python -m tradingagents.research.analyst_study --refresh     # download again instead of using the saved copy

It makes no AI calls and costs nothing. The first run downloads about 77 rating histories and the price history from
mid-2011; the downloads are saved under ``raw/`` so later runs (and changes to the statistics) take seconds. It writes
``report.md`` and the tables behind it next to the results folder, in ``research/analyst-track-record``.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.vendors.yahoo.analyst_history import (
    fetch_price_history,
    fetch_rating_history,
    fetch_split_history,
)
from tradingagents.default_config import build_default_config
from tradingagents.research.track_record import StudyResult, render_report, run_study

BENCHMARK = "SPY"
DEFAULT_START = "2011-06-01"
UNIVERSE = """AAPL MSFT GOOGL AMZN META NVDA AVGO TSM ASML ORCL CRM NOW ADBE AMD QCOM TXN AMAT LRCX KLAC MU INTU PANW CRWD PLTR
ANET CSCO IBM SNPS CDNS NFLX UBER SHOP LLY NVO UNH ABBV MRK JNJ ISRG TMO VRTX REGN AMGN SYK JPM V MA GS MS BRK-B AXP SCHW
COST WMT HD MCD NKE SBUX PG KO PEP LULU CAT GE DE HON ETN RTX LMT PH URI XOM CVX NEE VST CEG TSLA""".split()


def default_output_dir() -> Path:
    return Path(build_default_config()["results_dir"]).parent / "research" / "analyst-track-record"


def load_data(tickers: list[str], raw_dir: Path, start: str, refresh: bool) -> tuple[dict, dict, dict]:
    """Rating histories, prices and stock splits, from the saved copy when there is one."""
    saved = raw_dir / "data.pkl"
    if saved.exists() and not refresh:
        with open(saved, "rb") as handle:
            data = pickle.load(handle)                    # noqa: S301 - our own file, written below
        if set(tickers) <= set(data["histories"]) and data["start"] == start and "splits" in data:
            print(f"Using the saved download from {saved}", file=sys.stderr)
            return {t: data["histories"][t] for t in tickers}, data["prices"], data["splits"]
    histories: dict[str, pd.DataFrame] = {}
    splits: dict[str, pd.Series] = {}
    for i, symbol in enumerate(tickers, 1):
        try:
            histories[symbol] = fetch_rating_history(symbol)
            splits[symbol] = fetch_split_history(symbol)
        except Exception as exc:  # noqa: BLE001 - one missing stock must not end the study
            histories.pop(symbol, None)
            print(f"  {symbol}: no rating history ({type(exc).__name__})", file=sys.stderr)
            continue
        print(f"  {i}/{len(tickers)} {symbol}: {len(histories[symbol])} actions, {len(splits[symbol])} splits", file=sys.stderr)
    prices = fetch_price_history([*tickers, BENCHMARK], start)
    raw_dir.mkdir(parents=True, exist_ok=True)
    with open(saved, "wb") as handle:
        pickle.dump({"histories": histories, "prices": prices, "splits": splits, "start": start}, handle)
    return histories, prices, splits


def write_tables(result: StudyResult, out: Path) -> None:
    for horizon, outcomes in result.outcomes.items():
        outcomes.to_csv(out / f"rating_calls_{horizon}d.csv", index=False)
        result.firm_table[horizon].to_csv(out / f"firm_rating_skill_{horizon}d.csv")
    if result.targets is not None:
        result.targets.drop(columns=["quarter"]).to_csv(out / "price_targets.csv", index=False)
    if result.target_firms is not None:
        result.target_firms.to_csv(out / "firm_target_skill.csv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Study how well analysts' rating changes and price targets did, by firm.")
    parser.add_argument("--tickers", nargs="+", default=UNIVERSE, help="stocks to study (default: about 77 large US-listed stocks)")
    parser.add_argument("--out", type=Path, default=None, help="folder for the report and tables")
    parser.add_argument("--start", default=DEFAULT_START, help="first day of price history to download")
    parser.add_argument("--refresh", action="store_true", help="download again instead of using the saved copy")
    args = parser.parse_args(argv)
    out = args.out or default_output_dir()
    out.mkdir(parents=True, exist_ok=True)
    tickers = [t.upper() for t in args.tickers]
    histories, prices, splits = load_data(tickers, out / "raw", args.start, args.refresh)
    if BENCHMARK not in prices:
        print(f"No prices for {BENCHMARK}; cannot measure excess returns.", file=sys.stderr)
        return 1
    result = run_study(histories, prices, BENCHMARK, splits=splits)
    report = render_report(result, universe=len(tickers))
    (out / "report.md").write_text(report, encoding="utf-8")
    write_tables(result, out)
    print(report)
    print(f"\nSaved in {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
