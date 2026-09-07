"""Position Monitor — the "Monitor" stage of spec Section 6's chain.

    ... -> Supervisor -> Risk -> Execution -> **Monitor** -> Reflection -> Learning ...

THE GAP THIS FILLS
------------------
The event pipeline could OPEN a position and had nothing that ever closed one.

  * `TarApprovedEvent.stop_loss` travelled all the way to the Execution Engine,
    which logged it and moved on. No component compared price against it.
  * `PositionClosedEvent` was consumed by the CEO (equity tracking) and the
    Reflection agent (learning) but published by nobody, so both were dead.
  * `workers/monitor_worker.py` looked like the missing piece but was entirely
    mocked — `open_positions = [{"symbol": "BTC-USDT", "pnl_pct": -2.5}]`
    hardcoded, its publish call commented out — and `main.py` never started it.

So a position opened through the event chain was held forever, with a stop that
existed only as a number in a log line. Spec Section 22.8 names this exact
failure: *"the worst case is not 'the bot makes a bad trade' but 'the bot goes
silent while holding a leveraged position'."*

HOW IT KNOWS THE STOP
---------------------
Two events are needed and neither carries everything:
  * TAR_APPROVED  -> tar_id, stop_loss, take_profit, tab
  * ORDER_FILLED  -> tar_id, symbol, side, fill_price, fill_quantity

They are joined on `tar_id`. A fill whose TAR was never seen is tracked as an
UNPROTECTED position and logged as critical rather than quietly ignored — an
untracked open position is the thing this agent exists to prevent.

IN-PROCESS ONLY — STATED PLAINLY
--------------------------------
This is a soft stop: it fires only while this process is running and receiving
ticks. It is NOT a resting order at the exchange. If the backend dies, nothing
closes the position. That remains the single highest-value reliability gap in
the system, and this agent narrows it (from "nothing watches at all" to
"something watches while we're up") without closing it.

THE WATCH LIST IS NOW DURABLE — AND WHAT THAT DOES AND DOES NOT FIX
-------------------------------------------------------------------
`_open` and `_pending` used to exist only on the instance, so a restart forgot
every position. They are now mirrored to `monitored_positions`
(`services/position_store.py`) after every mutation, and `restore()` reads them
back at startup.

Read the boundary precisely, because overstating it is worse than the gap:

  * FIXED — the window between a restart and the next fill. The process comes
    back up already knowing what is open and at what stop, and the first tick
    after restore enforces it. Previously it came back empty and confident.
  * FIXED — a restart between TAR_APPROVED and ORDER_FILLED. The pending
    approval is persisted too, so the fill still joins to its approved stop
    instead of being logged as an UNPROTECTED position.
  * NOT FIXED — the process being DOWN. Nothing watches while it is not
    running, restore or no restore. Only a resting stop order at the exchange
    fixes that, and this system does not place one.

So this narrows the outage window from "forever, silently" to "the length of the
restart, and we know what we were holding". It is not a substitute for a resting
order and the docstrings here must not start implying it is.
"""

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.core.agent_base import BaseAgent
from backend.models.events import (
    BaseEvent,
    EventType,
    OrderFilledEvent,
    PositionClosedEvent,
    TarApprovedEvent,
    TickReceivedEvent,
)

logger = logging.getLogger(__name__)


