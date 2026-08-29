"""The operator's manual trade panel — `/api/operator/trade`.

WHAT THIS IS, AND WHY IT IS SEPARATE FROM EVERYTHING ELSE
---------------------------------------------------------
A human clicking Buy or Sell. It is the OPERATOR plane, not the agent plane, and
CLAUDE.md invariant 1 is explicit that the two are different:

    "components/Supervisor.tsx's reviewAndExecute() is the single execution path
     for every AI-originated trade ... Manual human clicks are deliberately out
     of scope — supervising agents means supervising agents, not overriding the
     operator."

So this route does NOT go through the Supervisor, the CRO or the debate. It is a
person deciding to trade. What it does NOT get to skip is the leverage ceiling,
because that one is not a supervision rule — it is a hard limit on the account.

WHY IT IS NOT FOLDED INTO api/operator_exchange.py
--------------------------------------------------
That module is the REAL-money path: it takes the operator's own keys per request,
stores nothing, and reaches a live venue. This one is mostly about the PAPER book,
which needs no credentials and touches no venue. Keeping them in one file would
put a credential-less code path next to an order-placing one and make "which of
these can move real money?" a question you answer by reading carefully.

For a REAL order this module deliberately does not reimplement anything: the panel
posts to `/api/operator/exchange/order`, which already owns idempotency keys,
venue symbol mapping and `origin_tag='manual-click'` persistence.

THE RATE-LIMIT PROBLEM THIS SOLVES
-----------------------------------
A trade panel wants price, balance and instrument rules together, and it wants
them whenever the operator changes the symbol. Fetching all three per keystroke
is how a shared exchange rate limit gets spent on a form nobody has submitted.

So `GET /context` answers all of it in ONE call, and the expensive half — the
authenticated balance — is CACHED with a TTL and only refreshed when the operator
explicitly asks. Price comes from the tick cache the backend already maintains
over a single websocket, so it costs no upstream call at all.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.core.auth import require_write_auth
from backend.core.config import settings
from backend.core.risk_manager import check_leverage, max_leverage_ceiling

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Balance cache
# ---------------------------------------------------------------------------
#
# 30 seconds. Long enough that changing the symbol, the side and the size in the
# panel costs ONE authenticated call rather than one per interaction; short
# enough that the number an operator sizes against is not meaningfully old.
#
# The cached value carries its own age and the panel shows it, because a balance
# presented without an age is presented as current — and the whole reason this
# cache exists is that it sometimes is not.
BALANCE_TTL_S = 30.0

_balance_cache: Dict[str, Any] = {"at": 0.0, "value": None, "error": None}


def _cached_balance_age() -> Optional[float]:
    if not _balance_cache["at"]:
        return None
    return time.monotonic() - _balance_cache["at"]


async def _real_balance(force: bool) -> Dict[str, Any]:
    """The live account balance, cached. Never raises.

    Returns `{available, currency, ageSeconds, cached, error}`. `available` is
    None when it could not be read — never 0.0, which an operator would size
    against as though the account were empty rather than unreadable.
    """
    age = _cached_balance_age()
    if not force and age is not None and age < BALANCE_TTL_S and _balance_cache["value"] is not None:
        return {
            **_balance_cache["value"],
            "ageSeconds": round(age, 1),
            "cached": True,
            "error": None,
        }

    try:
        from backend.services.exchange_client import get_exchange_client

        client = get_exchange_client()
        if not client.has_credentials():
            return {
                "available": None,
                "currency": "USDT",
                "ageSeconds": None,
                "cached": False,
                "error": (
                    "No exchange credentials are configured (BINANCE_API_KEY / "
                    "BINANCE_SECRET are empty), so the real balance cannot be read. "
                    "This is an unauthenticated client, NOT an empty account."
                ),
            }
        raw = await client.fetch_balance()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Operator balance read failed: %s", exc)
        return {
            "available": None,
            "currency": "USDT",
            "ageSeconds": None,
            "cached": False,
            "error": f"balance read failed ({type(exc).__name__}: {exc})",
        }

    available: Optional[float] = None
    if isinstance(raw, dict):
        free = raw.get("free")
        if isinstance(free, dict):
            candidate = free.get("USDT")
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                available = float(candidate)

    if available is None:
        return {
            "available": None,
            "currency": "USDT",
            "ageSeconds": None,
            "cached": False,
            "error": "the exchange answered but carried no usable USDT free balance",
        }

    value = {"available": available, "currency": "USDT"}
    _balance_cache["at"] = time.monotonic()
    _balance_cache["value"] = value
    return {**value, "ageSeconds": 0.0, "cached": False, "error": None}


def _price_for(symbol: str) -> Optional[float]:
    """Last price from the caches the backend already keeps. No upstream call.

    Reads the same `get_price` every graph node uses, so the number in the panel
    and the number the agent reasons over are the same number.
    """
    from backend.services.market_data import get_price

    price = get_price(symbol)
    return float(price) if isinstance(price, (int, float)) and price > 0 else None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@router.get("/context")
async def trade_context(
    symbol: str = Query("BTC/USDT"),
    refresh_balance: bool = Query(False, alias="refreshBalance"),
) -> Dict[str, Any]:
    """Everything the trade panel needs, in one call.

    ONE CALL ON PURPOSE. The panel needs price, balance, the leverage ceiling and
    the instrument's size rules together; four endpoints would mean four round
    trips every time the operator picks a different coin, and the authenticated
    one is rate-limited.
    """
    from backend.services.portfolio_store import get_portfolio

    tab = "real" if settings.LIVE_TRADING else "paper"
    price = _price_for(symbol)

    portfolio = await get_portfolio()
    paper = (portfolio or {}).get("paper") or {}
    paper_cash = paper.get("cash")

    balance: Dict[str, Any]
    if tab == "real":
        balance = await _real_balance(force=refresh_balance)
    else:
        # The paper book is in-process; there is no rate limit and no staleness.
        balance = {
            "available": float(paper_cash) if isinstance(paper_cash, (int, float)) else None,
            "currency": "USDT",
            "ageSeconds": 0.0,
            "cached": False,
            "error": None if isinstance(paper_cash, (int, float)) else "paper cash unavailable",
        }

    rules: Dict[str, Any] = {}
    try:
        from backend.services.instrument_rules import get_rules

        # `get_rules` is async and cached by the service; the field is
        # `step_size`, not `qty_step`. Both were wrong on the first pass and the
        # panel would have shown "step: null" for every instrument.
        r = await get_rules(symbol)
        rules = {
            "minQty": r.min_qty,
            "stepSize": r.step_size,
            "tickSize": r.tick_size,
            "minNotional": r.min_notional,
            # Non-None means every number above is UNKNOWN, not absent-and-fine.
            "unavailable": r.unavailable,
        }
    except Exception as exc:  # noqa: BLE001
        rules = {"error": f"instrument rules unavailable: {exc}"}

    positions = paper.get("positions") if tab == "paper" else (portfolio or {}).get("real", {}).get("positions")

    return {
        "symbol": symbol,
        "tab": tab,
        "liveTrading": settings.LIVE_TRADING,
        "price": price,
        "priceNote": (
            None if price is not None else
            "no live price for this symbol yet — the tick cache fills from the "
            "exchange websocket a few seconds after startup, and an order cannot "
            "be sized against a price that is not known"
        ),
        "balance": balance,
        "leverageCeiling": max_leverage_ceiling(tab),
        "leverageNote": (
            f"{max_leverage_ceiling(tab)}x is a HARD ceiling for the '{tab}' tab. It is "
            f"not operator-configurable and no setting, agent or confidence level can "
            f"raise it (CLAUDE.md invariant 2)."
        ),
        "instrumentRules": rules,
        "openPositions": positions or [],
        "credentialsConfigured": _credentials_configured(),
        "balanceCacheSeconds": BALANCE_TTL_S,
    }


def _credentials_configured() -> bool:
    try:
        from backend.services.exchange_client import get_exchange_client

        return bool(get_exchange_client().has_credentials())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Placing a manual PAPER trade
# ---------------------------------------------------------------------------

class PaperTradeRequest(BaseModel):
    symbol: str = Field(..., min_length=3)
    side: str = Field(..., pattern="^(buy|sell)$")
    qty: float = Field(..., gt=0)
    leverage: float = Field(1.0, gt=0)
    # Optional, and its absence is REPORTED rather than defaulted. See the route.
    stopLoss: Optional[float] = None
    takeProfit: Optional[float] = None


@router.post("/paper", dependencies=[Depends(require_write_auth)])
async def place_paper_trade(req: PaperTradeRequest) -> Dict[str, Any]:
    """Place a manual trade on the PAPER book. Touches no exchange.

    REFUSES IN LIVE MODE. With `LIVE_TRADING=true` the operator's intent when
    they press Buy is a real order, and quietly writing a paper one instead would
    be the most dangerous possible interpretation of an ambiguous click — the
    operator would believe they had a position they do not have. The panel routes
    to `/api/operator/exchange/order` in live mode, which needs their keys.

    THE LEVERAGE CEILING IS ENFORCED HERE TOO. It is not a supervision rule that
    manual trading is exempt from; it is a hard limit on the account, and
    `check_leverage` is the same function the Risk Gateway calls.
    """
    if settings.LIVE_TRADING:
        raise HTTPException(
            status_code=409,
            detail=(
                "LIVE_TRADING is ON, so this route refuses to write a paper trade. "
                "A Buy pressed in live mode means a real order; silently booking a "
                "simulated one would leave you believing you hold a position you do "
                "not hold. Use /api/operator/exchange/order with your credentials, "
                "or turn live trading off first."
            ),
        )

    leverage_check = check_leverage(req.leverage, tab="paper")
    if leverage_check.status == "reject":
        raise HTTPException(status_code=400, detail=leverage_check.detail)

    price = _price_for(req.symbol)
    if price is None:
        # Never invent a fill price. `create_market_order` once returned a
        # fabricated $60,000 fill on error and it reached the P&L and the audit
        # trail; that is the failure this refusal exists to prevent.
        raise HTTPException(
            status_code=503,
            detail=(
                f"No live price for {req.symbol}, so there is no honest price to fill "
                f"against. The tick cache fills from the exchange websocket shortly "
                f"after startup — retry in a few seconds."
            ),
        )

    from backend.services.portfolio_store import buy_paper, get_portfolio, sell_paper

    if req.side == "buy":
        ok = await buy_paper(req.symbol, req.qty, price, req.leverage)
        if not ok:
            portfolio = await get_portfolio()
            cash = ((portfolio or {}).get("paper") or {}).get("cash")
            margin = (req.qty * price) / req.leverage
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient paper cash: this order needs {margin:,.2f} margin "
                    f"({req.qty} x {price:,.2f} at {req.leverage}x) and the book holds "
                    f"{cash:,.2f}."
                ),
            )
    else:
        ok = await sell_paper(req.symbol, req.qty, price)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot sell {req.qty} {req.symbol}: the paper book does not hold "
                    f"that much. A short entry is not supported on the paper book — "
                    f"sell closes an existing long."
                ),
            )

    monitored = False
    monitor_note: str
    if req.side == "buy" and req.stopLoss is not None:
        monitored = await _register_with_monitor(req, price)
        monitor_note = (
            "Stop-loss registered with PositionMonitorAgent; it is enforced on every "
            "tick and can only ever be TIGHTENED."
            if monitored else
            "Stop-loss was supplied but the position monitor could not be reached, so "
            "NOTHING is enforcing it. Close this position manually."
        )
    elif req.side == "buy":
        # Stated loudly rather than defaulted. Inventing a stop would be inventing
        # the operator's risk tolerance.
        monitor_note = (
            "NO STOP-LOSS WAS SET, so this position is NOT monitored and nothing will "
            "close it automatically. That is the consequence of leaving the field "
            "empty — a stop was not computed for you, because the distance you are "
            "willing to lose is not something this route can infer."
        )
    else:
        monitor_note = "A sell closes an existing position; no stop applies."

    portfolio = await get_portfolio()
    return {
        "status": "success",
        "tab": "paper",
        "symbol": req.symbol,
        "side": req.side,
        "qty": req.qty,
        "fillPrice": price,
        "leverage": req.leverage,
        "notional": req.qty * price,
        "monitored": monitored,
        "monitorNote": monitor_note,
        "portfolio": (portfolio or {}).get("paper"),
        "note": (
            "Simulated on the in-process paper book. No exchange was contacted and no "
            "real funds moved."
        ),
    }


async def _register_with_monitor(req: PaperTradeRequest, price: float) -> bool:
    """Hand a manual paper position to the stop-loss watcher. Never raises.

    Delegates to `PositionMonitorAgent.track_manual_position`, which is the
    documented boundary between the operator plane and the agent plane. See that
    method for why this does NOT publish TAR_APPROVED / ORDER_FILLED — the short
    version is that a TAR means "the CRO approved this", and minting one for a
    human's click would put a fabricated approval in the audit trail while also
    making the agent's own executor place a duplicate order.

    A failure here is logged and reported to the caller as `monitored: False`,
    because the trade IS on the book either way and the operator needs to know
    that nothing is enforcing its stop.
    """
    try:
        from backend.agents.position_monitor import get_position_monitor

        await get_position_monitor().track_manual_position(
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            entry_price=price,
            stop_loss=float(req.stopLoss),
            take_profit=float(req.takeProfit) if req.takeProfit is not None else None,
            tab="paper",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Could not register the manual position with the monitor: %s. The trade "
            "IS on the book but its stop is NOT enforced.", exc,
        )
        return False
