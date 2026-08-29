"""Live price relay — the backend holds ONE Binance socket, the browser polls us.

WHAT THIS REPLACES
------------------
`components/MarketData.tsx` opened `wss://stream.binance.com:9443/stream` from
the BROWSER, once per visitor. That worked while the operator sat in an
unblocked region, and it is the reason live prices kept updating even when
/api/candles was returning 502 from Vercel — the two took completely different
routes to the same exchange.

It is moved here because it made the dashboard's correctness depend on the
VIEWER's location. Two people opening the same deployment from two countries got
different behaviour from the same build, and the one in a blocked region saw a
price grid that silently never ticked.

WHY POLLING AND NOT A WEBSOCKET TO THE BROWSER
-----------------------------------------------
The frontend is served from Vercel over https and this backend has no TLS
certificate (no domain). A browser on an https page refuses to open `ws://`, and
refuses plain `http://` fetches too — that is the mixed-content rule, and it
cannot be worked around from the page. A WebSocket also cannot be proxied through
a Vercel serverless function.

So the browser talks only to Vercel, over https, and Vercel talks to this backend
server-to-server where no browser rules apply. The last hop the browser makes is
therefore a normal HTTPS request, which means POLLING.

The cost is honest: ticks are as fresh as the poll interval (~2s) instead of
instant. The exchange socket itself is still real-time — only the last hop is
polled — so the cache a poll reads is never more than a moment old.

This becomes a real WebSocket again the day the backend has a TLS hostname; see
`docs/DEPLOYMENT_NETWORKING.md`. Nothing here needs to change for that, because
what is stored is a cache and the transport is the API's business.

SUBSCRIPTIONS ARE DEMAND-DRIVEN
-------------------------------
`services/live_market_data.py` watches a hardcoded three symbols
(BTC/ETH/SOL) for the agent's own use. This is deliberately NOT that: the
dashboard watches whatever is on the operator's watchlist, which changes at
runtime. Symbols are registered by the API when the frontend asks for them, and
the upstream subscription is rebuilt when the set changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

BINANCE_STREAM_URL = "wss://stream.binance.com:9443/stream"

# How long a symbol stays subscribed after the last request for it. Without an
# expiry the subscription set only ever grows: a symbol removed from the
# watchlist would be streamed forever because nothing ever says "stop".
SUBSCRIPTION_TTL_S = 300.0

# A tick older than this is reported as stale rather than returned as current.
# It is deliberately much larger than the poll interval — a quiet symbol
# genuinely does not tick often, and calling that "stale" would be wrong.
STALE_AFTER_S = 90.0

_MAX_SYMBOLS = 64  # Binance caps streams per connection; this is well inside it.


class _Tick:
    __slots__ = ("price", "prev_close", "ts")

    def __init__(self, price: float, prev_close: Optional[float], ts: float) -> None:
        self.price = price
        self.prev_close = prev_close
        self.ts = ts


class TickerStream:
    """Maintains one Binance combined-stream socket over a dynamic symbol set."""

    def __init__(self) -> None:
        # Binance stream slug (lowercase, e.g. "btcusdt") -> latest tick.
        self._ticks: Dict[str, _Tick] = {}
        # slug -> monotonic deadline after which it is dropped.
        self._wanted: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        # CREATED IN `start()`, NOT HERE, AND THAT IS NOT A STYLE CHOICE.
        #
        # `asyncio.Event` binds to the running loop the first time it is awaited.
        # This class is a module-level singleton, so building the Event in
        # `__init__` bound it to whichever loop happened to construct it — and
        # any later loop then failed with:
        #
        #     RuntimeError: <asyncio.locks.Event ...> is bound to a different
        #     event loop
        #
        # That is not a test-only concern. It happens to any process that runs a
        # second loop over the same singleton: `uvicorn --reload`, an embedded
        # harness, or a lifespan restart. It surfaced as 35 teardown errors the
        # moment more than one TestClient ran in a session, at a point far from
        # the cause.
        self._resubscribe: Optional[asyncio.Event] = None
        self._running = False
        self._connected = False
        self._last_error: Optional[str] = None
        self._connect_attempts = 0

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._running:
            return
        # A FRESH Event per start, bound to the loop that is about to use it.
        self._resubscribe = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Ticker stream relay started (no symbols subscribed yet).")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Dropped so the next `start()` cannot inherit an Event bound to the loop
        # that is now closing.
        self._resubscribe = None
        self._connected = False

    # ---------------- subscription ----------------

    def register(self, slugs: Iterable[str]) -> List[str]:
        """Mark these Binance slugs as wanted, refreshing their TTL.

        Returns the accepted slugs. Called on every poll, so the TTL refreshes
        naturally for as long as a browser is actually looking at a symbol and
        lapses on its own when nobody is.
        """
        now = time.monotonic()
        accepted: List[str] = []
        changed = False

        for raw in slugs:
            slug = (raw or "").strip().lower()
            if not slug or not slug.isalnum():
                # Slugs go straight into the upstream URL. Anything not
                # alphanumeric is either a mistake or an injection attempt, and
                # neither should reach Binance.
                continue
            if slug not in self._wanted:
                if len(self._wanted) >= _MAX_SYMBOLS:
                    logger.warning(
                        "Refusing to subscribe %s — already streaming %d symbols (cap %d).",
                        slug, len(self._wanted), _MAX_SYMBOLS,
                    )
                    continue
                changed = True
            self._wanted[slug] = now + SUBSCRIPTION_TTL_S
            accepted.append(slug)

        if self._expire_stale_subscriptions(now):
            changed = True
        # Guarded: symbols can be registered by an HTTP request before `start()`
        # has run (or after `stop()`). The wanted-set is still recorded, so the
        # subscription takes effect as soon as the relay does start.
        if changed and self._resubscribe is not None:
            self._resubscribe.set()
        return accepted

    def _expire_stale_subscriptions(self, now: float) -> bool:
        expired = [s for s, deadline in self._wanted.items() if deadline < now]
        for slug in expired:
            self._wanted.pop(slug, None)
            # The cached tick goes too. Keeping it would let a later request for
            # a re-added symbol read a price from minutes ago as if it were current.
            self._ticks.pop(slug, None)
        return bool(expired)

    # ---------------- reads ----------------

    def snapshot(self, slugs: Iterable[str]) -> Dict[str, Any]:
        """Current ticks for these slugs, each labelled with its own freshness."""
        now_wall = time.time()
        out: Dict[str, Any] = {}
        for raw in slugs:
            slug = (raw or "").strip().lower()
            tick = self._ticks.get(slug)
            if tick is None:
                out[slug] = None
                continue
            age = now_wall - tick.ts
            out[slug] = {
                "price": tick.price,
                "prevClose": tick.prev_close,
                "ts": int(tick.ts * 1000),
                "ageSeconds": round(age, 2),
                # Stated per symbol, not per response: one dead symbol in a
                # watchlist of ten does not make the other nine stale, and a
                # single response-level flag would imply it did.
                "stale": age > STALE_AFTER_S,
            }
        return out

    def price_for(self, symbol: str) -> Optional[float]:
        """Last price for an app symbol ('BTC/USDT'), or None.

        WHY THIS EXISTS
        ---------------
        This class holds a THIRD price cache. `market_data.get_price` consulted
        only the other two — `live_market_data._live_prices` and the polled ccxt
        cache — and both are empty for the first stretch after startup, while
        THIS one is already connected and ticking because the dashboard asked it
        to be.

        The visible cost was a graph run in that window aborting at
        `data_validation` with "no live price for BTC/USDT (feed returned 0.0)",
        having done nothing, while `/api/marketdata/ticks` was returning a live
        price for the same symbol at the same moment.

        REFUSES A STALE TICK. Returning one would hand a price from minutes ago
        to a node that is about to size a position against it, which is worse
        than returning nothing — `get_price`'s callers all handle a missing price
        and none of them can detect a silently old one.
        """
        slug = (symbol or "").split(":", 1)[0].replace("/", "").replace("-", "").lower()
        tick = self._ticks.get(slug)
        if tick is None:
            return None
        if (time.time() - tick.ts) > STALE_AFTER_S:
            return None
        return float(tick.price) if tick.price and tick.price > 0 else None

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "subscribedCount": len(self._wanted),
            "subscribed": sorted(self._wanted),
            "cachedCount": len(self._ticks),
            "connectAttempts": self._connect_attempts,
            "lastError": self._last_error,
        }

    # ---------------- the socket ----------------

    async def _run(self) -> None:
        """Connect, consume, reconnect. Never raises out of the task."""
        try:
            import websockets
        except ImportError:
            # Reported once, loudly. Without it the dashboard has no live prices
            # at all, and a silent failure here would look like "the market is
            # quiet" rather than "a dependency is missing".
            self._last_error = (
                "the `websockets` package is not installed, so live prices are unavailable"
            )
            logger.error("Ticker stream cannot start: %s", self._last_error)
            return

        backoff = 1.0
        while self._running:
            resubscribe = self._resubscribe
            if resubscribe is None:
                return  # stopped between iterations

            slugs = sorted(self._wanted)
            if not slugs:
                # Nothing to watch. Wait to be told there is, rather than
                # opening a socket with an empty stream list (which Binance
                # rejects) or busy-looping.
                resubscribe.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(resubscribe.wait(), timeout=30.0)
                continue

            url = f"{BINANCE_STREAM_URL}?streams=" + "/".join(f"{s}@ticker" for s in slugs)
            self._connect_attempts += 1
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._connected = True
                    self._last_error = None
                    backoff = 1.0
                    logger.info("Ticker stream connected for %d symbol(s).", len(slugs))
                    await self._consume(ws, set(slugs))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected = False
                self._last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "Ticker stream disconnected (%s). Reconnecting in %.0fs.",
                    self._last_error, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            else:
                self._connected = False

    async def _consume(self, ws: Any, subscribed: Set[str]) -> None:
        """Read frames until the wanted set changes, then return to resubscribe."""
        resubscribe = self._resubscribe
        if resubscribe is None:
            return
        resubscribe.clear()
        while self._running:
            # Race the next frame against a subscription change, so adding a
            # symbol takes effect immediately instead of waiting for whenever
            # the next tick happens to arrive.
            recv_task = asyncio.ensure_future(ws.recv())
            resub_task = asyncio.ensure_future(resubscribe.wait())
            done, pending = await asyncio.wait(
                {recv_task, resub_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            if resub_task in done:
                if set(self._wanted) != subscribed:
                    logger.info("Ticker subscription set changed; reconnecting.")
                    return
                resubscribe.clear()
                continue

            raw = recv_task.result()
            self._ingest(raw)

    def _ingest(self, raw: Any) -> None:
        """Parse one combined-stream frame into the cache.

        A malformed frame is dropped, not raised on. One bad message must not
        take down the feed for every symbol.
        """
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return

        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            return

        symbol = data.get("s")
        if not symbol:
            return

        try:
            price = float(data["c"])
        except (KeyError, TypeError, ValueError):
            return
        if price <= 0:
            # Zero is missing data, not a price collapse — the same rule
            # `PositionMonitorAgent._check_price` applies to ticks.
            return

        try:
            prev_close = float(data["o"])
        except (KeyError, TypeError, ValueError):
            prev_close = None

        self._ticks[str(symbol).lower()] = _Tick(price, prev_close, time.time())


_stream: Optional[TickerStream] = None


def get_ticker_stream() -> TickerStream:
    global _stream
    if _stream is None:
        _stream = TickerStream()
    return _stream
