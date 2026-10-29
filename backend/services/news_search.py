"""Two caller-supplied services: freenewsapi.io for NEWS, tinyfish.ai for WEB SEARCH.

WHAT EACH IS FOR, AND WHY THEY ARE NOT INTERCHANGEABLE
======================================================
The operator asked for both and was specific about the split:

    freenewsapi.io  NEWS. Joins the four RSS feeds `microstructure_feed`
                    already reads, as a fifth source. It is a HEADLINE feed,
                    which is what `algorithms/news_sentiment.score_headlines`
                    consumes.
    tinyfish.ai     WEB SEARCH ONLY. Not a news feed, not a decision input —
                    it answers a question a human asked. It is wired to the
                    chat/research surface and NOTHING in the trading plane
                    reads it.

THAT SECOND SENTENCE IS A DESIGN CONSTRAINT, NOT A NOTE. A web search is a
live, unbounded, non-replayable fetch: a node that called it would not be
replay-safe (Section 39.4), so a resumed checkpoint would reason over different
text than the original decision. `validate_market_data` is the single fetch
point for everything a decision reads, and web search is deliberately not in
it. `tests/test_news_search.py` asserts no graph node imports this function.

NEITHER CAN BLOCK A TRADE
=========================
`fetch_headlines` already returns `(items, failed_sources)` and the news
specialist has to tell "no feed" from "no news" — reporting one as the other is
a defect this project has already paid for. A new source that raised, hung or
returned garbage would turn an optional input into an outage, so both functions
here return `None` on failure and never raise. None is "could not ask"; `[]` is
"asked, nothing matched". That distinction is the same one
`fetch_usdt_perpetual_prices`, `open_positions()` and `venue_flat()` all carry,
and this codebase has now paid for conflating it five times.

KEYS ARE READ AT CALL TIME
==========================
`.env` holds them as lowercase `freenewsapi` and `tinyfish`. Both spellings are
accepted because an operator who later uppercases them should not silently lose
the integration — and a missing key DISABLES the source with a logged reason
rather than erroring, so a deployment without them behaves exactly as it did
before this module existed.

VERIFIED HOW
============
The request shapes come from each vendor's own documentation, read at build
time:

    freenewsapi   GET https://api.freenewsapi.io/v1/news
                  header `x-api-key: <key>`
                  -> {"data": [{uuid, title, published_at, publisher}], "meta": …}
    tinyfish      GET https://api.search.tinyfish.ai
                  header `X-API-Key: <key>`, param `query`

They were NOT exercised against the live services from the build environment —
sending the operator's keys from here was refused as data exfiltration, which
is the correct call. `scripts/verify_news_search.py` runs the live check on the
operator's own machine and prints exactly what came back, and until that has
been run this module should be treated as unverified against the wire. The
parsers are written to that uncertainty: every field is read defensively and an
unexpected shape produces an empty list with a logged reason rather than an
exception.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FREENEWS_URL = "https://api.freenewsapi.io/v1/news"
TINYFISH_SEARCH_URL = "https://api.search.tinyfish.ai"

# Enough to shade a verdict, not enough to dominate the RSS sources. The news
# specialist weights by COVERAGE, so flooding it from one provider would move
# the panel on a single source's editorial mix.
DEFAULT_NEWS_LIMIT = 20
TIMEOUT_S = 12.0


def freenews_key() -> Optional[str]:
    """Read at call time. Lowercase first — that is how `.env` spells it."""
    for name in ("freenewsapi", "FREENEWSAPI", "FREE_NEWS_API_KEY"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def tinyfish_key() -> Optional[str]:
    for name in ("tinyfish", "TINYFISH", "TINYFISH_API_KEY"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def _as_text(value: Any) -> str:
    """A publisher may arrive as a string or as an object. Both are real."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "title", "domain", "uuid"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


