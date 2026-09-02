"""Admin / human-in-the-loop API (`/api/admin`).

`agents/trading_agent.py:11` already imported `is_system_paused` from here,
and root `ARCHITECTURE.md` already documents `/pause` and
`/emergency-stop` as existing endpoints — but the module was never written,
so that import raised ImportError and took backend startup with it.

All state lives in `core/system_state.py`; this module is only the HTTP
surface over it. See that module's docstring for why the kill switch is not
owned here (short version: two API modules each holding their own copy of
the pause flag means pausing via one does not stop readers of the other).

Spec Section 22.8 frames the worst case as *"the bot goes silent while
holding a leveraged position"*. These endpoints are the operator's answer
to the inverse case — the bot is very much awake and they want it to stop.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.auth import auth_status, require_write_auth
# `settings` was USED BY THREE ROUTES AND NEVER IMPORTED, so each raised
# NameError -> HTTP 500:
#
#   GET  /trading-mode           the Settings page's mode display
#   POST /live-trading/enable    turning real-money trading ON
#   POST /live-trading/disable   turning it OFF  <-- the dangerous one
#
# The disable route raised on its FIRST statement, so it failed safe (the flag
# never changed) — but the documented way to switch back to paper over HTTP was
# dead. Found by sweeping every endpoint rather than by any test: nothing
# imported these three functions, so an import-time NameError could not surface.
from backend.core.config import settings
from backend.core.db import get_db_pool

from backend.core.system_state import (
    is_emergency_stopped,
    is_system_paused,
    pause,
    resume,
    snapshot,
    trigger_emergency_stop,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Re-exported so `from backend.api.admin import is_system_paused` (the
# existing call site in agents/trading_agent.py) keeps working. The real
# implementation is in core.system_state.
__all__ = ["router", "is_system_paused", "is_emergency_stopped"]


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    """Current kill-switch state, for the dashboard's status indicator."""
    state = snapshot()
    return {
        "status": "success",
        "isPaused": state["is_paused"],
        "emergencyStop": state["emergency_stop"],
        # Stated explicitly so a UI can't imply that a pause also blocks
        # exits — it does not, by design (CLAUDE.md invariant 4).
        "exitsAllowed": True,
        # Surfaced so an operator can confirm the service is protected rather
        # than assume it. When TRADES_API_KEY is unset this reports it plainly,
        # because "I thought auth was on" is how an open port stays open.
        "auth": auth_status(),
    }


@router.post("/pause", dependencies=[Depends(require_write_auth)])
async def pause_system() -> Dict[str, Any]:
    """Halt new position entries. Open positions stay monitored and closable."""
    pause("POST /api/admin/pause")
    return {
        "status": "success",
        "message": "New entries halted. Open positions are still monitored and can still be closed.",
    }


@router.post("/resume", dependencies=[Depends(require_write_auth)])
async def resume_system() -> Dict[str, Any]:
    """Clear pause and emergency stop."""
    resume("POST /api/admin/resume")
    return {"status": "success", "message": "System resumed."}


