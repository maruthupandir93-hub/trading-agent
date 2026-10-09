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
# A graph run costs candles, an order book, a trade tape, four RSS feeds and a
# model call; running it every few seconds would spend the venue's rate limit and
# the LLM budget re-deriving a view of a market that has barely moved. The POSITION
# MONITOR still checks every open position against every tick, so the stop-loss is
# not on this cadence — only the decision to open something new is.
#
# CONTINUOUS TRADING: the loop POLLS on the short `SESSION_POLL_S` so a position
# that just closed is noticed quickly, but it only runs the expensive decision
# every `DECISION_INTERVAL_S` while flat — EXCEPT immediately after a close, when it
# re-decides at once. That is what makes it feel continuous ("one trade ends, the
# next starts") without spending the rate limit polling a market that has not moved.
DECISION_INTERVAL_S = float(os.getenv("SESSION_DECISION_INTERVAL_S", "30") or 30)
SESSION_POLL_S = float(os.getenv("SESSION_POLL_S", "12") or 12)

# A session will not open a new position while one is already open for its symbol.
# Checked every tick; this is why the interval above can be slow without the
# session sitting idle through a move.
#
# Default floor: stop if equity falls to 50% of where the session started.
DEFAULT_FLOOR_FRACTION = 0.5

# Ceilings on how long a session may run and how many trades it may place.
#
# UNLIMITED BY DEFAULT (0 = no limit), at the operator's explicit request, and the
# reasoning is sound: a session's real bound is its TARGET, and a high target
# legitimately needs many trades over many days. With 72h/200 as hard limits a
# session that was working correctly toward an ambitious target would be killed
# mid-run and reported as "expired" — a failure message for a system doing exactly
# what it was told.
#
# WHAT IS LOST BY REMOVING THEM, STATED PLAINLY. "Until the target is reached" is
# not a bound. A session that never reaches its target and never falls to its
# floor now runs until the operator stops it, spending API quota and model tokens
# the whole time. The floor, the daily target and the emergency stop are what
# bound it instead — and the floor is the one that matters, because it is the only
# one that ends a session that is simply losing.
#
# Both remain settable for an operator who wants a bound back.
def _env_limit(name: str, default: float) -> float:
    """A positive limit, or 0.0 meaning unlimited. Never raises on a bad value."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s.", name, raw, default)
        return default
    return max(0.0, value)


MAX_SESSION_HOURS = _env_limit("MAX_SESSION_HOURS", 0.0)
MAX_TRADES_PER_SESSION = _env_limit("MAX_TRADES_PER_SESSION", 0.0)


@dataclass
class TradingSession:
    """One autonomous run toward a target. Serialisable; no live objects."""

    id: str
    symbol: str
    leverage: int
    start_equity: float
    target_equity: float
    floor_equity: float
    # The fraction of the account this session may deploy — 0.25 / 0.5 / 0.75 /
    # 1.0. The operator picks it on the home page: "trade with 75% of my balance".
    # Defaults to 1.0 so an existing session (or one started without the field)
    # behaves exactly as before. It scales BOTH the size of each trade and the
    # total capital the agent may commit at once — see `active_capital_fraction`
    # and the pool gate in `risk_gateway`. It is NOT a leverage source: the
    # leverage ceiling and mandatory stop are unchanged and still bound every
    # trade, so 100% means "use the whole account as margin", not "use more
    # leverage".
    capital_fraction: float = 1.0
    # EVERY INSTRUMENT THIS SESSION MAY OPEN A POSITION IN, scanned in turn.
    #
    # WHY THIS EXISTS. A session watched exactly ONE coin, and a single coin
    # spends most of its life doing nothing worth trading. Measured on the
    # operator's live XRP/USDT session over 35.5 hours: 2,397 graph runs, 3
    # trades, and the book FLAT for 65% of the window with gaps of 11.5 and 4.5
    # hours between fills. The refusals were not close calls — the specialist
    # panel read 0.00-0.12 against a 0.60 floor on 46 of the last 50 cycles.
    # Nothing was wrong; XRP was simply ranging, and all three fills were
    # Breakout, the three times it actually broke out.
    #
    # So the way to get more trades is MORE INSTRUMENTS, not a lower bar. The
    # bar is what made those three trades worth taking (+7.7% on two wins of
    # three), and lowering it buys entries precisely in the regime this
    # project's own backtest says the range strategies lose money in.
    #
    # EMPTY MEANS "JUST `symbol`", so every existing session and every caller
    # that does not set it behaves exactly as before.
    watch_symbols: List[str] = field(default_factory=list)
    # Where the rotation is up to. Persisted with the session so a restart does
    # not restart the sweep from the same coin every time.
    scan_index: int = 0
    # DAILY PROFIT TARGET (optional). When set (e.g. 0.02 = +2%), the session banks
    # the day: once equity is up this fraction versus the day's starting equity, it
    # stops OPENING new positions until the next UTC day, then resumes. It does NOT
    # end the session — the session keeps working toward its overall `target_equity`
    # across days, taking its 1-2% a day through many trades. None = no daily cap.
    # `day_anchor_*` track the current UTC day's start so the percentage is per-day.
    daily_target_pct: Optional[float] = None
    day_anchor_date: Optional[str] = None
    day_anchor_equity: Optional[float] = None
    # 'running' | 'reached' | 'floored' | 'stopped' | 'expired' | 'failed'
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    stop_reason: Optional[str] = None

    # POLLS, NOT DECISIONS — and the two were being read as one number.
    #
    # This increments at the top of every `SESSION_POLL_S` tick, BEFORE the pause
    # check, the equity read, the target/floor checks, the open-position check,
    # the daily-target lock and the decision interval. So a paused or fully
    # occupied session accumulates cycles indefinitely without ever calling the
    # analysis graph. Reproduced on a paused system: 10,001 cycles, 0 analysis
    # calls, 0 trades — and "9,000 cycles and no trade" reads as 9,000 failed
    # decisions when it may be zero attempted ones.
    #
    # `analyses_run` is the number that answers "did the agent actually think
    # about a trade?", and the two are now reported separately.
    cycles_run: int = 0
    analyses_run: int = 0
    trades_opened: int = 0
    # Plans the Risk Gateway approved. NOT the same as filled positions — the
    # CRO can still reject, and a fill can fail. See `trades_opened`.
    plans_approved: int = 0
    last_cycle_at: Optional[float] = None
    last_decision: Optional[str] = None
    last_rationale: Optional[str] = None
    # Every decision the session made, newest last. Bounded — this is a live
    # status object, not the audit trail; `decisions` in Postgres is that.
    log: List[Dict[str, Any]] = field(default_factory=list)

    def scan_list(self) -> List[str]:
        """The instruments to rotate over. Never empty, always starts with `symbol`.

        `symbol` stays FIRST and stays the session's identity: it is what the
        panel shows, what `start_session` refuses on, and what a reader means by
        "the session's coin". The rotation adds instruments to look at; it does
        not replace the one the operator chose.
        """
        out = [self.symbol]
        for sym in self.watch_symbols:
            if sym and sym not in out:
                out.append(sym)
        return out

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


async def equity_breakdown(tab: str) -> Dict[str, Any]:
    """Equity split into its three parts, so a flat number is self-explaining.

    WHY THIS EXISTS. The panel showed one figure — "Account now" — and an operator
    watching it sit at 10,000 could not tell whether the book was FLAT (correct,
    nothing open) or the number was STUCK (a bug). Those look identical and have
    completely different responses. The parts make it obvious: free cash 10,000 /
    locked 0 / unrealized 0 is plainly an idle account.

    It also shows where the money went the moment a position opens: cash drops by
    the margin, locked rises by the same, and unrealized starts moving with price.
    """
    from backend.services.market_data import get_price
    from backend.services.portfolio_store import book_equity, get_portfolio

    portfolio = await get_portfolio()
    book = (portfolio or {}).get(tab) or {}

    cash = book.get("cash")
    if not isinstance(cash, (int, float)) and tab == "real":
        cash = await real_account_balance()

    marks: Dict[str, float] = {}
    for pos in book.get("positions") or []:
        symbol = pos.get("symbol")
        if not symbol:
            continue
        price = get_price(symbol)
        if price and price > 0:
            marks[symbol] = float(price)

    result = book_equity({**book, "cash": cash}, marks)
    return {
        "freeCash": result["cash"],
        "lockedMargin": result["marginLocked"],
        "unrealized": result["unrealized"],
        "equity": result["equity"],
        "openPositions": len([p for p in (book.get("positions") or []) if p.get("qty")]),
        "unpricedSymbols": result["unpricedSymbols"],
        "meaning": (
            "equity = free cash + locked margin + unrealized. Cash alone does not "
            "move while a position is open — the margin is locked, not spent, and "
            "the profit or loss is in `unrealized` until the position closes."
        ),
    }


def session_progress(session: "TradingSession", equity: Optional[float]) -> Dict[str, Any]:
    """How far a session has come toward its target.

    Computed HERE rather than in the panel so the number the operator reads and
    the number the stop check uses come from one place. A progress bar that
    disagreed with the condition that ends the session would be worse than none.

    `fraction` is clamped to 0-1 for rendering, but `gained` and `remaining` are
    NOT clamped — a session that is down should say so rather than showing 0%.
    """
    start = session.start_equity
    target = session.target_equity
    span = target - start

    if equity is None or span <= 0:
        return {
            "fraction": None,
            "percent": None,
            "gained": None,
            "remaining": None,
            "reason": (
                "equity is not measurable right now"
                if equity is None else
                "the target is not above the starting amount"
            ),
        }

    gained = equity - start
    return {
        "fraction": max(0.0, min(1.0, gained / span)),
        "percent": round(max(0.0, min(1.0, gained / span)) * 100, 1),
        # Signed and unclamped: down $12 reads as -12.00, not as 0.
        "gained": gained,
        "remaining": target - equity,
        "startEquity": start,
        "targetEquity": target,
        "currentEquity": equity,
        "reason": None,
    }


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

    # FREE CASH + LOCKED MARGIN + UNREALIZED, via the one implementation.
    #
    # This used to be `cash + qty * price`, which is only correct at 1x. The book
    # deducts MARGIN from cash, so cash is free cash; adding the full notional
    # back double-counts the leveraged part. At 10x a $7,000 position funded by
    # $700 reported $6,300 of equity that did not exist — and a session compares
    # its target against this number, so its progress was overstated by the same
    # amount, in the direction that flatters the account.
    #
    # It also could not value a SHORT: `qty * price` ignores direction, so a
    # short moving against the operator read as equity going UP.
    from backend.services.portfolio_store import book_equity

    marks: Dict[str, float] = {}
    for pos in book.get("positions") or []:
        symbol = pos.get("symbol")
        if not symbol:
            return None
        price = get_price(symbol)
        if price and price > 0:
            marks[symbol] = float(price)

    result = book_equity({**book, "cash": cash}, marks)
    # None when any position could not be marked. Refused rather than valued at
    # cost, which would report a losing position as flat.
    return result["equity"]


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

    # Continuous-trading bookkeeping. The loop polls fast but only DECIDES on the
    # decision interval while flat — or immediately after a close, so the next trade
    # starts without waiting out the interval.
    from backend.core.message_bus import get_message_bus
    bus = get_message_bus()
    counted = set()
    async def count_fill(event):
        key = str(event.tar_id)
        if (session.active and event.tab == tab and event.symbol in session.scan_list()
                and event.fill_quantity > 0 and key not in counted):
            counted.add(key)
            session.trades_opened += 1
    bus.subscribe("ORDER_FILLED", count_fill)

    last_decided_at = 0.0
    was_holding = False

    try:
        while session.active:
            await asyncio.sleep(SESSION_POLL_S)
            if not session.active:
                break

            session.cycles_run += 1
            session.last_cycle_at = time.time()

            # -- terminating conditions, checked BEFORE acting ----------------
            if MAX_SESSION_HOURS > 0 and (time.time() - session.started_at) > MAX_SESSION_HOURS * 3600:
                _finish(session, "expired", f"ran for more than {MAX_SESSION_HOURS:.0f}h without reaching the target")
                break
            if MAX_TRADES_PER_SESSION > 0 and session.trades_opened >= MAX_TRADES_PER_SESSION:
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

            # -- daily profit target: bank the day, resume tomorrow ----------
            if _daily_target_reached(session, equity):
                continue

            # -- do not stack positions --------------------------------------
            # ACROSS EVERY SCANNED SYMBOL, not just the session's own. With a
            # rotation the position may be open in a DIFFERENT coin from the one
            # about to be analysed, and checking only `session.symbol` would let
            # the session open a second position while the first was still live —
            # which is `MAX_CONCURRENT_POSITIONS`' job to refuse, but refusing it
            # at the Risk Gateway costs a full 24-node run to reach a no.
            if await _any_open_position(session.scan_list(), tab):
                was_holding = True
                _note(session, "already holding a position; the monitor owns the exit. Waiting.")
                continue

            # -- decide: on the interval while flat, or AT ONCE after a close -
            just_closed = was_holding
            was_holding = False
            if not just_closed and (time.time() - last_decided_at) < DECISION_INTERVAL_S:
                # Flat with nothing to react to and the interval has not elapsed —
                # do not spend a graph run re-deriving an unchanged market.
                continue

            last_decided_at = time.time()
            # -- one full decision cycle, on ONE symbol -----------------------
            #
            # ROTATION IS COST-NEUTRAL, AND THAT IS THE WHOLE DESIGN. It runs the
            # graph exactly as often as before and simply points it at a
            # different instrument each time, so five coins cost the same LLM
            # budget and the same share of the 40/min rate limit as one. Running
            # all five per interval would be 5x the spend for no extra edge — a
            # breakout takes minutes to develop, so checking each coin every
            # ~4 minutes instead of every ~50 seconds misses nothing.
            scan = session.scan_list()
            target = scan[session.scan_index % len(scan)]
            session.scan_index = (session.scan_index + 1) % len(scan)
            # Counted HERE and not with `cycles_run`, because this is the only
            # point the 24-node graph is actually reached. See `analyses_run`.
            session.analyses_run += 1
            await _decide_once(session, target)

    except asyncio.CancelledError:
        # Stopped by the operator. Not an error, and the status was already set
        # by `stop_session` — do not overwrite it.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Autonomous session %s failed: %s", session.id, exc)
        _finish(session, "failed", f"the session loop raised: {type(exc).__name__}: {exc}")
    finally:
        bus.unsubscribe("ORDER_FILLED", count_fill)
        _tasks.pop(session_id, None)
        _persist()


def _tab_for_session() -> str:
    from backend.core.config import settings

    return "real" if settings.LIVE_TRADING else "paper"


# session_id -> UTC date we already logged the daily-lock note for, so the fast
# poll does not append the same "target reached" line every 12s.
_daily_lock_noted: Dict[str, str] = {}


def _daily_target_reached(session: TradingSession, equity: float) -> bool:
    """True when the day's +daily_target_pct is banked, so no NEW entry opens today.

    Resets the day's anchor equity on each new UTC day, so the percentage is per-day
    and the lock lifts automatically at the UTC rollover. Returns False when no daily
    target is configured. Open positions keep being monitored regardless — this gates
    OPENING only, never an exit (invariant 4 lives in the monitor, not here).
    """
    if not session.daily_target_pct or session.daily_target_pct <= 0:
        return False

    import datetime as _dt

    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    if session.day_anchor_date != today or session.day_anchor_equity is None:
        session.day_anchor_date = today
        session.day_anchor_equity = equity
        _daily_lock_noted.pop(session.id, None)
        return False

    anchor = session.day_anchor_equity
    if anchor <= 0:
        return False

    gain = (equity - anchor) / anchor
    if gain < session.daily_target_pct:
        return False

    if _daily_lock_noted.get(session.id) != today:
        _daily_lock_noted[session.id] = today
        _note(
            session,
            f"daily +{session.daily_target_pct * 100:.1f}% target reached "
            f"(+{gain * 100:.2f}% today) — no new entries until the next UTC day. "
            f"Open positions stay monitored.",
        )
    return True


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


async def _any_open_position(symbols: List[str], tab: str) -> bool:
    """True when ANY of these instruments is held or watched.

    Fails CLOSED through `_has_open_position`, which returns True when it cannot
    tell — so an unreadable book stops a new entry rather than permitting a
    second one.
    """
    for sym in symbols:
        if await _has_open_position(sym, tab):
            return True
    return False


def _no_decision_reason(result: Dict[str, Any]) -> str:
    """The graph's own account of why a run produced no decision.

    Never an empty string: "no reason was recorded" is itself information, and
    it points at the graph rather than leaving the operator with a bare colon.
    """
    reasons = [str(r) for r in (result.get("unavailable") or []) if r]
    if not reasons:
        return "no decision was reached and the run recorded no reason"
    return "no decision: " + "; ".join(reasons[-2:])


async def _decide_once(session: TradingSession, symbol: Optional[str] = None) -> None:
    """Run the full analysis graph once and let the existing chain act on it.

    THIS DOES NOT PLACE AN ORDER. It runs the reasoning graph, which — if the
    Supervisor decides to trade AND the Risk Gateway approves — publishes
    `EXECUTION_PLAN_READY`. Everything after that is the execution plane's
    business, exactly as it is for a market-triggered run. A session that placed
    its own orders would be a second execution path outside the CRO.
    """
    from backend.graphs.analysis import run_analysis_graph
    from backend.graphs.state import TriggerReason

    # DEFAULTS TO THE SESSION'S OWN SYMBOL, so every existing caller and test
    # keeps working unchanged; the loop passes the rotation's current pick.
    target = symbol or session.symbol

    result = await run_analysis_graph(
        target,
        TriggerReason(
            kind="autonomous_session",
            symbol=target,
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
    # A RUN THAT REACHED NO DECISION STILL HAS TO SAY WHY.
    #
    # This read `result.get("noDecisionReason")`, a key only `api/catalog`
    # produces from a stored decision row — the analysis result has never
    # carried it. So every no-thesis cycle logged `[SYM] NO_DECISION:` and
    # stopped, which is the operator's question ("why did it not trade?")
    # answered with a blank. Same shape as the `TarApprovedEvent` fields that
    # were passed but never declared: a defensive read of something that does
    # not arrive is indistinguishable from a legitimate absence.
    #
    # `unavailable` is where the graph records what it could not do, and
    # `summarise_analysis` now carries it. The LAST two entries are taken
    # because they are the nearest to the point the run stopped; the earlier
    # ones are usually standing notes (an absent feed, a missing track record)
    # that are true on every cycle and explain nothing about this one.
    rationale = decision.get("rationale") or _no_decision_reason(result)

    session.last_decision = action
    session.last_rationale = rationale

    if result.get("executionPlan"):
        # THE GATEWAY APPROVED A PLAN. THAT IS NOT A TRADE, and calling it one
        # overstated the session's activity in the field the operator reads and
        # in `MAX_TRADES_PER_SESSION`, which then expired sessions over trades
        # that never executed.
        #
        # A published plan still has to pass the CRO — which rejects, and was
        # rejecting every plan on GLOBAL_VAR_LIMIT for two days — and then fill.
        # `plans_approved` counts the authorisation; `trades_opened` counts the
        # fill, recorded where the fill happens.
        session.plans_approved += 1

    # THE SYMBOL IS NAMED IN THE LINE, not only in the `symbol` field. With a
    # rotation the log interleaves several instruments, and "DO NOT TRADE: the
    # Grid setup is LONG but the panel reads NEUTRAL at 0.05" is unreadable when
    # the reader cannot tell which coin it was about. It was omitted before
    # because a session had exactly one.
    _note(
        session,
        f"[{target}] {action}: {rationale}"[:400],
        decision=action,
        symbol=target,
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


# ---------------------------------------------------------------------------
# Can this instrument ever be opened at this account size?
# ---------------------------------------------------------------------------

# How long `start_session` will wait for the venue before starting anyway. Short
# on purpose: the operator is holding an HTTP response open while it runs, and
# an unreadable venue is explicitly not a refusal.
_VENUE_PRECHECK_DEADLINE_S = float(os.getenv("VENUE_PRECHECK_DEADLINE_S") or 8.0)


async def _venue_minimum_refusal(
    symbol: str,
    *,
    equity: float,
    leverage: int,
    capital_fraction: float,
) -> Optional[str]:
    """None if the session could place an order in `symbol`, else why it cannot.

    THE GAP THIS CLOSES IS THE SAME ONE THE UNTRADEABLE CHECK CLOSES, one level
    down. `tradeable_universe` answers "may we open this?"; this answers "could
    we, at this account size?" — and a NO to the second is just as permanent and
    was just as silent.

    Measured on the operator's live $1.92 account while choosing a rotation set,
    read from ccxt's parsed limits and cross-checked against the raw Binance
    filters in the same market object (2026-10-09). The last column is the
    smallest order the venue will accept — whichever of the two binds:

        XRP   minQty 0.1   = $0.14    min notional $5     -> $5.00
        SOL   minQty 0.01  = $1.10    min notional $5     -> $5.00
        DOGE  minQty 1     = $0.08    min notional $5     -> $5.00
        ADA   minQty 1     = $0.24    min notional $5     -> $5.00
        SUI   minQty 0.1   = $0.11    min notional $5     -> $5.00
        BNB   minQty 0.01  = $7.39    min notional $5     -> $7.39
        AVAX  minQty 1     = $10.17   min notional $5     -> $10.17
        ETH   minQty 0.001 = $2.49    min notional $20    -> $20.00   NEVER

    The whole account at 100% and 10x is $19.18 of notional, so ETH can never
    be opened at this size. An ETH slot in a five-coin rotation is a fifth of
    every cycle spent on a 24-node analysis whose order the venue would refuse
    — the same waste as the 55 doomed BTC runs, and harder to see, because ETH
    IS a tradeable instrument and nothing upstream of the venue objects to it.

    TWO COLUMNS, NOT ONE, and AVAX is why: its minimum notional is $5 but one
    contract costs $10.17, so quoting $5 would send an operator to top up to a
    figure that still cannot trade.

    THE TEST IS RUN AT THE LARGEST SIZE THE SESSION CAN EVER REACH. If the
    biggest order it could place is refused, every smaller one is too, so this
    is a statement about the session rather than about one moment's volatility.
    It deliberately goes through `Venue.check_size` — the exact call that
    refuses at execution time — rather than re-deriving the minimums here. Two
    copies of a venue rule is how `lib/riskManager.ts` and
    `core/risk_manager.py` drifted apart on the ATR multipliers.

    A VENUE THAT CANNOT BE ASKED IS NOT A REFUSAL. An outage, a cold price cache
    or a missing key must not stop an operator starting a session; the entry
    path still refuses the order, which is the behaviour that existed before
    this check. It logs and returns None.

    AND IT IS DEADLINED, for the same reason `external_consultation` is. This is
    the only I/O `start_session` does, and the operator is waiting on an HTTP
    response while it runs. A venue that answers slowly must cost a few seconds
    and then be treated as unreadable, not hold the request open -- a check
    whose entire value is saving wasted analysis may not become the slowest
    thing in the path it guards.
    """
    try:
        return await asyncio.wait_for(
            _venue_minimum_refusal_inner(
                symbol, equity=equity, leverage=leverage, capital_fraction=capital_fraction
            ),
            timeout=_VENUE_PRECHECK_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "Session pre-check for %s timed out after %.0fs; starting anyway. The "
            "entry path still enforces the venue's minimums.",
            symbol, _VENUE_PRECHECK_DEADLINE_S,
        )
        return None


async def _venue_minimum_refusal_inner(
    symbol: str,
    *,
    equity: float,
    leverage: int,
    capital_fraction: float,
) -> Optional[str]:
    """The body of `_venue_minimum_refusal`, split out so it can be deadlined."""
    from backend.core.risk_manager import MARGIN_BUFFER_MULTIPLIER
    from backend.services.market_data import get_price
    from backend.services.venue import get_venue

    try:
        venue = get_venue()
        resolved = await venue.resolve_symbol(symbol)
        if resolved is None:
            # Not a refusal from here. "No perpetual market" is a venue-identity
            # question, and a paper session may legitimately be pointed at a
            # venue whose markets have not loaded.
            logger.info(
                "Session pre-check skipped for %s: no linear perpetual resolved on %s.",
                symbol, venue.id,
            )
            return None

        # The cache is usually cold for a symbol the session has not scanned
        # yet, which is exactly the case this check exists for, so fall back to
        # the venue's own public ticker rather than skipping.
        price = get_price(symbol)
        if price <= 0:
            ticker = await venue.public.fetch_ticker(resolved)
            price = float((ticker or {}).get("last") or 0.0)
        if price <= 0:
            logger.info("Session pre-check skipped for %s: no price available.", symbol)
            return None

        # The ceiling the Risk Gateway's broker-style sizing works up to. The
        # buffer divides it for the same reason it does there: margin has to
        # stay coverable for the stop to be reachable before a margin call.
        max_notional = (capital_fraction * equity * leverage) / max(
            MARGIN_BUFFER_MULTIPLIER, 1.0
        )
        check = await venue.check_size(symbol, max_notional / price, price)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Session pre-check skipped for %s (%s: %s). The entry path still "
            "enforces the venue's minimums.",
            symbol, type(exc).__name__, exc,
        )
        return None

    if check.ok:
        return None

    minimum = None
    if check.min_notional is not None:
        minimum = float(check.min_notional)
    if check.min_qty is not None:
        minimum = max(minimum or 0.0, float(check.min_qty) * price)

    return (
        f"{symbol} cannot be opened at this account size on {venue.id}: the most "
        f"this session could ever stake is ${max_notional:.2f} "
        f"({capital_fraction * 100:.0f}% of ${equity:.2f} at {leverage}x), and the "
        f"venue's minimum order is "
        + (f"${minimum:.2f}. " if minimum else "larger than that. ")
        + f"({check.reason}) Every decision on it would run the full analysis and "
        f"be refused at the last step."
    )


async def start_session(
    *,
    symbol: str,
    # Extra instruments to rotate over while flat. See `TradingSession.watch_symbols`
    # for why: one coin ranges most of the time, and more instruments is the only
    # way to get more trades without lowering the bar that made the good ones good.
    symbols: Optional[List[str]] = None,
    leverage: int,
    target_equity: float,
    floor_equity: Optional[float] = None,
    start_amount: Optional[float] = None,
    capital_fraction: float = 1.0,
    daily_target_pct: Optional[float] = None,
) -> TradingSession:
    """Begin an autonomous session. Raises ValueError on an unusable request.

    `start_amount` is PAPER-ONLY and sets the book's cash before the session
    measures its starting equity — see `set_paper_starting_amount`. On the real
    book the starting amount is the exchange's balance and cannot be supplied.
    """
    from backend.core.risk_manager import max_leverage_ceiling
    from backend.services.tradeable_universe import refusal_reason as _untradeable_reason

    tab = _tab_for_session()

    # REFUSE AN UNTRADEABLE SYMBOL UP FRONT. A session on e.g. BTC/USDT (a
    # signal/benchmark, not a tradeable instrument — see `tradeable_universe`) would
    # run the full 23-node analysis every cycle and the Risk Gateway would reject it
    # every time at the tradeable-instrument gate. That was observed live: 55 full
    # `trade_analysis` runs on BTC that could never open a position — expensive
    # repeated analysis, LLM budget and rate limit spent, and nothing to show. The
    # gate still protects the ENTRY; this just stops a doomed session from being
    # started at all, with a message the operator can act on.
    _refusal = _untradeable_reason(symbol)
    if _refusal is not None:
        raise ValueError(
            f"{_refusal} A session cannot be run on it — it would analyse every "
            f"cycle and never open a trade. Pick a tradeable instrument (e.g. "
            f"SOL/USDT, ETH/USDT, XRP/USDT)."
        )

    # EVERY ROTATED SYMBOL IS CHECKED THE SAME WAY, for the same reason the
    # primary one is. An untradeable instrument in the list would take its turn
    # in the rotation, run the full 24-node graph, and be refused at the Risk
    # Gateway's tradeable-instrument gate every single time — which is exactly
    # the waste that refusal was added to stop (55 doomed BTC runs, measured
    # live). Silently DROPPING it would be worse than refusing: the operator
    # would be told the session covers five coins while it scanned four.
    extra: List[str] = []
    for raw in symbols or []:
        candidate = (raw or "").strip().upper()
        if not candidate or candidate == symbol.strip().upper():
            continue
        reason = _untradeable_reason(candidate)
        if reason is not None:
            raise ValueError(
                f"{reason} Remove it from the session's symbol list — it would "
                f"take its turn in the rotation and be refused every time."
            )
        if candidate not in extra:
            extra.append(candidate)

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

    # Clamp to the four allowed steps rather than trusting the caller. An
    # out-of-range fraction here would silently mis-size every trade in the
    # session, so it is bounded to (0, 1] with 1.0 as the safe default.
    try:
        cf = float(capital_fraction)
    except (TypeError, ValueError):
        cf = 1.0
    if not (0.0 < cf <= 1.0):
        cf = 1.0

    # Daily target: a positive fraction, or None. Clamped to a sane 0-50% band so a
    # typo (200) cannot make the lock unreachable or negative.
    dt_pct: Optional[float]
    try:
        dt_pct = float(daily_target_pct) if daily_target_pct is not None else None
    except (TypeError, ValueError):
        dt_pct = None
    if dt_pct is not None and not (0.0 < dt_pct <= 0.5):
        dt_pct = None

    # CAN ANY OF THESE EVER BE OPENED AT THIS ACCOUNT SIZE? Checked last,
    # because it needs the measured equity, the validated leverage and the
    # clamped fraction — the three numbers that set the largest order the
    # session can place.
    #
    # REFUSED, NOT DROPPED, for the same reason an untradeable symbol is: the
    # operator would otherwise be told the session covers five coins while a
    # fifth of every rotation was spent on one the venue will not accept.
    for _candidate in [symbol.strip().upper()] + extra:
        _why = await _venue_minimum_refusal(
            _candidate, equity=equity, leverage=leverage, capital_fraction=cf
        )
        if _why is not None:
            raise ValueError(_why)

    session = TradingSession(
        id=uuid.uuid4().hex[:12],
        symbol=symbol,
        leverage=leverage,
        start_equity=equity,
        target_equity=target_equity,
        floor_equity=floor,
        capital_fraction=cf,
        watch_symbols=extra,
        daily_target_pct=dt_pct,
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


def active_capital_fraction() -> float:
    """The fraction of the account the RUNNING session may deploy, or 1.0.

    Read by the Risk Gateway to size every trade against the operator's chosen
    allocation and to cap the total capital committed. 1.0 when no session is
    running (the agent trades the whole account, the pre-feature behaviour) and
    when a session predates the field. Never raises and never returns something
    outside (0, 1] — a bad value here would mis-size real trades.
    """
    session = active_session()
    if session is None:
        return 1.0
    try:
        cf = float(getattr(session, "capital_fraction", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return cf if 0.0 < cf <= 1.0 else 1.0


def active_session_leverage() -> Optional[int]:
    """The leverage the RUNNING session chose, or None when no session is running.

    Read by the Risk Gateway so an autonomous trade uses the leverage the operator
    picked on the home page — 5x, 10x — exactly as a broker (Binance/Bybit) applies
    it: the allocated margin times this leverage is the position's notional. Before
    this the gateway hardcoded 1x and the operator's choice did nothing.

    Returns None (not 1) when no session is running, so the gateway can tell "no
    session, use the autonomous 1x default" apart from "a session that chose 1x".
    The value was already bounded to the venue's hard ceiling at `start_session`
    (3x real / 10x paper — invariant 2, not raisable here), and the gateway bounds
    it AGAIN against the ceiling as a belt-and-braces guard: a leverage that came
    from anywhere must never exceed the hard limit.
    """
    session = active_session()
    if session is None:
        return None
    try:
        lev = int(getattr(session, "leverage", 1))
    except (TypeError, ValueError):
        return None
    return lev if lev >= 1 else None


def list_sessions(limit: int = 20) -> List[TradingSession]:
    return sorted(_sessions.values(), key=lambda s: s.started_at, reverse=True)[:limit]


async def stop_all(reason: str = "backend shutting down") -> None:
    for session_id in list(_tasks):
        await stop_session(session_id, reason)


def _reset_for_tests() -> None:
    _sessions.clear()
    _tasks.clear()
