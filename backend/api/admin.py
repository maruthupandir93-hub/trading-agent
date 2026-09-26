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
import os
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


@router.get("/venue")
async def venue_status() -> Dict[str, Any]:
    """Which exchange the agent trades on, and what switching would cost.

    Reports credentials PER VENUE and whether a switch is currently safe, so the
    operator sees the blocker before pressing anything rather than after.
    """
    from backend.services.venue import SUPPORTED, Venue, configured_venue, get_venue

    current = configured_venue()
    live = get_venue()

    # Credentials are read per venue WITHOUT building a full client for each —
    # constructing one opens a ccxt session, and this endpoint is polled.
    from backend.services.venue import _credentials, key_variable

    # The NETWORK decides which key pair each venue reads, so the panel must
    # report the variable that is actually in force. Naming the mainnet one while
    # the process signs testnet requests sends the operator to set a key that
    # will never be read.
    testnet = live.testnet

    venues = []
    for name in SUPPORTED:
        key, secret = _credentials(name, testnet=testnet)
        venues.append({
            "id": name,
            "current": name == current,
            "credentialsConfigured": bool(key and secret),
            "keyVariable": key_variable(name, testnet=testnet),
        })

    # THE BLOCKER. Switching venue while a position is open orphans it: the
    # monitor would go on enforcing a stop against an account we are no longer
    # talking to, and reconciliation would compare the local book to the WRONG
    # exchange and report every real position as a phantom.
    from backend.agents.position_monitor import get_position_monitor

    open_positions = get_position_monitor().snapshot_open()
    real_open = [p for p in open_positions if p.get("tab") == "real"]

    return {
        "current": current,
        "venues": venues,
        "liveTrading": settings.LIVE_TRADING,
        "openPositions": len(open_positions),
        "realOpenPositions": len(real_open),
        "canSwitch": not real_open,
        "blockedReason": (
            f"{len(real_open)} REAL position(s) are open on {current}. Switching venue "
            f"would leave them at that exchange with this process watching a different "
            f"account — the stop would stop being enforced and reconciliation would "
            f"report them as phantoms. Close them first."
        ) if real_open else None,
        "meaning": (
            "These are DIFFERENT ACCOUNTS holding DIFFERENT MONEY. Switching changes "
            "which exchange every order, balance read and position query goes to. It "
            "does not move funds, and it does not close anything."
        ),
    }


class SwitchVenueRequest(BaseModel):
    venue: str = Field(..., min_length=2)
    # Required, and checked against the venue name. Switching accounts from a
    # stray POST is not something a confirmation flag should be able to miss.
    confirm: str = Field(..., description="must equal the venue being switched to")


