"""Fetches order-book depth, the trade tape and headlines for the reasoning layer.

WHY A SERVICE AND NOT A CALL INTO api/marketdata.py
---------------------------------------------------
`api/marketdata.py` already fetches all three, and reusing its route functions was
the obvious shortcut. It is the wrong one twice over:

  * Those handlers raise `HTTPException` on an upstream failure, because their job
    is to answer a browser. A graph node must DEGRADE, not raise — `wrap_node`
    would catch it and mark the node errored, turning "the depth feed was slow"
    into "this specialist is broken".
  * `graphs/` importing from `api/` inverts the dependency direction the whole
    contracts module is built around, and would put a router import inside the
    reasoning layer.

So the upstream calls live here, phrased for a caller that must keep going. Every
function returns None rather than raising, and the reason is carried on the result
so a specialist can say WHY it has nothing rather than merely having nothing.

CACHED, AND THE TTLs ARE NOT ARBITRARY
--------------------------------------
Both the analysis graph and the autonomous loop run per symbol, and the trigger
worker can fire several within a few seconds. Without a cache each run would
re-fetch the same depth snapshot, and Binance's public rate limits are shared
across every caller in this process.

    depth + tape   5s   a book snapshot is stale almost immediately, so this is
                        short by design: long enough to serve a burst of runs on
                        one symbol, short enough that no run reasons over a book
                        from a different minute.
    headlines     120s  RSS feeds publish on the order of minutes and four HTTP
                        fetches on every graph run would dominate run latency for
                        data that has not changed.

A cached entry carries its own age and the caller can see it. Serving a stale
value while claiming freshness is the failure this codebase already has a
`_as_naive_utc` comment and a `stale` flag per tick about.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.services.upstream import fetch_json, fetch_text

logger = logging.getLogger(__name__)

BINANCE_SPOT = "https://api.binance.com"

DEPTH_LIMIT = 50
TRADES_LIMIT = 200

MICROSTRUCTURE_TTL_S = 5.0
HEADLINES_TTL_S = 120.0

# Same four keyless feeds `api/marketdata.py` uses. Duplicated as a constant
# rather than imported from the router for the dependency reason in the module
# docstring; `tests/test_specialist_feeds.py` asserts the two lists agree so they
# cannot drift into two different views of "the news".
RSS_FEEDS: Tuple[Tuple[str, str], ...] = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("Binance", "https://www.binance.com/en/support/announcement/rss"),
)


def to_binance_slug(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTCUSDT'.

    Three symbol spellings coexist in this codebase and picking the wrong one is
    not hypothetical: `market_data.fetch_prices` filtered futures tickers with
    `endswith("/USDT")` against keys shaped `BTC/USDT:USDT`, matched nothing, and
    logged a network error for months while the feed was working.
    """
    return (symbol or "").split(":", 1)[0].replace("/", "").replace("-", "").upper()


