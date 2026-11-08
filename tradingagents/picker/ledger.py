"""A running record of every list the picker has shown (a local addition).

One row per stock per list: when, which recipe, the stock's rank, price and how many conditions it met. Later, comparing each
row's price with the price some weeks on shows which recipes turned up stocks that did well, and which did not. The file
lives in the data folder, not in the project.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from tradingagents.csv_safe import safe_cell
from tradingagents.picker.quicklook import PickResult

COLUMNS = ("logged_at", "as_of", "recipe", "rank", "symbol", "name", "price", "conditions_met", "conditions", "score",
           "off_high_pct", "market_share_63d", "market_share_21d", "forward_pe", "settings")


def _row(result: PickResult, rank: int, row, logged_at: str) -> list:
    m = row.metrics
    return [logged_at, result.as_of.isoformat() if result.as_of else "", result.recipe, rank, safe_cell(row.symbol), safe_cell(row.name),
            f"{row.price:.2f}" if row.price is not None else "", row.met, " ".join(f"{k}:{c.status}" for k, c in row.conditions.items()),
            f"{row.score:.3f}", f"{m.off_high * 100:.1f}" if m else "",
            f"{m.w63.share:.2f}" if m and m.w63.share is not None else "", f"{m.w21.share:.2f}" if m and m.w21.share is not None else "",
            f"{row.facts.forward_pe:.1f}" if row.facts and row.facts.forward_pe else "",
            json.dumps({"below_high": result.settings.below_high, "share_63": result.settings.share_63,
                        "share_21": result.settings.share_21, "min_market_cap": result.settings.min_market_cap})]


def append_picks(path: str | Path, result: PickResult, top: int | None = None, now: datetime | None = None) -> int:
    """Add the top ``top`` stocks of ``result`` to the ledger at ``path`` (made if needed); returns how many rows were added."""
    rows = result.rows[:top] if top else result.rows
    if not rows:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logged_at = (now or datetime.now()).astimezone().isoformat(timespec="seconds")
    fresh = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        if fresh:
            writer.writerow(COLUMNS)
        for rank, row in enumerate(rows, 1):
            writer.writerow(_row(result, rank, row, logged_at))
    return len(rows)


def read_picks(path: str | Path) -> list[dict]:
    """Every row in the ledger, oldest first (an empty list when there is none)."""
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []
