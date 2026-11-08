"""A one-day cache of the fundamentals the picker downloads (a local addition).

A stock's quarterly statements and estimates do not change within a day, so a second run the same day reads them from disk
(a hundred stocks in a blink instead of a minute). One JSON file per stock under ``<folder>/<date>/``. Prices are not cached:
they download in a few seconds for the whole list and must be current.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

_SAFE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


class FundamentalsCache:
    def __init__(self, folder: str | Path | None, day: date) -> None:
        self.folder = None if folder is None else Path(folder) / day.isoformat()

    def _path(self, symbol: str) -> Path | None:
        return self.folder / f"{symbol}.json" if self.folder is not None and _SAFE.match(symbol) else None

    def get(self, symbol: str) -> dict | None:
        path = self._path(symbol)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def put(self, symbol: str, raw: dict) -> None:
        """Save a stock's data. A download that failed is not saved, so the next run tries again."""
        path = self._path(symbol)
        if path is None or raw.get("error"):
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass                                   # the cache is a convenience; losing a write costs only time
