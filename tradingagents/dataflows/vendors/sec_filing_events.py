"""SEC filing events: what the company has filed that statements do not show yet (a local addition).

From EDGAR, free and dated by filing date, so a run dated in the past is served only what had been filed by then:

* Form 8-K events by item (a notice of delisting, an auditor change, a sale of unregistered equity, a material
  agreement, an officer's departure, a shareholder vote and so on), newest first.
* Registration and prospectus filings (S-1, S-3, 424B prospectus supplements), late-filing notices (NT 10-K,
  NT 10-Q) and amended periodic reports (10-K/A, 10-Q/A).
* Schedules 13D and 13G filed about the company in the last year (holders of 5% or more).
* The share count on the cover of each periodic report, and how it grew over a year (dilution).

The report describes filings; it does not say how an event should be read. Items that usually matter for a
small company's survival or dilution are flagged, and the agreements and other corporate events are listed beside
them so the picture is not only the cautionary side.
"""

from datetime import date, timedelta
from typing import Annotated

import requests

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import sec_edgar

WINDOW_DAYS = 180
HOLDER_WINDOW_DAYS = 365
EIGHT_K_ROWS = 12
HOLDER_ROWS = 8
SHARE_GROWTH_FLAG = 0.10
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_HOLDER_FORMS = "SC 13D,SC 13D/A,SC 13G,SC 13G/A,SCHEDULE 13D,SCHEDULE 13D/A,SCHEDULE 13G,SCHEDULE 13G/A"

ITEMS = {
    "1.01": "Entry into a material definitive agreement", "1.02": "Termination of a material definitive agreement",
    "1.03": "Bankruptcy or receivership", "1.05": "Material cybersecurity incident",
    "2.01": "Completion of an acquisition or disposition", "2.02": "Results of operations (earnings release)",
    "2.03": "Creation of a direct financial obligation", "2.04": "Triggering event that accelerates an obligation",
    "2.05": "Exit or disposal costs", "2.06": "Material impairments",
    "3.01": "Notice of delisting or failure to meet a listing standard", "3.02": "Unregistered sales of equity securities",
    "3.03": "Material modification of shareholder rights", "4.01": "Change of auditor",
    "4.02": "Non-reliance on previously issued financial statements", "5.01": "Change in control",
    "5.02": "Departure or appointment of directors or officers", "5.03": "Amendment of the charter or bylaws",
    "5.07": "Results of a shareholder vote", "7.01": "Regulation FD disclosure", "8.01": "Other events",
}
DISTRESS_ITEMS = ("1.03", "2.04", "2.06", "3.01", "4.01", "4.02", "5.01")
FINANCING_ITEMS = ("2.03", "3.02", "3.03")
_REGISTRATION_PREFIXES = ("S-1", "S-3", "F-1", "F-3", "424B")


def _filings(submissions: dict) -> list[dict]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "reportDate", "items", "accessionNumber")
    columns = [recent.get(key) or [] for key in keys]
    return [dict(zip(keys, row, strict=False)) for row in zip(*columns, strict=False)]


def _items_text(codes: str) -> str:
    parts = [f"{code} {ITEMS[code]}" for code in (c.strip() for c in codes.split(",")) if code in ITEMS]
    return "; ".join(parts)


def _holder_filings(cik: str, as_of: str) -> list[dict] | None:
    """Schedule 13D and 13G filings about the company in the last year, from EDGAR's search; None if it fails."""
    start = (date.fromisoformat(as_of) - timedelta(days=HOLDER_WINDOW_DAYS)).isoformat()
    try:
        response = requests.get(_SEARCH_URL, params={"forms": _HOLDER_FORMS, "ciks": cik, "dateRange": "custom",
                                                     "startdt": start, "enddt": as_of},
                                headers={"User-Agent": sec_edgar._user_agent()}, timeout=30)
        response.raise_for_status()
        hits = response.json().get("hits", {}).get("hits", [])
    except (requests.RequestException, ValueError):
        return None
    found = []
    for hit in hits:
        source = hit.get("_source", {})
        names = [n.split("  (")[0].strip() for n in source.get("display_names", [])]
        found.append({"form": source.get("form", ""), "filed": source.get("file_date", ""),
                      "filer": ", ".join(names[1:3]) or (names[0] if names else "")})
    return sorted(found, key=lambda f: f["filed"], reverse=True)


