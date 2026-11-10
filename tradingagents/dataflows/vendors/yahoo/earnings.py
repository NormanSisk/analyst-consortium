"""Earnings history and analyst expectations from Yahoo.

Fundamentals analysts otherwise have no figures for whether the last report beat
or missed, so they repeat a headline's "earnings miss" without a number behind it.
This serves the reported-versus-estimated EPS history, the next scheduled report,
and the sell-side price targets and ratings.
"""

from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import YAHOO_HOST, yf_retry

REPORTS_SHOWN = 8


def _earnings_section(canonical: str, as_of_date: str | None) -> list[str]:
    """The reported-versus-estimate table, plus the next scheduled report."""
    history = yf_retry(lambda: yf.Ticker(canonical).get_earnings_dates(limit=REPORTS_SHOWN + 4))
    if history is None or history.empty:
        return []
    when = pd.to_datetime(history.index, utc=True)
    now = pd.Timestamp.now(tz="UTC")
    cutoff = pd.Timestamp(as_of_date, tz="UTC") if is_historical(as_of_date) else now
    reported = history[history["Reported EPS"].notna() & (when < cutoff)].head(REPORTS_SHOWN)
    upcoming = history[history["Reported EPS"].isna() & (when >= now)]

    lines: list[str] = []
    if not reported.empty:
        lines += [
            "## Reported earnings (EPS as Yahoo reports it, which can differ from the GAAP "
            "diluted EPS in the income statement)",
            "",
            "| Report date | EPS estimate | Reported EPS | Surprise |",
            "|---|---:|---:|---:|",
        ]
        for stamp, row in reported.iterrows():
            surprise = row.get("Surprise(%)")
            lines.append(
                f"| {pd.Timestamp(stamp):%Y-%m-%d} | {row.get('EPS Estimate'):.2f} | "
                f"{row['Reported EPS']:.2f} | "
                f"{'n/a' if pd.isna(surprise) else f'{surprise:+.1f}%'} |"
            )
        last_four = reported.head(4)["Surprise(%)"].dropna()
        if len(last_four):
            beats = int((last_four > 0).sum())
            lines += ["", f"Of the last {len(last_four)} reports, {beats} beat the estimate "
                          f"and {len(last_four) - beats} missed."]
    if not upcoming.empty and not is_historical(as_of_date):
        nxt = upcoming.index[-1]
        estimate = upcoming["EPS Estimate"].iloc[-1]
        due = pd.Timestamp(nxt).tz_localize(None).normalize() if pd.Timestamp(nxt).tzinfo is None else \
            pd.Timestamp(nxt).tz_convert("America/New_York").tz_localize(None).normalize()
        today = pd.Timestamp(as_of_date).normalize() if as_of_date else pd.Timestamp.now().normalize()
        days = (due - today).days
        # The weekday is worked out here because a model asked to name it has gotten it wrong.
        when = f" ({due:%A}), " + ("today" if days == 0 else f"{days} day{'s' if days != 1 else ''} from {today:%Y-%m-%d}")
        lines += ["", f"Next scheduled report: {due:%Y-%m-%d}{when}"
                      + ("" if pd.isna(estimate) else f", EPS estimate {estimate:.2f}")
                      + ". Yahoo does not say whether the release is before the open or after the close."]
    return lines


def _analyst_section(canonical: str) -> list[str]:
    """Sell-side price targets and the ratings split (live data, no history)."""
    lines: list[str] = []
    targets = yf_retry(lambda: yf.Ticker(canonical).analyst_price_targets)
    if targets and targets.get("mean"):
        current = targets.get("current")
        upside = f", {(targets['mean'] / current - 1) * 100:+.0f}% from the current price" if current else ""
        lines += [
            "## Analyst price targets (live; opinions, not forecasts)",
            "",
            f"Mean {targets['mean']:.2f}{upside}; median {targets.get('median', float('nan')):.2f}; "
            f"low {targets.get('low', float('nan')):.2f}; high {targets.get('high', float('nan')):.2f}.",
        ]
    ratings = yf_retry(lambda: yf.Ticker(canonical).recommendations_summary)
    if ratings is not None and not ratings.empty:
        now = ratings.iloc[0]
        total = int(now[["strongBuy", "buy", "hold", "sell", "strongSell"]].sum())
        lines += ["", f"Ratings now ({total} analysts): {int(now['strongBuy'])} strong buy, {int(now['buy'])} buy, "
                      f"{int(now['hold'])} hold, {int(now['sell'])} sell, {int(now['strongSell'])} strong sell. "
                      "Sell-side ratings skew positive; treat them as context, not a signal."]
    return lines


