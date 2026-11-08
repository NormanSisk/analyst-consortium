"""Reading a list of stocks you name: typed symbols, or a CSV or text file (a local addition).

A file may be one symbol per line, symbols separated by commas or spaces, or a spreadsheet export with a column called
symbol, ticker or similar (the first column is used when there is no such heading). A symbol the program cannot use is
skipped and reported, never guessed.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component

SYMBOL_HEADINGS = {"symbol", "symbols", "ticker", "tickers", "underlying", "instrument", "code"}
MAX_FILE_BYTES = 1_000_000


def clean_symbol(text: str) -> str | None:
    """A symbol as the data layer spells it, or None when it cannot be used."""
    token = str(text or "").strip().strip("\"'").upper()
    if not token:
        return None
    try:
        symbol = normalize_symbol(token)
        safe_ticker_component(symbol)
    except ValueError:
        return None
    return symbol


def parse_symbols(text: str) -> tuple[list[str], list[str]]:
    """(symbols without repeats, in order; the entries that were skipped) from the text of a file."""
    text = text.lstrip("﻿")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return [], []
    delimiter = next((d for d in (",", ";", "\t") if d in lines[0]), None)
    tokens: list[str] = []
    if delimiter:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
        header = [cell.strip().lower() for cell in rows[0]]
        column = next((i for i, cell in enumerate(header) if cell in SYMBOL_HEADINGS), None)
        if column is not None:
            tokens = [row[column] for row in rows[1:] if len(row) > column]
        elif len(rows[0]) > 1 and all(clean_symbol(cell) for cell in rows[0] if cell.strip()):
            tokens = [cell for row in rows for cell in row]             # one line of comma-separated symbols
        else:
            tokens = [row[0] for row in rows if row]
    else:
        tokens = [token for line in lines for token in line.split()]
    symbols, skipped = [], []
    for token in tokens:
        if not token.strip():
            continue
        symbol = clean_symbol(token)
        if symbol is None:
            skipped.append(token.strip()[:20])
        elif symbol not in symbols:
            symbols.append(symbol)
    return symbols, skipped


def read_symbols_file(path: str | Path) -> tuple[list[str], list[str]]:
    """Symbols from a file; raises ValueError (in plain words) when it cannot be read or is too large."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path.name} is larger than 1 MB, which is too big for a list of stocks")
        return parse_symbols(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc.strerror or type(exc).__name__}") from None
