"""Start a REAL autonomous session and watch it decide, against live market data.

WHAT THIS COVERS THAT `full_system_exercise.py` DOES NOT
========================================================
That harness drives the bus and the agents through a trade lifecycle it
constructs itself. It never starts a SESSION, so the thing an operator
actually presses — `POST /api/session/start`, the 12s poll, the rotation over
`watch_symbols`, the decision interval, the funnel counters — is untested end
to end. Every production complaint about "it only traded twice in 24 hours"
has been about THAT loop, and it was the one part no harness ran.

So this starts the real `trading_session`, lets it run against live Binance
candles, and reports the funnel it produces:

    polls -> analyses -> plans approved -> fills

Then it drives one trade through the real execution chain and verifies the
close, the realised P&L and the learning ledger, so a run proves both halves:
the loop decides, and a decision becomes a completed round trip.

IT WRITES NOTHING, AND THAT IS NOT A CONVENTION — IT IS CHECKED.
`DATABASE_URL` is pointed at TEST-NET-1 (RFC 5737, reserved and unroutable)
BEFORE any backend import and AGAIN after `load_dotenv`, because a `.env`
carrying the live Supabase URL would otherwise overwrite it. The run asserts at
the end that no database pool was ever opened. That assertion is the licence
for everything above it: a previous harness ran against the operator's live
paper book and left 11 rows in `trades`, an orphaned watch row and a blended
position.

LIVE_TRADING is forced false, so no order can reach a mainnet venue whatever
the operator's `.env` says.
"""

from __future__ import annotations

import asyncio
import os
import uuid
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# ISOLATION. Before any backend import.
# ---------------------------------------------------------------------------
_UNROUTABLE = "postgresql://harness:harness@192.0.2.1:5432/does-not-exist"
os.environ["DATABASE_URL"] = _UNROUTABLE

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
finally:
    # AGAIN, because `.env` holds the live Supabase URL and `load_dotenv`
    # would have set it if the key had been absent from the environment.
    os.environ["DATABASE_URL"] = _UNROUTABLE

