"""Operator Exchange API (`/api/operator/exchange`) — the HUMAN's own hands.

READ THIS BEFORE CHANGING ANYTHING HERE. IT PLACES REAL ORDERS.

WHY THIS IS A SEPARATE MODULE FROM `api/exchange.py`
----------------------------------------------------
`api/exchange.py` says, and still says:

    there is deliberately **no** order-placement route here ... An HTTP endpoint
    that placed an order would be a path to the exchange that bypasses the
    Supervisor, the CRO, and the leverage ceiling — reachable by anything that
    can reach the port.

That reasoning is correct and this module does not weaken it. `api/exchange.py`
remains read-only. This is a DIFFERENT thing, kept in a differently-named file so
nobody discovers order placement while reading the read-only one.

WHAT MAKES THIS LEGITIMATE RATHER THAN THE HOLE THAT WARNING DESCRIBES
----------------------------------------------------------------------
There are two distinct paths to an exchange in this system, and conflating them
is the actual danger:

  THE AGENT'S PATH   Supervisor -> CRO -> TAR_APPROVED -> ExecutionAgent.
                     Gated by LIVE_TRADING, risk-checked, leverage-capped,
                     stop-loss-mandatory, fully audited. Nothing here touches it
                     and nothing here can reach it.

  THE OPERATOR'S PATH (this file). A human clicking a button in their own
                     browser, with THEIR OWN API keys, which the browser sends
                     per request. CLAUDE.md invariant 1 is explicit that manual
                     human clicks are deliberately out of the Supervisor's scope:
                     *"supervising agents means supervising agents, not
                     overriding the operator."*

This module only ever moved because of a networking constraint — see
`docs/DEPLOYMENT_NETWORKING.md`. It previously lived in
`app/api/exchange/route.ts` and signed the same orders from Vercel, where
Binance's 451 applies. The capability is not new; its location is.

FIVE THINGS THAT KEEP IT FROM BECOMING AN AGENT PATH
-----------------------------------------------------
1. **Write auth on every route.** The Next.js route it replaces had no
   credential of its own. `require_write_auth` means an unauthenticated caller
   who reaches the port cannot place an order — which is precisely the exposure
   `api/exchange.py` warned about, and it is now closed rather than merely
   avoided.
2. **Credentials are per request and never stored.** No key is read from the
   environment, none is persisted, none is logged. A request with no keys can do
   nothing at all — there is no ambient authority here to borrow.
3. **Not registered with the AgentOS kernel or the message bus.** No agent can
   invoke it; there is no event that reaches it.
4. **Listed in `graphs/contracts.FORBIDDEN_IMPORTS`,** so the AST test fails if
   any module under `graphs/` so much as imports it.
5. **Every placed order is persisted and logged at WARNING** with
   `origin_tag='manual-click'`, so an operator order is distinguishable from an
   agent order forever after. The route it replaced recorded nothing server-side.

SPOT ONLY, deliberately, matching `lib/exchangeClients/types.ts`. Futures/margin
mechanics (isolated vs cross, funding, liquidation, position mode) are a
materially larger and riskier piece of work and must be their own deliberate
change, not folded in here.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.auth import require_write_auth
from backend.core.db import get_db_pool

logger = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED = ("binance", "bybit")


class Credentials(BaseModel):
    """Supplied by the operator's browser on every call. Never stored.

    `repr` is overridden so a stray log line, traceback or debugger frame cannot
    print the secret. This costs nothing and removes an entire class of leak —
    the one that happens when someone adds `logger.debug("req=%s", req)` while
    chasing an unrelated bug.
    """

    apiKey: str
    apiSecret: str
    testnet: bool = True

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Credentials(apiKey='***{self.apiKey[-4:] if self.apiKey else ''}', testnet={self.testnet})"

    __str__ = __repr__


class BalanceRequest(Credentials):
    exchange: str


class PlaceOrderRequest(Credentials):
    exchange: str
    symbol: str
    side: str
    qty: float = Field(gt=0)
    # OPTIONAL, and its absence is reported rather than defaulted.
    #
    # These do NOT place resting orders at the venue — `place_order` submits one
    # market order and nothing else. They register the filled position with
    # `PositionMonitorAgent`, which compares it against every tick and closes it
    # through the execution chokepoint when a level is breached.
    #
    # That distinction matters and the response states it: a monitored stop lives
    # in THIS process. If the process is down, nothing is watching. Only a resting
    # stop order at the exchange survives that, and this route does not place one.
    stopLoss: Optional[float] = None
    takeProfit: Optional[float] = None
    # THE IDEMPOTENCY KEY. Sent as Binance's `newClientOrderId` / Bybit's
    # `orderLinkId`, both of which the venue REJECTS duplicates of. It must be
    # deterministic for one logical trade intent — built by
    # lib/executionQuality.ts::buildClientOrderId, never random or timestamped,
    # because a retry has to reproduce it exactly for the rejection to work.
    clientOrderId: Optional[str] = None


class OrderRefRequest(Credentials):
    exchange: str
    symbol: str
    exchangeOrderId: str


def _to_exchange_symbol(app_symbol: str) -> str:
    """'BTC/USDT' -> 'BTCUSDT'. Both venues' spot APIs want the joined form."""
    return app_symbol.replace("/", "").upper().strip()