def _share_counts(cik: str, as_of: str) -> list[tuple[str, int, str]] | None:
    """(period end, shares on the cover, filed) from the periodic reports filed by ``as_of``, oldest first."""
    try:
        facts = sec_edgar._cached_json(sec_edgar._FACTS_URL.format(cik=cik), f"CIK{cik}.json")
    except VendorUnavailableError:
        return None
    values = ((facts.get("facts") or {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
              .get("units", {}).get("shares", []))
    latest: dict[str, tuple[str, int]] = {}
    for fact in values:
        if (fact.get("filed", "9999") <= as_of and fact.get("val")
                and (fact["end"] not in latest or fact["filed"] > latest[fact["end"]][0])):
            latest[fact["end"]] = (fact["filed"], int(fact["val"]))
    return [(end, val, filed) for end, (filed, val) in sorted(latest.items())]


def build_report(symbol: str, filings: list[dict], holders: list[dict] | None, shares: list | None, as_of: str) -> str:
    """The report from the filing list; ``holders`` and ``shares`` are None when their source failed."""
    since = (date.fromisoformat(as_of) - timedelta(days=WINDOW_DAYS)).isoformat()
    recent = [f for f in filings if since <= f["filingDate"] <= as_of]
    flags: list[str] = []
    sections = []

    events = [(f["filingDate"], f["items"]) for f in recent if f["form"] in ("8-K", "8-K/A") and f.get("items")]
    rows = ["| Filed | Items |", "|---|---|"]
    for filed, codes in sorted(events, reverse=True)[:EIGHT_K_ROWS]:
        text = _items_text(", ".join(c for c in codes.split(",") if c.strip() != "9.01"))
        if text:
            rows.append(f"| {filed} | {text} |")
    sections.append(f"## Form 8-K events, last {WINDOW_DAYS} days ({len(events)} filed)\n"
                    + ("\n".join(rows) if len(rows) > 2 else "None with an item that carries news."))
    for label, codes in (("Distress-type", DISTRESS_ITEMS), ("Financing-type", FINANCING_ITEMS)):
        hits = [f"{code} ({ITEMS[code]}) on {', '.join(sorted(d for d, c in events if code in c.split(',')))}"
                for code in codes if any(code in c.split(",") for _, c in events)]
        if hits:
            flags.append(f"{label} Form 8-K items in the last {WINDOW_DAYS} days: " + "; ".join(hits) + ".")

    registrations = [f for f in recent if f["form"].startswith(_REGISTRATION_PREFIXES)]
    late = [f for f in filings if f["form"].startswith("NT ") and f["filingDate"] >= (date.fromisoformat(as_of) - timedelta(days=365)).isoformat() and f["filingDate"] <= as_of]
    amended = [f for f in recent if f["form"] in ("10-K/A", "10-Q/A")]
    lines = []
    if registrations:
        counts: dict[str, int] = {}
        for f in registrations:
            counts[f["form"]] = counts.get(f["form"], 0) + 1
        latest = max(f["filingDate"] for f in registrations)
        lines.append("- Registration and prospectus filings: " + ", ".join(f"{form} x{n}" for form, n in sorted(counts.items()))
                     + f" (latest {latest}).")
        flags.append(f"{len(registrations)} registration or prospectus filings in the last {WINDOW_DAYS} days "
                     f"({', '.join(sorted(counts))}; latest {latest}): these usually accompany a stock or debt offering or a "
                     "resale registration, and the filing alone does not say how much was sold.")
    else:
        lines.append(f"- No registration or prospectus filings in the last {WINDOW_DAYS} days.")
    for f in late:
        lines.append(f"- Late-filing notice: {f['form']} filed {f['filingDate']}.")
    if late:
        flags.append(f"A late-filing notice ({late[0]['form']}) was filed on {late[0]['filingDate']} (within the last year).")
    for f in amended:
        lines.append(f"- Amended periodic report: {f['form']} filed {f['filingDate']} for the period ending {f.get('reportDate') or 'n/a'}.")
    if amended:
        flags.append(f"{amended[0]['form']} was filed on {amended[0]['filingDate']}: an amended periodic report can mean a correction.")
    periodic = [f for f in filings if f["form"] in ("10-K", "10-Q") and f["filingDate"] <= as_of]
    for form in ("10-K", "10-Q"):
        last = next((f for f in sorted(periodic, key=lambda f: f["filingDate"], reverse=True) if f["form"] == form), None)
        if last:
            lines.append(f"- Latest {form}: period ending {last.get('reportDate') or 'n/a'}, filed {last['filingDate']}.")
    sections.append("## Offerings, late notices and periodic reports\n" + "\n".join(lines))

    if holders is None:
        sections.append("## Holders of 5% or more\nEDGAR's search could not be reached this time.")
    else:
        if holders:
            table = ["| Filed | Form | Filer |", "|---|---|---|"] + [f"| {h['filed']} | {h['form']} | {h['filer']} |"
                                                                    for h in holders[:HOLDER_ROWS]]
            body = "\n".join(table) + ("" if len(holders) <= HOLDER_ROWS else f"\n({len(holders) - HOLDER_ROWS} more not shown.)")
        else:
            body = "No Schedule 13D or 13G filings about the company in the last year."
        sections.append(f"## Holders of 5% or more (Schedules 13D and 13G filed in the last {HOLDER_WINDOW_DAYS} days)\n" + body
                        + "\nA Schedule 13G is a passive holder's filing and a 13D states an intention to influence or control; "
                          "the percentage held is in the filing, not here.")
        activist = [h for h in holders if "13D" in h["form"] and h["filed"] >= since]
        if activist:
            flags.append(f"A Schedule 13D (the filer states an intention to influence or control) was filed on "
                         f"{activist[0]['filed']} by {activist[0]['filer']}.")

    if shares is None:
        sections.append("## Share count\nEDGAR's company facts could not be reached this time.")
    elif not shares:
        sections.append("## Share count\nNo cover-page share counts are on file.")
    else:
        shown = shares[-4:]
        body = ["- Shares on the cover of each periodic report: " + ", ".join(f"{val:,} ({end})" for end, val, _ in shown) + "."]
        newest_end, newest, _ = shares[-1]
        target = (date.fromisoformat(newest_end) - timedelta(days=365)).isoformat()
        older = [s for s in shares[:-1] if abs((date.fromisoformat(s[0]) - date.fromisoformat(target)).days) <= 75]
        if older:
            base_end, base, _ = min(older, key=lambda s: abs((date.fromisoformat(s[0]) - date.fromisoformat(target)).days))
            growth = newest / base - 1
            body.append(f"- {newest:,} on {newest_end} against {base:,} on {base_end}: {growth * 100:+.1f}% over about a year.")
            if growth >= SHARE_GROWTH_FLAG:
                flags.append(f"The share count grew {growth * 100:.1f}% in about a year ({base:,} to {newest:,}): dilution.")
        else:
            body.append("- Fewer than a year of cover-page counts are on file, so no yearly change is worked out.")
        sections.append("## Share count\n" + "\n".join(body))

    sections.append("## Flags to state in the report\n" + ("\n".join(f"- {f}" for f in flags) if flags else "No rule-based flags."))
    return (f"# SEC filing events for {symbol} (EDGAR filings through {as_of}; window {since} to {as_of})\n\n"
            "Every item is dated by its filing date. A filing says what was reported, not how it should be read.\n\n"
            + "\n\n".join(sections))


def get_filing_events(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Filing events for ``ticker`` from SEC EDGAR as filed by ``as_of_date``."""
    canonical = normalize_symbol(ticker)
    as_of = as_of_date or get_current_date()
    cik = sec_edgar.cik_for(canonical)
    if cik is None:
        raise NoMarketDataError(ticker, canonical, "not a US SEC filer (no EDGAR record)")
    filings = _filings(sec_edgar._cached_json(_SUBMISSIONS_URL.format(cik=cik), f"submissions-CIK{cik}.json"))
    return build_report(canonical, filings, _holder_filings(cik, as_of), _share_counts(cik, as_of), as_of)
