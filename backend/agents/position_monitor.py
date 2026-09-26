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

THE SOFT STOP, AND THE RESTING ORDER THAT NOW BACKS IT
------------------------------------------------------
This agent's own stop is a SOFT stop: `_check_price` fires only while this process
is running and receiving ticks. On its own that does nothing while the process is
down.

That gap is now backed for REAL positions: `_place_resting_stop` and
`_place_resting_tp` put a reduce-only stop-market AND a reduce-only take-profit AT
THE VENUE on every real fill, so a crash, deploy or restart leaves the position
protected on both sides by orders that keep working when this process does not.
See the two methods and the "stop-loss now RESTS AT THE VENUE" note in CLAUDE.md.
PAPER positions get no venue order (there is nothing behind a simulated fill), so
for paper the in-process stop is still the whole mechanism — which is correct,
because paper cannot be liquidated. Binance order placement of these resting orders
is unverified (ccxt dropped Binance futures testnet); Bybit is verified.

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
  * The process being DOWN is now covered FOR REAL POSITIONS by the resting stop
    and take-profit at the venue (see above). The in-process watch is still soft;
    the resting orders are what hold while it is down. For PAPER positions nothing
    watches while the process is down, which is acceptable because paper cannot be
    liquidated and no real money is exposed.

So restore narrows the in-process outage window from "forever, silently" to "the
length of the restart, and we know what we were holding", and the venue-resting
orders cover a REAL position across that window regardless. The soft stop is not a
substitute for the resting order — it is the fast path while alive, and the resting
order is the durable one.
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

import os

# Fee arithmetic lives in ONE module so the paper book, the closing row and the
# backtest cannot each carry their own idea of what a trade costs — which they
# did: `execution_agent` modelled 4 bps inline while nothing else modelled any.
from backend.services.fees import FeeResult, modelled_fee, round_trip_fee
from backend.services.funding import estimate_funding

# ---------------------------------------------------------------------------
# PARTIAL PROFIT-TAKING — bank part of the move so a pullback does not give the
# WHOLE gain back to the break-even stop.
#
# This is the fix for the operator's exact complaint: a position goes +1-2%, the
# trailing rule moves the stop to break-even, price drifts back to entry, and it
# closes at 0.0 — the gain evaporates. With scale-out, at +PARTIAL_TP_R the monitor
# CLOSES PARTIAL_TP_FRACTION of the position (banking a real, realised profit) and
# moves the stop on the RUNNER to break-even. So the worst case becomes "banked
# ~1% and the runner scratched", not "gave it all back to zero", and the best case
# still rides the runner to the full target.
#
# +1R (one unit of initial risk) is the scale-out point: with the 2.5-ATR stop that
# is roughly +1% on SOL, which is exactly the "1 to 2%" the operator wants to bank.
# Both are env-tunable; a fraction of 0 disables scale-out entirely (back to the old
# all-or-nothing behaviour) so it is fully opt-out.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


PARTIAL_TP_R = _env_float("PARTIAL_TP_R", 1.0)          # profit, in R, to scale out at
PARTIAL_TP_FRACTION = _env_float("PARTIAL_TP_FRACTION", 0.5)  # how much to bank (0 disables)

# TRAILING STOP — what turns a trending window into a large win instead of a +2R cap.
#
# The evidence for adding it is this system's own ledger: 12 closed trades, 3 wins,
# and ALL THREE landed inside one 30-minute trending window. A fixed 5-ATR target
# caps exactly the runs that pay for the stop-outs, and the existing protection
# stops ratcheting the moment the partial take-profit sets the stop to break-even —
# from +1R to the target the runner has no protection above entry at all.
#
# MEASURED IN R, NOT IN PERCENT, and that matters. A percentage trail is the same
# distance on a quiet coin and a violent one, so it is either inside the noise band
# (stopping out on every wiggle — the failure this system already diagnosed at
# 1.5 ATR) or far too wide. R is the ATR-derived risk this position was actually
# sized against, so the trail automatically widens on a volatile instrument and
# tightens on a calm one, with no second volatility model to keep in sync.
#
# ARMS AT +1R BY DEFAULT, which is where the partial take-profit also fires. That
# is deliberate sequencing, not a collision: the scale-out banks half and sets the
# stop to break-even, and from that same point the trail takes over protecting the
# runner. Below +1R the original ATR stop is still the right protection — trailing
# a position that has not yet proved anything just converts the ordinary noise this
# system widened its stop to survive back into stop-outs.
#
# DISTANCE 1.0R BY DEFAULT: the stop follows one full unit of initial risk behind
# the best price seen. At +2R the stop sits at +1R, so the target is no longer a
# ceiling — the position can run while giving back at most 1R from its peak.
#
# TRAILING_STOP_R = 0 disables the trail entirely (back to fixed stop + target).
# FIXED PROFIT TARGET, as a percentage of the ENTRY PRICE. 0 disables it.
#
# Added because 53.4% of this system's closed trades realised essentially nothing:
# the scale-out banks half at +1R, moves the runner's stop to break-even, and the
# runner then closes at ~0.00 far more often than it reaches the ATR target. The
# operator's description was exact — "it takes a profit and holding and sometimes
# the price reverses and when it reaches 0.00 it finishes".
#
# With this set, a position closes ENTIRELY at the first favourable move of this
# size. Every trade is then a clean win or a clean stop, with no runner left at
# break-even to scratch. It overrides the partial take-profit (see `_check_price`)
# rather than stacking with it, because stacking would reintroduce the break-even
# runner this is meant to remove.
# DEFAULT 2%, ON. Not 0.
#
# It shipped off so the change was opt-in, and that was the wrong default for
# this system: 53.4% of its trades were closing at ~0.00 because the scale-out
# left a runner sitting at break-even. The fixed target is what removes that, and
# an operator who does not know to set it keeps the failure.
#
# 2% is the operator's own figure, and under the default "account" basis it means
# 2% of the margin deployed whatever leverage the session uses — a 2% price move
# at 1x, 0.667% at 3x, 0.2% at 10x. Set it to 0 to go back to the ATR target plus
# scale-out.
PROFIT_TARGET_PCT = _env_float("PROFIT_TARGET_PCT", 2.0)

