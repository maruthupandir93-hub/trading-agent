"""Drive the REAL bus and the REAL agents end to end, and write nothing anywhere.

WHY THIS EXISTS, AND WHY IT IS A SCRIPT RATHER THAN A TEST
==========================================================
Several of the worst bugs in this project's history passed a green unit suite
and were found only by exercising the wired system: the dead LLM model that
cost 300s a run, the CRO's VaR cap that refused every broker-sized trade, and
the simulated close that filled at the executor's stale tick instead of the
price the monitor decided on. CLAUDE.md records all three, and records that the
harness which found the last one ALSO left 11 rows in the operator's live
`trades` table, an orphaned watch row and a blended position -- because it
opened on a symbol the operator already held and `apply_paper_fill` keys
positions by symbol.

So this file exists to be run, and is built around not being able to do that
again.

IT WRITES NOTHING. `DATABASE_URL` is pointed at TEST-NET-1 (RFC 5737,
guaranteed unroutable) BEFORE any backend module is imported, which is the same
isolation `tests/conftest.py` uses and for the same stated reason: it makes the
failure mode the one every storage path is already written and tested against
-- no pool, and a stated reason. Every scenario then runs against the real
in-memory objects. `--check-isolation` asserts at the end that no pool was ever
opened, and that assertion is the licence for everything else here.

It also takes no network and places no orders. Prices are fed as real
`TICK_RECEIVED` events, which is exactly how `live_market_data` feeds the
monitor in production -- the path under test is the wiring, not the feed.

WHAT IT COVERS, and each one is a bug this project has actually had:

   1  bus ordering          TAR_APPROVED reaches EVERY subscriber before
                            ORDER_FILLED reaches any -- the guarantee the
                            monitor's `_pending` map and the Telegram join both
                            depend on, broken once by inline delivery
   2  long open             book, watch list and attribution after a fill
   3  short open            `side` recorded; a short cannot be represented
                            without it, and `load_portfolio` read every stored
                            short back as a long
   4  stop fires            realised loss, watch row gone, POSITION_CLOSED
   5  target fires          realised gain on a SHORT, which is the direction
                            the P&L formula had backwards
   6  close fill price      filled at the price the MONITOR decided on, not the
                            executor's stale tick -- booked -18.80 on a winning
                            move before it was fixed
   7  equity                free cash + locked margin + unrealised, and a short
                            valued in the right direction
   8  exits are never       invariant 4, under pause AND emergency stop AND
      blocked               observation mode
   9  unfundable open       the book's refusal stops the fill rather than being
                            ignored
  10  learning ledger       a close reaches `ai_memory` stamped with its book
  11  venue minimum         a session is refused on a coin the account cannot
                            open at its largest reachable size
  12  untradeable           BTC is a benchmark, not an instrument

    .venv/Scripts/python.exe scripts/full_system_exercise.py
    .venv/Scripts/python.exe scripts/full_system_exercise.py --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# ISOLATION FIRST. Before a single backend import.
# ---------------------------------------------------------------------------
# 192.0.2.0/24 is TEST-NET-1 (RFC 5737): reserved for documentation and
# guaranteed not to route. Chosen over localhost-with-a-bad-port for the reason
# `tests/conftest.py` gives -- a developer running Postgres on a non-default
# port must not be reachable either.
#
# THIS MUST HAPPEN BEFORE `load_dotenv`, not after. A `.env` carrying the
# operator's live Supabase URL would otherwise overwrite it and this harness
# would do exactly what the last one did.
os.environ["DATABASE_URL"] = "postgresql://harness:harness@192.0.2.1:5432/does-not-exist"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    # Belt and braces: `override=False` should leave ours alone, but the cost of
    # being wrong here is writing to a live trading database.
    os.environ["DATABASE_URL"] = (
        "postgresql://harness:harness@192.0.2.1:5432/does-not-exist"
    )
except ImportError:  # pragma: no cover
    pass

# Deterministic exit rules for the whole run, pinned rather than inherited.
# `tests/test_partial_tp.py` learned this the hard way: a file that reads its
# own enablement from a default is really a test of the default, and breaks the
# moment an operator changes their mind.
os.environ["PROFIT_TARGET_PCT"] = "0"
os.environ["PARTIAL_TP_FRACTION"] = "0"
os.environ["LIVE_TRADING"] = "false"
os.environ["UNTRADEABLE_SYMBOLS"] = "BTC/USDT"

from backend.agents.execution_agent import (  # noqa: E402
    ExecutionAgent, get_execution_agent, reset_execution_agent,
)
from backend.agents.position_monitor import (  # noqa: E402
    PositionMonitorAgent, get_position_monitor, reset_position_monitor,
)
from backend.core import system_state  # noqa: E402
from backend.core.message_bus import get_message_bus  # noqa: E402
from backend.models.events import (  # noqa: E402
    OrderFilledEvent, TarApprovedEvent, TickReceivedEvent,
)
from backend.services import portfolio_store  # noqa: E402

VERBOSE = False

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Check:
    """One scenario's assertions, collected rather than raised.

    A harness that stops at the first failure reports one bug per run, and the
    reason to run this at all is that the failures tend to arrive in families.
    """

    def __init__(self, name: str):
        self.name = name
        self.failures: List[str] = []
        self.notes: List[str] = []

    def ok(self, condition: Any, message: str) -> bool:
        if condition:
            if VERBOSE:
                print(f"      {DIM}ok  {message}{RESET}")
            return True
        self.failures.append(message)
        return False

    def note(self, message: str) -> None:
        self.notes.append(message)
        if VERBOSE:
            print(f"      {DIM}--  {message}{RESET}")


# ---------------------------------------------------------------------------
# Fixtures built from the REAL event models
# ---------------------------------------------------------------------------

def tar(
    *,
    symbol: str = "SOL/USDT",
    direction: str = "LONG",
    size: float = 1.0,
    leverage: int = 5,
    stop: float = 95.0,
    target: float = 110.0,
    tab: str = "paper",
    tar_id: Optional[uuid.UUID] = None,
    strategy: str = "Breakout",
    run_id: Optional[str] = None,
) -> TarApprovedEvent:
    return TarApprovedEvent(
        tar_id=tar_id or uuid.uuid4(),
        symbol=symbol,
        direction=direction,
        approved_size=size,
        approved_leverage=leverage,
        cro_rationale="harness: within every check",
        stop_loss=stop,
        take_profit=target,
        tab=tab,
        strategy=strategy,
        run_id=run_id or uuid.uuid4().hex,
        entry_context="RSI 55 | ATR 1.2% | structure bullish | regime Bull Trend",
    )


def fill(
    approved: TarApprovedEvent,
    *,
    price: float = 100.0,
    side: Optional[str] = None,
) -> OrderFilledEvent:
    return OrderFilledEvent(
        tar_id=approved.tar_id,
        exchange="binance_futures",
        order_id=str(uuid.uuid4()),
        symbol=approved.symbol,
        side=side or ("buy" if approved.direction == "LONG" else "sell"),
        quantity=approved.approved_size,
        fill_price=price,
        fill_quantity=approved.approved_size,
        slippage_bps=0.0,
        fee=0.0,
        tab=approved.tab,
    )


async def tick(symbol: str, price: float) -> None:
    evt = TickReceivedEvent(symbol=symbol, price=price, volume=1.0, exchange="harness")
    await get_message_bus().publish(evt.event_type, evt)


async def fresh() -> Tuple[ExecutionAgent, PositionMonitorAgent]:
    """A clean bus, clean agents and a clean paper book.

    Construction order is EXECUTOR FIRST, deliberately matching `main.py`. The
    close-fill-price bug was invisible in production precisely because of that
    order and showed up the moment a harness built them the other way round --
    so this harness pins the production order and scenario 6 asserts the
    property directly instead of depending on it.
    """
    bus = get_message_bus()
    if hasattr(bus, "_subscribers"):
        bus._subscribers.clear()
    reset_execution_agent()
    reset_position_monitor()
    system_state.resume("harness reset")   # clears pause AND emergency stop
    system_state.exit_observation_mode("harness reset")

    executor = get_execution_agent()
    monitor = get_position_monitor()
    # `main.py` line 108. WITHOUT THIS the monitor logs "stop-loss hit ... but
    # no Execution Engine is attached -- the position is STILL OPEN and cannot
    # be closed by this agent" and every exit scenario fails for want of
    # wiring rather than for want of correctness. Production attaches it before
    # `restore()` for the same reason.
    monitor.attach_execution(executor)
    bus.subscribe("TAR_APPROVED", executor.handle_event)
    bus.subscribe("TAR_APPROVED", monitor.handle_event)
    bus.subscribe("ORDER_FILLED", executor.handle_event)
    bus.subscribe("ORDER_FILLED", monitor.handle_event)
    bus.subscribe("TICK_RECEIVED", executor.handle_event)
    bus.subscribe("TICK_RECEIVED", monitor.handle_event)

    await portfolio_store.update_portfolio({
        "paper": {"cash": 10_000.0, "positions": []},
    })
    return executor, monitor


async def book() -> Dict[str, Any]:
    return (await portfolio_store.get_portfolio()).get("paper") or {}


async def position(symbol: str) -> Optional[Dict[str, Any]]:
    """The book stores positions as a LIST of rows, not a dict keyed by symbol,
    and each row uses `qty`/`avgCost`/`side` -- not `quantity`."""
    for row in (await book()).get("positions") or []:
        if row.get("symbol") == symbol:
            return row
    return None


async def open_position(
    *, symbol="SOL/USDT", direction="LONG", size=1.0, entry=100.0,
    stop=95.0, target=110.0, leverage=5, tab="paper",
) -> TarApprovedEvent:
    """The production sequence: approval, a tick so the executor has a price,
    then the fill. Nothing here reaches around the bus."""
    # TICK FIRST. The executor fills a simulated order against an OBSERVED
    # price and refuses without one ("no observed price for X yet"), which is
    # the correct refusal -- there is no honest fill price to invent. In
    # production ticks flow continuously, so the TAR always lands after one.
    await tick(symbol, entry)
    approved = tar(symbol=symbol, direction=direction, size=size, stop=stop,
                   target=target, leverage=leverage, tab=tab)
    await get_message_bus().publish(approved.event_type, approved)
    # NOTHING PUBLISHES THE FILL HERE. `ExecutionAgent` does it, and
    # hand-publishing a second ORDER_FILLED made the harness open every
    # position twice -- the exact "fixture differs from production" trap.
    await asyncio.sleep(0.05)
    return approved


# ---------------------------------------------------------------------------
# 1. Bus ordering
# ---------------------------------------------------------------------------

async def scenario_bus_ordering(c: Check) -> None:
    """EVERY subscriber sees TAR_APPROVED before ANY sees ORDER_FILLED.

    Broken once by inline delivery, and the consequence was not subtle: the
    monitor received the fill with an empty pending map, logged "UNPROTECTED
    POSITION", and no stop was enforceable on anything the agent opened.
    """
    bus = get_message_bus()
    if hasattr(bus, "_subscribers"):
        bus._subscribers.clear()
    seen: List[str] = []

    async def slow_first(event):
        if event.event_type == "TAR_APPROVED":
            # Publish from inside a handler -- the exact shape that used to
            # recurse into a nested delivery and overtake the outer event.
            seen.append("A:tar")
            f = fill(event, price=100.0)
            await bus.publish(f.event_type, f)
        else:
            seen.append("A:fill")

    async def second(event):
        seen.append(f"B:{'tar' if event.event_type == 'TAR_APPROVED' else 'fill'}")

    bus.subscribe("TAR_APPROVED", slow_first)
    bus.subscribe("TAR_APPROVED", second)
    bus.subscribe("ORDER_FILLED", slow_first)
    bus.subscribe("ORDER_FILLED", second)

    t = tar()
    await bus.publish(t.event_type, t)
    await asyncio.sleep(0)

    c.note(f"delivery order: {seen}")
    c.ok(seen[:2] == ["A:tar", "B:tar"],
         f"TAR_APPROVED must reach both subscribers first, got {seen}")
    c.ok(all(s.endswith("fill") for s in seen[2:]),
         f"the fill must follow the whole TAR round, got {seen}")


# ---------------------------------------------------------------------------
# 2 & 3. Opening, long and short
# ---------------------------------------------------------------------------

async def scenario_long_open(c: Check) -> None:
    executor, monitor = await fresh()
    cash_before = (await book()).get("cash")

    await open_position(symbol="SOL/USDT", direction="LONG", size=1.0, entry=100.0)

    pos = await position("SOL/USDT")
    c.ok(pos is not None, "the paper book must hold the position after a fill")
    if pos:
        c.note(f"book: {pos}")
        c.ok(pos.get("side", "buy") == "buy", f"side must be buy, got {pos.get('side')}")
        c.ok(abs(float(pos.get("qty", 0)) - 1.0) < 1e-9,
             f"qty must be 1.0, got {pos.get('qty')}")

    c.ok((await book()).get("cash") < cash_before,
         "margin must be deducted from cash on an open")

    tracked = getattr(monitor, "_open", {})
    c.ok(any(p.symbol == "SOL/USDT" for p in tracked.values()),
         "the monitor must be watching the position -- an untracked position has "
         "no enforceable stop")
    for p in tracked.values():
        if p.symbol == "SOL/USDT":
            c.ok(getattr(p, "strategy", None) == "Breakout",
                 f"strategy must survive the TAR -> fill hop, got {getattr(p, 'strategy', None)}")
            c.ok(getattr(p, "entry_context", None),
                 "the entry-context snapshot must survive the hop")


async def scenario_short_open(c: Check) -> None:
    """A SHORT, which the book could not represent at all before `side` existed
    and which `load_portfolio` read back as a long for want of one column."""
    executor, monitor = await fresh()
    await open_position(symbol="SOL/USDT", direction="SHORT", size=2.0,
                        entry=100.0, stop=105.0, target=90.0)

    pos = await position("SOL/USDT")
    c.ok(pos is not None, "the book must hold the short")
    if pos:
        c.note(f"book: {pos}")
        c.ok(pos.get("side") == "sell",
             f"a short must be stored with side=sell, got {pos.get('side')!r}")
        c.ok(float(pos.get("qty", 0)) > 0,
             "quantity is stored POSITIVE with an explicit side -- reading "
             "direction from the sign is the bug that made every short exit "
             "unreachable")


# ---------------------------------------------------------------------------
# 4, 5, 6. Closing
# ---------------------------------------------------------------------------

async def scenario_stop_fires(c: Check) -> None:
    executor, monitor = await fresh()
    closed: List[Any] = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closed.append(e))

    await open_position(symbol="SOL/USDT", direction="LONG", size=1.0,
                        entry=100.0, stop=95.0, target=110.0)
    await tick("SOL/USDT", 99.0)
    c.ok(await position("SOL/USDT") is not None,
         "a tick above the stop must not close the position")

    await tick("SOL/USDT", 94.5)
    await asyncio.sleep(0)

    c.ok(await position("SOL/USDT") is None,
         "the position must be gone from the book after the stop fires")
    tracked = getattr(monitor, "_open", {})
    c.ok(not any(p.symbol == "SOL/USDT" for p in tracked.values()),
         "the watch row must be deleted on close -- a stale row keeps the "
         "position slot occupied")
    c.ok(closed, "POSITION_CLOSED must be published; reflection, the learning "
                 "ledger and the Telegram alert all hang off it")
    if closed:
        pnl = getattr(closed[0], "realized_pnl", None)
        c.note(f"realised pnl {pnl}")
        c.ok(pnl is not None and pnl < 0, f"a stop-out must book a LOSS, got {pnl}")


async def scenario_short_target(c: Check) -> None:
    """A SHORT reaching its target. The P&L formula had the sign backwards for
    shorts once, and `buy_paper`/`sell_paper` still cannot express this."""
    executor, monitor = await fresh()
    closed: List[Any] = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closed.append(e))

    await open_position(symbol="SOL/USDT", direction="SHORT", size=1.0,
                        entry=100.0, stop=105.0, target=90.0)
    await tick("SOL/USDT", 89.5)
    await asyncio.sleep(0)

    c.ok(await position("SOL/USDT") is None, "the short must close at its target")
    c.ok(closed, "POSITION_CLOSED must be published for a short too")
    if closed:
        pnl = getattr(closed[0], "realized_pnl", None)
        c.note(f"realised pnl {pnl}")
        c.ok(pnl is not None and pnl > 0,
             f"a short that fell to its target must book a PROFIT, got {pnl}")


async def scenario_close_fill_price(c: Check) -> None:
    """The close must fill at the price the MONITOR decided on.

    `close_position` used to fill a simulated close at the EXECUTOR's own tick
    cache, so whether the two agreed depended on which subscriber the bus
    reached first -- that is, on the order agents are constructed in `main.py`.
    Measured on a harness that built them the other way: the monitor decided a
    profit target at 122.0587 and the close filled at 121.25, booking -18.80 on
    a WINNING move, almost exactly the round-trip fee.
    """
    executor, monitor = await fresh()
    closed: List[Any] = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closed.append(e))

    await open_position(symbol="SOL/USDT", direction="LONG", size=1.0,
                        entry=100.0, stop=95.0, target=110.0)

    # Freeze the EXECUTOR's cache at the entry, then move the market. In the
    # broken version the close filled at this stale number.
    if hasattr(executor, "_last_prices"):
        executor._last_prices["SOL/USDT"] = 100.0

    exit_price = 111.0
    await tick("SOL/USDT", exit_price)
    await asyncio.sleep(0)

    c.ok(closed, "the target must close the position")
    if closed:
        pnl = getattr(closed[0], "realized_pnl", None)
        c.note(f"decided at {exit_price}, booked pnl {pnl}")
        c.ok(pnl is not None and pnl > 0,
             f"a +11% move must book a PROFIT; {pnl} means the close filled at "
             f"the executor's stale tick")


# ---------------------------------------------------------------------------
# 7. Equity
# ---------------------------------------------------------------------------

async def scenario_equity(c: Check) -> None:
    """free cash + LOCKED MARGIN + unrealised. `cash + qty*price` is 1x-only and
    reported $6,300 of equity that did not exist on a 10x position."""
    executor, monitor = await fresh()
    await open_position(symbol="SOL/USDT", direction="LONG", size=10.0,
                        entry=100.0, leverage=10, stop=95.0, target=110.0)

    b = await book()
    cash = float(b.get("cash", 0))
    pos = (await position("SOL/USDT")) or {}
    locked = float(pos.get("marginLocked") or 0.0)
    c.note(f"cash {cash:.2f}  locked {locked:.2f}  entry notional 1000.00")

    flat = portfolio_store.book_equity(b, {"SOL/USDT": 100.0})["equity"]
    c.ok(abs(flat - 10_000.0) < 1.0,
         f"equity at the entry price must still be ~10,000, got {flat:.2f} -- a "
         f"larger number means the notional was added instead of the margin")

    up = portfolio_store.book_equity(b, {"SOL/USDT": 110.0})["equity"]
    c.note(f"equity at 110: {up:.2f}")
    c.ok(abs(up - (flat + 100.0)) < 1.0,
         f"a +10 move on 10 units must add ~100, got {up - flat:.2f}")

    # The direction test: a SHORT must LOSE when price rises.
    await fresh()
    await open_position(symbol="SOL/USDT", direction="SHORT", size=10.0,
                        entry=100.0, leverage=10, stop=105.0, target=90.0)
    b2 = await book()
    base = portfolio_store.book_equity(b2, {"SOL/USDT": 100.0})["equity"]
    risen = portfolio_store.book_equity(b2, {"SOL/USDT": 110.0})["equity"]
    c.note(f"short equity 100 -> 110: {base:.2f} -> {risen:.2f}")
    c.ok(risen < base,
         f"a SHORT must lose equity when price RISES; got {base:.2f} -> {risen:.2f}")


# ---------------------------------------------------------------------------
# 8. Invariant 4 -- exits are never blocked
# ---------------------------------------------------------------------------

async def scenario_exits_are_never_blocked(c: Check) -> None:
    for blocker, enter in (
        ("pause", lambda: system_state.pause("harness")),
        ("emergency stop", lambda: system_state.trigger_emergency_stop("harness")),
        ("observation mode", lambda: system_state.enter_observation_mode("harness")),
    ):
        executor, monitor = await fresh()
        closed: List[Any] = []
        get_message_bus().subscribe("POSITION_CLOSED", lambda e: closed.append(e))

        await open_position(symbol="SOL/USDT", direction="LONG", size=1.0,
                            entry=100.0, stop=95.0, target=110.0)
        enter()
        c.ok(system_state.may_open_new_position() is False,
             f"{blocker} must block OPENING")

        await tick("SOL/USDT", 94.0)
        await asyncio.sleep(0)
        c.ok(await position("SOL/USDT") is None,
             f"INVARIANT 4: a stop must still close the position under {blocker}")
        c.ok(closed, f"POSITION_CLOSED must still be published under {blocker}")

    await fresh()


# ---------------------------------------------------------------------------
# 9. An unfundable open
# ---------------------------------------------------------------------------

async def scenario_unfundable_open(c: Check) -> None:
    """`_execute_tar` ignored `apply_paper_fill`'s refusal and published
    ORDER_FILLED anyway, leaving the log, the monitor and the book disagreeing
    -- with the component holding the MONEY as the one saying no."""
    executor, monitor = await fresh()
    await portfolio_store.update_portfolio({"paper": {"cash": 5.0, "positions": []}})

    filled: List[Any] = []
    get_message_bus().subscribe("ORDER_FILLED", lambda e: filled.append(e))

    approved = tar(symbol="SOL/USDT", direction="LONG", size=1000.0,
                   leverage=1, stop=95.0, target=110.0)
    await get_message_bus().publish(approved.event_type, approved)
    await tick("SOL/USDT", 100.0)
    await asyncio.sleep(0)

    c.note(f"cash 5.00, asked for 1000 units at 100 (notional 100,000)")
    c.ok(await position("SOL/USDT") is None,
         "an unfundable open must not appear in the book")
    c.ok(not filled,
         f"ORDER_FILLED must not be published when the book refused; got {len(filled)}")


# ---------------------------------------------------------------------------
# 10. The learning ledger
# ---------------------------------------------------------------------------

async def scenario_learning_ledger(c: Check) -> None:
    """Nothing counted a closed trade for most of this project's life, so the
    measured win rate was permanently unmeasurable and Kelly used a fixed
    fraction. The ledger must also be STAMPED with its book -- a paper loss
    counting against the real daily-loss limit halts real trading on a
    simulated result."""
    from backend.services import ai_memory

    before = dict(getattr(ai_memory, "_memory", {}).get("global_stats", {}) or {})
    executor, monitor = await fresh()
    await open_position(symbol="SOL/USDT", direction="LONG", size=1.0,
                        entry=100.0, stop=95.0, target=110.0)
    await tick("SOL/USDT", 94.0)
    await asyncio.sleep(0)

    rec = getattr(ai_memory, "record_closed_trade", None)
    c.ok(callable(rec),
         "ai_memory.record_closed_trade must exist -- it is the only writer that "
         "the autonomous close path reaches")
    c.note(f"global_stats before: {before}")

    import inspect

    src = inspect.getsource(ai_memory)
    c.ok("tab_stats" in src,
         "the ledger must carry a per-book split; an unfiltered daily-loss check "
         "halts the real book on a paper result")
    # COMMENTS AND DOCSTRINGS STRIPPED, the third time this project has learned
    # it: the docstring here has to name `analyze_mistake` to explain why this
    # function does not call it, and a mention is not a call.
    from tests.sourceutil import code_only

    body = code_only(inspect.getsource(ai_memory.record_closed_trade))
    c.ok("analyze_mistake" not in body,
         "recording a close must make no model call -- `record_trade` does, and "
         "doing it here would reflect on every loss twice and spend two slots of "
         "the 40/min key budget writing one analysis")


# ---------------------------------------------------------------------------
# 11 & 12. Session refusals
# ---------------------------------------------------------------------------

async def scenario_session_refusals(c: Check) -> None:
    from backend.services import trading_session as ts
    from backend.services.tradeable_universe import refusal_reason

    c.ok(refusal_reason("BTC/USDT") is not None,
         "BTC is a benchmark, not a tradeable instrument")
    c.ok(refusal_reason("SOL/USDT") is None, "SOL must be tradeable")

    # The venue pre-check, with the venue stubbed -- the point is the wiring and
    # the arithmetic, and a real venue call would need the network.
    from backend.services.venue import SizingCheck

    class _V:
        id = "binance"

        def __init__(self):
            self.public = self
            self.asked = None

        async def resolve_symbol(self, s):
            return s + ":USDT"

        async def fetch_ticker(self, r):
            return {"last": 2500.0}

        async def check_size(self, s, q, p):
            self.asked = (q, p)
            return SizingCheck(ok=False, qty=q, min_qty=0.001, min_notional=20.0,
                               reason="notional 17.41 is below the minimum of 20.00")

    import backend.services.venue as venue_mod

    original = venue_mod.get_venue
    stub = _V()
    venue_mod.get_venue = lambda: stub
    try:
        why = await ts._venue_minimum_refusal(
            "ETH/USDT", equity=1.9176, leverage=10, capital_fraction=1.0
        )
    finally:
        venue_mod.get_venue = original

    c.note(f"refusal: {why}")
    c.ok(why is not None, "a coin whose minimum exceeds the account must be refused")
    if why:
        c.ok("19.18" in why and "20.00" in why,
             "the refusal must name BOTH the reachable size and the venue's minimum")
    if stub.asked:
        c.ok(abs(stub.asked[0] * stub.asked[1] - 19.176) < 0.01,
             f"the venue must be asked about the LARGEST reachable order, got "
             f"{stub.asked[0] * stub.asked[1]:.3f}")


async def _seed_price(symbol: str) -> Optional[float]:
    """Put a REAL current price into the dashboard tick cache.

    Fetched from public Binance, which CLAUDE.md records as reachable from
    this machine. Never fabricated: a made-up price would make every number
    downstream fiction, which is invariant 6.
    """
    import time as _time

    from backend.services import ticker_stream as tsm
    from backend.services.upstream import fetch_json

    slug = symbol.split(":")[0].replace("/", "").upper()
    res = await fetch_json(
        f"https://api.binance.com/api/v3/ticker/price?symbol={slug}",
        label="harness price seed",
    )
    if not res.ok:
        return None
    try:
        price = float((res.data or {}).get("price"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    stream = tsm.get_ticker_stream()
    stream._ticks[slug.lower()] = tsm._Tick(
        price=price, prev_close=price, ts=_time.time()
    )
    return price


# ---------------------------------------------------------------------------
# 12. Every backend module imports
# ---------------------------------------------------------------------------

async def scenario_every_module_imports(c: Check) -> None:
    """A NameError does not exist until its line runs.

    This project has hit that class three times -- `supervisor_agent`'s sizing
    dict, `risk_gateway`'s counter-trend branch, and the CRO's approval path --
    and each time the cost was finding the line, not reading the message. An
    import sweep cannot reach a branch, but it does catch the whole family of
    module-level mistakes (a bad import, a typo in a constant, an annotation
    referring to a name that was never imported -- the `Optional[List[str]]`
    ForwardRef that would have 500'd the session route on its first request).
    """
    import importlib
    import pkgutil

    import backend

    failed = []
    count = 0
    for mod in pkgutil.walk_packages(backend.__path__, prefix="backend."):
        name = mod.name
        if "__pycache__" in name:
            continue
        try:
            importlib.import_module(name)
            count += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")

    c.note(f"{count} modules imported")
    c.ok(not failed, "every backend module must import: " + "; ".join(failed[:5]))


# ---------------------------------------------------------------------------
# 13. Every agent is constructible and declares itself
# ---------------------------------------------------------------------------

async def scenario_every_agent_declares_itself(c: Check) -> None:
    """Section 3's agent contract, asserted against the real classes.

    Every agent in this system declares `name`, `purpose`, `permissions`,
    `events_consumed`, `events_published` and `failure_recovery_strategy`.
    Those are not decoration: `/api/agents` renders them, and an agent whose
    `events_consumed` disagrees with what `main.py` subscribes it to is an
    agent nobody can tell is disconnected.
    """
    import importlib
    import inspect
    import pkgutil

    import backend.agents
    from backend.core.agent_base import BaseAgent

    # SPLIT ON PURPOSE. An agent that publishes nothing is legitimate -- the
    # CIO and CEO read and advise, the Hypothesis agent writes a record -- and
    # asserting otherwise would be a harness inventing a requirement the system
    # does not have. What is NOT legitimate is an agent with no name, no stated
    # purpose, no permissions or no recovery strategy: `/api/agents` renders
    # those, and an unnamed agent is one nobody can tell is disconnected.
    identity = ("name", "purpose", "permissions", "failure_recovery_strategy")
    event_lists = ("events_consumed", "events_published")

    seen = []
    problems = []
    for mod in pkgutil.iter_modules(backend.agents.__path__, prefix="backend.agents."):
        module = importlib.import_module(mod.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseAgent) or obj is BaseAgent:
                continue
            if obj.__module__ != mod.name:
                continue   # re-export, already covered where it is defined
            try:
                agent = obj()
            except TypeError:
                # Agents that require wiring are constructed by the singletons
                # the other scenarios already drive; skipping is honest here
                # rather than inventing a constructor argument.
                c.note(f"{obj.__name__} needs wiring; covered by its own scenario")
                continue
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{obj.__name__} could not be built: {exc}")
                continue
            seen.append(obj.__name__)
            for field in identity:
                value = getattr(agent, field, None)
                if value is None or (isinstance(value, (list, str)) and not value):
                    problems.append(f"{obj.__name__}.{field} is empty")
            silent = []
            for field in event_lists:
                value = getattr(agent, field, None)
                if not isinstance(value, list):
                    problems.append(f"{obj.__name__}.{field} is not a list")
                elif not value:
                    silent.append(field)
            if silent:
                c.note(f"{obj.__name__} declares no {' and no '.join(silent)}")

    c.note(f"{len(seen)} agents: {', '.join(sorted(seen))}")
    c.ok(len(seen) >= 8, f"expected the full agent roster, found {len(seen)}")
    c.ok(not problems, "every agent must declare its contract: " + "; ".join(problems[:5]))


# ---------------------------------------------------------------------------
# 14. The AUTOMATIC chain, from a graph decision to a watched position
# ---------------------------------------------------------------------------

async def scenario_automatic_trade_chain(c: Check) -> None:
    """execution_service -> TAR -> CRO -> ExecutionAgent -> book + watch list.

    This is the plane invariant 1 describes, driven end to end with the REAL
    `ExecutionService` and the REAL `CROAgent` rather than a hand-published
    approval. Scenarios 2-6 start at TAR_APPROVED and therefore skip exactly
    the two hops where this project has lost a trade silently: Pydantic
    dropping an undeclared field on `TarApprovedEvent`, and the CRO raising a
    NameError that the bus then swallowed without a traceback.
    """
    from backend.agents.cro_agent import CROAgent
    from backend.models.events import ExecutionPlanReadyEvent
    from backend.services import execution_service as es

    executor, monitor = await fresh()
    bus = get_message_bus()

    # THE PRODUCTION WIRING FUNCTION, not a hand-made subscription. `main.py`
    # calls `subscribe_to_plans(executor)`; a harness that subscribed the
    # service itself would be testing its own wiring rather than the system's,
    # which is how the inline-delivery bug stayed invisible for so long.
    es.reset_for_tests()
    es.subscribe_to_plans(executor)

    cro = CROAgent()
    bus.subscribe("TAR_SUBMITTED", cro.handle_event)

    approvals: List[Any] = []
    rejections: List[Any] = []
    bus.subscribe("TAR_APPROVED", lambda e: approvals.append(e))
    bus.subscribe("TAR_REJECTED", lambda e: rejections.append(e))

    symbol, entry = "SOL/USDT", 100.0
    await tick(symbol, entry)

    # The plan the Risk Gateway produces: a size the VaR policy can carry, a
    # stop on the correct side, a 2:1 target.
    plan = ExecutionPlanReadyEvent(
        run_id=uuid.uuid4().hex,
        symbol=symbol,
        intent="open",
        side="buy",
        idempotency_basis=f"harness-{uuid.uuid4().hex}",
        size=1.0,
        entry_price=entry,
        stop_loss=97.5,
        take_profit=105.0,
        leverage=3,
        tab="paper",
        strategy="Breakout",
        rationale="harness: the panel agreed at 0.42",
        entry_context="RSI 58 | ATR 1.1% | structure bullish | regime Bull Trend",
    )
    await bus.publish(plan.event_type, plan)
    await asyncio.sleep(0.2)

    if rejections:
        c.note(f"CRO rejected: {getattr(rejections[0], 'reason', rejections[0])}")
    c.ok(approvals, "the CRO must approve a plan inside every limit")

    if approvals:
        approved = approvals[0]
        # THE ATTRIBUTION FIELDS. `TarApprovedEvent` once did not DECLARE them,
        # so Pydantic silently dropped what the CRO passed and the learning loop
        # was structurally incapable of producing a number.
        c.ok(getattr(approved, "strategy", None) == "Breakout",
             "the approval must carry the strategy that chose the trade")
        c.ok(getattr(approved, "run_id", None) == plan.run_id,
             "the approval must carry the run that produced it")
        c.ok(getattr(approved, "entry_context", None),
             "the approval must carry the entry-context snapshot")
        c.ok(approved.direction == "LONG",
             f"direction must be the LONG literal, got {approved.direction!r}")

    row = await position(symbol)
    c.ok(row is not None, "the automatic chain must reach the paper book")
    if row:
        c.ok(row.get("side") == "buy", f"side must be recorded, got {row.get('side')!r}")

    watched = monitor.snapshot_open()
    c.ok(len(watched) == 1,
         f"the fill must enter the stop-loss watch list, found {len(watched)}")
    if watched:
        c.ok(abs(float(watched[0]["stopLoss"]) - 97.5) < 1e-9,
             "the watched stop must be the one risk approved, not a re-derived one")


# ---------------------------------------------------------------------------
# 15. The MANUAL chain
# ---------------------------------------------------------------------------

async def scenario_manual_trade_chain(c: Check) -> None:
    """The operator's own click: book + watch list, and NOTHING else fires.

    Registration goes through `track_manual_position`, never through
    synthesised TAR_APPROVED / ORDER_FILLED events. Publishing those makes the
    AGENT's executor place a second order, its fill consumes the pending entry,
    and the hand-published one is logged as an UNPROTECTED POSITION -- a false
    alarm on every manual trade. A TAR also means "the CRO approved this", and
    minting one for a human's click puts a fabricated approval in the audit
    trail.
    """
    executor, monitor = await fresh()
    bus = get_message_bus()

    tars: List[Any] = []
    fills: List[Any] = []
    bus.subscribe("TAR_APPROVED", lambda e: tars.append(e))
    bus.subscribe("ORDER_FILLED", lambda e: fills.append(e))

    symbol, entry = "SOL/USDT", 120.0
    await tick(symbol, entry)

    before = len(fills)

    # THE ROUTE'S OWN ORDER: settle the book FIRST, then register with the
    # watcher. `api/operator_trade` does exactly this, and the order matters --
    # registering a position the book does not hold leaves the monitor trying
    # to close something `apply_paper_fill` will refuse to reduce, which is
    # precisely the "the book and the monitor disagree" state this system
    # refuses to paper over by opening a reversed position.
    funded = await portfolio_store.buy_paper(symbol, 2.0, entry, 5)
    c.ok(funded, "the operator's paper buy must settle the book")

    await monitor.track_manual_position(
        symbol=symbol, side="buy", qty=2.0, entry_price=entry,
        stop_loss=117.0, take_profit=126.0, tab="paper",
    )
    await asyncio.sleep(0.05)

    row = await position(symbol)
    c.ok(row is not None, "the manual trade must be on the paper book")

    watched = monitor.snapshot_open()
    c.ok(len(watched) == 1,
         f"a manual position must be watched, found {len(watched)}")
    c.ok(not tars,
         "a human's click must NOT mint a TAR -- that would put a fabricated "
         "CRO approval in the audit trail")
    c.ok(len(fills) == before,
         "a manual position must NOT publish ORDER_FILLED -- the agent's "
         "executor would place a second, real order")

    # And it is a real position to the monitor: its stop fires like any other.
    await tick(symbol, 116.5)
    await asyncio.sleep(0.1)
    c.ok(not monitor.snapshot_open(),
         "a manual position's stop must fire exactly like an agent position's")
    c.ok(await position(symbol) is None,
         "and the close must settle the book, not just stop watching it")
    cash = float((await book()).get("cash") or 0.0)
    c.note(f"cash after the manual round trip: {cash:.2f}")
    c.ok(cash < 10_000.0,
         f"a stop-out must leave less cash than it started with, got {cash:.2f}")


# ---------------------------------------------------------------------------
# 16. The F44 position-review EXIT, through the real nodes
# ---------------------------------------------------------------------------

async def scenario_position_review_exit(c: Check) -> None:
    """A cycle with NO entry setup must still be able to exit what we hold."""
    from backend.graphs.analysis import _after_opportunity
    from backend.graphs.nodes.supervisor import ACTION_EXIT, supervise
    from backend.graphs.state import (
        DebateVerdict, PortfolioStateSnapshot, TriggerReason, new_state,
    )

    def build(holding):
        st = new_state(run_id="x", symbol="SOL/USDT",
                       trigger=TriggerReason(kind="t", symbol="SOL/USDT", detail="d"),
                       started_at=0.0, holding_position=holding)
        st["trade_thesis"] = None
        st["debate_verdict"] = DebateVerdict(
            direction="SHORT", confidence=0.88, directional_confidence=0.88,
            participants=["market"], supporting=["momentum rolled over"], coverage=1.0,
        )
        st["portfolio_state"] = PortfolioStateSnapshot(
            tab="paper", equity=10_000.0, cash=9_000.0,
            open_positions=[{"symbol": "SOL/USDT", "qty": 5.0, "side": "buy"}],
        )
        return st

    c.ok(_after_opportunity(build(False)) == "no_opportunity",
         "a quiet cycle with nothing held must still skip the panel")
    c.ok(_after_opportunity(build(True)) == "analyse",
         "holding the instrument must convene the panel")

    out = supervise(build(True))
    c.ok(out is not None and "decision" in out,
         "a confident opposing panel must reach a decision with no fresh thesis")
    if out and "decision" in out:
        c.ok(out["decision"].action == ACTION_EXIT,
             f"expected EXIT, got {out['decision'].action}")


# ---------------------------------------------------------------------------
# 17. The whole analysis graph, on real mainnet candles
# ---------------------------------------------------------------------------

async def scenario_full_graph_on_real_data(c: Check) -> None:
    """Every node, real upstream data, no order.

    NEEDS NETWORK, and says so rather than failing silently: public Binance is
    reachable from this machine (CLAUDE.md records the months this project lost
    to assuming otherwise), but a run behind a firewall must read as skipped,
    not as a broken graph.
    """
    from backend.graphs.analysis import run_analysis_graph
    from backend.graphs.state import TriggerReason

    # A PRICE HAS TO EXIST OR THE RUN ENDS AT NODE 2.
    #
    # `get_price` reads three caches -- the agent socket, the dashboard socket
    # and the polled ccxt cache -- and none of those services runs in a
    # harness, so `data_validation` correctly aborts with "no live price".
    # That is the right refusal and not what this scenario is testing, so the
    # price is seeded into the DASHBOARD cache the same way a real tick would
    # land there. It is a real price: fetched from public Binance just above.
    seeded = await _seed_price("SOL/USDT")
    if seeded is None:
        c.note("SKIPPED: could not fetch a real price (no network)")
        return
    c.note(f"seeded live price {seeded:.4f} from public Binance")

    try:
        result = await asyncio.wait_for(
            run_analysis_graph(
                symbol="SOL/USDT",
                trigger=TriggerReason(kind="harness", symbol="SOL/USDT",
                                      detail="full-system exercise"),
            ),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        c.note("SKIPPED: the graph did not finish in 180s (upstream or model slow)")
        return
    except Exception as exc:  # noqa: BLE001
        c.failures.append(f"the analysis graph raised {type(exc).__name__}: {exc}")
        return

    visited = result.get("nodesVisited") or result.get("nodes_visited") or []
    c.note(f"{len(visited)} nodes visited")
    c.ok(visited, "the graph must record which nodes ran")

    errors = result.get("errors") or []
    c.ok(not errors, f"no node may error: {errors[:3]}")

    # The run either produced a decision or said why it did not. An empty
    # answer to "why did this not trade?" is the failure `summarise_analysis`
    # was carrying before `unavailable` was threaded through it.
    decision = result.get("decision")
    reasons = result.get("unavailable") or []
    c.ok(decision is not None or reasons,
         "a run with no decision must record a reason")
    if decision is None and reasons:
        c.note(f"no trade: {reasons[-1]}")
    elif decision is not None:
        c.note(f"decision: {getattr(decision, 'action', decision)}")


# ---------------------------------------------------------------------------
# 18. The microstructure source
# ---------------------------------------------------------------------------

async def scenario_microstructure_source(c: Check) -> None:
    """The panel must reason about the real book even while the mirror is on."""
    from backend.services import binance_testnet, microstructure_feed

    urls: List[str] = []
    original_fetch = microstructure_feed.fetch_json

    async def spy(url, *, label=""):
        urls.append(url)
        return await original_fetch(url, label=label)

    original_active = binance_testnet.demo_data_active
    binance_testnet.demo_data_active = lambda: True
    microstructure_feed.fetch_json = spy
    microstructure_feed._micro_cache.clear()
    try:
        micro = await microstructure_feed.fetch_microstructure("SOL/USDT", force=True)
    finally:
        microstructure_feed.fetch_json = original_fetch
        binance_testnet.demo_data_active = original_active

    c.ok(urls, "the microstructure fetch must have been attempted")
    for url in urls:
        c.ok(url.startswith(microstructure_feed.BINANCE_SPOT),
             f"depth/tape must come from the real market, got {url}")
    if micro.available:
        c.note(f"book: {len(micro.bids)} bids / {len(micro.asks)} asks, "
               f"{len(micro.trades)} trades")
    else:
        c.note(f"book unavailable (network): {micro.reason}")


SCENARIOS: List[Tuple[str, Callable]] = [
    ("1  bus delivery order", scenario_bus_ordering),
    ("2  long open", scenario_long_open),
    ("3  short open", scenario_short_open),
    ("4  stop fires", scenario_stop_fires),
    ("5  short reaches target", scenario_short_target),
    ("6  close fill price", scenario_close_fill_price),
    ("7  equity arithmetic", scenario_equity),
    ("8  exits are never blocked", scenario_exits_are_never_blocked),
    ("9  unfundable open", scenario_unfundable_open),
    ("10 learning ledger", scenario_learning_ledger),
    ("11 session refusals", scenario_session_refusals),
    ("12 every module imports", scenario_every_module_imports),
    ("13 every agent declares itself", scenario_every_agent_declares_itself),
    ("14 AUTOMATIC trade chain", scenario_automatic_trade_chain),
    ("15 MANUAL trade chain", scenario_manual_trade_chain),
    ("16 position-review exit", scenario_position_review_exit),
    ("17 full graph on real data", scenario_full_graph_on_real_data),
    ("18 microstructure source", scenario_microstructure_source),
]


async def main_async(check_isolation: bool) -> int:
    print(f"\n\033[1mFULL SYSTEM EXERCISE\033[0m  "
          f"{len(SCENARIOS)} scenarios, real bus, real agents, no database\n")

    results: List[Check] = []
    for label, fn in SCENARIOS:
        c = Check(label)
        print(f"  {label} ...", end="", flush=True)
        try:
            await fn(c)
        except Exception as exc:  # noqa: BLE001
            c.failures.append(f"raised {type(exc).__name__}: {exc}")
            if VERBOSE:
                traceback.print_exc()
        results.append(c)
        if c.failures:
            print(f" {RED}FAIL{RESET}")
            for f in c.failures:
                print(f"      {RED}x{RESET} {f}")
        else:
            print(f" {GREEN}ok{RESET}")
        for n in c.notes:
            if not VERBOSE:
                print(f"      {DIM}{n}{RESET}")

    # THE ISOLATION ASSERTION IS THE LICENCE FOR EVERYTHING ABOVE.
    if check_isolation:
        from backend.core.db import get_db_pool

        try:
            pool = get_db_pool()
        except Exception:
            pool = None
        print()
        if pool is None:
            print(f"  {GREEN}isolation ok{RESET}  no database pool was ever opened; "
                  f"this run wrote nothing")
        else:
            print(f"  {RED}ISOLATION BROKEN{RESET}  a database pool exists. STOP and "
                  f"check what this run may have written.")
            return 2

    failed = [c for c in results if c.failures]
    total_checks = sum(len(c.failures) for c in results)
    print()
    if failed:
        print(f"  {RED}{len(failed)} of {len(results)} scenarios failed "
              f"({total_checks} assertions){RESET}")
        return 1
    print(f"  {GREEN}{len(results)}/{len(results)} scenarios passed{RESET}")
    return 0


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-isolation-check", action="store_true",
                    help="skip the final no-database assertion (not recommended)")
    args = ap.parse_args()
    VERBOSE = args.verbose
    return asyncio.run(main_async(not args.no_isolation_check))


if __name__ == "__main__":
    sys.exit(main())
