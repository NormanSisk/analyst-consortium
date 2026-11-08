"""The picker's answer as plain text, a spreadsheet file and JSON (a local addition).

The text is written to be read at a large size: a short table to choose from, then a block of plain sentences per stock with
the five conditions as PASS, FAIL or UNKNOWN in words (never color alone).
"""

from __future__ import annotations

import csv
import io

from tradingagents.csv_safe import safe_cell
from tradingagents.picker.conditions import money
from tradingagents.picker.quicklook import PickResult, PickRow

CSV_COLUMNS = ("rank", "symbol", "name", "sector", "industry", "price", "market_cap", "conditions_met", "score", "below_high_pct",
               "move_63d_pct", "move_21d_pct", "beta", "market_explains_63d", "market_explains_21d", "sector_fund",
               "sector_driven", "low_pe_check", "forward_pe", "earnings_yield_pct", "peers_used",
               "a_status", "a_detail", "b_status", "b_detail", "c_status", "c_detail", "d_status", "d_detail", "e_status", "e_detail", "note")


def _pct(value, digits: int = 0, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"


def _explains(row: PickRow, sessions: int) -> str:
    w = row.metrics.window(sessions) if row.metrics else None
    if w is None or not w.fell:
        return "no fall"
    if w.share is None:
        return "n/a"
    return ">100%" if w.share > 1 else _pct(w.share)


def render_text(result: PickResult, top: int | None = None, details: int = 3) -> str:
    """The whole answer as text: market, funnel, warnings, a table of the top stocks, then details for the first few."""
    top = top or result.settings.top
    lines = ["", f"{'BEATEN-DOWN QUALITY' if result.recipe == 'beaten-down' else 'YOUR LIST'}  (prices as of {result.as_of})", ""]
    m = result.market
    lines.append(f"Market: the S&P 500 is {-m.drawdown * 100:.1f}% below its high ({m.label})"
                 + (f"; VIX {m.vix:.1f}, above {m.vix_rank * 100:.0f}% of the past year" if m.vix is not None and m.vix_rank is not None else "") + ".")
    lines += ["", "HOW MANY STOCKS AT EACH STEP"]
    lines += [f"  {count:>5}  {label}" for label, count in result.funnel]
    if result.warnings:
        lines += ["", "NOTES"] + [f"  - {w}" for w in result.warnings]
    if not result.rows:
        return "\n".join(lines + ["", "No stocks to show. Loosen a setting (for example --below-high) and try again.", ""])
    shown = result.rows[:top]
    lines += ["", f"THE TOP {len(shown)} (most conditions met first; the score only breaks ties)", "",
              f"{'#':>2}  {'SYMBOL':<7}{'MET':<8}{'BELOW HIGH':<12}{'MARKET EXPLAINS 63d / 21d':<27}{'FWD P/E':<9}FLAGS"]
    for rank, r in enumerate(shown, 1):
        flags = []
        if r.sector_driven:
            flags.append("SECTOR-DRIVEN")
        if r.low_pe:
            flags.append("LOW P/E: CHECK")
        if r.note:
            flags.append("DATA TROUBLE")
        lines.append(f"{rank:>2}  {r.symbol:<7}{str(r.met) + ' of 5':<8}{_pct(-r.metrics.off_high) if r.metrics else 'n/a':<12}"
                     f"{_explains(r, 63) + ' / ' + _explains(r, 21):<27}{(f'{r.facts.forward_pe:.1f}x' if r.facts and r.facts.forward_pe else 'n/a'):<9}{', '.join(flags)}")
    for rank, r in enumerate(shown[:details], 1):
        lines += ["", f"{rank}. {r.symbol}  {r.name}  - meets {r.met} of 5", f"   {r.sector}{' / ' + r.industry if r.industry else ''}; "
                  f"market value {money(r.market_cap)}; price {r.price:.2f}, {_pct(-r.metrics.off_high) if r.metrics else 'n/a'} below its 52-week high."]
        if r.sector_driven:
            w = max((x for x in (r.sector_windows or ()) if x.fell and x.share is not None), key=lambda x: x.share)
            lines.append(f"   SECTOR-DRIVEN: {r.sector_label} explains {_pct(w.share)} of the {w.sessions}-session fall, so this is more a sector selloff than a market panic.")
        if r.low_pe:
            lines.append(f"   LOW P/E: CHECK: a forward P/E of {r.facts.forward_pe:.1f}x is so low that the market probably expects earnings to fall "
                         "(for example at the top of a cycle), so the valuation check may flatter this stock.")
        for code in "abcde":
            c = r.conditions[code]
            lines.append(f"   {c.status:<8}({code}) {c.title}: {c.detail}")
        if r.note:
            lines.append(f"   DATA TROUBLE: {r.note}")
    lines += ["", "This is a quick, free check in code. It does not read filings, news or the debate. Run the full analysis on the few you choose.", ""]
    return "\n".join(lines)


def to_rows(result: PickResult) -> list[dict]:
    rows = []
    for rank, r in enumerate(result.rows, 1):
        m, f = r.metrics, r.facts
        row = {"rank": rank, "symbol": r.symbol, "name": r.name, "sector": r.sector, "industry": r.industry,
               "price": round(r.price, 2) if r.price is not None else None, "market_cap": r.market_cap, "conditions_met": r.met,
               "score": round(r.score, 3), "below_high_pct": round(-m.off_high * 100, 1) if m else None,
               "move_63d_pct": round(m.w63.stock * 100, 1) if m and m.w63.stock is not None else None,
               "move_21d_pct": round(m.w21.stock * 100, 1) if m and m.w21.stock is not None else None,
               "beta": round(m.beta, 2) if m and m.beta is not None else None,
               "market_explains_63d": round(m.w63.share, 2) if m and m.w63.share is not None else None,
               "market_explains_21d": round(m.w21.share, 2) if m and m.w21.share is not None else None,
               "sector_fund": r.sector_label, "sector_driven": "yes" if r.sector_driven else "no", "low_pe_check": "yes" if r.low_pe else "no",
               "forward_pe": round(f.forward_pe, 1) if f and f.forward_pe else None,
               "earnings_yield_pct": round(100 / f.forward_pe, 1) if f and f.forward_pe and f.forward_pe > 0 else None,
               "peers_used": r.peers, "note": r.note, "explains_63d_text": _explains(r, 63), "explains_21d_text": _explains(r, 21)}
        for code in "abcde":
            row[f"{code}_status"], row[f"{code}_detail"] = r.conditions[code].status, r.conditions[code].detail
        rows.append(row)
    return rows


def to_csv(result: PickResult) -> bytes:
    """All the stocks as a CSV file (UTF-8 with a byte-order mark, text defused for spreadsheets)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    numeric = {"rank", "price", "market_cap", "conditions_met", "score", "below_high_pct", "move_63d_pct", "move_21d_pct", "beta",
               "market_explains_63d", "market_explains_21d", "forward_pe", "earnings_yield_pct"}
    for row in to_rows(result):
        writer.writerow([("" if row[c] is None else row[c]) if c in numeric else safe_cell(row[c]) for c in CSV_COLUMNS])
    return ("﻿" + out.getvalue()).encode("utf-8")


def to_json(result: PickResult) -> dict:
    return {"recipe": result.recipe, "as_of": result.as_of.isoformat() if result.as_of else None,
            "market": {"drawdown": result.market.drawdown, "label": result.market.label, "vix": result.market.vix,
                       "vix_rank": result.market.vix_rank},
            "treasury_yield": result.treasury, "funnel": [{"step": label, "count": count} for label, count in result.funnel],
            "warnings": result.warnings, "rows": to_rows(result)}
