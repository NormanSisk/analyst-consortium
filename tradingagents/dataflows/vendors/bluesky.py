"""Bluesky public post search for ticker-specific discussion.

Reddit ended its RSS feeds on 2026-11-13, so the sentiment analyst reads Bluesky in
its place. Bluesky's documented search endpoint (``app.bsky.feed.searchPosts``)
answers without a key or sign-in, and it can be asked for one day at a time, so a
week of chatter is read evenly. A single page of the newest posts covers only a few
hours of a busy stock such as NVDA.

Three things keep it honest:

- A fetch that fails is reported as ``<unavailable>``, never as "no posts found":
  the two are different claims.
- A post counts only when its text carries the stock's cashtag (``$NVDA``) or hashtag
  (``#NVDA``). The search also matches the company's name, and a short ticker such
  as ``$BE`` would otherwise bring in every post that says "be".
- A post belongs to the window by the time Bluesky's server indexed it (``indexedAt``),
  not by the date its author wrote on it, so a backdated post cannot reach into a
  past run. Engagement counts (likes, reposts) are left out for the same reason: they
  keep growing after the run's date, so for a past run they would leak what came later.

Bluesky keeps its posts, so a past window can be read; a post its author later deleted
is gone, and the service opened to the public on 2024-02-06, so earlier windows say so.
No API key required. Returns a plaintext block ready for prompt injection and never
raises, so callers never special-case missing data.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tradingagents.dataflows.date_window import in_window
from tradingagents.dataflows.symbols import crypto_base
from tradingagents.project import USER_AGENT

logger = logging.getLogger(__name__)

# api.bsky.app is the host Bluesky's own examples use for anonymous reads; the older
# public.api.bsky.app host answers 403 for search.
_API = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_UA = USER_AGENT

_PAGE = 100                 # the most posts one search returns
_DEFAULT_DAYS = 7           # a window with no dates reads the past week, like the analyst's
_MAX_DAYS = 14              # one request per day, so a long window is cut to its last two weeks
_OPENED = date(2024, 2, 6)  # Bluesky opened to the public; earlier windows cannot be answered for
_MAX_BYTES = 5 * 1024 * 1024
_SHOWN_CHARS = 280
_STOP_ON = (401, 403, 429)  # the service is refusing us: more requests would meet the same answer
_SWAMPED_BELOW = 0.3        # a full page with a smaller share of real cashtags is mostly other posts
_NAME_ENDINGS = frozenset({"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "holdings", "group",
                           "nv", "sa", "ag", "llc", "lp", "the", "class", "adr"})


def _symbol(ticker: str) -> str:
    """The symbol searched: a crypto pair (``BTC-USD``) by its base, anything else upper-cased."""
    return crypto_base(ticker) or ticker.strip().upper()


def _mentions(text: str, symbol: str) -> bool:
    """Whether ``text`` carries the cashtag or the hashtag of ``symbol``, not just the word."""
    return re.search(rf"(?<![\w$#])[$#]{re.escape(symbol)}(?!\w)", text or "", re.IGNORECASE) is not None


def _iso(raw) -> datetime | None:
    """Parse an ISO 8601 timestamp to an aware UTC datetime; None when missing or malformed."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _seen_at(post: dict) -> datetime | None:
    """When Bluesky's server indexed the post: a clock the author cannot set."""
    return _iso(post.get("indexedAt")) or _iso((post.get("record") or {}).get("createdAt"))


def _text(post: dict) -> str:
    return str((post.get("record") or {}).get("text") or "")


def _read_capped(resp) -> bytes:
    """Read a response body bounded to ``_MAX_BYTES``, raising on overflow."""
    data = resp.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise http.client.HTTPException(f"Bluesky answer exceeded {_MAX_BYTES} bytes; refusing to parse")
    return data