async def fetch_freenews(
    query: Optional[str] = None,
    *,
    limit: int = DEFAULT_NEWS_LIMIT,
    language: str = "en",
) -> Optional[List[Dict[str, Any]]]:
    """Headlines from freenewsapi.io in `microstructure_feed`'s own item shape.

    Returns None when the source could not be ASKED (no key, transport failure,
    non-200, unparseable body) and a list — possibly empty — when it answered.
    The news specialist distinguishes those two and must keep being able to.

    The shape returned is `{title, link, source, pubDate}`, identical to
    `_parse_rss`'s, so this slots into the existing pipeline without a second
    representation of a headline. A second shape would mean
    `score_headlines` had two things to understand, which is how a field like
    `pubDate` quietly stops being read for one source.
    """
    key = freenews_key()
    if not key:
        # NOT an error. A deployment without the key behaves exactly as it did
        # before this module existed, and says so once rather than per call.
        logger.debug("freenewsapi is not configured; skipping that source.")
        return None

    params: Dict[str, Any] = {"language": language}
    if query:
        params["q"] = query

    # THROUGH `services/upstream`, NOT A FRESH httpx CLIENT.
    #
    # This built its own `httpx.AsyncClient` and that was a REGRESSION, found
    # by the suite hanging at 78%: `fetch_headlines` is called by
    # `validate_market_data`, the single fetch point, so this runs inside every
    # graph test — and `tests/conftest.py`'s guard patches `socket.connect`,
    # which does NOT stop a DNS lookup. The resolve blocked, the whole suite
    # stalled, and the symptom was "slow" rather than "network".
    #
    # `upstream.fetch_json` is the one outbound helper: shared client, bounded
    # timeout, 4xx not retried, and an `UpstreamResult` instead of an
    # exception. `scan_market` was moved onto it for exactly this class of
    # reason, and a second ad-hoc client is how a timeout policy stops being
    # one policy.
    from backend.services.upstream import fetch_json

    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    result = await fetch_json(
        f"{FREENEWS_URL}?{query_string}",
        label="FreeNewsAPI",
        timeout=TIMEOUT_S,
        headers={"x-api-key": key},
    )
    if not result.ok:
        # An auth failure and a quota failure are different facts and the
        # status alone does not say which, so the helper's own error text is
        # carried through rather than replaced.
        logger.warning("freenewsapi could not be read: %s", result.error)
        return None

    payload = result.data
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        logger.warning(
            "freenewsapi returned no `data` array (keys: %s). The shape may have "
            "changed; run scripts/verify_news_search.py to see the live response.",
            sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__,
        )
        return None

    items: List[Dict[str, Any]] = []
    for row in rows[: max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        title = _as_text(row.get("title"))
        if not title:
            continue
        items.append({
            "title": title,
            "link": row.get("original_url") or row.get("url") or row.get("link"),
            # Named so a reader of the panel can tell which provider carried a
            # headline — the same reason `_parse_rss` stamps the feed name.
            "source": _as_text(row.get("publisher")) or "FreeNewsAPI",
            "pubDate": row.get("published_at") or row.get("pubDate"),
        })
    return items


async def tinyfish_search(
    query: str, *, limit: int = 8
) -> Optional[List[Dict[str, Any]]]:
    """Web search results for a question a HUMAN asked.

    **NOT A TRADING INPUT, AND THAT IS ENFORCED RATHER THAN DOCUMENTED.** A web
    search is a live, unbounded, non-replayable fetch, so a graph node calling
    it would not be replay-safe (Section 39.4): a resumed checkpoint would
    reason over different text than the original decision. A test asserts that
    no node imports this.

    Returns None when it could not be asked, a list when it answered.
    """
    key = tinyfish_key()
    if not key:
        logger.debug("tinyfish is not configured; web search is unavailable.")
        return None
    if not (query or "").strip():
        return []

    # The same one outbound helper. This one is never called from a graph node
    # (a test asserts that), but it shares the client and the timeout policy
    # for the same reason.
    from urllib.parse import quote

    from backend.services.upstream import fetch_json

    result = await fetch_json(
        f"{TINYFISH_SEARCH_URL}?query={quote(query)}",
        label="TinyFish search",
        timeout=TIMEOUT_S,
        headers={"X-API-Key": key},
    )
    if not result.ok:
        logger.warning("tinyfish could not be read: %s", result.error)
        return None

    payload = result.data

    # The result array has been seen under several keys across search vendors
    # and this one is unverified from the build environment, so the common
    # spellings are all accepted rather than one being assumed.
    rows: Any = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key_name in ("results", "data", "organic", "items", "web"):
            candidate = payload.get(key_name)
            if isinstance(candidate, list):
                rows = candidate
                break
    if not isinstance(rows, list):
        logger.warning(
            "tinyfish returned no recognisable result array (keys: %s). Run "
            "scripts/verify_news_search.py to see the live response.",
            sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__,
        )
        return None

    out: List[Dict[str, Any]] = []
    for row in rows[: max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        out.append({
            "title": _as_text(row.get("title")) or _as_text(row.get("name")),
            "url": row.get("url") or row.get("link") or row.get("href"),
            "snippet": (
                _as_text(row.get("snippet"))
                or _as_text(row.get("description"))
                or _as_text(row.get("content"))
            ),
        })
    return out


def status() -> Dict[str, Any]:
    """Whether each is configured. Never returns a key."""
    return {
        "news": {
            "provider": "freenewsapi.io",
            "configured": bool(freenews_key()),
            "envVar": "freenewsapi",
            "role": "a fifth headline source beside the four RSS feeds",
        },
        "webSearch": {
            "provider": "tinyfish.ai",
            "configured": bool(tinyfish_key()),
            "envVar": "tinyfish",
            "role": (
                "operator-facing web search only. No graph node reads it: a live "
                "fetch inside a decision would not be replay-safe."
            ),
        },
    }