@router.post("/emergency-stop", dependencies=[Depends(require_write_auth)])
async def emergency_stop() -> Dict[str, Any]:
    """Halt all new entries and mark every running task stopped.

    IMPORTANT — what this does NOT do: it does not market-close open
    positions. It stops the system from acting further and hands control
    back to the operator, who then closes positions deliberately.

    That restraint is intentional. An automatic "close everything at
    market" triggered by a panic button is itself a large, irreversible,
    slippage-bearing trade fired during exactly the conditions (fast market,
    possibly a broken data feed) where it is most likely to execute badly —
    and it would run on the same code path the operator has just declared
    untrustworthy by hitting the emergency stop.

    `ARCHITECTURE.md` and the old dashboard docstring both claimed this
    endpoint "market orders out of positions". It never did, and the
    docstrings are corrected rather than the behaviour, because stopping is
    the safe half and closing is the operator's call.
    """
    trigger_emergency_stop("POST /api/admin/emergency-stop")

    # Imported here rather than at module scope: api/agents.py imports
    # nothing from this module, but keeping the dependency inside the
    # function avoids any future import cycle between the two API modules.
    from backend.api.agents import _tasks, save_tasks

    stopped = []
    still_open = []
    for task_id, task in _tasks.items():
        if task.get("status") == "running":
            task["status"] = "stopped"
            stopped.append(task_id)
            if task.get("currentEntryPrice"):
                still_open.append(
                    {
                        "taskId": task_id,
                        "symbol": task.get("symbol"),
                        "qty": task.get("currentQty"),
                        "entryPrice": task.get("currentEntryPrice"),
                    }
                )
    save_tasks()

    if still_open:
        logger.critical(
            "EMERGENCY STOP: %d task(s) stopped, but %d still hold OPEN positions "
            "which were NOT closed: %s",
            len(stopped),
            len(still_open),
            still_open,
        )

    return {
        "status": "success",
        "message": "Emergency stop executed. New entries halted and all running tasks stopped.",
        "tasksStopped": stopped,
        # Surfaced, not buried in a log line: the operator needs to know
        # exactly what risk is still on the book after hitting the button.
        "positionsStillOpen": still_open,
        "warning": (
            "Open positions were NOT closed. Stopping the system does not flatten "
            "the book — close these positions deliberately."
            if still_open
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Live-trading runtime toggle
#
# The settings page originally said "Not togglable from a browser by design."
# That constraint is now relaxed: the toggle EXISTS but requires an explicit
# confirmation string so it cannot be flipped by a stray click, a browser
# extension, or a test runner hitting every endpoint.
# ---------------------------------------------------------------------------

class ResetPaperRequest(BaseModel):
    """Everything defaults to the safe thing; nothing is reset implicitly."""

    startingCash: float = Field(10_000.0, gt=0)
    # Off by default. The trade log is the audit trail of what the agent did, and
    # wiping it is a separate decision from resetting the book.
    clearTradeLog: bool = False
    # Required, and checked against the literal string. A reset is irreversible
    # and this route is reachable over HTTP; a stray POST should not be able to
    # erase a book.
    confirm: str = Field(..., description="must be exactly 'RESET PAPER'")


@router.post("/reset-paper", dependencies=[Depends(require_write_auth)])
async def reset_paper(req: ResetPaperRequest) -> Dict[str, Any]:
    """Reset the PAPER book: cash back to a chosen figure, no positions, watch list empty.

    WHY DELETING THE DATABASE ROWS DID NOT WORK
    -------------------------------------------
    This endpoint exists because the obvious approach silently fails. Truncating
    `agent_positions` / `agent_paper_account` / `monitored_positions` while the
    backend is RUNNING does nothing lasting:

      * `portfolio_store` holds the book in a module-level `_portfolio` dict and
        `_persist()` REPLACES the rows from memory after every fill. The next
        write puts the deleted position straight back.
      * `PositionMonitorAgent` holds its watch list in `self._open` and calls
        `save_watch_list`, which is a `DELETE` + re-`INSERT` of everything it is
        holding. Same result.

    So a reset has to clear the IN-MEMORY state first and let it persist itself
    down to empty. That ordering is the whole point of this route, and it is why
    "I reset the DB but the position is still there" is the expected outcome of
    doing it by hand against a live process.

    PAPER ONLY, AND REFUSED WHEN LIVE_TRADING IS ON
    -----------------------------------------------
    The real book is the exchange's, not ours; there is nothing here that could
    reset it and pretending otherwise would be dangerous. With `LIVE_TRADING=true`
    the agent's positions may be REAL, and clearing the watch list would stop the
    stop-loss monitor watching a live position while leaving it open on the venue.
    That is the single worst thing this file could do, so it refuses outright
    rather than resetting "just the paper part" of a live process.

    WHAT IT DOES NOT TOUCH
    ----------------------
    Decisions, reflections, graph traces and the volatility history are all
    observability, not the book. Wiping them would delete the record of how the
    agent reached the state being reset — which is the part worth keeping.
    """
    if req.confirm != "RESET PAPER":
        raise HTTPException(
            status_code=400,
            detail="confirm must be exactly 'RESET PAPER'. This erases the paper book.",
        )

    if settings.LIVE_TRADING:
        raise HTTPException(
            status_code=409,
            detail=(
                "LIVE_TRADING is ON, so open positions may be REAL. Clearing the "
                "watch list would stop the stop-loss monitor watching a position "
                "that is still open on the exchange. Turn live trading off first."
            ),
        )

    from backend.agents.position_monitor import get_position_monitor
    from backend.services import portfolio_store

    report: Dict[str, Any] = {}

    # 1. THE WATCH LIST FIRST. It is the safety-critical structure, and clearing
    #    it before the book means there is no window where the monitor is watching
    #    a position the book no longer has.
    monitor = get_position_monitor()
    watched_before = len(monitor.snapshot_open())
    cleared = await monitor.clear_all("operator reset the paper book")
    report["watchedCleared"] = watched_before
    report["watchListPersisted"] = cleared

    # 2. The book in memory, then straight down to the database through the
    #    module's own writer — so the rows match what the process believes.
    positions_before = len(portfolio_store._portfolio.get("paper", {}).get("positions") or [])
    await portfolio_store.update_portfolio(
        {
            "paper": {"cash": float(req.startingCash), "positions": []},
            "real": portfolio_store._portfolio.get("real", {"positions": []}),
        }
    )
    report["positionsCleared"] = positions_before
    report["cash"] = float(req.startingCash)

    # 3. The trade log, only if asked.
    report["tradesDeleted"] = None
    if req.clearTradeLog:
        pool = get_db_pool()
        if pool is None:
            report["tradesDeleted"] = "no database — the trade log was not touched"
        else:
            async with pool.acquire() as conn:
                deleted = await conn.fetchval(
                    "WITH d AS (DELETE FROM trades WHERE tab = 'paper' RETURNING 1) "
                    "SELECT count(*) FROM d"
                )
            report["tradesDeleted"] = int(deleted or 0)

    logger.warning(
        "OPERATOR RESET THE PAPER BOOK: cash=%.2f, %s position(s) cleared, "
        "%s watched position(s) cleared, trades deleted=%s",
        req.startingCash, positions_before, watched_before, report["tradesDeleted"],
    )

    return {
        "status": "success",
        **report,
        "meaning": (
            "The paper book is now empty at the chosen cash figure and the stop-loss "
            "watch list holds nothing. Decisions, reflections and graph traces are "
            "deliberately untouched — they are the record of how the agent reached "
            "the state you just reset."
        ),
        "note": (
            "Deleting these rows in SQL while the backend is running does NOT work: "
            "the in-memory book and watch list are re-persisted over the top on the "
            "next write. This route clears memory first, which is why it sticks."
        ),
    }


@router.get("/trading-mode")
async def get_trading_mode() -> Dict[str, Any]:
    """All three execution gates in one response."""
    from backend.services.execution_service import execution_enabled
    from backend.workers.position_worker import monitoring_enabled

    client = None
    try:
        from backend.services.exchange_client import get_exchange_client
        client = get_exchange_client()
    except Exception:
        pass

    return {
        "status": "success",
        "liveTradingEnabled": settings.LIVE_TRADING,
        "graphExecutionEnabled": execution_enabled(),
        "positionMonitoringEnabled": monitoring_enabled(),
        "credentialsConfigured": client.has_credentials() if client else False,
        "executionTab": settings.execution_tab,
        "ordersRoutedTo": (
            "simulation (no exchange orders)"
            if not settings.LIVE_TRADING
            else ("binance futures LIVE — REAL FUNDS" if not settings.USE_TESTNET else "binance futures TESTNET")
        ),
    }


@router.post("/live-trading/enable", dependencies=[Depends(require_write_auth)])
async def enable_live_trading(body: Dict[str, Any] = {}) -> Dict[str, Any]:
    """Enable real-money trading. Requires explicit confirmation.

    The body must contain `"confirm": "I understand this uses real funds"`.
    Without that exact string, the request is rejected. This is not security
    theatre — it is the difference between a toggle that can be flipped by
    a curl one-liner pasted from a README and one that requires reading.
    """
    confirm = (body.get("confirm") or "").strip()
    if confirm != "I understand this uses real funds":
        return {
            "status": "error",
            "message": (
                'Confirmation required. Send {"confirm": "I understand this uses real funds"} '
                "in the request body to enable live trading."
            ),
        }

    # Check credentials before enabling — live mode without keys means every
    # order attempt fails at the exchange, which is worse than staying in paper.
    try:
        from backend.services.exchange_client import get_exchange_client
        client = get_exchange_client()
        if not client.has_credentials():
            return {
                "status": "error",
                "message": (
                    "Cannot enable live trading: no exchange credentials configured "
                    "(BINANCE_API_KEY / BINANCE_SECRET are empty). Set them in .env first."
                ),
            }
    except Exception as e:
        logger.warning("Could not check exchange credentials: %s", e)

    settings.set_live_trading(True)
    logger.critical(
        "LIVE TRADING ENABLED via API. Orders will now route to the exchange. "
        "Tab is '%s'. This change has been persisted to .env.",
        settings.execution_tab,
    )
    return {
        "status": "success",
        "message": "Live trading ENABLED. Orders will now route to the real exchange.",
        "liveTradingEnabled": True,
        "executionTab": settings.execution_tab,
    }


@router.post("/live-trading/disable", dependencies=[Depends(require_write_auth)])
async def disable_live_trading() -> Dict[str, Any]:
    """Switch back to paper trading. No confirmation needed — this is always safe."""
    settings.set_live_trading(False)
    logger.info(
        "Live trading DISABLED via API. Orders now route to simulation. "
        "Tab is '%s'. Persisted to .env.",
        settings.execution_tab,
    )
    return {
        "status": "success",
        "message": "Live trading DISABLED. Back to paper/simulation mode.",
        "liveTradingEnabled": False,
        "executionTab": settings.execution_tab,
    }