def _to_ccxt_symbol(app_symbol: str) -> str:
    """ccxt wants the slashed form. Accepts either input so a caller passing
    'BTCUSDT' is not silently rejected — the app stores 'BTC/USDT' but the older
    route accepted both."""
    s = app_symbol.upper().strip()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}/{quote}"
    return s


def _build_client(exchange: str, creds: Credentials):
    """A throwaway ccxt client carrying only this request's credentials.

    NOT the shared `services/exchange_client` singleton, and that is the whole
    point: the singleton holds the BACKEND's keys and is the agent's path to the
    venue. Reusing it here would let an operator request execute with the
    agent's credentials, or worse, leave the operator's credentials on a
    long-lived object every agent shares.

    ccxt is used rather than hand-rolled HMAC because the signing schemes differ
    per venue (Binance: SHA256 over the query string; Bybit V5: SHA256 over
    timestamp+key+recvWindow+payload) and a subtle mistake in either produces a
    rejected order at best and a wrong order at worst.
    """
    import ccxt.async_support as ccxt_async

    if exchange not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f'exchange must be one of {SUPPORTED}')

    factory = getattr(ccxt_async, exchange)
    client = factory({
        "apiKey": creds.apiKey,
        "secret": creds.apiSecret,
        "enableRateLimit": True,
        # SPOT. Explicit rather than relying on the library default, which
        # differs between venues and between ccxt versions — and a silent switch
        # to futures would place a leveraged order where a spot one was intended.
        "options": {"defaultType": "spot"},
    })
    if creds.testnet:
        client.set_sandbox_mode(True)
    return client


def _describe(e: Exception) -> str:
    """A message safe to return to the caller.

    ccxt exceptions can embed the request that produced them, and that request is
    signed with the operator's secret. Only the exception type and its message are
    returned, never the exception's request context.
    """
    return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


@router.post("/balance", dependencies=[Depends(require_write_auth)])
async def get_balance(req: BalanceRequest) -> Dict[str, Any]:
    """Account balances. Returns the same `{ok, snapshot}` shape as before."""
    client = _build_client(req.exchange, req)
    try:
        raw = await client.fetch_balance()
    except Exception as e:
        logger.warning("Operator balance fetch failed on %s: %s", req.exchange, _describe(e))
        return {"ok": False, "error": _describe(e)}
    finally:
        await client.close()

    balances: List[Dict[str, Any]] = []
    for asset, amounts in (raw.get("total") or {}).items():
        free = (raw.get("free") or {}).get(asset) or 0.0
        used = (raw.get("used") or {}).get(asset) or 0.0
        # Zero rows are dropped: every venue lists hundreds of assets, and the
        # handful the operator actually holds is what the UI needs to show.
        if not (amounts or free or used):
            continue
        balances.append({"asset": asset, "free": float(free), "locked": float(used)})

    balances.sort(key=lambda b: b["free"] + b["locked"], reverse=True)
    return {"ok": True, "snapshot": {"balances": balances}}


# ---------------------------------------------------------------------------
# Place order — the one route in this file that moves money
# ---------------------------------------------------------------------------


