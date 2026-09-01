"""An autonomous trading SESSION — "trade this coin until I reach X, then stop".

WHAT THIS IS
------------
The operator picks a symbol, a leverage ceiling and a target equity, and presses
start. This service then drives the agent's existing decision chain in a loop:

    session tick -> analysis graph (23 nodes, all specialists, LLM narrative)
                 -> Supervisor decides -> Risk Gateway sizes and validates
                 -> EXECUTION_PLAN_READY -> Execution Service -> TAR -> CRO
                 -> ExecutionAgent fills -> PositionMonitorAgent watches the stop
                 -> POSITION_CLOSED -> ReflectionAgent learns
                 -> back to the top

None of that chain is new and none of it is bypassed. This module adds only the
thing that was missing: a REASON to keep going, and a definition of done.

THE TARGET IS A STOP CONDITION, NOT A SIZING INPUT — READ THIS BEFORE CHANGING IT
--------------------------------------------------------------------------------
CLAUDE.md's "Primary objective" is explicit, and it is the single most important
constraint on this file:

    "Do NOT optimize for a guaranteed return multiple (e.g. 'turn $X into $Y').
     That is a financial outcome, not an engineering requirement, and encoding it
     as a hard objective pushes the system toward unsafe risk-taking."

So `target_equity` is read in exactly one place: the check that decides whether to
STOP. It is never passed to the Risk Gateway, never used to size a position, and
never used to relax a threshold. An agent that sized up because it was behind
schedule would be the exact failure that warning describes — and the further
behind it got, the harder it would push.

What that means in practice: a session may end with `target_not_reached`. That is
a real outcome and it is reported as one. The alternative — a system that keeps
raising risk until it either hits the number or is wiped out — is not a better
trading agent, it is a martingale.

THE FLOOR IS NOT OPTIONAL
-------------------------
"Trade until you reach the target" with no lower bound means "trade until zero",
because a losing session has no other terminating condition. Every session
therefore carries a `floor_equity` it stops at, defaulted relative to the
starting capital. It can be lowered by the operator but not removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STORE_DIR = os.path.join(_ROOT, ".data")
_STORE_PATH = os.path.join(_STORE_DIR, "trading_sessions.json")

# How long between decision cycles while a session is running.
#
# 60s. A graph run costs candles, an order book, a trade tape, four RSS feeds and
# a model call; running it every few seconds would spend the venue's rate limit
# and the LLM budget re-deriving a view of a market that has barely moved. The
# POSITION MONITOR still checks every open position against every tick, so the
# stop-loss is not on this cadence — only the decision to open something new is.
DECISION_INTERVAL_S = 60.0

# A session will not open a new position while one is already open for its symbol.
# Checked every tick; this is why the interval above can be slow without the
# session sitting idle through a move.
#
# Default floor: stop if equity falls to 50% of where the session started.
DEFAULT_FLOOR_FRACTION = 0.5

# Hard ceiling on how long a session may run unattended, and on how many trades
# it may place. Both exist because "until the target is reached" is not a bound —
# a session that never reaches it and never loses enough to hit the floor would
# otherwise run forever, spending API quota and model tokens.
MAX_SESSION_HOURS = 72.0
MAX_TRADES_PER_SESSION = 200


@dataclass
class TradingSession:
    """One autonomous run toward a target. Serialisable; no live objects."""

    id: str
    symbol: str
    leverage: int
    start_equity: float
    target_equity: float
    floor_equity: float
    # 'running' | 'reached' | 'floored' | 'stopped' | 'expired' | 'failed'
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    stop_reason: Optional[str] = None

    cycles_run: int = 0
    trades_opened: int = 0
    last_cycle_at: Optional[float] = None
    last_decision: Optional[str] = None
    last_rationale: Optional[str] = None
    # Every decision the session made, newest last. Bounded — this is a live
    # status object, not the audit trail; `decisions` in Postgres is that.
    log: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def active(self) -> bool:
        return self.status == "running"


_MAX_LOG = 50

_sessions: Dict[str, TradingSession] = {}
_tasks: Dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist() -> None:
    """Write sessions to disk. Never raises.

    A session that vanishes on restart would leave the operator believing the
    agent is still working toward their target when nothing is. Restored
    sessions come back as `stopped` rather than resuming — see `restore`.
    """
    try:
        os.makedirs(_STORE_DIR, exist_ok=True)
        with open(_STORE_PATH, "w", encoding="utf-8") as fh:
            json.dump([s.as_dict() for s in _sessions.values()], fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not persist trading sessions: %s", exc)


def restore() -> int:
    """Reload sessions from disk. Returns how many were RUNNING when we stopped.

    A restored session is marked `stopped`, NOT resumed. Resuming automatically
    would restart real trading on process boot without anyone asking for it, and
    the operator may have restarted precisely because they wanted it to stop.
    The record is kept so the history is honest about what was interrupted.
    """
    if not os.path.exists(_STORE_PATH):
        return 0
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not read trading sessions: %s", exc)
        return 0

    interrupted = 0
    for row in rows or []:
        try:
            session = TradingSession(**row)
        except Exception:  # noqa: BLE001
            continue
        if session.status == "running":
            session.status = "stopped"
            session.stop_reason = (
                "the backend restarted while this session was running. It was NOT "
                "resumed automatically — start a new one if you still want it."
            )
            session.finished_at = time.time()
            interrupted += 1
        _sessions[session.id] = session

    if interrupted:
        logger.warning(
            "%d autonomous session(s) were running when this process last stopped. "
            "They are marked stopped and were NOT resumed.", interrupted,
        )
    _persist()
    return interrupted


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------

# The authenticated balance is CACHED, and the cache is the point.
#
# `/api/session` is polled every 5 seconds by the operator's panel. Reading the
# exchange balance on each of those is a private, weight-bearing call to Binance
# for a figure that moves only when a trade fills — the fastest way to spend a
# rate limit on a screen nobody is acting on. 30s matches the cache
# `operator_exchange.get_context` already applies for the same reason.
_REAL_BALANCE_TTL_S = 30.0
_real_balance_cache: Dict[str, Any] = {"value": None, "at": 0.0, "error": None}


async def real_account_balance(force: bool = False) -> Optional[float]:
    """Free USDT on the exchange, or None when it cannot be read.

    THE REAL BOOK'S STARTING AMOUNT IS THIS, AND IT IS NOT TYPEABLE. An operator
    who could type their own starting balance for a real session would be setting
    the denominator of every subsequent percentage while the exchange held a
    different number — the session would report progress against capital that does
    not exist.

    Returns None, never 0.0, on a failure. A zero balance and an unreadable one
    are different facts and only one of them means "you have no money".
    """
    now = time.time()
    if not force and _real_balance_cache["value"] is not None:
        if now - float(_real_balance_cache["at"]) < _REAL_BALANCE_TTL_S:
            return float(_real_balance_cache["value"])

    try:
        from backend.services.exchange_client import get_exchange_client

        client = get_exchange_client()
        raw = await client.fetch_balance()
        if not raw:
            _real_balance_cache["error"] = "the exchange returned no balance"
            return None

        # ccxt shapes: {'USDT': {'free': x}} and {'free': {'USDT': x}}. Both are
        # real; which one appears depends on the venue and the market type.
        free = None
        usdt = raw.get("USDT") if isinstance(raw, dict) else None
        if isinstance(usdt, dict):
            free = usdt.get("free")
        if free is None and isinstance(raw.get("free"), dict):
            free = raw["free"].get("USDT")

        if not isinstance(free, (int, float)):
            _real_balance_cache["error"] = "no USDT balance in the exchange response"
            return None

        _real_balance_cache.update({"value": float(free), "at": now, "error": None})
        return float(free)
    except Exception as exc:  # pragma: no cover - network path
        _real_balance_cache["error"] = str(exc)[:200]
        logger.warning("Could not read the exchange balance: %s", exc)
        return None


def real_balance_error() -> Optional[str]:
    """Why the last balance read failed, for the operator to act on."""
    return _real_balance_cache.get("error")


async def current_equity(tab: str) -> Optional[float]:
    """Cash plus the marked value of open positions, or None if unmarkable.

    None rather than a partial number: a session's stop condition is compared
    against this, and stopping (or failing to stop) on a figure that silently
    excluded an open position would be the worst kind of wrong.

    ON THE REAL BOOK THE CASH FIGURE COMES FROM THE EXCHANGE. This store has never
    held one — it tracks what the agent did, not what the venue says the account
    holds — so a real session used to be unstartable: `current_equity('real')`
    returned None and the panel reported "equity is not measurable" forever. The
    authenticated balance is the answer to that, cached, and it is still None
    rather than 0.0 when the venue cannot be reached.
    """
    from backend.services.market_data import get_price
    from backend.services.portfolio_store import get_portfolio

    portfolio = await get_portfolio()
    book = (portfolio or {}).get(tab) or {}

    cash = book.get("cash")
    if not isinstance(cash, (int, float)) and tab == "real":
        cash = await real_account_balance()
    if not isinstance(cash, (int, float)):
        # No cash figure and no exchange to ask. Say so instead of guessing.
        return None

    total = float(cash)
    for pos in book.get("positions") or []:
        symbol = pos.get("symbol")
        qty = pos.get("qty")
        if not isinstance(qty, (int, float)) or not symbol:
            return None
        price = get_price(symbol)
        if not price or price <= 0:
            # Unmarkable position. Refuse rather than value it at cost, which
            # would report a losing position as flat.
            return None
        total += float(qty) * float(price)
    return total


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def _run_session(session_id: str) -> None:
    """Drive one session until it terminates. Never raises out of the task."""
    from backend.core.system_state import is_emergency_stopped, is_system_paused

    session = _sessions.get(session_id)
    if session is None:
        return

    tab = _tab_for_session()
    logger.warning(
        "AUTONOMOUS SESSION %s STARTED: %s, %sx, from %.2f toward %.2f (floor %.2f) on the %s book.",
        session.id, session.symbol, session.leverage,
        session.start_equity, session.target_equity, session.floor_equity, tab,
    )

    try:
        while session.active:
            await asyncio.sleep(DECISION_INTERVAL_S)
            if not session.active:
                break

            session.cycles_run += 1
            session.last_cycle_at = time.time()

            # -- terminating conditions, checked BEFORE acting ----------------
            if (time.time() - session.started_at) > MAX_SESSION_HOURS * 3600:
                _finish(session, "expired", f"ran for more than {MAX_SESSION_HOURS:.0f}h without reaching the target")
                break
            if session.trades_opened >= MAX_TRADES_PER_SESSION:
                _finish(session, "expired", f"placed {MAX_TRADES_PER_SESSION} trades without reaching the target")
                break

            equity = await current_equity(tab)
            if equity is None:
                _note(session, "equity unmeasurable — an open position could not be marked; not acting this cycle")
                continue

            if equity >= session.target_equity:
                _finish(session, "reached", f"equity {equity:.2f} reached the {session.target_equity:.2f} target")
                break
            if equity <= session.floor_equity:
                _finish(session, "floored", f"equity {equity:.2f} fell to the {session.floor_equity:.2f} floor")
                break

            # -- operator gates ----------------------------------------------
            if is_emergency_stopped():
                _finish(session, "stopped", "the operator triggered the emergency stop")
                break
            if is_system_paused():
                _note(session, "system is paused — no new entries this cycle. Open positions stay monitored.")
                continue

            # -- do not stack positions --------------------------------------
            if await _has_open_position(session.symbol, tab):
                _note(session, f"already holding {session.symbol}; the monitor owns the exit. Waiting.")
                continue

            # -- one full decision cycle -------------------------------------
            await _decide_once(session)

    except asyncio.CancelledError:
        # Stopped by the operator. Not an error, and the status was already set
        # by `stop_session` — do not overwrite it.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Autonomous session %s failed: %s", session.id, exc)
        _finish(session, "failed", f"the session loop raised: {type(exc).__name__}: {exc}")
    finally:
        _tasks.pop(session_id, None)
        _persist()


def _tab_for_session() -> str:
    from backend.core.config import settings

    return "real" if settings.LIVE_TRADING else "paper"


async def _has_open_position(symbol: str, tab: str) -> bool:
    """True when this symbol is already held or already watched.

    Checked against BOTH the book and the monitor. They can disagree for a moment
    around a fill, and opening a second position because one of them had not
    caught up yet is how a session doubles its exposure by accident.
    """
    try:
        from backend.agents.position_monitor import get_position_monitor
        from backend.services.portfolio_store import get_portfolio

        for pos in get_position_monitor().snapshot_open():
            if pos.get("symbol") == symbol:
                return True

        portfolio = await get_portfolio()
        for pos in ((portfolio or {}).get(tab) or {}).get("positions") or []:
            if pos.get("symbol") == symbol and (pos.get("qty") or 0) > 0:
                return True
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED: if we cannot tell whether a position is open, do not open
        # another one.
        logger.warning("Could not determine open positions for %s: %s", symbol, exc)
        return True
    return False


async def _decide_once(session: TradingSession) -> None:
    """Run the full analysis graph once and let the existing chain act on it.

    THIS DOES NOT PLACE AN ORDER. It runs the reasoning graph, which — if the
    Supervisor decides to trade AND the Risk Gateway approves — publishes
    `EXECUTION_PLAN_READY`. Everything after that is the execution plane's
    business, exactly as it is for a market-triggered run. A session that placed
    its own orders would be a second execution path outside the CRO.
    """
    from backend.graphs.analysis import run_analysis_graph
    from backend.graphs.state import TriggerReason

    result = await run_analysis_graph(
        session.symbol,
        TriggerReason(
            kind="autonomous_session",
            symbol=session.symbol,
            detail=(
                f"session {session.id} working toward {session.target_equity:.2f} "
                f"(the target is a stop condition and was NOT used to size this trade)"
            ),
        ),
    )

    if not result.get("ok"):
        _note(session, f"analysis run failed: {result.get('error')}")
        return

    decision = result.get("decision") or {}
    action = decision.get("action") or "NO_DECISION"
    rationale = decision.get("rationale") or result.get("noDecisionReason") or ""

    session.last_decision = action
    session.last_rationale = rationale

    if result.get("executionPlan"):
        # The gateway approved and a plan was published. The fill is asynchronous
        # — counted here because this is the cycle that authorised it.
        session.trades_opened += 1

    _note(
        session,
        f"{action}: {rationale}"[:400],
        decision=action,
        run_id=result.get("runId"),
    )


def _note(session: TradingSession, message: str, **extra: Any) -> None:
    session.log.append({"ts": time.time(), "message": message, **extra})
    if len(session.log) > _MAX_LOG:
        del session.log[: len(session.log) - _MAX_LOG]
    _persist()


def _finish(session: TradingSession, status: str, reason: str) -> None:
    session.status = status
    session.stop_reason = reason
    session.finished_at = time.time()
    logger.warning("AUTONOMOUS SESSION %s ENDED (%s): %s", session.id, status, reason)
    _note(session, f"session ended — {status}: {reason}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def set_paper_starting_amount(amount: float) -> float:
    """Set the PAPER book's cash, so a session can start from a chosen amount.

    WHY THIS WRITES THE BOOK RATHER THAN JUST LABELLING THE SESSION
    ---------------------------------------------------------------
    The operator wants to run "$2 to $5". If the session merely recorded 2.00 as
    its starting figure while the paper book still held 10,000, then every number
    downstream would be about the 10,000: the Risk Gateway would size 1% of TEN
    THOUSAND, one position would be larger than the entire notional stake, and the
    progress bar would crawl because 2 -> 5 is invisible against that balance. The
    run would look like a $2 experiment and behave like a $10,000 one.

    So the amount is written to the book and everything downstream is genuinely
    about it.

    REFUSED WHILE PAPER POSITIONS ARE OPEN. Rewriting cash underneath a position
    leaves a book whose equity is part old-basis and part new, and the P&L on the
    next close would be measured against capital that never funded it.

    PAPER ONLY. There is no real equivalent and there must not be — see
    `real_account_balance`.
    """
    from backend.services.portfolio_store import get_portfolio, update_portfolio

    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValueError("the starting amount must be a positive number")

    portfolio = await get_portfolio()
    book = (portfolio or {}).get("paper") or {}
    open_positions = [p for p in (book.get("positions") or []) if p.get("qty")]
    if open_positions:
        held = ", ".join(str(p.get("symbol")) for p in open_positions[:5])
        raise ValueError(
            f"cannot set the starting amount while paper positions are open ({held}). "
            f"Rewriting cash underneath a position leaves an equity figure that is "
            f"part old-basis and part new, and the next close would be measured "
            f"against capital that never funded it. Close them first."
        )

    portfolio["paper"] = {**book, "cash": float(amount)}
    await update_portfolio(portfolio)
    logger.warning("PAPER BOOK CASH SET TO %.2f by the operator for a new session.", amount)
    return float(amount)


async def start_session(
    *,
    symbol: str,
    leverage: int,
    target_equity: float,
    floor_equity: Optional[float] = None,
    start_amount: Optional[float] = None,
) -> TradingSession:
    """Begin an autonomous session. Raises ValueError on an unusable request.

    `start_amount` is PAPER-ONLY and sets the book's cash before the session
    measures its starting equity — see `set_paper_starting_amount`. On the real
    book the starting amount is the exchange's balance and cannot be supplied.
    """
    from backend.core.risk_manager import max_leverage_ceiling

    tab = _tab_for_session()

    if start_amount is not None:
        if tab == "real":
            raise ValueError(
                "a starting amount cannot be set for a REAL session — it is the "
                "exchange's own balance. Typing one would set the denominator of "
                "every percentage this session reports while the venue held a "
                "different number."
            )
        # Set BEFORE the "already running" check would be pointless, and AFTER
        # equity is read would be too late — this has to land first so
        # `current_equity` below measures the amount the operator chose.
        await set_paper_starting_amount(start_amount)

    for existing in _sessions.values():
        if existing.active:
            raise ValueError(
                f"session {existing.id} is already running on {existing.symbol}. "
                f"Stop it before starting another — two sessions trading the same "
                f"book would size against each other's equity without knowing it."
            )

    ceiling = max_leverage_ceiling(tab)
    if leverage < 1 or leverage > ceiling:
        raise ValueError(
            f"leverage {leverage}x is outside the 1x-{ceiling}x range for the "
            f"'{tab}' book. This ceiling is not operator-configurable."
        )

    equity = await current_equity(tab)
    if equity is None:
        raise ValueError(
            "current equity cannot be measured (no cash figure, or an open "
            "position has no price). A session cannot define 'done' without it."
        )
    if target_equity <= equity:
        raise ValueError(
            f"target {target_equity:.2f} is not above the current equity "
            f"{equity:.2f} — there would be nothing for the session to do."
        )

    floor = floor_equity if floor_equity is not None else equity * DEFAULT_FLOOR_FRACTION
    if floor >= equity:
        raise ValueError(f"floor {floor:.2f} must be below the starting equity {equity:.2f}")
    if floor < 0:
        raise ValueError("floor cannot be negative")

    session = TradingSession(
        id=uuid.uuid4().hex[:12],
        symbol=symbol,
        leverage=leverage,
        start_equity=equity,
        target_equity=target_equity,
        floor_equity=floor,
    )
    _sessions[session.id] = session
    _persist()

    _tasks[session.id] = asyncio.create_task(_run_session(session.id))
    return session


async def stop_session(session_id: str, reason: str = "stopped by the operator") -> Optional[TradingSession]:
    """Stop a session. Open positions are NOT closed.

    Deliberately. Closing everything at market because a session was stopped
    would be a large, irreversible, slippage-bearing trade fired by a button
    labelled "stop" — the same reasoning `api/admin.emergency_stop` gives for not
    flattening the book. The monitor keeps enforcing the stop-loss on anything
    still open; the operator closes it deliberately.
    """
    session = _sessions.get(session_id)
    if session is None:
        return None

    if session.active:
        _finish(session, "stopped", reason)

    task = _tasks.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()

    _persist()
    return session


def get_session(session_id: str) -> Optional[TradingSession]:
    return _sessions.get(session_id)


def active_session() -> Optional[TradingSession]:
    for session in _sessions.values():
        if session.active:
            return session
    return None


def list_sessions(limit: int = 20) -> List[TradingSession]:
    return sorted(_sessions.values(), key=lambda s: s.started_at, reverse=True)[:limit]


async def stop_all(reason: str = "backend shutting down") -> None:
    for session_id in list(_tasks):
        await stop_session(session_id, reason)


def _reset_for_tests() -> None:
    _sessions.clear()
    _tasks.clear()
