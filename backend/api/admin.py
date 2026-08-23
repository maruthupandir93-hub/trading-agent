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

from fastapi import APIRouter, Depends

from backend.core.auth import auth_status, require_write_auth

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