REVISION_FLAG_PERCENT = 3.0     # a consensus moved this much in 90 days is being raised or cut, not just nudged
REVISION_FLAG_ANALYSTS = 3      # this many more analysts raising than cutting in 30 days is a direction
ACTION_DAYS = 90
ACTIONS_SHOWN = 8


def _billions(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if number != number else f"${number / 1e9:,.1f}B"


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def estimate_lines(trend, revisions, revenue, actions, labels: dict, price: float | None, now: pd.Timestamp) -> list[str]:
    """The consensus by period, how it has moved, and what analysts have done lately, with rule-based flags.

    ``trend``, ``revisions``, ``revenue`` and ``actions`` are Yahoo's EPS trend, EPS revisions, revenue
    estimate and upgrades/downgrades tables (any may be None); ``labels`` names the periods.
    """
    lines: list[str] = []
    flags: list[str] = []
    if trend is not None and not trend.empty:
        lines += ["| Period | EPS consensus now | 30 days ago | 90 days ago | Change in 90 days | Revenue consensus | "
                  "Revenue growth | Analysts | Raised / cut EPS in 30 days |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for key, label in labels.items():
            if key not in trend.index:
                continue
            row = trend.loc[key]
            now_eps, month, quarter = _num(row.get("current")), _num(row.get("30daysAgo")), _num(row.get("90daysAgo"))
            change = None if now_eps is None or not quarter or quarter <= 0 else (now_eps / quarter - 1) * 100
            rev = revenue.loc[key] if revenue is not None and key in revenue.index else None
            growth = _num(rev.get("growth")) if rev is not None else None
            analysts = _num(rev.get("numberOfAnalysts")) if rev is not None else None
            rev_cell = _billions(rev.get("avg")) if rev is not None else "n/a"
            up = down = None
            if revisions is not None and key in revisions.index:
                up, down = _num(revisions.loc[key].get("upLast30days")), _num(revisions.loc[key].get("downLast30days"))
            lines.append(
                f"| {label} | {'n/a' if now_eps is None else f'{now_eps:.2f}'} | {'n/a' if month is None else f'{month:.2f}'} | "
                f"{'n/a' if quarter is None else f'{quarter:.2f}'} | {'n/a' if change is None else f'{change:+.1f}%'} | {rev_cell} | "
                f"{'n/a' if growth is None else f'{growth * 100:+.1f}%'} | {'n/a' if analysts is None else int(analysts)} | "
                f"{'n/a' if up is None else f'{int(up)} up / {int(down or 0)} down'} |")
            if key in ("0y", "+1y") and change is not None:
                net = (up or 0) - (down or 0)
                counts = f"{int(up or 0)} analysts raised and {int(down or 0)} cut it in the last 30 days"
                if (change >= REVISION_FLAG_PERCENT and net < 0) or (change <= -REVISION_FLAG_PERCENT and net > 0):
                    # The year's consensus can jump when reported quarters, one-time gains included, replace
                    # estimates, with no analyst raising a view.
                    flags.append(f"For {label} the consensus EPS moved {change:+.1f}% over 90 days, but {counts}, so the move "
                                 "probably comes from reported results replacing estimates (one-time items included) and "
                                 "not from analysts changing their views.")
                elif change >= REVISION_FLAG_PERCENT or net >= REVISION_FLAG_ANALYSTS:
                    flags.append(f"Estimates for {label} are being raised: consensus EPS {change:+.1f}% over 90 days, {counts}.")
                elif change <= -REVISION_FLAG_PERCENT or net <= -REVISION_FLAG_ANALYSTS:
                    flags.append(f"Estimates for {label} are being cut: consensus EPS {change:+.1f}% over 90 days, {counts}.")

    if actions is not None and not actions.empty:
        frame = actions.reset_index()
        date_column = frame.columns[0]
        frame[date_column] = pd.to_datetime(frame[date_column], utc=True, errors="coerce").dt.tz_localize(None)
        recent = frame[frame[date_column] >= now - pd.Timedelta(days=ACTION_DAYS)].sort_values(date_column, ascending=False)
        if not recent.empty:
            target_action = recent["priceTargetAction"].astype(str).str.lower() if "priceTargetAction" in recent else pd.Series(dtype=str)
            kind = recent["Action"].astype(str).str.lower() if "Action" in recent else pd.Series(dtype=str)
            lines += ["", f"Analyst actions in the last {ACTION_DAYS} days ({len(recent)}): price target raised "
                          f"{int(target_action.str.startswith('raise').sum())}, lowered {int(target_action.str.startswith('lower').sum())}, "
                          f"maintained {int(target_action.str.startswith(('maintain', 'reiterate')).sum())}, new coverage "
                          f"{int((kind == 'init').sum())}; rating upgrades {int((kind == 'up').sum())}, downgrades {int((kind == 'down').sum())}."]
            latest = recent.drop_duplicates("Firm") if "Firm" in recent else recent
            targets = pd.to_numeric(latest.get("currentPriceTarget"), errors="coerce")
            targets = targets[targets > 0].dropna()
            if len(targets) >= 3:
                spread = f"each firm's latest target ranges from {targets.min():g} to {targets.max():g}, median {targets.median():g}"
                if price:
                    spread += f" ({(targets.median() / price - 1) * 100:+.0f}% from the price)"
                lines.append(f"Among the {len(targets)} firms with a target in that window, {spread}.")
            lines.append("Most recent, newest first:")
            for _, action in recent.head(ACTIONS_SHOWN).iterrows():
                before, after = _num(action.get("priorPriceTarget")), _num(action.get("currentPriceTarget"))
                move = ""
                if after:
                    verb = str(action.get("priceTargetAction", "")).lower()
                    move = (f", target {verb} {before:g} to {after:g}".replace("  ", " ") if before
                            else f", target {after:g}" + (" (new coverage)" if str(action.get("Action", "")).lower() == "init" else ""))
                grade = action.get("ToGrade") or ""
                prior = f" (from {action.get('FromGrade')})" if action.get("FromGrade") and action.get("FromGrade") != action.get("ToGrade") else ""
                lines.append(f"- {action[date_column]:%Y-%m-%d} {action.get('Firm', '')}: {grade}{prior}{move}")
            lowered = [a for _, a in recent.head(3).iterrows() if str(a.get("priceTargetAction", "")).lower().startswith("lower")]
            if len(lowered) >= 2:
                flags.append(f"{len(lowered)} of the 3 most recent analyst actions lowered the price target.")

    if not lines:
        return []
    out = ["## Analyst estimates and how they are moving (live; consensus, not a forecast)", "", *lines]
    if flags:
        out += ["", "Flags to state in the report:", *[f"- {flag}" for flag in flags]]
    return out


def _estimates_section(canonical: str) -> list[str]:
    """Yahoo's estimate tables for ``canonical``, each fetched on its own so one gap does not lose the rest."""
    def get(attribute):
        try:
            return yf_retry(lambda: getattr(yf.Ticker(canonical), attribute))
        except VendorUnavailableError:
            return None

    info = get("info") or {}
    stamp = _num(info.get("nextFiscalYearEnd"))
    if stamp is None:
        this_year, next_year = "this fiscal year", "next fiscal year"
    else:
        end = pd.Timestamp(stamp, unit="s")
        this_year, next_year = f"this fiscal year (ending {end:%b %Y})", f"next fiscal year (ending {end + pd.DateOffset(years=1):%b %Y})"
    labels = {"0q": "current quarter", "+1q": "next quarter", "0y": this_year, "+1y": next_year}
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    return estimate_lines(get("eps_trend"), get("eps_revisions"), get("revenue_estimate"), get("upgrades_downgrades"),
                          labels, price, pd.Timestamp.now())


def get_earnings_facts(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date in YYYY-MM-DD format"] = None,
):
    """Earnings history, analyst expectations and how they are moving for ``ticker`` from Yahoo.

    Past reports are served up to the analysis date. Price targets, ratings, estimates and
    analyst actions have no historical vintage, so a run dated in the past is not served them.
    """
    canonical = normalize_symbol(ticker)
    sections: list[list[str]] = []
    failures = 0
    builders = (
        lambda: _earnings_section(canonical, as_of_date),
        lambda: [] if is_historical(as_of_date) else _analyst_section(canonical),
        lambda: [] if is_historical(as_of_date) else _estimates_section(canonical),
    )
    for build in builders:
        try:
            sections.append(build())
        except VendorUnavailableError:
            failures += 1
    if failures == len(builders):
        raise VendorUnavailableError("Yahoo Finance earnings and analyst data could not be retrieved")
    body = ["\n".join(section) for section in sections if section]
    if not body:
        if not vendor_reachable(YAHOO_HOST):
            raise VendorUnavailableError("Yahoo Finance is unreachable; earnings data was not retrieved")
        return f"No earnings or analyst data is reported for '{canonical}' (funds and some listings have none)."
    note = "" if not is_historical(as_of_date) else (
        f"\n\nPoint-in-time as of {as_of_date}: price targets, ratings, estimates and analyst actions are withheld "
        "because they have no historical vintage."
    )
    return f"# Earnings and analyst expectations for {canonical}\n\n" + "\n\n".join(body) + note
