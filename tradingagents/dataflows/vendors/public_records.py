"""Public records about the company: federal contract awards, clinical trials and FDA recalls (a local addition).

Free, keyless government sources that show revenue and risk before a statement does:

* USAspending.gov: federal contract awards to recipients whose name matches the company, with the largest listed.
  Dated by award start date, so a run dated in the past is served as of its date. Defense contracts can post up to
  about 90 days late.
* ClinicalTrials.gov: studies the company sponsors that are recruiting, by phase (current status only, so it is not
  served to past runs). Only for companies Yahoo files under health care.
* openFDA drug enforcement reports (recalls) naming the company in the last 12 months, dated by report date.

The company name comes from SEC's ticker table, so a company that sells through a prime contractor, or trades under
a name different from its registered one, will not appear. Absence here is not absence of a business.
"""

import re
from datetime import date, timedelta
from typing import Annotated

import requests
import yfinance as yf

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import sec_edgar
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

_HEADERS = {"User-Agent": "Mozilla/5.0 (TradingAgents research)"}
_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_COUNT_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award_count/"
_TRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
_RECALLS_URL = "https://api.fda.gov/drug/enforcement.json"
AWARDS_SHOWN = 5
_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|llc|lp|sa|nv|ag|de|new|"
                     r"class [a-c]|common stock)\b\.?", re.I)


def company_search_name(title: str) -> str:
    """A registered name as a search string: 'KOPIN CORP' -> 'Kopin', 'QUALCOMM INC/DE' -> 'Qualcomm'."""
    cleaned = _SUFFIX.sub(" ", title.replace("/", " "))
    cleaned = re.sub(r"[^A-Za-z0-9&' -]", " ", cleaned)
    return " ".join(word.capitalize() if word.isupper() else word for word in cleaned.split())


def _sec_title(symbol: str) -> str | None:
    table = sec_edgar._cached_json(sec_edgar._TICKERS_URL, "company_tickers.json")
    return next((e["title"] for e in table.values() if e.get("ticker", "").upper() == symbol), None)


def _request(method: str, url: str, **kwargs):
    try:
        return requests.request(method, url, headers=_HEADERS, timeout=40, **kwargs)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"{url.split('/')[2]} request failed ({type(exc).__name__})") from exc


def _awards(name: str, as_of: str) -> dict | None:
    """Federal contract awards in the 12 months to ``as_of`` to recipients matching ``name``; None if unreachable."""
    start = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    # new_awards_only: awards that began in the window, not older awards that merely had a modification in it.
    filters = {"recipient_search_text": [name],
               "time_period": [{"start_date": start, "end_date": as_of, "date_type": "new_awards_only"}],
               "award_type_codes": ["A", "B", "C", "D"]}
    try:
        counted = _request("POST", _COUNT_URL, json={"filters": filters})
        listed = _request("POST", _AWARDS_URL, json={
            "filters": filters, "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date", "Description"],
            "sort": "Award Amount", "order": "desc", "limit": AWARDS_SHOWN, "page": 1})
        if counted.status_code != 200 or listed.status_code != 200:
            return None
        return {"contracts": int((counted.json().get("results") or {}).get("contracts", 0)), "top": listed.json().get("results") or []}
    except (VendorUnavailableError, ValueError):
        return None


def _clinical(name: str, as_of: str) -> dict | None:
    """Recruiting studies the company sponsors, by phase, and FDA drug recalls naming it in the last 12 months."""
    out: dict = {}
    try:
        for key, extra in (("recruiting", {}), ("phase3", {"aggFilters": "phase:3"}), ("phase2", {"aggFilters": "phase:2"})):
            response = _request("GET", _TRIALS_URL, params={"query.spons": name, "filter.overallStatus": "RECRUITING",
                                                            "pageSize": 1, "countTotal": "true", **extra})
            out[key] = response.json().get("totalCount") if response.status_code == 200 else None
        start = (date.fromisoformat(as_of) - timedelta(days=365)).strftime("%Y%m%d")
        search = f'recalling_firm:"{name}" AND report_date:[{start} TO {as_of.replace("-", "")}]'
        for key, query in (("recalls", search), ("class_one", search + ' AND classification:"Class I"')):
            response = _request("GET", _RECALLS_URL, params={"search": query, "limit": 1})
            out[key] = (response.json().get("meta", {}).get("results", {}).get("total", 0) if response.status_code == 200
                        else 0 if response.status_code == 404 else None)
    except (VendorUnavailableError, ValueError):
        return None
    return out


