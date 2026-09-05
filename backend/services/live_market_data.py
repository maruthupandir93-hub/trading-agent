"""The agent's live tick feed — and the one thing every stop-loss depends on.

WHY THE SUBSCRIPTION SET IS DYNAMIC
===================================
This module used to watch a hardcoded list:

    symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

`PositionMonitorAgent` enforces every stop-loss by reacting to `TICK_RECEIVED`,
and THIS IS THE ONLY PUBLISHER OF THAT EVENT. So a position in any instrument
outside that list received no ticks, `_check_price` never ran for it, and **its
stop could never fire**. The position would sit open indefinitely with a stop
that existed only on paper.

Nothing reported it, either. The monitor showed the position as watched, the
dashboard showed its stop, and the operator had every reason to believe it was
protected. The failure was silent and unbounded — exactly the shape of bug the
position monitor exists to prevent.

So the watched set is now derived from what actually needs watching:

    the open positions the monitor is tracking   (safety-critical)
    the running session's symbol                  (about to open a position)
    DEFAULT_SYMBOLS                               (so the dashboard is never blank)

and it is RECONCILED on a timer, because positions open and close while this
process runs. A set computed once at startup would be the same bug with extra
steps.

WHAT THIS DELIBERATELY DOES NOT DO
==================================
It never DROPS a symbol that has an open position, even if the reconciler's other
inputs stop mentioning it. Cancelling the feed for a position that is still open
is the precise failure this file was written to fix, so the desired set is a
union and open positions are always in it.

A feed that cannot be established is logged at CRITICAL rather than retried
quietly. An operator holding a position whose ticks are not arriving needs to
know that the automatic stop is not currently enforceable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Set

import ccxt.pro as ccxtpro

from backend.core.message_bus import get_message_bus
from backend.models.events import TickReceivedEvent

logger = logging.getLogger(__name__)

# In-memory cache for the latest prices. `get_price` in `market_data` reads this
# first — see its docstring for the three-cache ordering.
_live_prices: Dict[str, float] = {}

# Always watched, so the dashboard has something to show on a fresh process and
# the common instruments are never waiting on a reconcile.
DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")

# How often the watched set is compared against what needs watching. 20s: a
# position opens and needs its feed promptly, but each reconcile only inspects
# in-memory state, so there is no reason to spin faster than a stop could
# plausibly need to fire.
RECONCILE_INTERVAL_S = 20.0

# symbol -> the task watching it.
_watchers: Dict[str, asyncio.Task] = {}
_exchange = None


async def _watch_ticker_loop(exchange, symbol: str, bus) -> None:
    """Watch one ticker and publish TICK_RECEIVED. Runs until cancelled."""
    failures = 0
    while True:
        try:
            ticker = await exchange.watch_ticker(symbol)
            price = ticker.get("last")
            volume = ticker.get("baseVolume", 0.0)

            if price is not None:
                failures = 0
                _live_prices[symbol] = price
                await bus.publish(
                    "TICK_RECEIVED",
                    TickReceivedEvent(
                        agent_id="live_market_data",
                        symbol=symbol,
                        price=price,
                        volume=volume,
                        exchange="binance",
                    ),
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1
            # Escalated after a few consecutive failures rather than logged at
            # ERROR forever. A symbol whose feed is down has no enforceable stop,
            # and that is worth saying loudly once it stops looking transient.
            if failures == 3:
                logger.critical(
                    "No ticks for %s after %d consecutive failures (%s). Any open position "
                    "on this symbol has NO enforceable stop-loss in this process until the "
                    "feed recovers.",
                    symbol, failures, e,
                )
            else:
                logger.warning("Error watching ticker %s: %s", symbol, e)
            await asyncio.sleep(5)


async def _symbols_needing_ticks() -> Set[str]:
    """Everything that must have a live feed right now.

    A UNION, deliberately. An open position's symbol is included whatever else is
    or is not asking for it — dropping the feed for a position that is still open
    is the bug this module was rewritten to fix.
    """
    wanted: Set[str] = set(DEFAULT_SYMBOLS)

    # 1. OPEN POSITIONS. The safety-critical input: these are the symbols whose
    #    stops the monitor is currently responsible for enforcing.
    try:
        from backend.agents.position_monitor import get_position_monitor

        for pos in get_position_monitor().snapshot_open():
            symbol = pos.get("symbol")
            if isinstance(symbol, str) and symbol:
                wanted.add(symbol)
    except Exception as exc:  # pragma: no cover - defensive
        # Logged, not raised: a reconcile that failed here must still keep the
        # existing feeds running rather than tearing them down.
        logger.error("Could not read open positions for tick subscription: %s", exc)

    # 2. The running session's symbol — it is about to open a position, and
    #    waiting for the fill before subscribing would leave the first moments of
    #    a new position unwatched.
    try:
        from backend.services.trading_session import active_session

        session = active_session()
        if session is not None and getattr(session, "symbol", None):
            wanted.add(session.symbol)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not read the active session for tick subscription: %s", exc)

    # 3. The paper/real book, for positions the monitor is not tracking (a
    #    restored book, or a manual trade opened without a stop). They still need
    #    marking for equity, and `book_equity` returns None for an unpriced one.
    try:
        from backend.services.portfolio_store import get_portfolio

        portfolio = await get_portfolio()
        for book in (portfolio or {}).values():
            for pos in (book or {}).get("positions") or []:
                symbol = pos.get("symbol")
                if isinstance(symbol, str) and symbol:
                    wanted.add(symbol)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not read the portfolio for tick subscription: %s", exc)

    return wanted


async def _reconcile(bus) -> None:
    """Start feeds for newly needed symbols; stop feeds nothing needs."""
    global _exchange
    if _exchange is None:
        return

    wanted = await _symbols_needing_ticks()

    # Reap tasks that died, so a crashed watcher is restarted rather than leaving
    # its symbol permanently unwatched while still appearing subscribed.
    for symbol in [s for s, task in _watchers.items() if task.done()]:
        logger.warning("Tick watcher for %s had stopped; restarting it.", symbol)
        _watchers.pop(symbol, None)

    for symbol in sorted(wanted - set(_watchers)):
        _watchers[symbol] = asyncio.create_task(
            _watch_ticker_loop(_exchange, symbol, bus), name=f"tick:{symbol}"
        )
        logger.info("Subscribed to live ticks for %s (%d watched).", symbol, len(_watchers))

    for symbol in sorted(set(_watchers) - wanted):
        task = _watchers.pop(symbol)
        task.cancel()
        # THE CACHED PRICE GOES WITH THE SUBSCRIPTION.
        #
        # `get_live_price` has no timestamp, so a value left behind here is served
        # forever as a live websocket price for a symbol nothing is watching. It
        # would read as fresh — `/api/market/price` even labels its source
        # "websocket" — and if a position later reopened on that symbol, the first
        # read before the feed reconnected would return a price from an arbitrary
        # time in the past. Dropping it means `get_price` falls through to the
        # polled cache, which at least knows how old it is.
        _live_prices.pop(symbol, None)
        logger.info("Unsubscribed from %s — nothing open or configured needs it.", symbol)


async def start_live_data_feed() -> None:
    """Open the socket and keep the subscription set in step with what is open."""
    global _exchange

    logger.info("Starting live market data feed via ccxt.pro...")
    # No credentials: this is public ticker data, and a keyed client would spend
    # the operator's rate budget on it. Same split as `services/venue.py`.
    _exchange = ccxtpro.binance({"enableRateLimit": True})
    bus = get_message_bus()

    try:
        await _reconcile(bus)
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL_S)
            try:
                await _reconcile(bus)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("Tick subscription reconcile failed; feeds left as they are.")
    except asyncio.CancelledError:
        logger.info("Live data feed cancelled.")
        raise
    finally:
        for task in list(_watchers.values()):
            task.cancel()
        _watchers.clear()
        # Nothing is arriving any more, so nothing here is a live price.
        _live_prices.clear()
        try:
            await _exchange.close()
        except Exception:  # pragma: no cover
            pass
        _exchange = None


def get_live_price(symbol: str) -> float:
    """Latest price from the live socket cache, or 0.0 when none has arrived."""
    return _live_prices.get(symbol, 0.0)


def watched_symbols() -> Set[str]:
    """What currently has a live feed. Exposed for diagnostics and tests."""
    return set(_watchers)
