"""The venue's own order updates, pushed — so a fill is known in milliseconds, not a minute.

WHAT WAS MISSING
================
This system learned about order state from exactly one place: the return value of
its own `create_order` call. Nothing ever listened to the exchange. Consequences,
all of them silent:

  * A RESTING STOP THAT FIRES IS INVISIBLE. `_place_resting_stop` puts a
    reduce-only stop-market at the venue precisely so a position is protected
    when this process is not watching. When it fills, the venue knows instantly
    and this process does not — the local book still shows the position open, the
    monitor keeps ticking against a stop that has already executed, and the only
    thing that eventually notices is `reconciliation`, which runs ONCE A MINUTE
    and deliberately only reports.

  * A LIQUIDATION OR ADL IS THE SAME STORY, with worse consequences and the same
    up-to-60-second blind window.

  * THE REAL COMMISSION NEVER ARRIVES. `services/fees` falls back to a modelled
    taker rate whenever the create-order response carries no fee, which on a
    market order is common. The exchange reports the true commission on the TRADE
    update, which is exactly what this stream carries.

IT REPORTS. IT DOES NOT REPAIR. THIS IS THE SAME RULE `reconciliation` FOLLOWS.
==============================================================================
This module never closes, opens, forgets or resizes a position, and
`tests/test_order_stream.py` asserts that against its own source.

The temptation is obvious: the stream KNOWS the stop filled, so why not close the
position locally and be done? Because every automatic "fix" is itself a trading
decision made on one message from one connection. A dropped websocket that
reconnects mid-gap, a message for a manual order the operator placed in the
Binance app, a partial fill of a stop — each would have this module mutate the
book with no gate, no Risk Gateway, and no audit trail. `PositionMonitorAgent`
owns closes; `reconciliation` owns comparison; this owns OBSERVATION.

What it does instead is publish what it saw and log loudly, so the operator and
the existing reconciliation path both have a far fresher picture than a 60-second
poll can give.

GATED THE SAME WAY RECONCILIATION IS
====================================
Runs only while `LIVE_TRADING` is on and credentials exist. A paper fill has no
venue order behind it, so there is nothing to stream; connecting anyway would
open an authenticated socket to support a book the exchange has never heard of.

WHY ccxt.pro RATHER THAN A HAND-ROLLED SOCKET
=============================================
`ccxt.pro` ships inside the ccxt package this project already pins, and its
`watch_orders` is implemented for both venues. Rolling this by hand means
Binance's listenKey lifecycle (POST to create, PUT every 30-60 minutes or the
stream dies silently) and Bybit's signed private-channel handshake — two
venue-specific protocols to get right and keep right, when the adapter layer this
project already depends on has both. The venue split (`binance` vs `bybit`) stays
where every other venue difference lives: below this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# How long to wait before reconnecting after the stream drops, and the ceiling on
# that backoff. A websocket that fails because the venue is rejecting our
# credentials will fail again immediately, and a tight reconnect loop against an
# authenticated endpoint is how an API key gets rate-limited or banned.
_RECONNECT_BASE_S = 2.0
_RECONNECT_MAX_S = 60.0

# Terminal order states worth announcing. An order that is merely `open` or
# repeatedly `partially filled` produces a lot of traffic and tells the operator
# nothing they did not already know from placing it.
_NOTABLE_STATUSES = frozenset({"closed", "filled", "canceled", "cancelled", "expired", "rejected"})


@dataclass
class OrderEvent:
    """One order update as observed. Carries no authority over any book."""

    order_id: Optional[str]
    client_order_id: Optional[str]
    symbol: Optional[str]
    side: Optional[str]
    status: Optional[str]
    filled: Optional[float]
    average: Optional[float]
    fee_cost: Optional[float]
    fee_currency: Optional[str]
    reduce_only: bool
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_fill(self) -> bool:
        return (self.status or "").lower() in ("closed", "filled") and bool(self.filled)


def stream_enabled() -> bool:
    """Whether to run at all. Live trading only, and opt-out via env.

    Mirrors `reconciliation`'s gating: a paper book has no venue orders to watch,
    so an authenticated socket would be observing a book the exchange has never
    heard of.
    """
    from backend.core.config import settings

    if not settings.LIVE_TRADING:
        return False
    raw = (os.getenv("ORDER_STREAM_ENABLED") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


class OrderStream:
    """Watches the venue's private order feed and records what it sees.

    Deliberately holds NO reference to the position monitor, the portfolio store
    or the execution agent. It cannot mutate a book because it has nothing to
    mutate a book with — the report-only property is structural here, not a rule
    someone has to remember.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._recent: List[OrderEvent] = []
        self._connected = False
        self._last_error: Optional[str] = None
        self._events_seen = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not stream_enabled():
            logger.info(
                "Order stream not started (LIVE_TRADING off, or ORDER_STREAM_ENABLED=false). "
                "Venue order state will only be observed by the once-a-minute reconciliation."
            )
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._connected = False

    # -- the loop --------------------------------------------------------

    async def _run(self) -> None:
        """Reconnecting watch loop. Never raises out of the task."""
        backoff = _RECONNECT_BASE_S
        client = None
        while True:
            try:
                if client is None:
                    client = await self._build_client()
                    if client is None:
                        # No credentials or no ccxt.pro. Said once, then the task
                        # ends — retrying a missing dependency forever would fill
                        # the log with a problem no retry can fix.
                        return
                orders = await client.watch_orders()
                self._connected = True
                self._last_error = None
                backoff = _RECONNECT_BASE_S
                for raw in orders or []:
                    self._record(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                self._last_error = str(exc)
                logger.warning(
                    "Order stream dropped (%s). Reconnecting in %.0fs. Venue order state is "
                    "unobserved until it returns — reconciliation still runs every minute.",
                    exc, backoff,
                )
                # Rebuild the client: a socket that errored may be in a state
                # ccxt will not recover from, and reusing it reconnects to
                # nothing while reporting success.
                client = await self._close_client(client)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_S)

    async def _build_client(self):
        try:
            import ccxt.pro as ccxtpro
        except ImportError:
            logger.warning(
                "ccxt.pro is not available, so venue order updates cannot be streamed. "
                "Falling back to the once-a-minute reconciliation only."
            )
            return None

        from backend.services.venue import _credentials, configured_venue
        from backend.core.config import settings

        venue_id = configured_venue()
        testnet = bool(settings.USE_TESTNET)
        key, secret = _credentials(venue_id, testnet=testnet)
        if not key or not secret:
            logger.warning(
                "No %s credentials, so the order stream cannot authenticate. Venue order "
                "state will only be observed by reconciliation.", venue_id,
            )
            return None

        client = getattr(ccxtpro, venue_id)({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap", "defaultSubType": "linear"},
        })
        if testnet:
            client.set_sandbox_mode(True)
        logger.info("Order stream connected to %s (testnet=%s).", venue_id, testnet)
        return client

    async def _close_client(self, client):
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        return None

    # -- observation -----------------------------------------------------

    def _record(self, raw: Dict[str, Any]) -> None:
        """Turn one ccxt order dict into an OrderEvent and note it. Never raises."""
        try:
            fee = raw.get("fee") or {}
            event = OrderEvent(
                order_id=str(raw.get("id")) if raw.get("id") is not None else None,
                client_order_id=raw.get("clientOrderId"),
                symbol=raw.get("symbol"),
                side=raw.get("side"),
                status=raw.get("status"),
                filled=_as_float(raw.get("filled")),
                average=_as_float(raw.get("average") or raw.get("price")),
                fee_cost=_as_float(fee.get("cost")) if isinstance(fee, dict) else None,
                fee_currency=(fee.get("currency") if isinstance(fee, dict) else None),
                reduce_only=bool(raw.get("reduceOnly") or (raw.get("info") or {}).get("reduceOnly")),
                raw=raw,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unparseable order update ignored: %s", exc)
            return

        self._events_seen += 1
        status = (event.status or "").lower()
        if status not in _NOTABLE_STATUSES:
            return

        self._recent.append(event)
        # A bounded ring. This is a diagnostic buffer read through an API, not a
        # record of truth — `trades` is that — so it must not grow without limit
        # in a process that is expected to run for weeks.
        if len(self._recent) > 200:
            del self._recent[: len(self._recent) - 200]

        if event.is_fill and event.reduce_only:
            # THE ONE THE OPERATOR ACTUALLY NEEDS. A reduce-only fill this
            # process did not initiate is a resting stop or take-profit that
            # fired at the venue — which means a position closed there while the
            # local book still shows it open.
            #
            # CRITICAL, and still only a log. Acting on it would make this a
            # second closing authority; see the module docstring.
            logger.critical(
                "VENUE CLOSED A POSITION: reduce-only %s on %s filled %.10g at %s "
                "(order %s). The local book may still show this position OPEN until "
                "the monitor or reconciliation catches up. NO action taken here.",
                event.side, event.symbol, event.filled, event.average, event.order_id,
            )
        else:
            logger.info(
                "Venue order update: %s %s %s -> %s (filled %s at %s)",
                event.side, event.symbol, event.order_id, event.status,
                event.filled, event.average,
            )

    # -- read ------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Status and recent updates, for `GET /api/graphs/order-stream`."""
        return {
            "enabled": stream_enabled(),
            "connected": self._connected,
            "eventsSeen": self._events_seen,
            "lastError": self._last_error,
            "recent": [
                {
                    "orderId": e.order_id,
                    "clientOrderId": e.client_order_id,
                    "symbol": e.symbol,
                    "side": e.side,
                    "status": e.status,
                    "filled": e.filled,
                    "average": e.average,
                    "feeCost": e.fee_cost,
                    "feeCurrency": e.fee_currency,
                    "reduceOnly": e.reduce_only,
                }
                for e in reversed(self._recent[-50:])
            ],
        }


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_stream: Optional[OrderStream] = None


def get_order_stream() -> OrderStream:
    """The process-wide order stream. A singleton for the same reason the monitor is.

    Two instances would mean two authenticated sockets on one API key, each
    consuming the venue's connection allowance, and each logging every fill twice.
    """
    global _stream
    if _stream is None:
        _stream = OrderStream()
    return _stream


def reset_order_stream() -> None:
    """Drop the singleton. For tests, and after a venue switch."""
    global _stream
    _stream = None