def build_report(symbol: str, name: str, awards: dict | None, clinical: dict | None, clinical_note: str, as_of: str) -> str:
    """The report; ``awards`` or ``clinical`` is None when its source failed, ``clinical_note`` says why it is absent."""
    flags: list[str] = []
    if awards is None:
        award_text = "USAspending could not be reached this time."
    elif awards["contracts"] == 0:
        award_text = (f"No new federal contract awards were found in the last 12 months for recipients matching '{name}'. The "
                      "search matches recipient names, so a company that sells through a prime contractor or under another "
                      "name will not appear.")
    else:
        rows = ["| Start date | Awarding agency | Amount | Recipient | Description |", "|---|---|---|---|---|"]
        for award in awards["top"]:
            description = (award.get("Description") or "").strip().replace("|", "/")[:90]
            rows.append(f"| {award.get('Start Date', 'n/a')} | {award.get('Awarding Agency', 'n/a')} | "
                        f"${float(award.get('Award Amount') or 0):,.0f} | {award.get('Recipient Name', 'n/a')} | {description} |")
        shown = sum(float(a.get("Award Amount") or 0) for a in awards["top"])
        award_text = (f"{awards['contracts']} new federal contract awards in the last 12 months to recipients matching '{name}'; "
                      f"the {len(awards['top'])} largest, ${shown:,.0f} in all:\n" + "\n".join(rows)
                      + "\nAn award's amount is its obligated value so far, not revenue recognized, and defense awards can post "
                        "up to about 90 days late.")
    sections = ["## Federal contract awards (USAspending)\n" + award_text]
    if clinical is not None:
        recruiting = clinical.get("recruiting")
        trial_line = ("ClinicalTrials.gov could not be reached." if recruiting is None else
                      f"Studies sponsored by '{name}' that are recruiting now: {recruiting}"
                      + (f" (phase 3: {clinical.get('phase3')}, phase 2: {clinical.get('phase2')})" if recruiting else "") + ".")
        recalls = clinical.get("recalls")
        recall_line = ("openFDA could not be reached." if recalls is None else
                       f"FDA drug enforcement reports (recalls) naming '{name}' in the last 12 months: {recalls} "
                       f"(Class I, the most serious: {clinical.get('class_one') or 0}).")
        if (clinical.get("class_one") or 0) >= 1:
            flags.append(f"{clinical['class_one']} Class I FDA recall(s) naming the company were reported in the last 12 months.")
        sections.append("## Clinical studies and FDA recalls\n" + trial_line + "\n" + recall_line)
    elif clinical_note:
        sections.append("## Clinical studies and FDA recalls\n" + clinical_note)
    sections.append("## Flags to state in the report\n" + ("\n".join(f"- {f}" for f in flags) if flags else "No rule-based flags."))
    return f"# Public records for {symbol} (searched as '{name}', through {as_of})\n\n" + "\n\n".join(sections)


def get_public_records(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Federal awards, clinical studies and FDA recalls for ``ticker`` from public government sources."""
    canonical = normalize_symbol(ticker)
    as_of = as_of_date or get_current_date()
    title = _sec_title(canonical)
    if not title:
        raise NoMarketDataError(ticker, canonical, "not a US SEC filer, so there is no registered name to search")
    name = company_search_name(title)
    awards = _awards(name, as_of)
    clinical, note = None, ""
    if is_historical(as_of_date):
        note = "Not served to a run dated in the past: ClinicalTrials.gov gives only each study's current status."
    else:
        try:
            info = yf_retry(lambda: yf.Ticker(canonical).info) or {}
        except VendorUnavailableError:
            info = {}
        if info.get("sectorKey") == "healthcare":
            clinical = _clinical(name, as_of)
        else:
            note = "Not searched: Yahoo does not file this company under health care."
    return build_report(canonical, name, awards, clinical, note, as_of)