# WHAT THE PROFIT TARGET PERCENTAGE IS A PERCENTAGE *OF*.
#
# This distinction is the whole difference between a 2% target and a 20% one, and
# it is not obvious from the number alone — which is exactly why it is an explicit
# setting rather than a convention someone has to remember.
#
#   "price"    a move of that much in the INSTRUMENT. Leverage then multiplies
#              the account effect: 2% at 10x is a 20% gain on the margin used.
#
#   "account"  a gain of that much on the MARGIN DEPLOYED, which is what an
#              operator means by "take 2% per trade". The required price move is
#              the target divided by leverage — 0.2% at 10x, 0.4% at 5x, 2% at 1x.
#
# ACCOUNT IS THE DEFAULT, because it is the only one that means the same thing to
# the operator as they change leverage. Under "price" the same setting silently
# becomes a different trade every time the session's leverage changes, and the
# operator is never told.
#
# A CONSEQUENCE WORTH SEEING PLAINLY: at high leverage the required move gets
# very small, and a move that small is inside the spread and the fees on many
# instruments. `_check_price` refuses a target it cannot clear costs on rather
# than banking a "profit" that is really a loss — see MIN_TARGET_MOVE_PCT.
PROFIT_TARGET_BASIS = (os.getenv("PROFIT_TARGET_BASIS") or "account").strip().lower()

# The smallest price move a profit target may be reduced to.
#
# A round trip costs ~0.10% in taker fees alone (0.05% a side), before spread.
# So a target that resolves to a move below this is not profit — it is a trade
# that pays the venue to close at a loss, dressed as a win. At 10x a 2% account
# target is a 0.2% move, which clears it; at 10x a 1% target would be 0.1% and
# would not.
MIN_TARGET_MOVE_PCT = 0.15

# WHEN THE STOP-LOSS ORDER IS PLACED AT THE VENUE.
#
# The take-profit always rests from entry. The STOP has three options, because
# the operator asked for a specific arrangement: "when the trade is executed via
# API it also sets only the TP; my agent continuously monitors, and if it feels
# any reverse it could make the SL by my agent".
#
#   "always"      Both legs rest from the moment of the fill. The safest, and
#                 what this system did before this setting existed.
#
#   "on_adverse"  THE OPERATOR'S ARRANGEMENT. Only the TP rests at entry. The
#                 monitor holds the stop in memory and places it at the venue the
#                 moment the position has moved against us by
#                 `RESTING_STOP_ARM_R`. Protection appears exactly on the trades
#                 that turn out to need it.
#
#   "never"       No stop ever rests. The in-process monitor is the only stop.
#
# WHAT THE RESTING STOP IS ACTUALLY FOR, so the trade-off is visible rather than
# implied: it does nothing while this process is alive, because the monitor fires
# first. It exists for the window when the process is NOT alive — a deploy, a
# restart, an OOM kill, a reboot. In that window "never" means the position has
# no protection at all, and at 10x leverage liquidation is ~9.5% away.
#
# "on_adverse" is the honest middle: a position that is winning does not need a
# venue stop, and one that is losing gets one before it can get far. It costs one
# extra API call per losing trade and it is armed long before liquidation.
#
# INVARIANT 3 IS UNAFFECTED BY ALL THREE. Every position still REQUIRES a computed
# stop — `risk_gateway` refuses a trade without one, and the monitor enforces it
# on every tick. This setting only decides whether a copy of it also sits at the
# exchange.
RESTING_STOP_MODE = (os.getenv("RESTING_STOP_MODE") or "always").strip().lower()

# How far against us the position must move before "on_adverse" places the stop.
#
# 0.5R — half the distance to the stop. Early enough that the venue order is in
# place well before the stop could be reached, late enough that a winning trade
# never spends the API call. Below ~0.2R ordinary noise would arm it on almost
# every position and the mode would collapse into "always" with extra latency.
RESTING_STOP_ARM_R = _env_float("RESTING_STOP_ARM_R", 0.5)

TRAILING_STOP_R = _env_float("TRAILING_STOP_R", 1.0)        # distance behind peak, in R
TRAILING_ACTIVATE_R = _env_float("TRAILING_ACTIVATE_R", 1.0)  # profit, in R, before it arms