@router.post("/order", dependencies=[Depends(require_write_auth)])
async def place_order(req: PlaceOrderRequest) -> Dict[str, Any]:
    """Place a MARKET order with the operator's own credentials.

    NO RISK CHECK RUNS HERE, AND THAT IS THE EXISTING, DELIBERATE BEHAVIOUR
    RATHER THAN AN OVERSIGHT. CLAUDE.md invariant 1: manual human clicks are out
    of the Supervisor's scope. The route this replaces behaved identically; the
    operator's own judgment and their exchange's own limits are the controls.

    What is NOT skipped is the record. Every attempt is logged at WARNING and
    every fill is persisted with `origin_tag='manual-click'`, so an operator
    order can never be mistaken for an agent order after the fact.
    """
    side = req.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail='side must be "buy" or "sell"')

    ccxt_symbol = _to_ccxt_symbol(req.symbol)
    native_symbol = _to_exchange_symbol(req.symbol)

    # BEFORE the order, not after. If this process dies mid-request, the log
    # already says an order was attempted — an order with no record of the
    # attempt is the state that cannot be reconciled.
    logger.warning(
        "OPERATOR ORDER (manual, unsupervised): %s %s %s on %s testnet=%s clientOrderId=%s",
        side, req.qty, native_symbol, req.exchange, req.testnet, req.clientOrderId,
    )

    params: Dict[str, Any] = {}
    if req.clientOrderId:
        # Native parameter names per venue. ccxt's generic `clientOrderId` is
        # mapped for most venues but not consistently across versions, and this
        # is the field that makes a retry safe — it must not depend on the
        # library's mapping table being right.
        params["newClientOrderId" if req.exchange == "binance" else "orderLinkId"] = req.clientOrderId

    client = _build_client(req.exchange, req)
    try:
        order = await client.create_order(ccxt_symbol, "market", side, req.qty, None, params)
    except Exception as e:
        logger.error(
            "OPERATOR ORDER REJECTED: %s %s %s on %s — %s",
            side, req.qty, native_symbol, req.exchange, _describe(e),
        )
        return {"ok": False, "error": _describe(e)}
    finally:
        await client.close()

    exchange_order_id = str(order.get("id") or "")
    filled = order.get("filled")
    avg = order.get("average") or order.get("price")

    logger.warning(
        "OPERATOR ORDER PLACED: id=%s status=%s filled=%s avg=%s",
        exchange_order_id, order.get("status"), filled, avg,
    )

    record = await _persist_operator_trade(
        exchange_order_id=exchange_order_id,
        symbol=req.symbol,
        side=side,
        qty=float(filled) if filled else req.qty,
        price=float(avg) if avg else None,
        testnet=req.testnet,
    )

    monitor = await _register_real_position(
        req=req, side=side,
        filled_qty=float(filled) if filled else req.qty,
        fill_price=float(avg) if avg is not None else None,
    )

    return {
        "ok": True,
        "exchangeOrderId": exchange_order_id,
        # Whether anything in this process is enforcing the stop. Reported on
        # every order, including when no stop was asked for, because "nothing is
        # watching this position" is not something an operator should have to
        # infer from the absence of a field.
        "monitored": monitor["monitored"],
        "monitorNote": monitor["note"],
        # The venue's own status string, not normalized further — the frontend
        # already displays it verbatim and normalizing would lose detail the
        # operator may need.
        "status": str(order.get("status") or "unknown"),
        "filledQty": float(filled) if filled is not None else None,
        "avgFillPrice": float(avg) if avg is not None else None,
        # Whether a LOCAL record exists, which is not the same question as
        # whether the order exists. Surfaced so the operator is never left
        # believing the trade log is complete when it is not.
        "recorded": record["recorded"],
        **({"recordNote": record["note"]} if record["note"] else {}),
        "raw": order.get("info"),
    }