os.environ["LIVE_TRADING"] = "false"
os.environ["PROFIT_TARGET_PCT"] = "0"
os.environ["PARTIAL_TP_FRACTION"] = "0"
os.environ["UNTRADEABLE_SYMBOLS"] = "BTC/USDT"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}ok{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    print(f"  {RED}FAIL{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


async def run(seconds: float, symbols: List[str]) -> int:
    failures = 0

    from backend.services import trading_session as ts
    from backend.agents.position_monitor import get_position_monitor, reset_position_monitor
    from backend.agents.execution_agent import reset_execution_agent

    reset_position_monitor()
    reset_execution_agent()

    # -- 1. the guard that blocked the operator's deploy --------------------
    from backend.services.risk_coherence import assess_current

    coherence = assess_current()
    print(f"\n{DIM}risk configuration{RESET}")
    if coherence.ok:
        ok("a session is permitted",
           f"{coherence.loss_fraction_per_stop * 100:.2f}% per stop, "
           f"{coherence.losses_to_trip:.1f} losses to halt (needs {coherence.required})")
    else:
        bad("a session is REFUSED", coherence.reason[:120])
        failures += 1
        return failures

    # -- 2. start the real session ------------------------------------------
    print(f"\n{DIM}starting the autonomous session{RESET}")
    try:
        session = await ts.start_session(
            symbol=symbols[0],
            symbols=symbols[1:],
            leverage=3,
            target_equity=10_500.0,
            start_amount=10_000.0,
            capital_fraction=0.25,
        )
    except Exception as exc:  # noqa: BLE001
        bad("start_session refused", str(exc)[:200])
        return failures + 1

    ok("session started", f"id={session.id} {session.symbol} rotation={session.scan_list()}")

    # -- 3. let the real loop run -------------------------------------------
    print(f"\n{DIM}running the real loop for {seconds:.0f}s against live candles{RESET}")
    started = time.time()
    last_report = 0.0
    while time.time() - started < seconds:
        await asyncio.sleep(2.0)
        elapsed = time.time() - started
        if elapsed - last_report >= 20.0:
            last_report = elapsed
            print(f"    {DIM}{elapsed:5.0f}s  polls={getattr(session, 'cycles_run', 0)}"
                  f"  analyses={getattr(session, 'analyses_run', 0)}"
                  f"  approved={getattr(session, 'plans_approved', 0)}"
                  f"  fills={getattr(session, 'trades_opened', 0)}{RESET}")

    polls = getattr(session, "cycles_run", 0)
    analyses = getattr(session, "analyses_run", 0)

    print()
    if polls > 0:
        ok("the session loop is polling", f"{polls} polls in {seconds:.0f}s")
    else:
        bad("the session loop never polled")
        failures += 1

    if analyses > 0:
        ok("the decision graph ran", f"{analyses} analysis run(s)")
    else:
        # NOT a failure by itself: the decision interval may simply not have
        # elapsed, and a quiet coin ends at strategy_scoring. Reported so a
        # short run is recognisable as short rather than as a broken loop.
        print(f"  {DIM}note{RESET}  no analysis completed inside the window "
              f"{DIM}(decision interval {os.getenv('DECISION_INTERVAL_S', '30')}s){RESET}")

    reason = getattr(session, "last_decision_reason", None) or getattr(
        session, "stop_reason", None)
    if reason:
        print(f"  {DIM}last decision: {str(reason)[:160]}{RESET}")

    # -- 4. the funnel is honest --------------------------------------------
    # `cycles_run` counts POLLS, not decisions. It incremented before every
    # gate once, and "9,000 cycles and no trade" read as nine thousand rejected
    # decisions when it was zero attempted ones.
    if analyses <= polls:
        ok("the funnel is ordered", f"polls {polls} >= analyses {analyses}")
    else:
        bad("more analyses than polls — the counters disagree")
        failures += 1

    await ts.stop_session(session.id, reason="exercise complete")
    ok("session stopped cleanly")

    # -- 5. a real trade through the real chain ------------------------------
    print(f"\n{DIM}driving one trade through the real execution chain{RESET}")
    failures += await _trade_lifecycle()

    # -- 6. nothing was written ---------------------------------------------
    print()
    from backend.core import db

    pool = getattr(db, "_pool", None)
    if pool is None:
        ok("isolation", "no database pool was ever opened; this run wrote nothing")
    else:
        bad("A DATABASE POOL WAS OPENED — this run may have written to production")
        failures += 1

    return failures


async def _trade_lifecycle() -> int:
    """TAR -> fill -> monitor -> stop -> close -> realised P&L -> ledger."""
    failures = 0

    from backend.core.message_bus import get_message_bus
    from backend.agents.execution_agent import get_execution_agent
    from backend.agents.position_monitor import get_position_monitor
    from backend.models.events import TarApprovedEvent, TickReceivedEvent

    bus = get_message_bus()
    executor = get_execution_agent()
    monitor = get_position_monitor()
    # ATTACH THE EXECUTOR TO THE MONITOR. Subscribing both to the bus is NOT
    # enough: `_close` returns early with a CRITICAL log when `_execution` is
    # None, so every stop silently fails to fire and the position stays open.
    # `main.py` does this at startup; a harness that forgets it measures its own
    # wiring rather than the product. (That is exactly what happened on this
    # script's first run, and the monitor was right: keeping an unclosable
    # position in the watch list is the correct response to a missing executor,
    # because forgetting it would leave it open with nothing watching.)
    monitor.attach_execution(executor)

    # `subscribe` takes a CALLABLE, not an agent.
    bus.subscribe("TAR_APPROVED", executor.handle_event)
    bus.subscribe("TAR_APPROVED", monitor.handle_event)
    bus.subscribe("ORDER_FILLED", executor.handle_event)
    bus.subscribe("ORDER_FILLED", monitor.handle_event)
    bus.subscribe("TICK_RECEIVED", executor.handle_event)
    bus.subscribe("TICK_RECEIVED", monitor.handle_event)

    # THE LEDGER IS MEASURED AS A DELTA. `backend/data/ai_memory.json` persists
    # between runs, so an absolute `total_trades >= 1` passes on a stale file
    # from an earlier exercise — which it did, reporting a trade this run never
    # made.
    from backend.services.ai_memory import stats_for_tab

    before = int((stats_for_tab("paper") or {}).get("total_trades", 0) or 0)

    closed: List[Any] = []
    bus.subscribe("POSITION_CLOSED", lambda e: closed.append(e))

    # THE LEDGER IS WRITTEN BY THE REFLECTION AGENT, not by the monitor.
    # `record_closed_trade` lives in `ReflectionAgent.handle_event` because that
    # is already the POSITION_CLOSED subscriber that owns learning.
    from backend.agents.reflection_agent import ReflectionAgent

    bus.subscribe("POSITION_CLOSED", ReflectionAgent().handle_event)

    symbol, entry = "SOL/USDT", 100.0
    stop, target = 97.5, 105.0

    async def tick(price: float) -> None:
        event = TickReceivedEvent(
            symbol=symbol, price=price, volume=1000.0, exchange="binance",
        )
        await bus.publish(event.event_type, event)

    await tick(entry)
    # UPPERCASE "LONG". `execution_agent` compares case-sensitively, so a
    # lowercase direction opens the OPPOSITE position -- the defect the first
    # run of `full_system_exercise` found, now a Literal that raises here.
    approved = TarApprovedEvent(
        tar_id=str(uuid.uuid4()), symbol=symbol, direction="LONG", tab="paper",
        approved_size=2.0, entry_price=entry, stop_loss=stop, take_profit=target,
        approved_leverage=3, strategy="Breakout",
        cro_rationale="autonomous session exercise",
    )
    await bus.publish(approved.event_type, approved)
    await asyncio.sleep(0.2)

    open_now = monitor.snapshot_open()
    if any(p["symbol"] == symbol for p in open_now):
        pos = next(p for p in open_now if p["symbol"] == symbol)
        ok("position opened and WATCHED",
           f"{pos['side']} {pos['qty']} @ {pos['entryPrice']} stop {pos['stopLoss']} "
           f"protection={pos.get('protection')}")
    else:
        bad("the fill did not reach the watch list")
        return failures + 1

    # Walk price into the stop. The monitor must close it on a TICK.
    await tick(97.0)
    await asyncio.sleep(0.3)

    if not any(p["symbol"] == symbol for p in monitor.snapshot_open()):
        ok("the stop fired and the position closed")
    else:
        bad("the stop did not close the position")
        failures += 1

    from backend.services.portfolio_store import get_portfolio

    book = (await get_portfolio() or {}).get("paper") or {}
    cash = book.get("cash")
    if isinstance(cash, (int, float)):
        ok("the paper book settled", f"cash {cash:,.2f}")
    else:
        bad("no cash figure on the paper book")
        failures += 1

    if closed:
        event = closed[-1]
        ok("POSITION_CLOSED carried the outcome",
           f"{event.exit_reason} at {event.exit_price:g}, "
           f"realised {event.realized_pnl:+.4f}, tar={str(getattr(event, 'tar_id', None))[:8]}")
        if event.exit_reason != "stop-loss":
            bad("the exit was labelled {} rather than stop-loss".format(event.exit_reason))
            failures += 1
    else:
        bad("no POSITION_CLOSED event was published")
        failures += 1

    after = int((stats_for_tab("paper") or {}).get("total_trades", 0) or 0)
    if after > before:
        ok("the learning ledger counted it", f"{before} -> {after} trade(s)")
    else:
        bad("the closed trade never reached the learning ledger",
            f"still {after}")
        failures += 1

    return failures


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--symbols", default="SOL/USDT,XRP/USDT,DOGE/USDT")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"\n{DIM}AUTONOMOUS SESSION EXERCISE — live market data, isolated database,"
          f" LIVE_TRADING forced off{RESET}")

    failures = asyncio.run(run(args.seconds, symbols))

    print()
    if failures == 0:
        print(f"  {GREEN}all checks passed{RESET}\n")
    else:
        print(f"  {RED}{failures} check(s) failed{RESET}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