@router.post("/venue", dependencies=[Depends(require_write_auth)])
async def switch_venue(req: SwitchVenueRequest) -> Dict[str, Any]:
    """Switch the exchange the agent trades on. Persists to `.env`.

    REFUSES WHILE A REAL POSITION IS OPEN, and that refusal is the point of the
    route. Binance and Bybit are different accounts holding different money — a
    position opened on one does not exist on the other. Switching underneath an
    open position would leave it at the old venue while:

      * `PositionMonitorAgent` keeps enforcing its stop by placing orders on the
        NEW venue, where the position does not exist;
      * the resting stop this process left behind stays live and uncancellable
        through the new client;
      * reconciliation compares the local book against the wrong exchange and
        reports every real position as a phantom.

    Paper positions do not block it: they have no venue counterpart at all.

    THE CLIENT IS REBUILT, not reconfigured. `reset_venue()` drops the singleton
    so the next call re-reads the environment — the old instance holds the other
    venue's markets, credentials and cached position mode, and reusing it would
    place orders with one venue's parameters against the other's API.
    """
    from backend.services.venue import SUPPORTED, configured_venue, get_venue, reset_venue

    target = req.venue.strip().lower()
    if target not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=f"{req.venue!r} is not supported. Choose one of: {', '.join(SUPPORTED)}.",
        )
    if req.confirm.strip().lower() != target:
        raise HTTPException(
            status_code=400,
            detail=f"confirm must equal {target!r}. This switches which exchange account trades.",
        )

    current = configured_venue()
    if target == current:
        return {"status": "unchanged", "current": current, "note": f"already trading on {current}"}

    from backend.agents.position_monitor import get_position_monitor

    real_open = [p for p in get_position_monitor().snapshot_open() if p.get("tab") == "real"]
    if real_open:
        held = ", ".join(str(p.get("symbol")) for p in real_open[:5])
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot switch venue: {len(real_open)} REAL position(s) open on {current} "
                f"({held}). They exist at that exchange and not at {target}; switching would "
                f"leave them unwatched while this process places orders on a different "
                f"account. Close them first."
            ),
        )

    settings._persist_env("EXCHANGE_ID", target)
    os.environ["EXCHANGE_ID"] = target
    reset_venue()

    from backend.services.venue import _credentials, key_variable

    testnet = get_venue().testnet
    key, secret = _credentials(target, testnet=testnet)
    configured = bool(key and secret)
    variable = key_variable(target, testnet=testnet)

    logger.warning(
        "OPERATOR SWITCHED VENUE: %s -> %s. LIVE_TRADING=%s, credentials %s.",
        current, target, settings.LIVE_TRADING,
        "configured" if configured else "MISSING",
    )

    return {
        "status": "success",
        "previous": current,
        "current": target,
        "credentialsConfigured": configured,
        # Said plainly rather than left to fail at the first order.
        "warning": None if configured else (
            f"{target} has no API credentials configured "
            f"({variable} / its _SECRET are empty). "
            f"Market data still works — it needs no key — but every order, balance read "
            f"and position query will be refused until they are set."
        ),
        "meaning": (
            "Every order, balance read and position query now goes to "
            f"{target}. No funds moved and nothing was closed — this changed which "
            "account the agent talks to, not what it holds."
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



# ---------------------------------------------------------------------------
# EXIT RULES — the numbers that decide when a position is closed
# ---------------------------------------------------------------------------
#
# These were environment variables only, which meant changing how the agent
# takes profit required an SSH session, an editor and a restart. They are the
# settings an operator most wants to tune while watching the thing run, so they
# belong on the Settings page.
#
# EVERY ONE IS READ AT CALL TIME by its consumer (`position_monitor` reads
# `PROFIT_TARGET_PCT` per tick, `trade_scope` reads the session rules per
# entry), so a change here takes effect on the NEXT TICK with no restart. That
# is the whole reason those reads were written that way — see the
# `simulation_mode` note in CLAUDE.md for what a frozen setting costs.

_EXIT_RULES: Dict[str, Dict[str, Any]] = {
    "PROFIT_TARGET_PCT": {
        "label": "Take profit at",
        "unit": "%",
        "default": "2.0",
        "min": 0.0,
        "max": 50.0,
        "help": (
            "Close the WHOLE position at this favourable price move. 0 disables it "
            "and restores the ATR target plus scale-out. With leverage this is "
            "amplified against margin: at 3x a 2% move is ~6% of the margin used."
        ),
    },
    "TRAILING_STOP_R": {
        "label": "Trailing stop distance",
        "unit": "R",
        "default": "1.0",
        "min": 0.0,
        "max": 10.0,
        "help": (
            "How far behind the best price the stop follows, in units of the "
            "position's initial risk. 0 disables trailing."
        ),
    },
    "TRAILING_ACTIVATE_R": {
        "label": "Trailing arms at",
        "unit": "R",
        "default": "1.0",
        "min": 0.0,
        "max": 10.0,
        "help": "Profit, in R, before the trail starts following. Below this the original stop holds.",
    },
    "PARTIAL_TP_FRACTION": {
        "label": "Scale out fraction",
        "unit": "",
        "default": "0.5",
        "min": 0.0,
        "max": 1.0,
        "help": (
            "How much to bank at the scale-out point. 0 disables it. IGNORED while "
            "a profit target is set — the two together reintroduce the break-even "
            "runner that produced trades closing at 0.00."
        ),
    },
    "PROFIT_TARGET_BASIS": {
        "label": "Target is a % of",
        "unit": "",
        "default": "account",
        "min": 0.0,
        "max": 0.0,
        "choices": ["account", "price"],
        "help": (
            "account: the target is a share of the MARGIN used, so the price only "
            "has to move target/leverage (2% at 10x = a 0.2% move). price: the "
            "target is the price move itself, so leverage multiplies the account "
            "effect (2% at 10x = a 20% gain)."
        ),
    },
    "RESTING_STOP_MODE": {
        "label": "Stop-loss at the venue",
        "unit": "",
        "default": "always",
        "min": 0.0,
        "max": 0.0,
        "choices": ["always", "on_adverse", "never"],
        "help": (
            "always: both legs rest from entry. on_adverse: only the take-profit "
            "rests; the stop is placed once the trade moves against you. never: no "
            "venue stop. The venue copy only matters while this process is DOWN — "
            "the monitor fires first while it is up."
        ),
    },
    "RESTING_STOP_ARM_R": {
        "label": "Place the stop after",
        "unit": "R against",
        "default": "0.5",
        "min": 0.0,
        "max": 1.0,
        "help": "How far the trade must move against you before on_adverse places the venue stop.",
    },
    "MAX_CONCURRENT_POSITIONS": {
        "label": "Positions at once",
        "unit": "",
        "default": "1",
        "min": 1.0,
        "max": 20.0,
        "help": "How many positions may be open across all instruments.",
    },
}


class ExitRulesRequest(BaseModel):
    profitTargetPct: float | None = Field(None, ge=0, le=50)
    profitTargetBasis: str | None = Field(None, pattern="^(account|price)$")
    restingStopMode: str | None = Field(None, pattern="^(always|on_adverse|never)$")
    restingStopArmR: float | None = Field(None, ge=0, le=1)
    trailingStopR: float | None = Field(None, ge=0, le=10)
    trailingActivateR: float | None = Field(None, ge=0, le=10)
    partialTpFraction: float | None = Field(None, ge=0, le=1)
    maxConcurrentPositions: int | None = Field(None, ge=1, le=20)


_REQUEST_TO_ENV = {
    "profitTargetPct": "PROFIT_TARGET_PCT",
    "profitTargetBasis": "PROFIT_TARGET_BASIS",
    "restingStopMode": "RESTING_STOP_MODE",
    "restingStopArmR": "RESTING_STOP_ARM_R",
    "trailingStopR": "TRAILING_STOP_R",
    "trailingActivateR": "TRAILING_ACTIVATE_R",
    "partialTpFraction": "PARTIAL_TP_FRACTION",
    "maxConcurrentPositions": "MAX_CONCURRENT_POSITIONS",
}


@router.get("/exit-rules")
async def get_exit_rules() -> Dict[str, Any]:
    """The current exit rules and what each one means."""
    out = {}
    for env_key, meta in _EXIT_RULES.items():
        raw = os.getenv(env_key)
        out[env_key] = {
            **meta,
            "value": raw if raw not in (None, "") else meta["default"],
            "isDefault": raw in (None, ""),
        }
    return {
        "rules": out,
        # Surfaced so the panel can warn rather than let the operator set a
        # combination that silently disables one of the two.
        "partialIgnored": float(os.getenv("PROFIT_TARGET_PCT") or 0) > 0,
    }


@router.post("/exit-rules", dependencies=[Depends(require_write_auth)])
async def set_exit_rules(req: ExitRulesRequest) -> Dict[str, Any]:
    """Change the exit rules. Takes effect on the NEXT TICK — no restart.

    Persisted to `.env` through the same `_persist_env` the live-trading toggle
    uses, so a restart keeps the change; and written to `os.environ` so the
    running process sees it immediately. Both halves are needed: without the
    first the setting is lost on restart, without the second the operator is
    told it worked while the running agent keeps the old value.
    """
    changed: Dict[str, str] = {}
    for field, env_key in _REQUEST_TO_ENV.items():
        value = getattr(req, field, None)
        if value is None:
            continue
        if isinstance(value, str):
            # The choice settings (`PROFIT_TARGET_BASIS`, `RESTING_STOP_MODE`) are
            # words, not numbers. Coercing them through float() would raise and
            # take the whole request with it — the Pydantic pattern above has
            # already restricted them to the accepted values.
            text = value
        elif env_key == "MAX_CONCURRENT_POSITIONS":
            text = str(int(value))
        else:
            text = str(float(value))
        os.environ[env_key] = text
        settings._persist_env(env_key, text)
        changed[env_key] = text

    if not changed:
        raise HTTPException(status_code=400, detail="no exit rule supplied")

    logger.warning("EXIT RULES CHANGED BY THE OPERATOR: %s", changed)
    return {"ok": True, "changed": changed, "appliesFrom": "the next price tick"}