class _Tracked:
    """One open position being watched."""

    __slots__ = (
        "tar_id", "symbol", "side", "tab", "qty", "entry_price",
        "stop_loss", "take_profit", "opened_at", "peak_price",
        # THE ADVERSE EXTREME — the mirror of `peak_price`, and the half that was
        # never recorded.
        #
        # `peak_price` answers "how far did this go my way?" (maximum favourable
        # excursion). `worst_price` answers "how far did it go against me before
        # it worked?" (maximum adverse excursion), which is the only way to tell a
        # stop that was genuinely hit from one that was merely too tight — a trade
        # that dipped to -0.9R and then reached the target is evidence the stop was
        # nearly right; a hundred of them is evidence it is too tight.
        #
        # Neither is derivable from a closed trade log afterwards: the log records
        # entry and exit, never the path between. That is exactly why the trailing
        # stop could not be evaluated against five days of real fills.
        "worst_price",
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
        # Set once the position has scaled out at +PARTIAL_TP_R, so it banks only
        # ONCE. In-memory only (not a DB column): after a scale-out the stop is
        # moved to break-even, so on a restart the restored stop sits AT entry and
        # the R multiple that gates a scale-out is 0 — which naturally prevents a
        # second scale-out without needing to persist this flag.
        "partial_done",
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
        # THE LEVERAGE THIS POSITION WAS OPENED AT.
        #
        # Needed because a profit target expressed as a share of the ACCOUNT is a
        # different price move at every leverage: a 2% account gain is a 2% price
        # move at 1x and a 0.2% move at 10x. Without it the monitor can only
        # measure price, and an operator asking for "2%" at 10x would get 20%.
        "leverage",
        # THE FEE PAID TO OPEN. Carried so `_close` can report P&L net of the
        # whole round trip. Without it the close knows only its own side's cost,
        # and a trade that paid more in fees than it made would still be recorded
        # as a winner — which is what every closed trade in this system did.
        "entry_fee",
        # THE ENTRY-TO-STOP DISTANCE AT ENTRY, in price units.
        #
        # A SEPARATE FIELD FROM `stop_loss` ON PURPOSE, and the reason is the
        # interaction with the partial take-profit. `_r_multiple` derives R from
        # `abs(entry_price - stop_loss)`, which is correct for gating the
        # scale-out precisely BECAUSE it collapses to zero once the stop moves to
        # break-even — that is the documented guard stopping a second scale-out
        # after a restart. The trail needs the opposite property: a denominator
        # that does NOT move when the stop does, or the trail distance would
        # shrink every time it tightened and ratchet itself into the price.
        #
        # So the two measurements are kept apart rather than one being reused for
        # both. See `_r_from_initial_risk`.
        "initial_risk",
        # THE FUNDING RATE AS IT STOOD AT ENTRY, per settlement.
        #
        # Captured at fill time — where an HTTP call is already being made to
        # place the resting stop — so the CLOSE path never waits on one. That
        # makes the funding figure an estimate: the rate floats between
        # settlements and a long hold may be billed a different one at each.
        # `services/funding` reports it as modelled for exactly that reason.
        "funding_rate",
        # The furthest the trail has actually moved the stop, or None while the
        # trail has not yet armed. In-memory only: it is derivable from
        # `stop_loss` and exists to keep the log line honest about whether a
        # given tighten came from the trail or from the break-even move.
        "trail_armed",
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
                "worst_price": None,
                "opened_at": None,
                # A pending approval has no venue order yet — the stop is placed
                # on the FILL — so this is genuinely None rather than dropped.
                "stop_order_id": None,
                "tp_order_id": None,
                "strategy": appr.get("strategy"),
                "run_id": appr.get("run_id"),
                "entry_context": appr.get("entry_context"),
                # No fill has happened, so no fee has been paid and there is no
                # entry price to measure a risk distance from. Both are genuinely
                # None here rather than zero — a 0.0 entry fee would be read as
                # "this fill was free" by the close that nets it.
                "entry_fee": None,
                "initial_risk": None,
                "funding_rate": None,
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
                "worst_price": pos.worst_price,
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
                "entry_fee": pos.entry_fee,
                "initial_risk": pos.initial_risk,
                "funding_rate": pos.funding_rate,
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
                # Same fallback and the same reasoning as peak_price: the entry is
                # the one price guaranteed to have been touched, so it is the
                # honest floor for an excursion we have no record of.
                worst_price=(
                    row.get("worst_price") if row.get("worst_price") is not None
                    else row["entry_price"]
                ),
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
                # Restored so the eventual close still nets the ENTRY fee, not
                # just the exit's. Losing it across a restart would report the
                # round trip as cheaper than it was, in the flattering direction.
                entry_fee=row.get("entry_fee"),
                # Restored so the trailing stop keeps its scale. A position
                # opened before this column existed restores with None, and
                # `_apply_trailing_stop` then leaves it on the fixed stop and
                # target it already had — degraded, but never less protected.
                initial_risk=row.get("initial_risk"),
                # Restored so a position held across a restart is still charged
                # for the settlements it lived through. Losing it would report a
                # multi-day hold as free, in the flattering direction.
                funding_rate=row.get("funding_rate"),
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

    @staticmethod
    def _excursions(pos: "_Tracked") -> Tuple[Optional[float], Optional[float]]:
        """(MFE, MAE) in units of INITIAL risk, or (None, None) if unmeasurable.

        WHAT THESE ANSWER THAT A TRADE LOG CANNOT
        -----------------------------------------
        A closed trade records entry and exit. It does not record the PATH, and
        without the path two questions are unanswerable from history:

          * Would a TRAILING stop have beaten a fixed target? A trail sits at
            `peak - TRAILING_STOP_R`, so the answer depends entirely on where the
            peak was. Replaying a trail against five days of real fills was
            impossible for exactly this reason.
          * Was the stop too TIGHT? A trade that dipped to -0.9R and then reached
            its target is evidence the stop was nearly right. Many of them is
            evidence it is converting winners into losers.

        MEASURED IN R, not percent or price, so they are comparable across
        instruments and volatility regimes — the same reason the trail itself is
        in R. `initial_risk` is the denominator because it is fixed at entry;
        using the CURRENT stop would make the scale move whenever the stop did.

        BOTH ARE None WHEN `initial_risk` IS UNKNOWN, never 0.0. A zero MFE is a
        real and rare fact — a trade that never went a single tick into profit —
        and it must not be confused with "not measured" (invariant 6).

        MAE is returned as a POSITIVE magnitude of adverse movement: 0.9 means it
        went 0.9R against the position. Signing it would invite the sign being
        applied twice by a reader who assumed it was already negative.
        """
        if not pos.initial_risk or pos.initial_risk <= 0 or pos.entry_price is None:
            return None, None
        d = 1.0 if pos.side == "buy" else -1.0
        mfe = mae = None
        if pos.peak_price is not None:
            mfe = ((pos.peak_price - pos.entry_price) * d) / pos.initial_risk
        if pos.worst_price is not None:
            mae = ((pos.entry_price - pos.worst_price) * d) / pos.initial_risk
        # Clamped at zero: an excursion cannot be negative in its own direction.
        # A peak below entry means price never went favourable at all, which is
        # MFE 0, not a negative "favourable" excursion.
        return (
            round(max(0.0, mfe), 6) if mfe is not None else None,
            round(max(0.0, mae), 6) if mae is not None else None,
        )

    async def _persist_closed_trade(
        self, pos: "_Tracked", exit_price: float, realized: float, reason: str,
        qty: Optional[float] = None, fee: Optional["FeeResult"] = None,
        funding: Optional[float] = None,
    ) -> None:
        """Record the completed round trip, WITH its realized P&L. Never raises.

        `qty` overrides the position's quantity for a PARTIAL close (scale-out): the
        row must record the quantity actually closed, not the whole position, or the
        P&L reconstruction would pair the wrong size. Defaults to the full position.

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
        row_qty = qty if qty is not None else pos.qty
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades
                        (id, ts, tab, symbol, side, qty, price, pnl, origin_tag, note,
                         strategy, run_id, entry_context, fee, fee_measured, funding,
                         mfe_r, mae_r)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                            $17, $18)
                    """,
                    str(uuid.uuid4()), datetime.datetime.utcnow(), pos.tab,
                    pos.symbol, exit_side, row_qty, exit_price, realized,
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
                    # THIS LEG's fee only. `pnl` above is already net of BOTH
                    # legs, so summing the `fee` column across a round trip gives
                    # the total cost without double-counting it into the P&L —
                    # the two columns answer different questions and must not be
                    # combined by a later reader expecting one to include the
                    # other.
                    fee.cost if fee is not None else None,
                    fee.measured if fee is not None else None,
                    # SIGNED, and already included in `pnl` above. Stored
                    # separately so the cost of HOLDING can be told apart from
                    # the cost of TRADING — a strategy that is profitable per
                    # trade but bleeds funding on long holds looks identical to
                    # one that is simply losing, unless the two are split.
                    funding,
                    # THE PATH, not just the endpoints. On a PARTIAL close these
                    # are the excursions SO FAR; the final close carries the
                    # position's whole life. That difference is deliberate and
                    # useful — comparing the two shows how much further a runner
                    # travelled after the scale-out, which is precisely the
                    # question the partial-vs-trail decision turns on.
                    *self._excursions(pos),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to persist the closing trade for %s (realized %+.2f): %s",
                pos.symbol, realized, exc,
            )

    async def _arm_resting_stop_if_adverse(self, pos: "_Tracked", price: float) -> None:
        """Place the venue stop once the position has moved against us. Never raises.

        THE OPERATOR'S ARRANGEMENT, made concrete: only the take-profit rests at
        entry, and the stop-loss order appears at the exchange the moment the
        trade starts going wrong. A winning position never spends the API call; a
        losing one is protected long before the stop could be reached.

        IDEMPOTENT. `stop_order_id` being set means a stop is already resting, so
        this does nothing on every subsequent tick. Without that check a losing
        position would place a new stop on every price update — dozens of live
        reduce-only orders, and after the first one fires the rest become orders
        to OPEN the opposite position.

        Measured against `mae_r`, the adverse excursion the monitor already
        tracks, so this needs no new measurement and inherits its direction
        handling — a short arms when price rises, a long when it falls.
        """
        if RESTING_STOP_MODE != "on_adverse":
            return
        if pos.tab != "real" or pos.stop_order_id is not None:
            return
        if pos.stop_loss is None:
            return

        _, mae = self._excursions(pos)
        if mae is None or mae < RESTING_STOP_ARM_R:
            return

        logger.warning(
            "%s has moved %.2fR against us and has no resting stop. Placing one at "
            "the venue now (RESTING_STOP_MODE=on_adverse).",
            pos.symbol, mae,
        )
        await self._place_resting_stop(pos)
        self._persist_soon()

    async def _capture_funding_rate(self, pos: "_Tracked") -> None:
        """Record the funding rate in force at entry. Best-effort, never raises.

        REAL POSITIONS ONLY, and the reason is the same one that keeps the resting
        stop off the paper path: a paper fill has no venue order behind it, so
        reaching the exchange for it makes a SIMULATED book depend on live network
        I/O. That is a dependency the simulation should not have — it fails
        differently, it is slower, and it makes the paper path untestable without
        a network.

        A paper position is still CHARGED funding. It just uses the venue baseline
        rate rather than the live one, and `services/funding` says so in its own
        detail string, so the estimate is never dressed up as a measurement
        (invariant 6). The difference between the baseline and the live rate is a
        modelling error in a simulation; omitting funding entirely — which is what
        happened before — was a systematic overstatement of every long hold.

        Leaves `funding_rate` as None on any failure rather than substituting a
        number, for the same reason.
        """
        if pos.tab != "real":
            return
        try:
            from backend.services.venue import get_venue

            rate = await get_venue().funding_rate(pos.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not capture the funding rate for %s (%s). Funding on this "
                "position will be estimated from the venue baseline.",
                pos.symbol, exc,
            )
            return
        if rate is not None:
            pos.funding_rate = rate

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

        # THE VENUE COPY MUST SIT WHERE THIS MONITOR WOULD ACTUALLY ACT, NOT AT
        # THE ATR TARGET. This was a REAL-vs-PAPER divergence, and it only bit
        # real money.
        #
        # `pos.take_profit` is the Risk Gateway's 5x-ATR target. But since
        # PROFIT_TARGET_PCT became the default exit, the in-process monitor
        # closes at a FIXED PERCENTAGE instead, and that is much nearer. Measured
        # on a live 3x SOL/USDT short: entry 121.37, the 2%-of-margin target is a
        # 0.667% move -> 120.56, while the ATR target sat at 116.96 — 5.4x
        # further away.
        #
        # So on paper the position closed at 120.56, and a real one would too
        # WHILE THIS PROCESS IS ALIVE. But the resting order — the whole point of
        # which is the window when it is NOT alive — sat at 116.96. A real trade
        # that reached its target during a deploy or a restart would sail through
        # it and ride back, while the paper book booked the win. Same settings,
        # same symbol, different outcome, and only on real money.
        #
        # `_effective_target` takes whichever level the monitor would reach
        # FIRST, so the exchange enforces the same exit this process would.
        target = self._effective_target(pos)

        exit_side = "sell" if pos.side == "buy" else "buy"
        result = await venue.place_take_profit(
            symbol=pos.symbol,
            side=exit_side,
            qty=pos.qty,
            take_profit_price=target,
            client_order_id=f"tp_{pos.tar_id}"[:36],
        )

        if result.ok:
            pos.tp_order_id = result.order_id
            logger.info(
                "Resting take-profit placed at %s for %s %s @ %s (order %s). The target is "
                "now captured even if this process stops.",
                venue.id, pos.symbol, pos.qty, target, result.order_id,
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
            worst_price=entry_price,
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
                "worstPrice": pos.worst_price,
                # Live excursion, so an operator can see how far a position has
                # travelled in each direction without waiting for it to close.
                "mfeR": self._excursions(pos)[0],
                "maeR": self._excursions(pos)[1],
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
                # AND THE LEVERAGE, for the same reason: the fill event does not
                # carry it, so if it is not kept here the monitor can only measure
                # PRICE — and an account-based profit target needs to divide by
                # the leverage to know what price move satisfies it.
                "leverage": getattr(event, "approved_leverage", None),
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
                # THE TAKE-PROFIT ALWAYS RESTS; THE STOP DEPENDS ON THE MODE.
                #
                # Under "on_adverse" the stop is deliberately NOT placed here —
                # `_arm_resting_stop_if_adverse` places it on the first tick that
                # goes against the position. Under "always" this is unchanged.
                if RESTING_STOP_MODE == "always":
                    await self._place_resting_stop(tracked)
                # The take-profit rests beside the stop — see `_place_resting_tp`.
                # Placed AFTER the stop deliberately: the stop is the
                # safety-critical leg and goes on first, so a failure placing the
                # TP cannot delay the downside protection.
                await self._place_resting_tp(tracked)
                # THE FUNDING RATE, CAPTURED HERE AND NOT AT CLOSE.
                #
                # This is the one moment an HTTP call is affordable: the position
                # is open, both protective legs are already placed, and nothing is
                # waiting on this. Reading it at CLOSE time instead would put a
                # network round trip between a stop firing and the position
                # leaving the watch list — and an unclosed position is a risk
                # problem while an unmeasured cost is only an accounting one.
                #
                # LAST, after both protective orders, so a funding-rate failure
                # can never delay the stop. It never raises and a None simply
                # means `services/funding` falls back to the venue baseline.
                await self._capture_funding_rate(tracked)
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
            worst_price=event.fill_price,
            strategy=approved.get("strategy"),
            run_id=approved.get("run_id"),
            entry_context=approved.get("entry_context"),
            # THE ENTRY FEE, carried off the fill event so the eventual close can
            # report P&L net of the whole round trip. `OrderFilledEvent.fee` has
            # always existed and nothing ever read it — the cost was published,
            # then discarded, and every realized figure was gross.
            entry_fee=getattr(event, "fee", None),
            # From the CRO's approval — the leverage the venue was actually set to.
            leverage=approved.get("leverage"),
            # THE SCALE THE TRAIL MEASURES ON, captured once, here, while the stop
            # is still the one the Risk Gateway approved. Computed now rather than
            # on demand because `stop_loss` moves: the partial take-profit sets it
            # to break-even, after which `abs(entry - stop)` is zero and the trail
            # would have no denominator. None when either side is missing, which
            # `_apply_trailing_stop` treats as "no trail", never as zero risk.
            initial_risk=(
                abs(event.fill_price - approved["stop_loss"])
                if approved.get("stop_loss") is not None and event.fill_price is not None
                else None
            ),
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
                pos.worst_price = min(
                    pos.worst_price if pos.worst_price is not None else price, price
                )
                hit_stop = pos.stop_loss is not None and price <= pos.stop_loss
                hit_target = pos.take_profit is not None and price >= pos.take_profit
            else:
                pos.peak_price = min(pos.peak_price, price)
                # A SHORT's adverse direction is UP. Mirrored rather than shared,
                # because "worst" is not "lowest" — getting this backwards would
                # record every short's best price as its worst.
                pos.worst_price = max(
                    pos.worst_price if pos.worst_price is not None else price, price
                )
                hit_stop = pos.stop_loss is not None and price >= pos.stop_loss
                hit_target = pos.take_profit is not None and price <= pos.take_profit

            if not hit_stop and not hit_target:
                # ---- FIXED PROFIT TARGET: bank the whole position at +X% -----
                #
                # THE SCRATCH PROBLEM, MEASURED. Of 4,003 closed trades, 2,136
                # (53.4%) realised less than 0.001 — and 1,121 of those exited as
                # "stop-loss". That is not the market: it is this system's own
                # scale-out. The partial banks half at +1R and moves the runner's
                # stop to BREAK-EVEN, so the runner's most likely outcome is an
                # exit at almost exactly zero. The operator watched a position go
                # into profit, pull back, and close at 0.00, over and over.
                #
                # A fixed percentage target closes the WHOLE position in one go.
                # Every trade then ends as a clean win at +PROFIT_TARGET_PCT or a
                # clean loss at the stop — there is no runner left sitting at
                # break-even, which is the only thing that produced the scratches.
                #
                # WHAT THE PERCENTAGE MEANS: a favourable move of that much in
                # PRICE. With leverage it is amplified against the margin — at 3x
                # a 2% move is ~6% of the margin deployed, at 10x ~20%. It is
                # measured on price rather than on account equity so that the same
                # setting means the same thing whatever leverage the session uses.
                #
                # IT BYPASSES THE PARTIAL DELIBERATELY. Leaving both on would
                # scale out at +1R, move the stop to break-even, and reintroduce
                # exactly the scratch this exists to remove. Off (0) restores the
                # previous ATR-target + scale-out behaviour completely.
                if PROFIT_TARGET_PCT > 0 and pos.entry_price:
                    move_pct = (
                        (price - pos.entry_price) / pos.entry_price * 100.0
                        * (1.0 if pos.side == "buy" else -1.0)
                    )
                    needed = self._target_move_pct(pos)
                    if needed is not None and move_pct >= needed:
                        await self._close(pos, price, "profit-target")
                        continue

                # PARTIAL PROFIT-TAKING, before the plain HOLD. If the position has
                # reached +PARTIAL_TP_R and has not yet scaled out, bank part of it
                # and move the runner's stop to break-even. This is what stops a
                # +1-2% gain from dying at 0.0 on a pullback.
                if PROFIT_TARGET_PCT <= 0 and PARTIAL_TP_FRACTION > 0 and not pos.partial_done:
                    r = self._r_multiple(pos, price)
                    if r is not None and r >= PARTIAL_TP_R:
                        await self._take_partial(pos, price)

                # ARM THE VENUE STOP IF THIS IS GOING WRONG.
                #
                # Under "on_adverse" no stop rests until the position has moved
                # against us. This is the tick that notices. Placed BEFORE the
                # trail so a position that is losing gets its venue protection
                # ahead of any bookkeeping.
                await self._arm_resting_stop_if_adverse(pos, price)

                # THEN TRAIL. After the scale-out, not before it — `_take_partial`
                # moves the stop to break-even and the trail must ratchet from
                # there, never propose something looser and be refused.
                #
                # Ordered this way rather than `elif` because on the tick that
                # scales out, the position is already far enough along to deserve a
                # trailed stop too; making it wait a tick leaves the runner at
                # break-even while price is at its peak.
                self._apply_trailing_stop(pos, price)
                continue

            # Stop takes precedence when a single tick spans both levels. A
            # candle that gapped through the stop AND the target is far more
            # likely to have hit the stop first, and assuming the favourable
            # one would systematically overstate performance.
            reason = "stop-loss" if hit_stop else "take-profit"
            await self._close(pos, price, reason)

    def _r_multiple(self, pos: "_Tracked", price: float) -> Optional[float]:
        """Profit in units of INITIAL risk, or None when it cannot be computed.

        Once the stop has been moved to break-even (which a scale-out does), the
        entry-to-stop distance is 0, so this returns None and a second scale-out
        cannot fire — the natural guard that makes persisting `partial_done`
        unnecessary across a restart.
        """
        if pos.entry_price is None or pos.stop_loss is None or not pos.qty:
            return None
        risk = abs(pos.entry_price - pos.stop_loss)
        if risk <= 0:
            return None
        move = (price - pos.entry_price) if pos.side == "buy" else (pos.entry_price - price)
        return move / risk

    def _effective_target(self, pos: "_Tracked") -> float:
        """The price this monitor would actually close at, taking profit.

        Two targets can be in play and only the NEARER one is ever reached:

          * `pos.take_profit` — the Risk Gateway's ATR-derived level, carried on
            the TAR.
          * PROFIT_TARGET_PCT — the operator's fixed percentage, which is what
            `_check_price` actually tests once it is set (and it is, by default).

        Returns whichever is closer to entry in the direction of the trade, so a
        resting exchange order enforces the same exit this process would. Falls
        back to `pos.take_profit` when the percentage target is off or
        unmeasurable, which is the previous behaviour exactly.

        NOT RE-PLACED WHEN THE SETTING CHANGES. The exit-rules panel applies on
        the next tick, and the in-process monitor picks that up immediately; the
        resting order keeps the level computed at entry. That is acceptable
        because the venue copy exists only for the window when this process is
        DOWN — while it is up, the monitor closes first either way — and
        re-placing every resting order on a settings change would cancel and
        re-issue live reduce-only orders across the whole book to fix a window
        that is not open.
        """
        target = pos.take_profit
        if target is None:
            return target
        move = self._target_move_pct(pos)
        if move is None or not pos.entry_price:
            return target
        if pos.side == "buy":
            pct_target = pos.entry_price * (1.0 + move / 100.0)
            return min(target, pct_target)
        pct_target = pos.entry_price * (1.0 - move / 100.0)
        return max(target, pct_target)

    @staticmethod
    def _target_move_pct(pos: "_Tracked") -> Optional[float]:
        """The PRICE move that satisfies the profit target for this position.

        Under `PROFIT_TARGET_BASIS="account"` the operator's percentage is a share
        of the MARGIN DEPLOYED, so the price only has to move that much divided by
        the leverage: a 2% account target is a 2% move at 1x and a 0.2% move at
        10x. That is the arithmetic an operator means by "take 2% per trade", and
        it is the only reading that keeps the setting meaning the same thing when
        the session's leverage changes.

        UNKNOWN LEVERAGE FALLS BACK TO 1x, which makes the required move the FULL
        percentage — the most demanding interpretation. Erring the other way would
        divide by a leverage we are not sure of and close positions early on a
        guess, which is the direction that invents profit.

        REFUSES A TARGET SMALLER THAN THE ROUND TRIP COSTS. A 0.05%-a-side taker
        fee means ~0.10% before spread, so a target that resolves below
        `MIN_TARGET_MOVE_PCT` is not a profit at all — it is a trade that pays the
        venue to close, recorded as a win. Returning None leaves the position to
        the stop, the trail and the ATR target, all of which are cost-aware
        because they are derived from volatility rather than from a fixed number.
        """
        if PROFIT_TARGET_PCT <= 0:
            return None
        if PROFIT_TARGET_BASIS == "price":
            return PROFIT_TARGET_PCT

        try:
            lev = float(pos.leverage or 1.0)
        except (TypeError, ValueError):
            lev = 1.0
        lev = max(1.0, lev)

        needed = PROFIT_TARGET_PCT / lev
        if needed < MIN_TARGET_MOVE_PCT:
            logger.warning(
                "%s: a %.2f%% ACCOUNT target at %gx needs only a %.3f%% price move, "
                "which is below the %.2f%% round-trip cost floor. The fixed target is "
                "SKIPPED for this position; its stop, trail and ATR target still apply.",
                pos.symbol, PROFIT_TARGET_PCT, lev, needed, MIN_TARGET_MOVE_PCT,
            )
            return None
        return needed

    def _r_from_initial_risk(self, pos: "_Tracked", price: float) -> Optional[float]:
        """Profit in units of the risk this position was ORIGINALLY sized against.

        Deliberately NOT `_r_multiple`. That one divides by the CURRENT
        entry-to-stop distance, which is the right denominator for gating the
        scale-out — it collapses to zero once the stop reaches break-even, and
        that collapse is the documented guard preventing a second scale-out after
        a restart.

        The trail needs the opposite property. If its denominator shrank every
        time the stop tightened, the trail distance would shrink with it and the
        stop would ratchet itself into the price, closing a healthy position on
        ordinary noise — a runaway that gets worse the better the trade is doing.
        So it measures against `initial_risk`, which is captured once at entry and
        never moves.
        """
        if pos.entry_price is None or not pos.initial_risk or pos.initial_risk <= 0:
            return None
        move = (price - pos.entry_price) if pos.side == "buy" else (pos.entry_price - price)
        return move / pos.initial_risk

    def _apply_trailing_stop(self, pos: "_Tracked", price: float) -> None:
        """Ratchet the stop to TRAILING_STOP_R behind the best price seen. Never raises.

        WHY THIS IS SYNCHRONOUS AND GOES THROUGH `tighten_stop`
        ------------------------------------------------------
        `tighten_stop` is the one-way ratchet, and routing the trail through it
        means the trail CANNOT widen a stop even if this method computes something
        wrong — the refusal path is already written, already tested, and already
        handles the "would fire on the next tick" case. A trail that wrote
        `pos.stop_loss` directly would be a second, unguarded authority over the
        single number invariant 3 exists to protect.

        It also inherits the resting-order replacement for free: `tighten_stop`
        schedules `_replace_resting_stop_soon`, so the venue's stop moves with the
        local one. A trail that only moved the in-process stop would leave the
        exchange protecting this position at the ORIGINAL level while every screen
        showed the trailed one — and the exchange's copy is the one that survives
        a crash.

        A refusal is ORDINARY here, not an error: on most ticks the trailed level
        is worse than the current stop and `tighten_stop` declines. That is the
        ratchet working, so it is logged at debug and nothing else happens.
        """
        if TRAILING_STOP_R <= 0:
            return  # trail disabled by configuration
        if pos.entry_price is None or not pos.initial_risk or pos.initial_risk <= 0:
            # No scale to measure against. Positions opened before `initial_risk`
            # existed restore without one; they keep their fixed stop and target,
            # which is exactly the protection they had before this feature.
            return

        progress = self._r_from_initial_risk(pos, price)
        if progress is None or progress < TRAILING_ACTIVATE_R:
            # Not yet proved enough to protect. Below the activation point the
            # original ATR stop is the correct protection — trailing here would
            # turn the noise this system widened its stop to survive back into
            # stop-outs, which is the failure the 1.5 -> 2.5 ATR change fixed.
            return

        distance = TRAILING_STOP_R * pos.initial_risk
        # `peak_price` is the best price SEEN, maintained by the caller on every
        # tick. Trailing from the peak rather than from the current price is what
        # makes this a ratchet at all: from the current price the stop would
        # follow a pullback back down and give up the ground it had gained.
        anchor = pos.peak_price if pos.peak_price is not None else price
        candidate = (anchor - distance) if pos.side == "buy" else (anchor + distance)

        applied, why = self.tighten_stop(pos.tar_id, candidate)
        if applied:
            pos.trail_armed = True
            logger.info(
                "Trailing stop on %s %s: +%.2fR reached, stop now %.8g "
                "(%.2fR behind peak %.8g).",
                pos.side, pos.symbol, progress, candidate, TRAILING_STOP_R, anchor,
            )
        else:
            logger.debug("Trail on %s not applied: %s", pos.symbol, why)

    async def _take_partial(self, pos: "_Tracked", price: float) -> None:
        """Bank PARTIAL_TP_FRACTION of the position at +PARTIAL_TP_R, ONCE.

        Closes part of the position down the same ungated close path a stop uses
        (reduce-only for real), records the realised P&L as a trade row so the win
        rate and P&L dashboard count it, trims the tracked quantity, and moves the
        RUNNER's stop to break-even.

        It does NOT publish POSITION_CLOSED — the position is trimmed, not closed —
        so reflection and the Telegram close alert still fire once, on the eventual
        full exit. A failure is logged and left for the next tick: banking profit is
        not safety-critical the way a stop is, so it must never raise into the loop.
        """
        if self._execution is None or not pos.qty or pos.tar_id in self._closing:
            return

        full_qty = abs(pos.qty)
        partial_qty = full_qty * PARTIAL_TP_FRACTION
        if partial_qty <= 0:
            return

        self._closing.add(pos.tar_id)
        try:
            fill_price = await self._execution.close_position(
                symbol=pos.symbol, entry_side=pos.side, qty=partial_qty,
                tab=pos.tab, reason="partial-tp",
                # Same reason as the full close — the scale-out is triggered by a
                # price this agent observed, and a simulated fill must use it.
                observed_price=price,
            )
            if fill_price is None:
                logger.warning(
                    "Partial take-profit on %s did not fill; will retry next tick.",
                    pos.symbol,
                )
                return

            sign = 1 if pos.side == "buy" else -1
            gross = (fill_price - pos.entry_price) * partial_qty * sign

            # NET, and the entry fee is apportioned BY SIZE.
            #
            # A scale-out closes part of the position, so it must bear only the
            # matching part of the entry cost — charging the whole entry fee here
            # would make the banked half look worse than it was and leave the
            # runner's eventual close carrying none of it, so the round trip would
            # still net correctly in total but be attributed wrongly between the
            # two rows. `strategy_performance` reads rows, not round trips, so
            # that misattribution would land directly in the win rate.
            share = partial_qty / full_qty if full_qty else 0.0
            entry_share = abs(pos.entry_fee or 0.0) * share
            exit_fee = modelled_fee(partial_qty * fill_price)
            # Funding on the SCALED-OUT portion only, for the settlements it was
            # open across. The runner keeps accruing its own and is charged for
            # the full window on its eventual close — the two windows overlap,
            # which is correct: both halves really were open for the first one.
            partial_funding = estimate_funding(
                side=pos.side,
                notional=partial_qty * pos.entry_price,
                opened_at=pos.opened_at,
                closed_at=datetime.datetime.utcnow(),
                rate=pos.funding_rate,
            )
            realized = gross - round_trip_fee(entry_share, exit_fee.cost) - partial_funding.cost

            # Trim to the runner, and mark done so this fires only once (belt to the
            # break-even braces below).
            pos.qty = full_qty - partial_qty
            pos.partial_done = True
            # The runner keeps only the UNBANKED remainder of the entry fee, so
            # its own close does not charge the part this row already paid.
            pos.entry_fee = abs(pos.entry_fee or 0.0) - entry_share

            await self._persist_closed_trade(
                pos, fill_price, realized, "partial-tp", qty=partial_qty,
                fee=exit_fee, funding=partial_funding.cost,
            )
            await self.persist_watch_list()

            logger.info(
                "Partial TP on %s: banked %.10g (%.0f%%) at %s, realized %+.2f — "
                "runner %.10g left, moving its stop to break-even.",
                pos.symbol, partial_qty, PARTIAL_TP_FRACTION * 100, fill_price,
                realized, pos.qty,
            )
            self.record_decision(
                "partial-take-profit",
                f"{pos.symbol} banked {PARTIAL_TP_FRACTION * 100:.0f}% at {fill_price} "
                f"(+{PARTIAL_TP_R:g}R), realized {realized:+.2f}; runner to break-even.",
                {"partialQty": partial_qty, "exitPrice": fill_price, "realizedPnl": realized},
                acted=True,
            )
        finally:
            self._closing.discard(pos.tar_id)

        # Move the runner's stop to break-even, OUTSIDE the _closing guard so the
        # tighten's own resting-order replacement is not skipped. tighten_stop
        # refuses anything not tighter, so from the original stop this always
        # applies; it also makes the entry-to-stop distance 0, which is the guard
        # that stops a second scale-out (see `_r_multiple`).
        if pos.entry_price is not None:
            applied, why = self.tighten_stop(pos.tar_id, pos.entry_price)
            if not applied:
                logger.info("Break-even move on %s not applied: %s", pos.symbol, why)

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
                # THE PRICE THIS DECISION WAS MADE AGAINST. A simulated fill
                # otherwise used the executor's own tick cache, which is a beat
                # behind whenever the bus reaches the executor after this agent —
                # an ordering dependency on the construction order in `main.py`.
                # Measured: a profit-target decided at 122.0587 filled at 121.25
                # and booked -18.80 on a winning move. A REAL close ignores this
                # and reports the venue's fill, which is why the P&L below is
                # still computed from `fill_price` and not from `trigger_price`.
                observed_price=trigger_price,
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
            gross = (fill_price - pos.entry_price) * pos.qty * sign

            # NET OF THE WHOLE ROUND TRIP, and this is the line that was missing.
            #
            # `realized` used to be the gross figure above, stored in `trades.pnl`
            # and aggregated by `strategy_performance` — so every P&L number this
            # system has ever reported excluded the cost of producing it. At this
            # system's stop distance that is ~0.093R per round trip against
            # backtested edges of 0.125-0.160R: 58-74% of the entire edge, and
            # enough to flip Swing and Trend from break-even to losing.
            #
            # BOTH legs are subtracted here because both are paid by this round
            # trip. The entry fee was paid at entry and carried on `_Tracked`; the
            # exit fee is modelled on this fill. Netting only the exit — the
            # tempting simplification, since it is the one this method computes —
            # would still report a trade as profitable that paid more in fees
            # than it made.
            exit_fee = modelled_fee(abs(pos.qty) * fill_price)
            fees = round_trip_fee(pos.entry_fee, exit_fee.cost)

            # FUNDING, for the settlements this position was actually open across.
            #
            # DISCRETE, not pro-rated: funding is charged at 00:00/08:00/16:00 UTC
            # to whoever is open at that instant, so a six-hour hold that crosses
            # none of them owes NOTHING. Pro-rating by hours would bill most of
            # this system's trades — which are typically well under 8 hours — for
            # funding they never paid. See `services/funding`.
            #
            # Signed: a short is CREDITED when the rate is positive, and that
            # income is real, so it is added back rather than discarded.
            funding = estimate_funding(
                side=pos.side,
                notional=abs(pos.qty) * pos.entry_price,
                opened_at=pos.opened_at,
                closed_at=datetime.datetime.utcnow(),
                rate=pos.funding_rate,
            )
            realized = gross - fees - funding.cost
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
            await self._persist_closed_trade(
                pos, fill_price, realized, reason, fee=exit_fee, funding=funding.cost,
            )

            logger.info(
                "Closed %s at %s (%s, triggered at %s): gross %+.2f, fees %.4f, "
                "funding %+.4f (%d settlement(s)), realized %+.2f after %.0fs. "
                "%d position(s) still watched.",
                pos.symbol, fill_price, reason, trigger_price, gross, fees,
                funding.cost, funding.settlements, realized, held, len(self._open),
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