def _short_name(name: str) -> str:
    """The first two words of a company name without its legal ending: "Bloom Energy Corporation" -> "Bloom Energy"."""
    words = [w for w in re.sub(r"[,.()]", " ", name or "").split() if w.lower() not in _NAME_ENDINGS]
    return " ".join(words[:2])


def _company_name(name_of) -> str:
    """The short company name from the caller's ``name_of``, or "" when there is none or it fails."""
    if name_of is None:
        return ""
    try:
        return _short_name(name_of() or "")
    except Exception as exc:  # noqa: BLE001 - the name only sharpens a search; never fail the fetch over it
        logger.debug("Could not resolve a company name for the Bluesky search: %s", exc)
        return ""


def _share(posts: list[dict], symbol: str) -> float:
    """The share of ``posts`` that carry the symbol's cashtag or hashtag."""
    return sum(_mentions(_text(p), symbol) for p in posts) / len(posts) if posts else 1.0


def _search_day(symbol: str, day: date, timeout: float, name: str = "") -> list[dict]:
    """The newest posts for ``$symbol`` indexed on ``day`` (UTC), at most one page.

    The search drops the ``$``, so a ticker that is also a common word (``$BE``) matches
    every post that says "be". With a company ``name`` the search asks for both, which
    brings the stock's own posts back.
    """
    since = datetime(day.year, day.month, day.day, tzinfo=UTC)
    query = urlencode({
        "q": f"{name} ${symbol}".strip(), "sort": "latest", "limit": _PAGE,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": (since + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    req = Request(f"{_API}?{query}", headers={"User-Agent": _UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(_read_capped(resp))
    posts = data.get("posts") if isinstance(data, dict) else None
    return [p for p in posts if isinstance(p, dict)] if isinstance(posts, list) else []


def _why(exc: Exception) -> str:
    return f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__


def _parse_day(text: str | None) -> date | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _unavailable(reason: str) -> str:
    return f"<Bluesky unavailable: {reason}; this is not an absence of discussion>"


def fetch_bluesky_posts(
    ticker: str,
    limit: int = 30,
    timeout: float = 10.0,
    start_date: str | None = None,
    end_date: str | None = None,
    screen=None,
    name_of=None,
) -> str:
    """Fetch Bluesky posts that carry ``ticker``'s cashtag and return them as a plaintext block.

    The window ``start_date``..``end_date`` (yyyy-mm-dd, both inclusive) is read one day at
    a time, each day's newest 100 matching posts, so a busy day cannot crowd out a quiet one.
    The block heads with posts per day (a ``+`` marks a day that had more than the page read)
    and then lists up to ``limit`` posts, an even share per day, newest first within a day.
    Identical posts (reposting bots) are shown once.

    ``screen``, when given, takes the post texts and returns a keep flag per post and a note
    line that heads the block; it sees only the posts that would be shown.

    ``name_of``, when given, is called (at most once, and only when needed) for the company's
    name. A day whose full page is mostly posts without the cashtag is asked again with the
    name added, which isolates a ticker that is also a common word. A stock that still cannot
    be isolated is reported as unavailable, never as silence.

    A fetch that fails is reported as unavailable. A day that fails while others succeed is
    named, so its missing count is not read as zero.
    """
    symbol = _symbol(ticker)
    today = datetime.now(UTC).date()
    end = _parse_day(end_date) or today
    start = _parse_day(start_date) or end - timedelta(days=_DEFAULT_DAYS)
    label = f"{start}..{end}"
    if end < _OPENED:
        return _unavailable(f"Bluesky was not open to the public before {_OPENED} (window {label})")
    days = [start + timedelta(days=i) for i in range((min(end, today) - start).days + 1)][-_MAX_DAYS:]
    if not days:
        return _unavailable(f"the window {label} is after today")

    start_dt, end_dt = datetime(start.year, start.month, start.day), datetime(end.year, end.month, end.day)
    seen_text: set[str] = set()
    per_day: dict[date, list[tuple[datetime, dict]]] = {}
    capped: set[date] = set()
    swamped: set[date] = set()
    failed: list[date] = []
    reason = ""
    name: str | None = None
    narrowed = False
    for index, day in enumerate(days):
        try:
            raw = _search_day(symbol, day, timeout)
            if len(raw) >= _PAGE and _share(raw, symbol) < _SWAMPED_BELOW:
                if name is None:
                    name = _company_name(name_of)
                if name:
                    raw = _search_day(symbol, day, timeout, name)
                    narrowed = True
                if len(raw) >= _PAGE and _share(raw, symbol) < _SWAMPED_BELOW:
                    swamped.add(day)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers URLError, timeouts and resets; HTTPError is one too.
            logger.warning("Bluesky search failed for $%s on %s: %s", symbol, day, exc)
            reason = reason or _why(exc)
            if isinstance(exc, HTTPError) and exc.code in _STOP_ON:
                failed.extend(days[index:])
                break
            failed.append(day)
            continue
        if len(raw) >= _PAGE:
            capped.add(day)
        kept = []
        for post in sorted(raw, key=lambda p: _seen_at(p) or datetime.min.replace(tzinfo=UTC), reverse=True):
            seen = _seen_at(post)
            text = _text(post)
            fingerprint = " ".join(text.lower().split())
            if not _mentions(text, symbol) or not in_window(seen, start_dt, end_dt) or fingerprint in seen_text:
                continue
            seen_text.add(fingerprint)
            kept.append((seen, post))
        per_day[day] = kept

    if len(failed) == len(days):
        return _unavailable(f"search failed ({reason})")
    missing = (f"<{len(failed)} of {len(days)} days could not be fetched ({reason}): their counts are missing, not zero>"
               if failed else "")
    chosen = [item for day in sorted(per_day) for item in per_day[day][: max(1, math.ceil(limit / len(days)))]]
    if not chosen:
        if failed:
            return _unavailable(f"{len(failed)} of {len(days)} days failed ({reason}) and the rest had no matching posts")
        if swamped:
            return _unavailable(f"the search for ${symbol} returns full pages of other posts (the symbol is also a common word), "
                                "so it cannot isolate this stock")
        if narrowed:
            return _unavailable(f"${symbol} is also a common word, so the search had to include the company name; it found no "
                                "posts, but posts that carry only the cashtag cannot be searched for")
        return f"<no Bluesky posts with the cashtag ${symbol} found within {label}>"

    chosen.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC), reverse=True)
    note = ""
    if screen:
        keep, note = screen([_text(post) for _, post in chosen])
        screened = len(chosen)
        chosen = [item for item, kept in zip(chosen, keep, strict=True) if kept]
        if not chosen:
            return f"{note}\n\n<none of the {screened} Bluesky posts is about ${symbol}>"

    counts = " · ".join(f"{day:%m-%d}: {len(per_day[day])}{'+' if day in capped else ''}" for day in sorted(per_day))
    lines = []
    for seen, post in chosen:
        created = _iso((post.get("record") or {}).get("createdAt")) or seen
        stamp = f"{created:%Y-%m-%dT%H:%MZ}" if created else "?"
        handle = (post.get("author") or {}).get("handle", "?")
        body = " ".join(_text(post).split())
        if len(body) > _SHOWN_CHARS:
            body = body[:_SHOWN_CHARS] + "…"
        lines.append(f"[{stamp} · @{handle}] {body}")
    head = (
        f"Bluesky posts with the cashtag ${symbol}, {label} (read day by day; no engagement counts or stance tags). "
        f"Posts per day: {counts}. A + marks a day with more posts than the {_PAGE} read. "
        f"Shown: {len(chosen)}, an even share per day."
        + (f" ${symbol} is also a common word, so the search had to include the company name: posts that carry only "
           "the cashtag are missing from these counts." if narrowed else "")
    )
    return "\n\n".join(part for part in (note, missing, head, "\n".join(lines)) if part)