class _Tracked:
    """One open position being watched."""

    __slots__ = (
        "tar_id", "symbol", "side", "tab", "qty", "entry_price",
        "stop_loss", "take_profit", "opened_at", "peak_price",
        # The venue's id for the RESTING stop protecting this position, when one
        # was placed. None for paper (there is no venue order) and None when the
        # venue refused it — which is a materially less safe position and is
        # logged as such rather than left to be inferred from a null.
        "stop_order_id",
        # The venue's id for the RESTING take-profit, when one was placed. Same
        # rules as `stop_order_id`: None for paper and when the venue refused it.
        # Both rest reduce-only, so if one fires while the process is down the
        # other cannot reverse the position — see `Venue.place_take_profit`.
        "tp_order_id",
        # ATTRIBUTION, carried from the approval so the CLOSING trade row can
        # record it.
        #
        # THE LEARNING LOOP WAS STRUCTURALLY DEAD WITHOUT THIS.
        # `strategy_performance` aggregates `WHERE pnl IS NOT NULL AND strategy
        # IS NOT NULL`. Only a CLOSE carries a pnl, and only an OPEN carried a
        # strategy — so the intersection was always empty, every profile's
        # `historical_success_rate` stayed None forever, and the 0.2 track-record
        # weight in strategy scoring was permanently neutral. The agent could not
        # learn from a single one of its own outcomes.
        #
        # Confirmed against the live database before fixing: 12 closed trades,
        # strategy NULL on all 12.
        "strategy", "run_id", "entry_context",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class PositionMonitorAgent(BaseAgent):
    version = "1.0.0"
    priority = 15  # after execution, before learning

    def __init__(self, execution_agent=None) -> None:
        # tar_id -> stop/target, captured at approval and joined to the fill.
        self._pending: Dict[str, Dict[str, Any]] = {}
        # tar_id -> tracked open position
        self._open: Dict[str, _Tracked] = {}
        self._execution = execution_agent
        # Guards against a re-entrant close: a tick can arrive while an await
        # on the exchange is in flight, and without this the same position
        # would be closed twice.
        self._closing: set = set()
        super().__init__()

    # ---------------- contract ----------------

    @property
    def name(self) -> str:
        return "Position Monitor"

    @property
    def purpose(self) -> str:
        return "Watches every open position against its approved stop-loss and take-profit, closes it when either is reached, and reports the realized outcome."

    @property
    def permissions(self) -> List[str]:
        # CLOSE_POSITIONS but not ROUTE_ORDERS: it may exit a position, never
        # enter one. The distinction is what stops a monitoring loop from
        # becoming a second entry path that bypasses Risk.
        return ["READ_MARKET_DATA", "CLOSE_POSITIONS"]

    @property
    def inputs(self) -> List[str]:
        return [
            "TAR_APPROVED events (stop-loss and take-profit, keyed by tar_id)",
            "ORDER_FILLED events (entry price, quantity, symbol, side)",
            "TICK_RECEIVED events (current price, to compare against the stop)",
        ]

    @property
    def outputs(self) -> List[str]:
        return [
            "POSITION_CLOSED events with real realized P&L and a specific exit_reason",
            "Market close orders via the Execution Engine's close_position()",
            "Critical log lines for a fill with no matching TAR (an unprotected position)",
        ]

    @property
    def category(self) -> str:
        return "execution"

    @property
    def events_consumed(self) -> List[EventType]:
        return ["TAR_APPROVED", "ORDER_FILLED", "TICK_RECEIVED"]

    @property
    def events_published(self) -> List[EventType]:
        return ["POSITION_CLOSED"]

    @property
    def responsibilities(self) -> List[str]:
        return [
            "Join TAR_APPROVED to ORDER_FILLED so every position has a known stop.",
            "Compare each tick against stop and target, and close on breach.",
            "Report realized P&L and WHY the position closed, not just that it did.",
            "Flag any filled position it cannot protect.",
        ]

    @property
    def dependencies(self) -> List[str]:
        return ["MessageBus", "ExecutionAgent (for the close path)"]

    @property
    def memory_ttl(self) -> str:
        return (
            "Open positions held in-process for the life of the position and mirrored to the "
            "monitored_positions table after every change, so a restart resumes the watch. Still "
            "a soft stop: nothing enforces it while the process is down, so it is not a "
            "substitute for a resting exchange order."
        )

    @property
    def knowledge_sources(self) -> List[str]:
        return ["Approved TARs", "Order fills", "Live ticks"]

    @property
    def prompt_reference(self) -> str:
        return "POSITION_MONITOR_DETERMINISTIC_V1"

    @property
    def apis_used(self) -> List[str]:
        return ["Exchange market orders, via ExecutionAgent.close_position"]

    @property
    def database_tables(self) -> List[str]:
        # NOT `positions` — that table is the browser's book and is replaced
        # wholesale by lib/portfolioStore.server.ts::saveBook. See db/schema.sql
        # SECTION 3b.
        return ["monitored_positions"]

    @property
    def metrics_reported(self) -> List[str]:
        return ["Open positions watched", "Closes by exit reason", "Unprotected fills detected"]

    @property
    def failure_recovery_strategy(self) -> str:
        return (
            "A failed close leaves the position tracked and retries on the next tick — it is NOT "
            "dropped from the watch list, because an untracked open position is the failure this "
            "agent exists to prevent. A restart reloads the watch list from monitored_positions "
            "via restore(); with no database that reload is empty and the agent says so at "
            "WARNING rather than starting up silently blank."
        )

    @property
    def health_status(self) -> str:
        return "Active"

    # ---------------- behaviour ----------------

    def attach_execution(self, execution_agent) -> None:
        """Wire the close path after construction (main.py builds both)."""
        self._execution = execution_agent

    @property
    def open_position_count(self) -> int:
        return len(self._open)

    # ------------------------------------------------------------------
    # Durability
    # ------------------------------------------------------------------

    def _watch_rows(self) -> List[Dict[str, Any]]:
        """The whole watch list as storable rows — pending approvals included.

        Pending rows matter as much as open ones. A restart between TAR_APPROVED
        and ORDER_FILLED would otherwise lose the approved stop, and the fill
        arriving afterwards would land in `_register_fill` with no match and be
        logged as an UNPROTECTED POSITION — a real, monitorable position
        reported as unmonitorable purely because of the restart.
        """
        rows: List[Dict[str, Any]] = []

        for tar_id, appr in self._pending.items():
            rows.append({
                "tar_id": tar_id,
                "status": "pending",
                "symbol": appr.get("symbol"),
                "tab": appr.get("tab"),
                "side": None,
                "qty": None,
                "entry_price": None,
                "stop_loss": appr.get("stop_loss"),
                "take_profit": appr.get("take_profit"),
                "peak_price": None,
                "opened_at": None,
                # A pending approval has no venue order yet — the stop is placed
                # on the FILL — so this is genuinely None rather than dropped.
                "stop_order_id": None,
                "tp_order_id": None,
                "strategy": appr.get("strategy"),
                "run_id": appr.get("run_id"),
                "entry_context": appr.get("entry_context"),
            })

        for pos in self._open.values():
            rows.append({
                "tar_id": pos.tar_id,
                "status": "open",
                "symbol": pos.symbol,
                "tab": pos.tab,
                "side": pos.side,
                "qty": pos.qty,
                "entry_price": pos.entry_price,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "peak_price": pos.peak_price,
                "opened_at": pos.opened_at,
                # WAS MISSING ENTIRELY, and `save_watch_list` binds by name from
                # `_FIELDS` — so `stop_order_id` was written as NULL on every
                # single row. The column existed, the schema comment explained
                # why it mattered, and nothing ever put a value in it.
                #
                # The consequence is the one that column was added to prevent: a
                # restart could not cancel the stop this process left resting at
                # the venue, and a reduce-only stop left on a flat account is an
                # order to OPEN a reversed position the next time price touches
                # it. `_cancel_resting_stop` had no id to work with.
                "stop_order_id": pos.stop_order_id,
                "tp_order_id": pos.tp_order_id,
                "strategy": pos.strategy,
                "run_id": pos.run_id,
                "entry_context": pos.entry_context,
            })

        return rows

    async def persist_watch_list(self) -> bool:
        """Mirror the current watch list to storage. Never raises.

        Called after every mutation. Awaited rather than fired into a background
        task on purpose: a task scheduled and not awaited can lose the race
        against the very crash this exists to survive, which would make the
        durability guarantee true only when it was not needed.
        """
        from backend.services.position_store import save_watch_list

        return await save_watch_list(self._watch_rows())

    def _persist_soon(self) -> None:
        """Persist from a SYNCHRONOUS caller, best-effort.

        Exists for exactly one caller: `tighten_stop`, which is sync and has nine
        tests plus a graph node depending on that signature. Making it async to
        get one awaited write would be a wide change to the most safety-critical
        method in the file.

        The compromise is stated honestly rather than hidden: this schedules the
        write on the running loop and returns immediately, so a crash in the
        microseconds before it lands leaves the OLD stop on disk. That old stop is
        always WIDER than the new one (tighten_stop is a one-way ratchet), so the
        failure mode is "restores with less protection than it had", never "more".
        Callers already in async context should await `persist_watch_list()`
        directly — `graphs/monitoring.py::_apply_modify` does.

        With no running loop (a sync unit test) this is a no-op. That is correct:
        there is no database in that context either.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.persist_watch_list())

    async def _replace_resting_stop(self, pos: "_Tracked") -> None:
        """Move the venue-side stop to `pos.stop_loss`. Cancel first, then place.

        A TIGHTENED STOP THAT IS NOT MOVED AT THE VENUE IS THE WORST OF BOTH.
        The operator is told the stop is now tighter, the in-process monitor
        enforces the tighter level, and the order actually resting at the exchange
        is still at the ORIGINAL level. If the process then dies, the position is
        protected at a level the operator was told it had moved away from — a
        larger loss than they believe is possible.

        Cancel-then-place, in that order and not the reverse: two live reduce-only
        stops on one position means the second one, after the first fires and
        flattens, becomes an order to OPEN the opposite position. A brief window
        with NO stop is recoverable; a duplicate that opens a reversed position is
        not.
        """
        if pos.tab != "real" or pos.stop_loss is None:
            return
        await self._cancel_resting_stop(pos, "stop tightened")
        await self._place_resting_stop(pos)

    def _replace_resting_stop_soon(self, pos: "_Tracked") -> None:
        """Schedule `_replace_resting_stop` from a SYNCHRONOUS caller.

        Same compromise as `_persist_soon`, and stated as plainly: `tighten_stop`
        is sync and widely depended on, so this returns immediately and the venue
        order moves a moment later. Until it does, the exchange still holds the
        OLDER, WIDER stop — which is less protection than the operator was just
        told they have, but never more. A failure is logged at CRITICAL by
        `_place_resting_stop`, so it does not pass silently.
        """
        if pos.tab != "real":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._replace_resting_stop(pos))

    async def restore(self) -> int:
        """Reload the watch list at startup. Returns how many positions resumed.

        WHAT THIS DOES NOT CLAIM: nothing was watching while the process was
        down. Restoring means the stop is enforced again from the next tick
        onward, not that it was enforced during the outage. Price may already be
        far through it, in which case the first tick closes at whatever the
        market is now — which is exactly what a stop-out after a gap looks like,
        and is reported as `stop-loss` because that is what it is.

        Safe to call on a live agent: restored entries never overwrite something
        already tracked in memory, so a restore racing a live fill cannot revert
        that fill's position to a stale snapshot.
        """
        from backend.services.position_store import load_watch_list

        rows = await load_watch_list()
        if not rows:
            return 0

        resumed = 0
        pending = 0
        for row in rows:
            tar_id = row["tar_id"]

            if row["status"] == "pending":
                if tar_id not in self._pending and tar_id not in self._open:
                    self._pending[tar_id] = {
                        "stop_loss": row["stop_loss"],
                        "take_profit": row["take_profit"],
                        "tab": row["tab"],
                        "symbol": row["symbol"],
                        "strategy": row.get("strategy"),
                        "run_id": row.get("run_id"),
                        "entry_context": row.get("entry_context"),
                    }
                    pending += 1
                continue

            if tar_id in self._open:
                continue

            # A stored open position with no stop cannot be enforced. It is
            # loaded anyway and flagged CRITICAL rather than dropped: an open
            # position nobody is tracking is worse than one tracked without a
            # level, and dropping it would hide it from every dashboard too.
            if row["stop_loss"] is None:
                logger.critical(
                    "Restored position %s (%s) has NO stop-loss on record. It is being "
                    "tracked so it stays visible, but no level can be enforced — close it "
                    "manually or set a stop.",
                    tar_id, row["symbol"],
                )

            self._open[tar_id] = _Tracked(
                tar_id=tar_id,
                symbol=row["symbol"],
                side=row["side"],
                tab=row["tab"],
                qty=row["qty"],
                entry_price=row["entry_price"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                opened_at=row["opened_at"] or datetime.datetime.utcnow(),
                # Falls back to the entry price, not to 0 or None. peak_price
                # feeds tighten_stop's "would this fire immediately?" guard, and
                # a None there would disable that guard on every restored
                # position. The entry is the one value guaranteed to have been
                # reached, so it is the honest conservative floor.
                peak_price=row["peak_price"] if row["peak_price"] is not None else row["entry_price"],
                # Restored so a close after a restart can CANCEL the stop this
                # process left resting at the venue. An orphaned stop is an order
                # to open the opposite position the next time price touches it.
                stop_order_id=row.get("stop_order_id"),
                tp_order_id=row.get("tp_order_id"),
                # Restored so a position that opened before a restart still
                # attributes its eventual close to the strategy that chose it.
                # Dropping them here would reopen the learning-loop gap one
                # restart at a time.
                strategy=row.get("strategy"),
                run_id=row.get("run_id"),
                entry_context=row.get("entry_context"),
            )
            resumed += 1

        if resumed or pending:
            logger.warning(
                "Restored %d open position(s) and %d pending approval(s) from storage. "
                "NOTHING enforced these stops while this process was down — the first tick "
                "for each symbol will act on the CURRENT price, which may already be through "
                "the stop.",
                resumed, pending,
            )
            self.record_decision(
                "watch-list-restored",
                f"Resumed monitoring {resumed} position(s) and {pending} pending approval(s) after a restart.",
                {"resumed": resumed, "pending": pending},
                acted=True,
            )

        return resumed

    # ------------------------------------------------------------------
    # Phase 30 / spec Section 13 — read and modify, for the monitoring graph
    # ------------------------------------------------------------------

    async def _persist_closed_trade(
        self, pos: "_Tracked", exit_price: float, realized: float, reason: str
    ) -> None:
        """Record the completed round trip, WITH its realized P&L. Never raises.

        The row's `side` is the EXIT side, not the entry side, because that is
        what actually happened at this moment — a long closing is a sell. The
        entry leg is already in the table from `execution_agent._persist_trade`,
        so recording the exit as another buy would make the log read as two
        opens.

        A failure here is logged and swallowed: the position IS closed and the
        money has already moved. Raising would leave the caller believing the
        close failed and retrying an exit for a position that is already flat —
        the exact double-exit the persist-before-publish ordering above exists to
        prevent.
        """
        import uuid

        from backend.core.db import get_db_pool

        pool = get_db_pool()
        if pool is None:
            logger.error(
                "Closed %s with realized %+.2f but did NOT persist it: no database "
                "pool. The P&L dashboard and win rate will not include this trade.",
                pos.symbol, realized,
            )
            return

        exit_side = "sell" if pos.side == "buy" else "buy"
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades
                        (id, ts, tab, symbol, side, qty, price, pnl, origin_tag, note,
                         strategy, run_id, entry_context)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    str(uuid.uuid4()), datetime.datetime.utcnow(), pos.tab,
                    pos.symbol, exit_side, pos.qty, exit_price, realized,
                    "agent-close", f"closed by {reason} from entry {pos.entry_price:.8g}",
                    # THE ROW THE LEARNING LOOP READS. `strategy_performance`
                    # selects closed trades carrying a strategy; before this the
                    # close was the only row with a pnl and the only row WITHOUT
                    # a strategy, so that query could never return anything.
                    #
                    # None stays None — a manually tracked position genuinely has
                    # no strategy, and inventing one would attribute a human's
                    # outcome to an algorithm that never chose it.
                    pos.strategy, pos.run_id, pos.entry_context,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to persist the closing trade for %s (realized %+.2f): %s",
                pos.symbol, realized, exc,
            )

    async def _place_resting_stop(self, pos: "_Tracked") -> None:
        """Put the stop-loss ON THE EXCHANGE for a real position.

        THIS CLOSES THE GAP CLAUDE.md HAS ALWAYS NAMED. Every stop in this system
        was enforced by `_check_price` reacting to ticks IN THIS PROCESS. That
        works while the process is alive and does nothing whatsoever while it is
        not: a crash, a deploy, an OOM or a restart left a real position open with
        no stop anywhere in the world. Restoring the watch list narrowed the window
        from "forever, silently" to "the length of the restart" — only an order
        resting at the venue closes it, because the venue keeps working when we do
        not.

        PAPER POSITIONS GET NOTHING, and that is correct rather than an omission:
        there is no venue order behind a simulated fill, so there is nothing to
        rest. The in-process monitor is the whole mechanism there and always was.

        A FAILURE HERE DOES NOT CLOSE OR REJECT THE POSITION. The position is
        already open and the money has already moved; refusing to track it would
        leave it open AND unwatched, which is strictly worse. It is logged at
        CRITICAL because the operator is now relying on this process staying up.
        """
        if pos.tab != "real" or pos.stop_loss is None:
            return

        from backend.services.venue import get_venue

        venue = get_venue()
        if not venue.has_credentials():
            return

        # The EXIT side: a long is closed by selling.
        exit_side = "sell" if pos.side == "buy" else "buy"
        result = await venue.place_stop_loss(
            symbol=pos.symbol,
            side=exit_side,
            qty=pos.qty,
            stop_price=pos.stop_loss,
            client_order_id=f"sl_{pos.tar_id}"[:36],
        )

        if result.ok:
            pos.stop_order_id = result.order_id
            logger.warning(
                "Resting stop placed at %s for %s %s @ %s (order %s). The position is now "
                "protected even if this process stops.",
                venue.id, pos.symbol, pos.qty, pos.stop_loss, result.order_id,
            )
        else:
            pos.stop_order_id = None
            logger.critical(
                "NO RESTING STOP AT THE VENUE for %s (%s): %s. The in-process monitor is the "
                "ONLY thing enforcing this stop, so a crash or restart leaves this REAL "
                "position unprotected until the process returns.",
                pos.symbol, pos.tar_id, result.error,
            )

    async def _cancel_resting_stop(self, pos: "_Tracked", reason: str) -> None:
        """Remove the venue-side stop once the position it protected is gone.

        A stop left resting after its position closes is an order to OPEN the
        opposite position the next time price touches that level. Cancelling is
        therefore not tidy-up, it is the second half of the close.
        """
        if not pos.stop_order_id:
            return

        from backend.services.venue import get_venue

        venue = get_venue()
        ok = await venue.cancel_order(pos.stop_order_id, pos.symbol)
        if ok:
            pos.stop_order_id = None
        else:
            logger.critical(
                "COULD NOT CANCEL the resting stop %s for %s after %s. It may still be live at "
                "%s, where it would OPEN an opposite position if price reaches it. Cancel it "
                "manually.",
                pos.stop_order_id, pos.symbol, reason, venue.id,
            )

    async def _place_resting_tp(self, pos: "_Tracked") -> None:
        """Put the take-profit ON THE EXCHANGE for a real position, beside the stop.

        The mirror of `_place_resting_stop`. It captures the UPSIDE while the
        process is down: a favourable move that reaches the target during a restart
        is otherwise simply missed, the position rides back through it, and the
        monitor returns to a smaller or negative unrealised. A resting TP makes the
        target as durable as the stop.

        PAPER GETS NOTHING (no venue order behind a simulated fill), and a position
        with no `take_profit` gets nothing — some entries are stop-only.

        A FAILURE HERE DOES NOT CLOSE OR REJECT THE POSITION, and is only a WARNING
        rather than the stop's CRITICAL: an unprotected DOWNSIDE is a loss that can
        run, but a missed target is only an upside not captured while the process is
        down — the in-process monitor still takes it the moment the process is
        alive. The stop is the safety-critical leg; this is the profit leg.
        """
        if pos.tab != "real" or pos.take_profit is None:
            return

        from backend.services.venue import get_venue

        venue = get_venue()
        if not venue.has_credentials():
            return

        exit_side = "sell" if pos.side == "buy" else "buy"
        result = await venue.place_take_profit(
            symbol=pos.symbol,
            side=exit_side,
            qty=pos.qty,
            take_profit_price=pos.take_profit,
            client_order_id=f"tp_{pos.tar_id}"[:36],
        )

        if result.ok:
            pos.tp_order_id = result.order_id
            logger.info(
                "Resting take-profit placed at %s for %s %s @ %s (order %s). The target is "
                "now captured even if this process stops.",
                venue.id, pos.symbol, pos.qty, pos.take_profit, result.order_id,
            )
        else:
            pos.tp_order_id = None
            logger.warning(
                "No resting take-profit at the venue for %s (%s): %s. The stop still rests; "
                "only the upside target waits on this process being alive.",
                pos.symbol, pos.tar_id, result.error,
            )

    async def _cancel_resting_tp(self, pos: "_Tracked", reason: str) -> None:
        """Remove the venue-side take-profit once its position is gone.

        Same reasoning as `_cancel_resting_stop`: a reduce-only order left resting
        on a now-flat account is clutter that a reconcile would flag, so cancelling
        is the second half of the close. It is reduce-only so it cannot reverse the
        position, but a stale order must not be left behind.
        """
        if not pos.tp_order_id:
            return

        from backend.services.venue import get_venue

        venue = get_venue()
        ok = await venue.cancel_order(pos.tp_order_id, pos.symbol)
        if ok:
            pos.tp_order_id = None
        else:
            logger.warning(
                "Could not cancel the resting take-profit %s for %s after %s. It is "
                "reduce-only so it cannot reverse the position, but cancel it manually at "
                "%s to keep the venue's open-order list clean.",
                pos.tp_order_id, pos.symbol, reason, venue.id,
            )

    async def track_manual_position(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        stop_loss: float,
        take_profit: Optional[float] = None,
        tab: str = "paper",
    ) -> str:
        """Watch a position the OPERATOR opened by hand. Returns its tracking id.

        WHY THIS IS A PUBLIC METHOD AND NOT A PAIR OF BUS EVENTS
        --------------------------------------------------------
        The obvious way to register a manual trade is to publish TAR_APPROVED and
        ORDER_FILLED, so it travels the same path as an agent trade. That was
        tried and it is wrong on three counts, each found by running it:

          1. `ExecutionAgent` also subscribes to TAR_APPROVED, so publishing one
             makes the AGENT'S EXECUTOR place a second (simulated) order for a
             trade the operator has already booked.
          2. Its own ORDER_FILLED then consumes the pending entry, and a
             hand-published second fill arrives for a `tar_id` that is no longer
             pending — logged, correctly, as an UNPROTECTED POSITION. A false
             alarm on every manual trade is how a real one stops being read.
          3. `ExecutionAgent` compares `tar.direction` against the literal
             "LONG", so the case of a hand-built event silently decides whether
             the order is a buy or a sell.

        More fundamentally, a TAR is an AGENT artifact: it means the Supervisor
        proposed and the CRO approved. Synthesising one for a human's click would
        put a fabricated approval in the audit trail for a decision no agent made.
        CLAUDE.md invariant 1 keeps the operator plane outside the agent plane;
        this method is that boundary, and the `tar_id` it mints is prefixed
        `manual-` so nothing downstream can mistake it for a CRO approval.

        WHAT IT DOES NOT RELAX. The position is watched by exactly the same
        `_check_price` loop as an agent position, the stop can still only ever be
        TIGHTENED, and it is persisted to the same watch list so a restart
        restores it.
        """
        import uuid as _uuid

        tar_id = f"manual-{_uuid.uuid4().hex[:12]}"
        self._open[tar_id] = _Tracked(
            tar_id=tar_id,
            symbol=symbol,
            side=side,
            tab=tab,
            qty=qty,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=datetime.datetime.utcnow(),
            # Seeded at entry, exactly as `_register_fill` does. A None peak
            # would make the first tick look like an unbounded excursion.
            peak_price=entry_price,
        )
        logger.info(
            "Monitoring MANUAL %s %s %s from %s (stop %s, target %s). %d position(s) watched.",
            side, qty, symbol, entry_price, stop_loss, take_profit, len(self._open),
        )
        self.record_decision(
            "manual-position-tracked",
            f"{symbol} opened by the operator; stop {stop_loss} is now enforced.",
            {"tarId": tar_id, "entryPrice": entry_price, "stopLoss": stop_loss},
            acted=True,
        )
        await self.persist_watch_list()
        return tar_id

    async def clear_all(self, reason: str) -> bool:
        """Forget every watched position, in memory AND in storage.

        FOR AN OPERATOR RESET ONLY. This does NOT close anything — it stops
        watching. On the paper book that is exactly right: the reset is throwing
        the whole book away, so there is nothing left to protect. On a real book
        it would be the worst possible action, leaving a live position open at the
        venue with nothing enforcing its stop, which is why the only caller
        (`api/admin.reset_paper`) refuses to run while `LIVE_TRADING` is on.

        Pending approvals are cleared too. A TAR whose fill has not arrived yet
        would otherwise open a position into a book that no longer expects it, and
        be logged as UNPROTECTED for a position the operator believes they deleted.

        Returns whether the empty list reached storage. False means memory is
        clear but `monitored_positions` still holds rows — a restart would then
        resurrect them, so the caller should say so rather than report success.
        """
        count = len(self._open)
        self._open.clear()
        self._pending.clear()
        self._closing.clear()
        logger.warning("Position monitor cleared: %s watched position(s) dropped (%s).", count, reason)
        return await self.persist_watch_list()

    def snapshot_open(self) -> List[Dict[str, Any]]:
        """Plain-dict view of every watched position.

        THIS AGENT IS THE SINGLE SOURCE OF TRUTH ON WHAT IS OPEN, and the
        monitoring graph reads through here rather than keeping its own book. Two
        books would disagree after a restart, and the one that must be right is the
        one enforcing the stop.

        Returns copies, not `_Tracked` objects: a caller holding a reference could
        otherwise assign `pos.stop_loss` directly and bypass `tighten_stop`'s
        widen-refusal, which is the one rule in this phase that must not be
        bypassable.
        """
        out: List[Dict[str, Any]] = []
        for pos in self._open.values():
            out.append({
                "tarId": pos.tar_id,
                "symbol": pos.symbol,
                "side": pos.side,
                "tab": pos.tab,
                "qty": pos.qty,
                "entryPrice": pos.entry_price,
                "stopLoss": pos.stop_loss,
                "takeProfit": pos.take_profit,
                "openedAtTs": pos.opened_at.timestamp() if pos.opened_at else None,
                "peakPrice": pos.peak_price,
            })
        return out

    def tighten_stop(self, tar_id: str, new_stop: float) -> Tuple[bool, str]:
        """Move a stop CLOSER to price. Refuses to move it further away.

        THE MOST IMPORTANT RULE IN PHASE 30.

        Widening a stop increases risk beyond what the Risk Gateway approved and
        sized the position against. The per-trade risk limit was computed from the
        entry-to-stop distance, so moving the stop away silently invalidates that
        computation — the position now risks more than 3% of equity while every
        record still says it risks 3%.

        It is also the specific mechanism by which a small loss becomes a large
        one: "give it room to breathe" is a widened stop, and a stop that can be
        widened when price approaches it is not a stop at all.

        So this is a one-way ratchet, enforced here rather than trusted to callers.
        The monitoring graph REQUESTS a new stop; this method decides.

        Returns `(applied, reason)`. A refusal is not an error — a trailing rule
        that proposes a stop already worse than the current one is ordinary.
        """
        pos = self._open.get(tar_id)
        if pos is None:
            return False, f"no open position with tar_id {tar_id} is being monitored"

        if new_stop is None or new_stop <= 0:
            return False, f"refusing a non-positive stop ({new_stop!r})"

        current = pos.stop_loss
        if current is None:
            # A tracked position with no stop should be impossible — `_register_fill`
            # requires an approved stop. Accepting one here anyway is strictly safer
            # than leaving it unprotected, and it is logged loudly.
            pos.stop_loss = new_stop
            logger.warning(
                "Position %s had NO stop; set to %s. This should be unreachable — "
                "_register_fill requires an approved stop.",
                pos.symbol, new_stop,
            )
            self._persist_soon()
            self._replace_resting_stop_soon(pos)
            return True, f"position had no stop; set to {new_stop:.8g}"

        # 'buy' means a long: a HIGHER stop is tighter. 'sell' is the mirror.
        tighter = new_stop > current if pos.side == "buy" else new_stop < current

        if not tighter:
            return False, (
                f"refused: {new_stop:.8g} is not tighter than the current "
                f"{current:.8g} for a {'long' if pos.side == 'buy' else 'short'}. "
                f"Widening a stop increases risk beyond what was approved and sized "
                f"against — this is a one-way ratchet."
            )

        # A stop already through the current price would close instantly at whatever
        # the next tick is. That is not a tightened stop, it is a market exit
        # disguised as one, and it must be requested as an EXIT so it is recorded as
        # a decision rather than as a stop-out.
        if pos.side == "buy" and pos.peak_price is not None and new_stop >= pos.peak_price:
            return False, (
                f"refused: {new_stop:.8g} is at or above the peak price "
                f"{pos.peak_price:.8g}, so it would fire on the next tick. Request an "
                f"EXIT instead of disguising one as a stop."
            )
        if pos.side == "sell" and pos.peak_price is not None and new_stop <= pos.peak_price:
            return False, (
                f"refused: {new_stop:.8g} is at or below the trough price "
                f"{pos.peak_price:.8g}, so it would fire on the next tick. Request an "
                f"EXIT instead of disguising one as a stop."
            )

        pos.stop_loss = new_stop
        logger.info(
            "Tightened stop on %s %s: %.8g -> %.8g (entry %s)",
            pos.side, pos.symbol, current, new_stop, pos.entry_price,
        )
        self.record_decision(
            "stop-tightened",
            f"{pos.symbol} stop moved {current:.8g} -> {new_stop:.8g} (tighter only).",
            {"tarId": tar_id, "previousStop": current, "newStop": new_stop},
            acted=True,
        )
        self._persist_soon()
        # The venue's resting order has to move too, or the exchange keeps
        # protecting this position at the OLD, wider level while everything on
        # screen says otherwise.
        self._replace_resting_stop_soon(pos)
        return True, f"stop tightened {current:.8g} -> {new_stop:.8g}"

    async def handle_event(self, event: BaseEvent) -> None:
        if isinstance(event, TarApprovedEvent):
            self._pending[str(event.tar_id)] = {
                "stop_loss": event.stop_loss,
                "take_profit": event.take_profit,
                "tab": event.tab,
                # Carried so the pending row can be stored and restored. The
                # fill supplies the symbol too, but not if the restart lands
                # between the approval and the fill — which is the whole reason
                # pending approvals are persisted.
                "symbol": event.symbol,
                # THE APPROVAL IS THE LAST PLACE THESE EXIST. The chain carries
                # them plan -> TAR -> CRO -> approval, and this handler was the
                # hop that dropped them: the fill event does not carry them, so
                # anything not kept here is gone by the time the position closes.
                "strategy": getattr(event, "strategy", None),
                "run_id": getattr(event, "run_id", None),
                "entry_context": getattr(event, "entry_context", None),
            }
            await self.persist_watch_list()
            return

        if isinstance(event, OrderFilledEvent):
            tracked = self._register_fill(event)
            # Placed BEFORE the persist, so the row that lands carries the stop
            # order id. Persisting first and placing after would leave a window
            # where a crash loses the id and orphans the stop at the venue.
            if tracked is not None:
                await self._place_resting_stop(tracked)
                # The take-profit rests beside the stop — see `_place_resting_tp`.
                # Placed AFTER the stop deliberately: the stop is the
                # safety-critical leg and goes on first, so a failure placing the
                # TP cannot delay the downside protection.
                await self._place_resting_tp(tracked)
            await self.persist_watch_list()
            return

        if isinstance(event, TickReceivedEvent):
            await self._check_price(event.symbol, event.price)
            return

    def _register_fill(self, event: OrderFilledEvent) -> Optional["_Tracked"]:
        """Join a fill to its approval. Returns the tracked position, or None when
        there was no matching approval and nothing can be watched."""
        tar_id = str(event.tar_id)
        approved = self._pending.pop(tar_id, None)

        if approved is None:
            # A fill with no matching approval. Loud, not silent: this position
            # is open and has no stop this agent can enforce.
            logger.critical(
                "UNPROTECTED POSITION: %s %s %s filled at %s (order %s) with no matching "
                "TAR_APPROVED, so no stop-loss is known and this position will NOT be "
                "monitored. Close it manually or restart the pipeline.",
                event.side, event.fill_quantity, event.symbol, event.fill_price, event.order_id,
            )
            self.record_decision(
                "unprotected-fill",
                f"{event.symbol} filled with no approved stop — not monitorable.",
                {"orderId": event.order_id, "tarId": tar_id},
                acted=False,
            )
            return None

        tracked = _Tracked(
            tar_id=tar_id,
            symbol=event.symbol,
            side=event.side,
            tab=approved["tab"],
            qty=event.fill_quantity,
            entry_price=event.fill_price,
            stop_loss=approved["stop_loss"],
            take_profit=approved["take_profit"],
            opened_at=datetime.datetime.utcnow(),
            peak_price=event.fill_price,
            strategy=approved.get("strategy"),
            run_id=approved.get("run_id"),
            entry_context=approved.get("entry_context"),
        )
        self._open[tar_id] = tracked
        logger.info(
            "Monitoring %s %s %s from %s (stop %s, target %s). %d position(s) watched.",
            event.side, event.fill_quantity, event.symbol, event.fill_price,
            approved["stop_loss"], approved["take_profit"], len(self._open),
        )
        self.record_decision(
            "monitoring",
            f"Watching {event.symbol} from {event.fill_price} with stop {approved['stop_loss']}.",
            {"tarId": tar_id, "stopLoss": approved["stop_loss"], "takeProfit": approved["take_profit"]},
            acted=True,
        )
        return tracked

    async def _check_price(self, symbol: str, price: float) -> None:
        if price <= 0:
            return  # a zero tick is missing data, not a price collapse

        # Snapshot the keys: closing mutates self._open mid-iteration.
        for tar_id in list(self._open.keys()):
            pos = self._open.get(tar_id)
            if pos is None or pos.symbol != symbol or tar_id in self._closing:
                continue

            if pos.side == "buy":
                pos.peak_price = max(pos.peak_price, price)
                hit_stop = pos.stop_loss is not None and price <= pos.stop_loss
                hit_target = pos.take_profit is not None and price >= pos.take_profit
            else:
                pos.peak_price = min(pos.peak_price, price)
                hit_stop = pos.stop_loss is not None and price >= pos.stop_loss
                hit_target = pos.take_profit is not None and price <= pos.take_profit

            if not hit_stop and not hit_target:
                continue

            # Stop takes precedence when a single tick spans both levels. A
            # candle that gapped through the stop AND the target is far more
            # likely to have hit the stop first, and assuming the favourable
            # one would systematically overstate performance.
            reason = "stop-loss" if hit_stop else "take-profit"
            await self._close(pos, price, reason)

    async def _close(self, pos: _Tracked, trigger_price: float, reason: str) -> None:
        if self._execution is None:
            logger.critical(
                "%s hit for %s at %s but no Execution Engine is attached — the position is "
                "STILL OPEN and cannot be closed by this agent.",
                reason, pos.symbol, trigger_price,
            )
            return

        self._closing.add(pos.tar_id)
        try:
            fill_price = await self._execution.close_position(
                symbol=pos.symbol,
                entry_side=pos.side,
                qty=pos.qty,
                tab=pos.tab,
                reason=reason,
            )

            if fill_price is None:
                # Kept in the watch list on purpose — see
                # failure_recovery_strategy. Dropping it would leave an open
                # position with nothing watching it.
                logger.error(
                    "Close of %s failed (%s at %s). Position REMAINS TRACKED and will be "
                    "retried on the next tick.",
                    pos.symbol, reason, trigger_price,
                )
                return

            # Realized P&L from the ACTUAL fill, not the trigger price. The two
            # differ by slippage, and using the trigger would report the P&L we
            # hoped for rather than the one we got.
            sign = 1 if pos.side == "buy" else -1
            realized = (fill_price - pos.entry_price) * pos.qty * sign
            held = (datetime.datetime.utcnow() - pos.opened_at).total_seconds()

            # CANCEL THE RESTING STOP BEFORE FORGETTING THE POSITION.
            #
            # A stop left at the venue after its position closes is not litter —
            # it is a live reduce-only order that, on a venue now flat, becomes an
            # order to OPEN the opposite position the next time price touches that
            # level. Cancelling is the second half of the close, not tidy-up.
            #
            # Done here rather than after `_open.pop` so the id is still in hand,
            # and awaited so a failure is logged while the symbol is still known.
            await self._cancel_resting_stop(pos, f"close by {reason}")
            # And the take-profit — the other resting leg. Cancelled here for the
            # same reason and while its id is still in hand.
            await self._cancel_resting_tp(pos, f"close by {reason}")

            self._open.pop(pos.tar_id, None)
            # Persisted BEFORE POSITION_CLOSED is published. A crash between the
            # two would otherwise leave a closed position on the watch list, and
            # the restart would resume monitoring something that no longer
            # exists — then "close" it again at the next stop touch, sending a
            # second exit order for a position already flat.
            #
            # `peak_price` deliberately is NOT persisted on every tick; only real
            # mutations write. A restored peak that lags the true one makes
            # tighten_stop's would-fire-immediately guard STRICTER, never looser,
            # so the stale value is safe in the only direction that matters.
            await self.persist_watch_list()

            # THE CLOSING TRADE, WITH ITS REALIZED P&L. This did not exist, and it
            # is why the P&L dashboard, the win rate and the trade log were empty.
            #
            # `execution_agent._persist_trade` writes the OPENING fill, and an
            # opening fill has no P&L — the trade has not finished. Nothing then
            # wrote the CLOSE, so of 2,626 rows in `trades`, 2,623 (every
            # `agent-plan` row) carried `pnl IS NULL`. `lib/api/portfolio.realised`
            # counts only rows where `typeof t.pnl === 'number'`, so it counted 3,
            # reported "no trade carries a pnl", and every derived figure — total
            # P&L, win rate, biggest win, max drawdown — was blank or meaningless.
            #
            # It is written HERE rather than in the executor because this is the
            # only place that holds entry, exit, quantity and side together, which
            # is exactly what a closed round trip is. The executor sees one leg.
            await self._persist_closed_trade(pos, fill_price, realized, reason)

            logger.info(
                "Closed %s at %s (%s, triggered at %s): realized %+.2f after %.0fs. "
                "%d position(s) still watched.",
                pos.symbol, fill_price, reason, trigger_price, realized, held, len(self._open),
            )
            self.record_decision(
                f"closed-{reason}",
                f"{pos.symbol} closed at {fill_price} ({reason}), realized {realized:+.2f}.",
                {
                    "entryPrice": pos.entry_price,
                    "exitPrice": fill_price,
                    "triggerPrice": trigger_price,
                    "realizedPnl": realized,
                    "heldSeconds": held,
                },
                acted=True,
            )

            await self.publish(
                PositionClosedEvent(
                    trade_id=pos.tar_id,
                    symbol=pos.symbol,
                    side=pos.side,
                    tab=pos.tab,
                    entry_price=pos.entry_price,
                    exit_price=fill_price,
                    quantity=pos.qty,
                    realized_pnl=realized,
                    exit_reason=reason,
                    held_seconds=held,
                    # `strategies` (the list the deterministic attribution reads)
                    # AND the singular `strategy` — populated from the tracked
                    # position, which now carries it from the approval. This list
                    # was previously always empty, so `classify_outcome`'s
                    # attribution never fired and the lesson had nothing to
                    # attribute to.
                    strategies=[pos.strategy] if pos.strategy else [],
                    strategy=pos.strategy,
                    run_id=pos.run_id,
                    entry_context=pos.entry_context,
                )
            )
        finally:
            self._closing.discard(pos.tar_id)


# The ONE monitor. See `get_position_monitor`.
_monitor: Optional[PositionMonitorAgent] = None


def get_position_monitor() -> PositionMonitorAgent:
    """The process-wide position monitor.

    IT WAS NOT A SINGLETON, AND THAT SILENTLY BROKE /api/graphs/positions
    ---------------------------------------------------------------------
    This used to be `return PositionMonitorAgent()` — a NEW, EMPTY agent on every
    call. `main.py` builds one at startup, subscribes it to the bus and hands it
    the execution engine, and that instance is the one holding every watched
    position. Every other caller got a different object.

    So `GET /api/graphs/positions`, whose own docstring calls this agent "the
    single source of truth on what is open", constructed a fresh empty monitor and
    reported `count: 0` — always, no matter how many positions the real one was
    watching. The positions view was not showing an empty book; it was showing a
    different book that could never have anything in it.

    The same shape hid a second fault: `ExecutionAgent` keeps `_last_prices` from
    TICK_RECEIVED, so a freshly constructed one has seen no ticks and refuses to
    simulate a fill with "no observed price for X yet".

    Every other accessor in this codebase is already a singleton —
    `get_message_bus`, `get_event_buffer`, `get_ticker_stream`,
    `get_polymarket_client`. This one only looked like one.
    """
    global _monitor
    if _monitor is None:
        _monitor = PositionMonitorAgent()
    return _monitor


def reset_position_monitor() -> None:
    """Drop the singleton. For tests, which must not inherit another test's book."""
    global _monitor
    # DETACH BEFORE DROPPING. The bus holds a bound method, so releasing the
    # reference alone leaves the old agent subscribed and still receiving
    # events forever — see `BaseAgent.detach`.
    if _monitor is not None:
        _monitor.detach()
    _monitor = None