@dataclass
class Microstructure:
    """One depth snapshot plus the recent tape, or the reason there is none."""

    symbol: str
    available: bool
    reason: Optional[str] = None
    bids: List[Dict[str, float]] = field(default_factory=list)
    asks: List[Dict[str, float]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    fetched_at: Optional[float] = None
    # Seconds since fetch when served from cache. 0.0 on a fresh fetch.
    age_seconds: float = 0.0


_micro_cache: Dict[str, Tuple[float, Microstructure]] = {}
_micro_locks: Dict[str, asyncio.Lock] = {}
_headline_cache: Optional[Tuple[float, List[Dict[str, Any]], List[str]]] = None
_headline_lock: Optional[asyncio.Lock] = None

# The loop the cached locks belong to. See `_locks_for_this_loop`.
_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _locks_for_this_loop() -> None:
    """Drop every cached lock if the running event loop has changed.

    AN asyncio PRIMITIVE BELONGS TO ONE LOOP, AND THESE ARE MODULE-LEVEL.
    Creating the locks lazily is not enough on its own: once created they are
    cached for the life of the PROCESS, while the loop they bound to can go away.
    Awaiting such a lock from a later loop does not raise a clear error — it
    HANGS.

    That is not hypothetical here. It stopped the test suite dead at roughly a
    third of the way through, with no failure and no output: pytest gives each
    async test its own event loop, so the first test to fetch microstructure data
    created the locks, and a later test awaiting them waited forever on a loop
    that no longer existed.

    `services/ticker_stream.py` carries the same scar from the same mistake — an
    `asyncio.Event` built in `__init__` produced "bound to a different event
    loop" across 35 test teardowns — and its fix was to build the Event in
    `start()`. The equivalent here is to notice the loop changed and rebuild.

    Clearing the locks is safe. They exist only to stop two concurrent callers
    issuing the same upstream fetch; losing one costs at most a duplicate
    request, whereas keeping a dead one costs the whole process.
    """
    global _lock_loop

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop: nothing to bind to, and the caller is about to fail
        # for its own reasons. Leave the cache alone.
        return

    if _lock_loop is not loop:
        _micro_locks.clear()
        globals()["_headline_lock"] = None
        _lock_loop = loop


def _lock_for(key: str) -> asyncio.Lock:
    """One lock per symbol, bound to the CURRENT loop.

    Created on demand rather than at import, because an `asyncio` primitive binds
    to the running loop and this module is imported at process start. See
    `_locks_for_this_loop` for why lazy creation alone was not enough.
    """
    _locks_for_this_loop()
    lock = _micro_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _micro_locks[key] = lock
    return lock


async def fetch_microstructure(symbol: str, *, force: bool = False) -> Microstructure:
    """Depth and tape for one symbol. Never raises.

    Concurrent callers for the same symbol share one upstream fetch: the trigger
    worker can start several runs on one symbol within a second, and letting each
    issue its own pair of requests is how a shared public rate limit gets spent on
    duplicates.
    """
    slug = to_binance_slug(symbol)
    if not slug.isalnum():
        return Microstructure(
            symbol=symbol,
            available=False,
            reason=f"{symbol!r} does not resolve to a Binance slug ({slug!r})",
        )

    now = time.monotonic()
    if not force:
        cached = _micro_cache.get(slug)
        if cached is not None and (now - cached[0]) < MICROSTRUCTURE_TTL_S:
            hit = cached[1]
            return Microstructure(
                symbol=hit.symbol, available=hit.available, reason=hit.reason,
                bids=hit.bids, asks=hit.asks, trades=hit.trades,
                fetched_at=hit.fetched_at, age_seconds=round(now - cached[0], 3),
            )

    async with _lock_for(slug):
        # Re-checked inside the lock: whoever held it may have just refreshed.
        now = time.monotonic()
        cached = _micro_cache.get(slug)
        if not force and cached is not None and (now - cached[0]) < MICROSTRUCTURE_TTL_S:
            hit = cached[1]
            return Microstructure(
                symbol=hit.symbol, available=hit.available, reason=hit.reason,
                bids=hit.bids, asks=hit.asks, trades=hit.trades,
                fetched_at=hit.fetched_at, age_seconds=round(now - cached[0], 3),
            )

        depth_result, trades_result = await asyncio.gather(
            fetch_json(
                f"{BINANCE_SPOT}/api/v3/depth?symbol={slug}&limit={DEPTH_LIMIT}",
                label="Binance depth (specialist)",
            ),
            fetch_json(
                f"{BINANCE_SPOT}/api/v3/aggTrades?symbol={slug}&limit={TRADES_LIMIT}",
                label="Binance aggTrades (specialist)",
            ),
        )

        problems = []
        if not depth_result.ok:
            problems.append(f"depth: {depth_result.error}")
        if not trades_result.ok:
            problems.append(f"tape: {trades_result.error}")

        if problems:
            # A PARTIAL result is still returned. The orderflow specialist can
            # vote on the tape alone at reduced confidence, and the liquidity
            # specialist needs only the book — refusing both because one failed
            # would discard evidence that arrived.
            result = Microstructure(
                symbol=symbol,
                available=depth_result.ok or trades_result.ok,
                reason="; ".join(problems),
                bids=_levels((depth_result.data or {}).get("bids")) if depth_result.ok else [],
                asks=_levels((depth_result.data or {}).get("asks")) if depth_result.ok else [],
                trades=_tape(trades_result.data) if trades_result.ok else [],
                fetched_at=time.time(),
            )
            logger.warning("Microstructure fetch for %s incomplete: %s", symbol, result.reason)
        else:
            depth = depth_result.data or {}
            result = Microstructure(
                symbol=symbol,
                available=True,
                bids=_levels(depth.get("bids")),
                asks=_levels(depth.get("asks")),
                trades=_tape(trades_result.data),
                fetched_at=time.time(),
            )

        _micro_cache[slug] = (time.monotonic(), result)
        return result


def _levels(rows: Any) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for row in rows or []:
        try:
            out.append({"price": float(row[0]), "qty": float(row[1])})
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _tape(rows: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in rows if isinstance(rows, list) else []:
        try:
            out.append({
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "time": int(t["T"]),
                # True = the buyer was the MAKER, i.e. the seller crossed the
                # spread. `algorithms/microstructure.analyse_tape` spells out the
                # sense again at the point it is used, because inverting it there
                # would hand the debate a confident reading of the wrong side.
                "buyerIsMaker": bool(t["m"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def fetch_headlines(*, force: bool = False) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Recent headlines from the RSS feeds, plus the feeds that failed.

    Returns `(items, failed_sources)`. An empty list with a populated
    `failed_sources` is a FEED failure; an empty list with no failures means the
    feeds answered and had nothing recent. The news specialist has to tell those
    apart — "no news feed" must never be read downstream as "no news".
    """
    global _headline_cache, _headline_lock

    now = time.monotonic()
    if not force and _headline_cache is not None:
        stamped, items, failed = _headline_cache
        if (now - stamped) < HEADLINES_TTL_S:
            return list(items), list(failed)

    # Same loop check as `_lock_for`. Without it this lock outlives the loop that
    # created it and a later await on it hangs rather than raising.
    _locks_for_this_loop()
    if _headline_lock is None:
        _headline_lock = asyncio.Lock()

    async with _headline_lock:
        now = time.monotonic()
        if not force and _headline_cache is not None:
            stamped, items, failed = _headline_cache
            if (now - stamped) < HEADLINES_TTL_S:
                return list(items), list(failed)

        results = await asyncio.gather(
            *(fetch_text(url, label=f"{name} RSS") for name, url in RSS_FEEDS)
        )

        items: List[Dict[str, Any]] = []
        failed: List[str] = []
        for (name, _url), result in zip(RSS_FEEDS, results):
            if not result.ok or not isinstance(result.data, str):
                # Named, not counted. "3 of 4 feeds answered" does not tell an
                # operator that the one carrying exchange announcements is the
                # one that did not.
                failed.append(f"{name}: {result.error}")
                continue
            items.extend(_parse_rss(result.data, name))

        _headline_cache = (time.monotonic(), items, failed)
        if failed:
            logger.warning("Headline feeds unavailable: %s", "; ".join(failed))
        return list(items), list(failed)


ATOM_NS = "http://www.w3.org/2005/Atom"


def _parse_rss(xml_text: str, source: str, limit: int = 40) -> List[Dict[str, Any]]:
    """Items from one RSS or Atom document. Never raises.

    THE `or` TRAP THIS AVOIDS, WHICH ALREADY COST THIS PROJECT A BUG.
    `ElementTree.Element` is FALSY when it has no children, so the natural

        node.find("title") or node.find(f"{{{ATOM_NS}}}title")

    silently discards a valid `<title>text</title>` — an element with text but no
    child elements — and falls through to the Atom branch. The RSS route returned
    zero articles while reporting `available: true`. Hence the explicit
    `is not None` checks below.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("Could not parse %s RSS: %s", source, e)
        return []

    nodes = root.findall(".//item")
    if not nodes:
        nodes = root.findall(f".//{{{ATOM_NS}}}entry")

    items: List[Dict[str, Any]] = []
    for node in nodes[:limit]:
        def text(tag: str) -> Optional[str]:
            found = node.find(tag)
            if found is None:
                found = node.find(f"{{{ATOM_NS}}}{tag}")
            if found is None or found.text is None:
                return None
            return found.text.strip()

        title = text("title")
        if not title:
            continue

        link = text("link")
        if not link:
            atom_link = node.find(f"{{{ATOM_NS}}}link")
            if atom_link is not None:
                link = atom_link.get("href")

        items.append({
            "title": title,
            "link": link,
            "source": source,
            "pubDate": text("pubDate") or text("published") or text("updated"),
        })
    return items


def reset_caches() -> None:
    """Drop every cached value AND every lock. For tests, and for a forced refresh.

    The locks go too. A test that resets the caches and then runs on a fresh loop
    would otherwise still be holding primitives bound to the previous one.
    """
    global _headline_cache, _headline_lock, _lock_loop
    _micro_cache.clear()
    _micro_locks.clear()
    _headline_cache = None
    _headline_lock = None
    _lock_loop = None