async def _register_real_position(*, req, side: str, filled_qty: float, fill_price):
    """Put a REAL manual fill under the stop-loss watcher. Never raises.

    WHY THIS IS CONDITIONAL AND NOT UNCONDITIONAL
    ---------------------------------------------
    The monitor closes a breached position by calling
    `ExecutionAgent.close_position`, which uses `get_exchange_client()` — the
    BACKEND'S OWN credentials from .env. This route, by design, places the order
    with the OPERATOR'S credentials, sent per request and never stored.

    So if the backend has no credentials of its own, the monitor can watch the
    price cross the stop and can do nothing about it. Registering the position
    anyway would put a row in the positions view that looks protected and is not,
    which is worse than not registering it: an operator who can see a stop
    believes it will fire.

    The two credential sets are also not verified to be the same account. Nothing
    here can check that, so the note says so rather than implying otherwise.

    WHAT THIS DOES NOT DO. It does not place a resting stop order at the venue.
    A monitored stop lives in this process and stops existing when this process
    does — `agents/execution_agent.py` has said as much for a long time and it is
    still true. The note repeats it at the point the operator is deciding.
    """
    if side != "buy":
        return {
            "monitored": False,
            "note": "A sell reduces or closes an existing position; no stop applies to it.",
        }

    if req.stopLoss is None:
        return {
            "monitored": False,
            "note": (
                "NO STOP-LOSS WAS SET, so nothing in this process is watching this "
                "REAL position and nothing will close it automatically. A stop was "
                "not computed for you — the distance you are willing to lose is not "
                "something this route can infer."
            ),
        }

    if fill_price is None or fill_price <= 0:
        return {
            "monitored": False,
            "note": (
                "The venue reported no average fill price, so there is no honest "
                "entry to measure a stop against. The ORDER WENT THROUGH — check it "
                "at the exchange and set a stop there."
            ),
        }

    try:
        from backend.services.exchange_client import get_exchange_client

        backend_has_keys = get_exchange_client().has_credentials()
    except Exception:  # noqa: BLE001
        backend_has_keys = False

    if not backend_has_keys:
        return {
            "monitored": False,
            "note": (
                "NOT MONITORED: this order used YOUR keys, but the monitor closes a "
                "breached position through the backend's own credentials "
                "(BINANCE_API_KEY / BINANCE_SECRET in .env), which are not set. It "
                "could watch the stop and would be unable to act on it, so the "
                "position is deliberately not registered rather than shown as "
                "protected. Set the backend's keys, or manage this stop at the venue."
            ),
        }

    try:
        from backend.agents.position_monitor import get_position_monitor

        await get_position_monitor().track_manual_position(
            symbol=req.symbol,
            side=side,
            qty=filled_qty,
            entry_price=fill_price,
            stop_loss=float(req.stopLoss),
            take_profit=float(req.takeProfit) if req.takeProfit is not None else None,
            tab="paper" if req.testnet else "real",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "REAL position %s filled but could NOT be registered with the monitor: %s. "
            "Nothing is enforcing its stop.", req.symbol, exc,
        )
        return {
            "monitored": False,
            "note": f"the position monitor could not be reached ({exc}); nothing is enforcing this stop",
        }

    return {
        "monitored": True,
        "note": (
            "Stop registered with PositionMonitorAgent: it is compared against every "
            "tick and can only ever be TIGHTENED. NOTE that this is an IN-PROCESS "
            "stop, not a resting order at the exchange — if this backend is down, "
            "nothing is watching. Only a stop order placed at the venue survives that."
        ),
    }


