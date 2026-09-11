"""Autonomous trading sessions — `/api/session`.

The operator sets a symbol, a leverage ceiling and a target equity, and the agent
trades toward it on its own until it gets there, hits the floor, or is stopped.

WHAT THE START ROUTE ACTUALLY AUTHORISES
----------------------------------------
It is the most consequential button in this application, so it is worth being
precise about what pressing it means. It does NOT place an order. It starts a
loop that runs the ordinary decision chain on a timer:

    analysis graph -> Supervisor -> Risk Gateway -> EXECUTION_PLAN_READY
                   -> Execution Service -> CRO -> ExecutionAgent -> monitor

Every gate in that chain still applies. The session cannot size a position, cannot
raise the leverage ceiling, cannot skip the mandatory stop-loss, and cannot
approve its own trades. With `LIVE_TRADING=true` those orders are real, which is
why the route is write-authenticated and says so in its response.

THE TARGET DOES NOT INFLUENCE SIZING
------------------------------------
See `services/trading_session.py` — it is read only by the check that decides
whether to stop. CLAUDE.md forbids encoding a return multiple as an objective
because it drives a system toward taking more risk the further behind it falls,
and a session that sized up to catch up would be a martingale.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.auth import require_write_auth
from backend.core.config import settings
from backend.services import trading_session as sessions

logger = logging.getLogger(__name__)

router = APIRouter()


class StartSessionRequest(BaseModel):
    symbol: str = Field(..., min_length=3)
    leverage: int = Field(1, ge=1)
    # BOTH OF THESE ARE ACCOUNT AMOUNTS IN USD, NOT COIN PRICES.
    #
    # "$2 into $5" is a statement about the WALLET, and the session ends when the
    # account reaches it — whatever SOL happens to be worth at the time. A coin
    # price target would be a different and much weaker instruction: it says
    # nothing about how much was staked, so the same price move could double the
    # account or barely move it.
    targetEquity: float = Field(..., gt=0)
    # PAPER ONLY. Sets the paper book's cash so the run genuinely happens at that
    # size — see `trading_session.set_paper_starting_amount`. Rejected for a real
    # session, where the starting amount is the exchange's own balance.
    startAmount: Optional[float] = Field(None, gt=0)
    # Optional. Defaults to half the starting equity — see DEFAULT_FLOOR_FRACTION.
    floorEquity: Optional[float] = Field(None, ge=0)
    # How much of the account this session may trade with: 0.25 / 0.5 / 0.75 / 1.0
    # (the home page shows them as 25/50/75/100%). Defaults to 1.0 = the whole
    # account, the pre-feature behaviour. Bounded (0, 1]; `start_session` clamps
    # anything else back to 1.0. Applies to both paper and real.
    capitalFraction: float = Field(1.0, gt=0, le=1.0)
    # Optional daily profit target as a FRACTION (0.02 = +2%). Once the day is up
    # this much vs. its start, the session stops opening new positions until the
    # next UTC day, then resumes — "take 2% a day over many trades". None = off.
    # Bounded (0, 0.5]; `start_session` clamps anything else to None.
    dailyTargetPct: Optional[float] = Field(None, gt=0, le=0.5)


@router.get("")
async def session_status() -> Dict[str, Any]:
    """The running session (if any), recent ones, and what a session can/cannot do."""
    active = sessions.active_session()
    tab = "real" if settings.LIVE_TRADING else "paper"

    equity = await sessions.current_equity(tab)

    # On the real book this IS the starting amount and it is not typeable; on
    # paper it is None and the operator supplies one. Surfaced separately from
    # `currentEquity` so the panel can label the field as fetched rather than
    # editable without having to infer that from the tab.
    real_balance = await sessions.real_account_balance() if tab == "real" else None

    # The three parts of equity. A single "Account now" figure sitting at 10,000
    # looks identical whether the book is FLAT or the number is STUCK, and those
    # have opposite responses.
    breakdown = await sessions.equity_breakdown(tab)

    return {
        "active": active.as_dict() if active else None,
        "equityBreakdown": breakdown,
        # Progress toward the stop condition, computed in the service so the
        # number the operator reads and the one that ends the session cannot
        # disagree. None when there is no session — not 0, which would read as
        # "started and got nowhere".
        "progress": sessions.session_progress(active, equity) if active else None,
        "recent": [s.as_dict() for s in sessions.list_sessions(limit=10)],
        "tab": tab,
        "liveTrading": settings.LIVE_TRADING,
        "currentEquity": equity,
        # The real account's free USDT, straight from the venue, cached 30s
        # because this endpoint is polled every 5s and the balance moves only on
        # a fill. None on paper, and None (never 0.0) when it cannot be read.
        "realBalance": real_balance,
        "realBalanceError": sessions.real_balance_error() if tab == "real" else None,
        "startAmountEditable": tab != "real",
        "startAmountMeaning": (
            "The starting amount is your ACCOUNT balance, not a coin price. On the "
            "real book it is fetched from the exchange and cannot be typed — a "
            "typed figure would set the denominator of every percentage while the "
            "venue held a different number. On paper you set it, and it is written "
            "to the paper book's cash so the run happens at that size for real: "
            "position sizing, P&L and the progress bar are all about the amount "
            "you chose."
        ),
        "equityNote": (
            None if equity is not None else
            "equity is not measurable right now — either the book reports no cash "
            "figure or an open position has no live price. A session cannot start "
            "without it, because it would have no definition of done."
        ),
        "decisionIntervalSeconds": sessions.DECISION_INTERVAL_S,
        "maxSessionHours": sessions.MAX_SESSION_HOURS,
        "maxTrades": sessions.MAX_TRADES_PER_SESSION,
        "targetMeaning": (
            "The target is a STOP CONDITION, not a sizing input. It is read only by "
            "the check that ends the session. Position sizes come from the Risk "
            "Gateway and are unaffected by how far the session is from its target — "
            "an agent that sized up to catch up would be a martingale, and CLAUDE.md "
            "forbids encoding a return multiple as an objective for that reason."
        ),
        "floorMeaning": (
            "Every session has a floor it stops at. 'Trade until the target' with no "
            "lower bound means 'trade until zero', because a losing session has no "
            "other terminating condition."
        ),
        "stopMeaning": (
            "Stopping a session does NOT close open positions. The position monitor "
            "keeps enforcing their stop-losses; flattening the book from a button "
            "labelled 'stop' would be a large irreversible trade nobody asked for."
        ),
    }


@router.post("/start", dependencies=[Depends(require_write_auth)])
async def start(req: StartSessionRequest) -> Dict[str, Any]:
    """Start one autonomous session. With LIVE_TRADING on, this trades real funds."""
    try:
        session = await sessions.start_session(
            symbol=req.symbol,
            leverage=req.leverage,
            target_equity=req.targetEquity,
            floor_equity=req.floorEquity,
            start_amount=req.startAmount,
            capital_fraction=req.capitalFraction,
            daily_target_pct=req.dailyTargetPct,
        )
    except ValueError as exc:
        # A refusal the operator can act on, not a server fault.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.warning(
        "OPERATOR STARTED AUTONOMOUS SESSION %s on %s toward %.2f. LIVE_TRADING=%s.",
        session.id, session.symbol, session.target_equity, settings.LIVE_TRADING,
    )

    return {
        "status": "success",
        "session": session.as_dict(),
        "warning": (
            "LIVE_TRADING is ON — this session places REAL orders with real funds."
            if settings.LIVE_TRADING else
            "Paper mode: every order is simulated and no real funds are at risk."
        ),
    }


@router.post("/stop", dependencies=[Depends(require_write_auth)])
async def stop(body: Dict[str, Any] = {}) -> Dict[str, Any]:
    """Stop the running session, or a specific one by id."""
    session_id = body.get("sessionId")
    if not session_id:
        active = sessions.active_session()
        if active is None:
            return {"status": "success", "message": "No session is running.", "session": None}
        session_id = active.id

    session = await sessions.stop_session(str(session_id))
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")

    return {
        "status": "success",
        "session": session.as_dict(),
        "note": (
            "Open positions were NOT closed. They remain under the position "
            "monitor's stop-loss enforcement — close them deliberately."
        ),
    }