async def _persist_operator_trade(
    *,
    exchange_order_id: str,
    symbol: str,
    side: str,
    qty: float,
    price: Optional[float],
    testnet: bool,
) -> Dict[str, Any]:
    """Record the fill in `trades`. Logged, never raised.

    Returns `{"recorded": bool, "note": str|None}` so the caller can tell the
    operator whether a local record exists — that is not always the same as
    whether the order exists.

    The order is already at the exchange by the time this runs. Failing the
    caller because the database is down would tell the operator their order
    failed when it did not — the worst possible lie on this path.

    `tab` is 'paper' on testnet and 'real' on mainnet, so testnet fills never mix
    into real history — the same rule `settings.execution_tab` applies to the
    agent's own fills.

    A ROW IS NOT WRITTEN WITHOUT A FILL PRICE, AND THAT IS NOT LAZINESS.

    `db/schema.sql` declares `trades.price` as `numeric NOT NULL`, and the reason
    it must stay that way is downstream: `lib/tradeStore.server.ts` reads it as
    `toNumber(r.price) ?? 0`, so a NULL would surface in the operator's trade log
    as A TRADE AT PRICE ZERO. Relaxing the constraint would trade an honest gap
    for a plausible-looking lie, which is the exact failure mode CLAUDE.md
    invariant 6 forbids — and it would poison every P&L and equity figure derived
    from the log.

    A `trades` row means "a fill happened at this price". An order the venue
    accepted but has not reported a fill for is an ORDER, not a trade, and it is
    reported as such: logged at ERROR with the id and the reconciliation step,
    and flagged in the response.
    """
    tab = "paper" if testnet else "real"

    if price is None:
        # Rare for a spot market order — both venues normally return fills
        # synchronously — but possible for one accepted and queued.
        logger.error(
            "OPERATOR ORDER %s (%s %s %s, tab=%s) was ACCEPTED BY THE EXCHANGE but reported "
            "no fill price, so no trade row was written. The order is REAL and is not in the "
            "local log. Reconcile with: POST /api/operator/exchange/order/status "
            "{exchangeOrderId: %s}.",
            exchange_order_id, side, qty, symbol, tab, exchange_order_id,
        )
        return {
            "recorded": False,
            "note": (
                "Order placed, but the exchange reported no fill price, so it was NOT written "
                "to the local trade log. Check the order status and record it manually if it "
                "filled."
            ),
        }

    pool = get_db_pool()
    if not pool:
        logger.error(
            "Operator order %s (%s %s %s) executed but NOT persisted: no database pool. "
            "This order exists at the exchange with no local record.",
            exchange_order_id, side, qty, symbol,
        )
        return {"recorded": False, "note": "No database configured — the order is not in the local trade log."}

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trades (id, ts, tab, symbol, side, qty, price, origin_tag, exchange_order_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                str(uuid.uuid4()),
                datetime.datetime.utcnow(),
                tab,
                symbol,
                side,
                qty,
                price,
                # The tag that separates this from every agent order, forever.
                "manual-click",
                exchange_order_id,
            )
    except Exception as e:
        logger.error("Failed to persist operator order %s: %s", exchange_order_id, e)
        return {"recorded": False, "note": f"Order placed, but the local record failed: {e}"}

    return {"recorded": True, "note": None}


# ---------------------------------------------------------------------------
# Order status and cancel
# ---------------------------------------------------------------------------


@router.post("/order/status", dependencies=[Depends(require_write_auth)])
async def order_status(req: OrderRefRequest) -> Dict[str, Any]:
    """Status of one order.

    Auth-gated despite being a read: it takes credentials, and an endpoint that
    accepts credentials is a place an attacker can probe with stolen ones.
    """
    client = _build_client(req.exchange, req)
    try:
        order = await client.fetch_order(req.exchangeOrderId, _to_ccxt_symbol(req.symbol))
    except Exception as e:
        return {"ok": False, "error": _describe(e)}
    finally:
        await client.close()

    filled = order.get("filled")
    avg = order.get("average") or order.get("price")
    return {
        "ok": True,
        "status": str(order.get("status") or "unknown"),
        "filledQty": float(filled) if filled is not None else None,
        "avgFillPrice": float(avg) if avg is not None else None,
        "raw": order.get("info"),
    }


@router.post("/order/cancel", dependencies=[Depends(require_write_auth)])
async def cancel_order(req: OrderRefRequest) -> Dict[str, Any]:
    """Cancel a resting order.

    NEVER BLOCKED, by anything. CLAUDE.md invariant 4 is about exits, and the
    same reasoning applies to cancelling: refusing to let an operator undo an
    order they have already placed is actively harmful, and it holds for real
    money more, not less.
    """
    logger.warning(
        "OPERATOR CANCEL: order %s on %s (%s)",
        req.exchangeOrderId, req.exchange, _to_exchange_symbol(req.symbol),
    )
    client = _build_client(req.exchange, req)
    try:
        result = await client.cancel_order(req.exchangeOrderId, _to_ccxt_symbol(req.symbol))
    except Exception as e:
        return {"ok": False, "error": _describe(e)}
    finally:
        await client.close()

    return {"ok": True, "raw": result.get("info") if isinstance(result, dict) else result}
