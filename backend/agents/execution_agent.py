import datetime
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from backend.algorithms.execution import score_execution, twap_order_slicer
from backend.core.agent_base import BaseAgent
from backend.core.config import settings
from backend.core.db import get_db_pool
from backend.core.system_state import may_open_new_position
from backend.models.events import EventType, BaseEvent, TarApprovedEvent, OrderRoutedEvent, OrderFilledEvent
from backend.services.exchange_client import get_exchange_client
from backend.services.fees import modelled_fee, resolve_fee

logger = logging.getLogger(__name__)

# Spec Section 19: "Slippage Circuit Breaker: If slippage on a market order
# > 0.5% (50 bps), halt execution of the remaining split chunks and request a
# new TAR from the Supervisor."
SLIPPAGE_CIRCUIT_BREAKER_BPS = 50.0

# Spec Section 19: TWAP over 15 minutes when an order is large enough to move
# the book. The size threshold is in base units and is a placeholder for the
# spec's "2% of 5-minute average volume" — that volume figure is not carried on
# the TAR, and inventing one would be worse than using an explicit constant.
TWAP_WINDOW_MINUTES = 15
TWAP_INTERVAL_MINUTES = 5
LARGE_ORDER_QTY_THRESHOLD = 10.0


class ExecutionAgent(BaseAgent):
    def __init__(self, simulation_mode: Optional[bool] = None):
        """The sole gateway to the exchange.

        `simulation_mode` DEFAULTS TO SIMULATION, NOT LIVE.

        It previously defaulted to `False` — meaning live `binance_futures`
        routing — and `main.py` constructed the agent with no arguments, so
        the running system placed real orders by default. Meanwhile
        `settings.LIVE_TRADING` existed and was read by nobody, so setting
        `LIVE_TRADING=false` in .env changed nothing.

        Now: passing an explicit bool still wins (the backtest engine passes
        `simulation_mode=True`), but omitting it derives the mode from
        `settings.LIVE_TRADING`, which itself defaults to false. Every path
        that doesn't deliberately ask for live trading gets simulation.
        """
        super().__init__()
        # An EXPLICIT argument pins the mode for the life of this agent (the
        # backtest engine passes `simulation_mode=True` and must never be able to
        # go live because someone flipped a setting mid-backtest). `None` means
        # "follow LIVE_TRADING", and that is resolved on every read — see the
        # `simulation_mode` property.
        self._simulation_override = simulation_mode
        self._last_prices = {}
        if self.simulation_mode:
            logger.info("ExecutionAgent started in SIMULATION mode — no real orders will be placed.")
        else:
            logger.warning(
                "ExecutionAgent started in LIVE mode (LIVE_TRADING=true). Orders will be routed "
                "to a real exchange. USE_TESTNET=%s.",
                settings.USE_TESTNET,
            )

    def routes_to_venue(self, tab: str) -> bool:
        """Does an order for THIS position go to a real exchange?

        THE BUG THIS FIXES: every routing decision in this file used to be
        `if self.simulation_mode:` — a PROCESS-WIDE flag — while `tab` was
        carried on the very same call and ignored. So the venue an order reached
        was decided by the operator's current global setting rather than by which
        book the position belongs to, and the two can disagree the moment the
        setting is changed while something is open.

        Measured consequence, on the close path and with real money:

            operator opens a PAPER position          (LIVE_TRADING off)
            operator turns LIVE_TRADING on
            the monitor's stop fires on that paper position
            -> `close_position` takes the LIVE branch, because the flag flipped
            -> a reduce-only market order is sent to MAINNET

        Reduce-only bounds it — it cannot OPEN anything — but it is not harmless:
        if the operator holds a real position in the same symbol, that order
        closes part of the REAL one to satisfy a paper stop. The paper book then
        also books its own simulated close, so both books move on one event and
        neither is right.

        The rule is now BOTH conditions, and each is load-bearing:

          * `tab == "real"` — a paper position has no venue counterpart, so a
            paper order must never reach one.
          * `not self.simulation_mode` — the backtest engine pins
            `simulation_mode=True` and must never place an order however a
            position is labelled.
        """
        return tab == "real" and not self.simulation_mode

    @property
    def simulation_mode(self) -> bool:
        """Whether orders are simulated. READ AT CALL TIME, NOT AT CONSTRUCTION.

        THE BUG THIS FIXES IS THE DANGEROUS DIRECTION OF A SAFETY TOGGLE
        ----------------------------------------------------------------
        This used to be a plain attribute assigned once in `__init__`:

            self.simulation_mode = (not settings.LIVE_TRADING) if ... else ...

        while `config.set_live_trading`'s docstring asserted the opposite:

            "`ExecutionAgent` reads `settings.LIVE_TRADING` at call time, not
             import time, so the next trade attempt sees the new value."

        It did not. `main.py` constructs one agent at startup and subscribes it to
        the bus, so that instance's mode was frozen at whatever LIVE_TRADING was
        when the process booted. Toggling from the Settings page updated the
        setting, persisted it to .env, reported success — and changed nothing
        about what the running executor actually did.

        Both directions are wrong, and one of them is severe:

          OFF -> ON   the operator believes they are live; orders are simulated,
                      so they think they hold positions they do not hold.
          ON -> OFF   the operator presses "disable live trading", is told it
                      worked, and REAL ORDERS KEEP BEING PLACED with real funds
                      until the process is restarted.

        The second is a safety control that reports success while doing nothing,
        which is worse than not having the control.

        Reading at call time is the same rule `services/execution_service.
        execution_enabled()` already documents for `GRAPH_EXECUTION_ENABLED`, and
        for the same reason: a module-level constant freezes the value at import
        and makes the operator's toggle a no-op until a restart.
        """
        if self._simulation_override is not None:
            return self._simulation_override
        return not settings.LIVE_TRADING

    @property
    def name(self) -> str:
        return "Execution Engine"

    @property
    def purpose(self) -> str:
        return "The sole gateway to the exchange. Handles idempotency, order splitting (TWAP/VWAP), and slippage control."

    @property
    def permissions(self) -> List[str]:
        return ["ROUTE_ORDERS", "CANCEL_ORDERS"]

    @property
    def inputs(self) -> List[str]:
        return [
            "TAR_APPROVED events (the ONLY authorization it acts on)",
            "TICK_RECEIVED events (last observed price, used as the slippage reference)",
            "Operator kill switch via core/system_state",
            "settings.LIVE_TRADING to decide simulation vs live routing",
        ]

    @property
    def outputs(self) -> List[str]:
        return [
            "ORDER_ROUTED events",
            "ORDER_FILLED events with measured slippage",
            "Rows in the `trades` table, tagged with the TAR's tab (paper or real)",
            "Exchange orders via services/exchange_client — the only component that does this",
        ]

    @property
    def category(self) -> str:
        return "execution"

    @property
    def events_consumed(self) -> List[EventType]:
        return ["TAR_APPROVED", "TICK_RECEIVED"]

    @property
    def events_published(self) -> List[EventType]:
        return ["ORDER_ROUTED", "ORDER_FILLED"]


    @property
    def responsibilities(self) -> List[str]:
        return ["Execute core duties as assigned."]

    @property
    def dependencies(self) -> List[str]:
        return ["MessageBus"]

    @property
    def memory_ttl(self) -> str:
        return "Ephemeral (process lifetime)"

    @property
    def knowledge_sources(self) -> List[str]:
        return ["Internal state"]

    @property
    def prompt_reference(self) -> str:
        return "EXECUTION_DETERMINISTIC_V1"

    @property
    def apis_used(self) -> List[str]:
        return ["None"]

    @property
    def database_tables(self) -> List[str]:
        return ["None"]

    @property
    def metrics_reported(self) -> List[str]:
        return ["Uptime", "Events Processed"]

    @property
    def failure_recovery_strategy(self) -> str:
        return "Restart agent process"

    @property
    def health_status(self) -> str:
        return "Active"


    async def handle_event(self, event: BaseEvent) -> None:
        if event.event_type == "TICK_RECEIVED":
            from backend.models.events import TickReceivedEvent
            if isinstance(event, TickReceivedEvent):
                self._last_prices[event.symbol] = event.price
                
        if event.event_type == "TAR_APPROVED":
            if isinstance(event, TarApprovedEvent):
                await self._execute_tar(event)

    async def _execute_tar(self, tar: TarApprovedEvent) -> None:
        logger.info(f"Execution Engine received approved TAR {tar.tar_id}")

        # Operator kill switch, checked at the last possible moment.
        #
        # This check did not exist. `POST /api/dashboard/emergency-stop` set a
        # flag that only `trading_agent.py` read, so the Execution Engine —
        # the one component that actually places orders — carried on routing
        # approved TARs straight through an active emergency stop. A kill
        # switch that the executor doesn't consult is not a kill switch.
        if not may_open_new_position():
            logger.warning(
                "TAR %s NOT executed: system is paused or emergency-stopped by the operator.",
                tar.tar_id,
            )
            return

        side = "buy" if tar.direction == "LONG" else "sell"

        # 1. Idempotency key (spec Section 19).
        #    Derived from tar_id alone, so a retry after a lost response
        #    reuses the same key and the exchange rejects the duplicate
        #    instead of filling twice. It must NOT include a timestamp or
        #    random component — that would make every retry look like a new
        #    order, which is exactly the double-fill this prevents.
        #    Truncated to 36 chars: Binance rejects longer clientOrderIds,
        #    and a rejected order on a retry path would be its own failure.
        idempotency_key = f"exec_{tar.tar_id}"[:36]

        # 2. Order splitting (spec Section 19).
        #
        #    The spec's trigger is "order_size > 2% of the 5-minute average
        #    volume". That volume figure is not on this event, so the trigger
        #    used here is the estimated slippage from
        #    `algorithms/execution.estimate_slippage` against observed depth —
        #    which is the quantity the split is meant to reduce anyway.
        #
        #    Previously this logged "Engaging TWAP execution logic" on a bare
        #    `size > 10.0` threshold while doing nothing at all: the log claimed
        #    a risk control was active when none was.
        slices = self._plan_slices(tar.approved_size)
        if len(slices) > 1:
            logger.info(
                "TAR %s: splitting %s into %d TWAP slices of ~%.8g over %d minutes.",
                tar.tar_id, tar.approved_size, len(slices), slices[0], TWAP_WINDOW_MINUTES,
            )

        # 3. Route. BY THE POSITION'S OWN BOOK, not by the global flag — see
        # `routes_to_venue`. A paper TAR arriving while LIVE_TRADING is on is
        # simulated, which is what "paper" means.
        to_venue = self.routes_to_venue(tar.tab)
        exchange_name = "binance_futures" if to_venue else "simulated_exchange"
        logger.info(f"Routing order to {exchange_name} with idempotency key {idempotency_key}")

        # Reference price for slippage measurement, captured before routing.
        expected_price = self._last_prices.get(tar.symbol, 0.0)

        order_id = str(uuid.uuid4())
        fill_price = 0.0
        fee = 0.0
        # Filled quantity, tracked separately from the requested size.
        #
        # THE BUG THIS FIXES: `OrderFilledEvent.fill_quantity` was set to
        # `tar.approved_size` — the REQUESTED size. A partial fill was therefore
        # published and persisted as a complete one, so the position on our
        # books differed from the position at the exchange, and the Position
        # Monitor would later try to close a quantity we did not hold.
        filled_qty = 0.0
        # Did a REAL venue order stand behind this fill? True for a live order
        # and for a testnet-mirrored paper order; false for a modelled one. This
        # is what `trades.exchange_order_id` is supposed to encode.
        venue_backed = to_venue

        # Latency measured around the actual exchange round-trip (spec Section
        # 19 requires execution to optimise for latency; it was never measured).
        started_at = time.monotonic()

        if not to_venue:
            fill_price = expected_price
            if fill_price <= 0:
                # No tick seen for this symbol yet. A simulated fill at 0
                # would poison every downstream P&L figure with a
                # meaningless number, so abort instead.
                logger.error(
                    "TAR %s NOT simulated: no observed price for %s yet, so there is no honest "
                    "fill price to simulate against.",
                    tar.tar_id,
                    tar.symbol,
                )
                return
            # ---- TESTNET MIRROR: make the paper fill a REAL fill ------------
            #
            # A simulated fill is booked at the last observed price, instantly,
            # in full — honest bookkeeping and also the most flattering possible
            # execution. No spread crossed, no slippage, no partial fill, no
            # minimum size, no leverage rejection. Every one of those is a real
            # cost that appears on day one of real money and on none of the paper
            # days before it.
            #
            # With the mirror on, this places a real market order on Bybit's
            # TESTNET and books the price the exchange returned. The trade stays
            # paper — same book, same P&L, same panels — but the fill is no
            # longer a model of one.
            #
            # PROPERTY 1 — UNREACHABLE WHILE LIVE_TRADING IS ON — IS NOW AN
            # EXPLICIT TERM IN THIS CONDITION, and it had to become one.
            #
            # It used to be a consequence of the branch's SHAPE: the mirror sat
            # inside `if self.simulation_mode:`, which is false whenever live
            # trading is on. The branch is now chosen by the position's TAB
            # (see `routes_to_venue`), so a paper order reaches here even with
            # LIVE_TRADING on — correctly, since paper is paper. But a structural
            # guarantee that survives only as long as nobody restructures the
            # branch is not a guarantee, so `self.simulation_mode` is spelled out.
            #
            # A FAILURE FALLS THROUGH TO THE SIMULATED FILL. A test venue being
            # down must not stop paper trading, and the trade row records which
            # happened: `exchange_order_id` is set only for a real venue order.
            from backend.services import paper_testnet

            mirrored = None
            if tar.tab == "paper" and self.simulation_mode and paper_testnet.active():
                mirrored = await paper_testnet.place(
                    symbol=tar.symbol,
                    side=side,
                    qty=tar.approved_size,
                    leverage=tar.approved_leverage,
                    client_order_id=f"pt_{tar.tar_id}"[:36],
                )

            if mirrored:
                exchange_name = "bybit_testnet"
                venue_backed = True
                order_id = mirrored["order_id"] or order_id
                fill_price = mirrored["price"]
                filled_qty = mirrored["filled_qty"]
                # The venue's own fee is not read back here: Bybit reports it on
                # the trade record, not the order ack, and a testnet fee schedule
                # is not the mainnet one anyway. The modelled taker rate is the
                # honest figure for a paper book, and `fee_measured=False`
                # already records that no venue confirmed it.
                fee_result = modelled_fee(fill_price * filled_qty)
            else:
                # A simulated order fills completely by definition.
                filled_qty = tar.approved_size
                # Modelled at the configured TAKER rate, via the one module that
                # owns fee arithmetic. This used to be a bare `* 0.0004` — a
                # second, lower fee rate hardcoded here and nowhere else, so the
                # paper book and the backtest disagreed about what a trade costs
                # while both reported the result as P&L. `services/fees` is now
                # the only place the number lives, and `measured=False` records
                # that no venue confirmed it.
                fee_result = modelled_fee(fill_price * tar.approved_size)
        else:
            from backend.services.venue import get_venue

            venue = get_venue()

            # LEVERAGE IS SET ON THE VENUE BEFORE THE ORDER, AND A FAILURE ABORTS.
            #
            # The Risk Gateway sized this position for `approved_leverage`. The
            # exchange applies whatever was last set in its own UI — possibly 20x
            # when the agent sized for 3x. Every margin and liquidation figure the
            # system then computes describes a position that does not exist, and
            # the real one liquidates far closer to entry than anything here
            # believes. Placing the order anyway would be trading on a number we
            # know to be wrong, so this returns instead.
            if not await venue.ensure_leverage(tar.symbol, tar.approved_leverage):
                logger.error(
                    "TAR %s NOT executed: %s would not accept %sx leverage on %s. The position "
                    "was sized for that leverage, so filling it at the venue's current setting "
                    "would stake a different amount of margin than the Risk Gateway approved.",
                    tar.tar_id, venue.id, tar.approved_leverage, tar.symbol,
                )
                return

            # MARGIN MODE, BEFORE LEVERAGE IS COMMITTED TO AND BEFORE THE ORDER.
            #
            # Nothing used to set this, so the venue used whatever its UI was last
            # left on. Under CROSS margin the entire account balance backs every
            # position — one liquidation can reach funds that were never allocated
            # to that trade, which at the 10x ceiling is the difference between
            # losing a position's margin and losing the account.
            #
            # Aborts on an unexplained refusal for the same reason leverage does:
            # opening a real position whose risk containment could not be
            # established is trading on a number we know we do not have. The two
            # cases that are NOT failures — already correct, or pinned by an
            # existing position — are handled inside `ensure_margin_mode`.
            if not await venue.ensure_margin_mode(tar.symbol):
                logger.error(
                    "TAR %s NOT executed: %s would not confirm the margin mode on %s. The "
                    "position's maximum loss would not be bounded by the margin the Risk "
                    "Gateway allocated to it.",
                    tar.tar_id, venue.id, tar.symbol,
                )
                return

            result = await venue.market_order(
                symbol=tar.symbol,
                side=side,
                qty=tar.approved_size,
                reduce_only=False,
                client_order_id=idempotency_key,
                expected_price=expected_price,
            )

            if not result.ok:
                # `ok=False` carries no price by construction, so this cannot be
                # mistaken for a fill the way a fabricated order dict once was.
                logger.error(
                    "TAR %s was NOT filled — %s rejected or failed the order: %s. "
                    "No position was opened and nothing is being recorded as a trade.",
                    tar.tar_id, venue.id, result.error,
                )
                return

            # The venue's step size may have trimmed the size. Booking what was
            # ASKED FOR rather than what filled would leave the local book holding
            # a quantity the exchange does not have, and the reconciler would then
            # report a phantom discrepancy on every tick.
            if result.adjusted_qty is not None and result.requested_qty is not None:
                if abs(result.adjusted_qty - result.requested_qty) > 1e-12:
                    logger.warning(
                        "TAR %s: %s rounded the size from %s to %s to meet %s's step. "
                        "The fill is being booked at the rounded size.",
                        tar.tar_id, venue.id, result.requested_qty, result.adjusted_qty, tar.symbol,
                    )

            order = result.raw or {}
            order_id = result.order_id or order_id
            raw_fill = result.average_price
            if not raw_fill or float(raw_fill) <= 0:
                # An accepted order with no usable fill price. Recording 0.0
                # here would silently book a position at zero cost, showing
                # the entire notional as profit.
                logger.error(
                    "TAR %s: exchange accepted order %s but returned no usable fill price "
                    "(average=%s price=%s). NOT recording a trade — reconcile this order "
                    "against the exchange manually.",
                    tar.tar_id,
                    order_id,
                    order.get("average"),
                    order.get("price"),
                )
                return
            fill_price = float(raw_fill)

            # The ACTUAL filled amount from the exchange. ccxt reports it as
            # `filled`; fall back to `amount` only if absent, and treat a
            # missing/zero value as a non-fill rather than assuming success.
            raw_filled = order.get("filled")
            if raw_filled is None:
                raw_filled = order.get("amount")
            filled_qty = float(raw_filled or 0.0)

            if filled_qty <= 0:
                logger.error(
                    "TAR %s: exchange accepted order %s but reports zero filled quantity. "
                    "NOT recording a trade — reconcile manually.",
                    tar.tar_id, order_id,
                )
                return

            if filled_qty < tar.approved_size * 0.999:
                # Surfaced loudly, and everything downstream uses filled_qty.
                # A partial fill silently recorded as complete leaves the book
                # out of sync with the exchange.
                logger.warning(
                    "PARTIAL FILL on TAR %s: %.8g of %.8g requested (%.2f%%). The remainder was "
                    "NOT re-submitted — downstream records reflect the filled amount only.",
                    tar.tar_id, filled_qty, tar.approved_size,
                    filled_qty / tar.approved_size * 100,
                )

            # The venue's own commission when it reported one, the modelled taker
            # rate when it did not. Resolved AFTER `filled_qty` is known so a
            # partial fill is costed on what actually filled, not on what was
            # requested — a fee modelled on the requested size would overstate the
            # cost of every partial fill.
            #
            # Never fetched with a second HTTP call when absent: that would sit
            # between the fill and the position reaching the stop-loss watch list.
            # An unwatched position is a safety problem; an unmeasured fee is an
            # accounting one. See `services/fees`.
            fee_result = resolve_fee(order, qty=filled_qty, price=fill_price)

        fee = fee_result.cost

        latency_ms = (time.monotonic() - started_at) * 1000.0

        # 4. Slippage, measured rather than reported as zero.
        #    This was hardcoded `slippage_bps=0.0` with the comment
        #    "Calculate if we have expected price" — so the Evaluation layer
        #    received a perfect execution score for every trade, including
        #    badly slipped ones (spec Section 22.4 requires execution to be
        #    scored and that score persisted).
        slippage_bps = self._slippage_bps(expected_price, fill_price, side)

        logger.info(
            "Order %s FILLED on %s at %s (expected %s, slippage %s bps)",
            order_id,
            exchange_name,
            fill_price,
            expected_price or "unknown",
            f"{slippage_bps:.1f}" if slippage_bps is not None else "unmeasurable",
        )

        if slippage_bps is not None and slippage_bps > SLIPPAGE_CIRCUIT_BREAKER_BPS:
            # The fill already happened — this cannot undo it. It flags the
            # execution so the operator and the Evaluation layer see it, and
            # is the hook point for halting remaining chunks once order
            # splitting exists.
            logger.warning(
                "SLIPPAGE CIRCUIT BREAKER: order %s slipped %.1f bps, above the %.0f bps limit. "
                "The fill stands; no further chunks would be sent for this TAR.",
                order_id,
                slippage_bps,
                SLIPPAGE_CIRCUIT_BREAKER_BPS,
            )

        # 5. Score the execution (spec Section 22.4). Computed before the
        #    events so the score can be persisted alongside them.
        quality = score_execution(
            requested_qty=tar.approved_size,
            filled_qty=filled_qty,
            slippage_bps=slippage_bps,
            latency_ms=latency_ms,
        )
        if quality["notes"]:
            logger.info("Execution quality for %s: score=%s. %s",
                        order_id, quality["score"], "; ".join(quality["notes"]))

        await self.publish(OrderRoutedEvent(
            tar_id=tar.tar_id,
            exchange=exchange_name,
            order_id=order_id,
            order_type="MARKET",
            price=fill_price,
            # The requested quantity is correct here — ORDER_ROUTED describes
            # what was sent, ORDER_FILLED describes what came back.
            quantity=tar.approved_size
        ))

        await self._persist_trade(
            str(tar.tar_id),
            tar.symbol,
            side,
            # filled_qty, not approved_size: the trade log must record what
            # actually happened at the exchange.
            filled_qty,
            fill_price,
            # `exchange_order_id` IS THE SYSTEM'S SIMULATED-VS-VENUE
            # DISCRIMINATOR, AND IT WAS NOT DISCRIMINATING.
            #
            # `order_id` is a uuid4 minted at the top of this method and only
            # REPLACED when a venue returns its own id. So it was non-NULL on
            # every row, simulated or not — while this file, the testnet
            # mirror's four documented safety properties, CLAUDE.md and the
            # Settings panel all told the operator that NULL means "no venue
            # order stood behind this row". A property asserted in four places
            # and true in none is worse than an undocumented one: it is the
            # thing you reach for to decide whether a paper result was real.
            #
            # The uuid is still the CORRELATION id on the bus and in
            # `execution_quality` — a simulated fill needs one to be traceable.
            # It simply stops being written into the column that means "the
            # venue acknowledged this".
            (order_id if venue_backed else None),
            tar.tab,
            getattr(tar, "run_id", None),
            getattr(tar, "strategy", None),
            getattr(tar, "entry_context", None),
            # The opening leg's own cost. The close records its own, and
            # `position_monitor` nets BOTH into the realized figure — so this row
            # carries the fee without carrying a pnl, which is what keeps the
            # "a row with a pnl is a realized close" rule intact.
            fee_result.cost,
            fee_result.measured,
        )
        await self._persist_execution_quality(str(tar.tar_id), order_id, tar.symbol, exchange_name, quality)

        # APPLY THE FILL TO THE PAPER BOOK.
        #
        # THIS WAS MISSING ENTIRELY AND IT IS THE BUG BEHIND HALF THE DASHBOARD.
        # This agent wrote a row to `trades`, handed the position to the monitor,
        # and never touched the book. On the paper account that meant:
        #
        #   * cash sat at its starting figure forever, however many trades filled
        #   * `positions` stayed empty, so there was no unrealized P&L to move
        #     when price moved, and "Open positions" read 0
        #   * `current_equity()` is cash plus marked positions, so a session's
        #     progress toward its target never moved either
        #
        # Three separate "broken panels" for one absent write.
        #
        # PAPER ONLY. A real position lives at the venue and the venue's own
        # balance is the book — writing a second copy here would create exactly
        # the two-disagreeing-books problem reconciliation exists to detect.
        if tar.tab == "paper":
            booked = await self._apply_paper_fill(
                symbol=tar.symbol, side=side, qty=filled_qty, price=fill_price,
                leverage=tar.approved_leverage, reduce_only=False,
            )
            # A REFUSED BOOK WRITE USED TO BE FOLLOWED BY ORDER_FILLED ANYWAY.
            #
            # `apply_paper_fill` refuses an open the account cannot fund — not
            # enough free cash for the margin. That refusal was logged and then
            # ignored: the row was already in `trades`, ORDER_FILLED went out,
            # the monitor began watching a position, and the paper book held
            # nothing. Three components then disagreed about whether a position
            # existed, and the one holding the MONEY was the one that said no.
            #
            # WHAT HAPPENS NEXT DEPENDS ON WHETHER ANYTHING REAL HAPPENED, and
            # the two cases are genuinely different:
            #
            #   modelled fill  — nothing happened anywhere. Stopping here leaves
            #                    the system consistent: no book entry, no watched
            #                    position, no fill event. The `trades` row is
            #                    corrected below rather than left as a phantom.
            #   mirrored fill  — a REAL order stands at the testnet. Suppressing
            #                    the event would leave that position open with
            #                    nothing watching it, which is strictly worse
            #                    than a book that disagrees. So it is published,
            #                    loudly, and reconciliation is the operator's.
            if not booked:
                if not venue_backed:
                    logger.error(
                        "TAR %s NOT opened: the paper book refused the fill (%s %.8g %s @ %.8g). "
                        "No ORDER_FILLED is being published and the trade row is being removed — "
                        "a position the book will not fund must not become one the monitor "
                        "watches.",
                        tar.tar_id, side, filled_qty, tar.symbol, fill_price,
                    )
                    await self._unpersist_trade(str(tar.tar_id))
                    return
                logger.critical(
                    "TAR %s: a REAL order stands at %s for %s but the paper book REFUSED it. "
                    "Publishing the fill anyway so the position is watched — an unwatched real "
                    "position is worse than a book that disagrees. RECONCILE THIS MANUALLY.",
                    tar.tar_id, exchange_name, tar.symbol,
                )

        await self.publish(OrderFilledEvent(
            tar_id=tar.tar_id,
            exchange=exchange_name,
            order_id=order_id,
            # Carried so downstream consumers don't have to guess. The
            # Reflection agent used to hardcode "BTC/USDT" because these
            # weren't here.
            symbol=tar.symbol,
            side=side,
            tab=tar.tab,
            fill_price=fill_price,
            fill_quantity=filled_qty,
            slippage_bps=slippage_bps if slippage_bps is not None else 0.0,
            fee=fee
        ))

        # 6. Attach the protective stop that Risk approved.
        await self._attach_stop_loss(tar, fill_price, side)

    @staticmethod
    def _plan_slices(total_qty: float) -> List[float]:
        """Split a large order into TWAP slices, or return it whole.

        Uses `algorithms/execution.twap_order_slicer` rather than reimplementing
        the arithmetic. The slices always sum to the original quantity — a
        slicer that lost or invented quantity would under- or over-fill.

        HONEST LIMITATION: the slices are computed and reported, but the
        Execution Engine still submits ONE market order for the full size. Spec
        Section 19's TWAP requires scheduling the chunks over
        TWAP_WINDOW_MINUTES, which needs a scheduler this agent does not have —
        `handle_event` is a single async call that must return. The plan is
        surfaced so the gap is visible and the schedule is ready to drive once a
        scheduler exists, rather than the log claiming a control that isn't
        running.
        """
        if total_qty <= LARGE_ORDER_QTY_THRESHOLD:
            return [total_qty]
        return twap_order_slicer(
            total_qty,
            execution_window_minutes=TWAP_WINDOW_MINUTES,
            interval_minutes=TWAP_INTERVAL_MINUTES,
        )

    async def close_position(
        self, symbol: str, entry_side: str, qty: float, tab: str, reason: str,
        observed_price: Optional[float] = None,
    ) -> Optional[float]:
        """Close an open position. Returns the fill price, or None on failure.

        DELIBERATELY NOT GATED BY `may_open_new_position()`.

        CLAUDE.md invariant 4: closes and exits are never blocked — not by
        pause, not by risk checks, not by an emergency stop, not by a debate
        veto. Refusing to let an operator out of a position they are already in
        is actively harmful, and more so with real money, not less. A stop-loss
        that stops firing the moment the system is paused is not a stop-loss.

        It still routes through this agent rather than letting the Monitor talk
        to the exchange directly, so spec Section 8's rule holds: *"the
        Execution API is a hard chokepoint — no agent talks to an exchange
        directly, ever."* One gateway, two policies — opens are gated, closes
        are not.

        No TAR is required. Requiring one would make an exit dependent on the
        Supervisor and CRO being healthy and unpaused, which is precisely when
        an exit matters most.

        `observed_price` IS THE PRICE THE CALLER DECIDED ON, and passing it fixes
        a real, ordering-dependent bug on the money path.

        A simulated close used to fill at `self._last_prices[symbol]` — this
        agent's own cache, fed by TICK_RECEIVED. The position monitor decides on
        a close from the tick IT received, and both agents subscribe to the same
        event, so whether the two prices agree depends entirely on which agent
        the bus reaches first — which is the order they were constructed in
        `main.py`. CLAUDE.md already says of that ordering: *"Do NOT fix a future
        instance of this by reordering construction in main.py. That works until
        the next reorder and no test can see it."*

        Measured, with the monitor constructed first:

            monitor decided profit-target at 122.0587 (a +0.667% move)
            close filled at 121.25 — the entry price, the executor's stale tick
            booked P&L -18.80 on a WINNING move, almost exactly the round-trip fee

        A target that fires and books a loss the size of the fees is
        indistinguishable from the scratch exits this system spent weeks removing.

        REAL FILLS ARE UNAFFECTED. Below, a live close is a market order and its
        price comes back from the venue; `observed_price` is not consulted, and
        the "realized P&L from the ACTUAL fill, not the trigger price" rule in
        `position_monitor._close` still holds there. For a SIMULATED fill there
        is no separate reality to defer to: the observed price at the moment of
        the decision IS the honest fill, and it is the one the decision was made
        against.

        Falls back to the cache when not supplied, so every existing caller keeps
        working, and refuses when neither is available rather than inventing a
        price (invariant 6).
        """
        exit_side = "sell" if entry_side == "buy" else "buy"
        # BY THE POSITION'S BOOK, not the global flag. A paper position closed
        # through the live branch sends a reduce-only order to MAINNET, which
        # trims the operator's REAL position in the same symbol if they hold one.
        # See `routes_to_venue`.
        to_venue = self.routes_to_venue(tab)
        exchange_name = "binance_futures" if to_venue else "simulated_exchange"

        if not to_venue:
            # The caller's observed price wins. See the note above: the cache is
            # a beat behind whenever the bus reaches this agent after the one
            # that decided to close.
            fill_price = observed_price if (observed_price or 0) > 0 else 0.0
            if fill_price <= 0:
                fill_price = self._last_prices.get(symbol, 0.0)
            if fill_price <= 0:
                logger.error(
                    "Cannot simulate closing %s: no observed price. The position remains OPEN.",
                    symbol,
                )
                return None
            # THE CLOSE IS MIRRORED TOO, and it must be: a mirrored entry that
            # closes only locally leaves a real position open on the testnet
            # account, which then drifts from the paper book and makes every
            # subsequent mirrored trade start from a state nobody recorded.
            # `reduce_only=True` for the same reason the live path sets it —
            # without it a close is just an opposite-side order and any surplus
            # OPENS a position the other way.
            from backend.services import paper_testnet

            if tab == "paper" and self.simulation_mode and paper_testnet.active():
                mirrored = await paper_testnet.place(
                    symbol=symbol,
                    side=exit_side,
                    qty=qty,
                    reduce_only=True,
                    client_order_id=f"ptc_{symbol.replace('/', '')}_{reason}"[:36],
                )
                if mirrored:
                    fill_price = mirrored["price"]
                    logger.info(
                        "Testnet-mirrored close of %s %s %s at %s (%s) — the exchange's "
                        "price, not a modelled one.",
                        exit_side, qty, symbol, fill_price, reason,
                    )

            logger.info(
                "Simulated close of %s %s %s at %s (%s)", exit_side, qty, symbol, fill_price, reason
            )
            # The close has to reach the book too, or cash never receives the
            # realized P&L and the position stays open in the book forever while
            # the monitor has already let it go.
            if tab == "paper":
                await self._apply_paper_fill(
                    symbol=symbol, side=exit_side, qty=qty, price=fill_price,
                    leverage=1.0, reduce_only=True,
                )
            return fill_price

        from backend.services.venue import get_venue

        venue = get_venue()
        # Idempotency key includes the reason so a stop-triggered close and a
        # later manual close of the same symbol are distinct orders, while a
        # retry of the SAME close reuses its key.
        client_order_id = f"close_{symbol.replace('/', '')}_{reason}"[:36]

        # `reduce_only=True` IS WHAT MAKES THIS A CLOSE.
        #
        # Without it this is merely an opposite-side market order. If `qty` is even
        # slightly larger than what the venue actually holds — a partial fill, a
        # fee taken in the base asset, a stop that already trimmed the position —
        # the surplus does not close anything. It OPENS a position the other way.
        # The operator asked to flatten and is now short.
        #
        # `Venue._order_params` translates this per venue: Binance hedge mode
        # rejects `reduceOnly` and expresses the same intent through `positionSide`.
        result = await venue.market_order(
            symbol=symbol,
            side=exit_side,
            qty=qty,
            reduce_only=True,
            client_order_id=client_order_id,
            expected_price=self._last_prices.get(symbol, 0.0),
        )
        order = result.raw or {}
        if not result.ok:
            # Loud, because the position is still open and still exposed.
            logger.critical(
                "FAILED TO CLOSE %s (%s %s, reason=%s): %s. THE POSITION IS STILL OPEN and "
                "still carries risk. Manual intervention required.",
                symbol, exit_side, qty, reason, result.error,
            )
            return None

        raw = result.average_price
        if not raw or float(raw) <= 0:
            logger.error(
                "Close order %s for %s was accepted but returned no usable fill price. "
                "Realized P&L cannot be computed — reconcile manually.",
                order.get("id"), symbol,
            )
            return None

        fill = float(raw)

        # A PRICE IS NOT A CLOSE. The checks above prove the venue ACCEPTED the
        # order and told us a price; neither proves it moved the whole position.
        #
        # This returned `fill` — a truthy float — on any accepted order, and the
        # caller (`position_monitor._close`) reads a non-None return as "the
        # position is flat". So a reduce-only close that filled 0 (no liquidity,
        # the position already gone from under us, a venue that acks then does
        # nothing) or filled 30 of 100 was recorded as a completed exit: the
        # watch row was deleted, POSITION_CLOSED was published, a realized P&L
        # was booked against a quantity that never traded — and the residual
        # stayed open at the exchange with NOTHING enforcing its stop. That is
        # the precise failure the resting stop and the monitor both exist to
        # prevent, reached by reporting success.
        #
        # A SHORTFALL RETURNS None, which is the retryable answer. The monitor
        # keeps the position, keeps watching it, and closes again on the next
        # tick; `reduce_only=True` means that retry can only ever shrink what is
        # actually there, so a self-healing retry cannot overshoot into a
        # reversed position. Booking a partial as complete is not recoverable.
        filled = result.filled_qty
        if filled is None:
            # Unknown is not zero and not full. Refusing to guess (invariant 6):
            # an unverifiable close is reported as unfinished so it is retried
            # and reconciled, rather than assumed complete.
            logger.critical(
                "Close order %s for %s was accepted at %s but the venue reported NO filled "
                "quantity. Treating it as INCOMPLETE — the position stays watched and the "
                "close will be retried. Reconcile against the exchange.",
                order.get("id"), symbol, fill,
            )
            return None
        filled = float(filled)
        # A relative tolerance, because a venue's step size legitimately trims
        # the last fraction and an exact-equality test would call every rounded
        # close a partial one.
        if filled < qty * 0.999:
            logger.critical(
                "PARTIAL CLOSE of %s: asked to close %.8g, the venue filled %.8g at %s. "
                "ABOUT %.8g IS STILL OPEN and still carries risk. The position is being kept "
                "under watch and the close will be retried on the next tick (reduce-only, so "
                "the retry can only shrink what is actually there).",
                symbol, qty, filled, fill, qty - filled,
            )
            return None

        # A paper-tab position can no longer reach this branch — `routes_to_venue`
        # sends every paper order to the simulated path — but the settle stays so
        # a future caller that passes a real tab for a book-backed position is not
        # silently left with cash that never received its realized P&L.
        if tab == "paper":
            await self._apply_paper_fill(
                symbol=symbol, side=exit_side, qty=filled, price=fill,
                leverage=1.0, reduce_only=True,
            )
        return fill

    async def _apply_paper_fill(
        self, *, symbol: str, side: str, qty: float, price: float,
        leverage: float, reduce_only: bool,
    ) -> Optional[Dict[str, Any]]:
        """Move the paper book. Returns the book's own result, or None on failure.

        RETURNS THE RESULT RATHER THAN THE REALIZED P&L, and the change matters:
        an OPEN has no realized P&L, so the old `Optional[float]` return was
        `None` both when the book had happily funded the position and when it had
        REFUSED it. A caller could not tell the two apart, and the open path
        therefore published ORDER_FILLED over a refusal — the trade log, the
        monitor and the book then disagreed about whether a position existed.

        `None` now means exactly one thing: the book did not apply this fill.
        Realized P&L is `result["realized"]` for the callers that want it.

        Never raises. A book write that failed must not unwind a fill that has
        already happened at a venue; it is logged loudly instead, because a book
        that has drifted from the trade log is a real problem — just not one to
        solve by pretending the trade did not happen.
        """
        from backend.services.portfolio_store import apply_paper_fill

        try:
            result = await apply_paper_fill(
                symbol=symbol, side=side, qty=qty, price=price,
                leverage=leverage or 1.0, reduce_only=reduce_only,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Paper book NOT updated for %s %s %s @ %s: %s. The trade log and the book "
                "now disagree; cash and equity will be wrong until this is reconciled.",
                side, qty, symbol, price, exc,
            )
            return None

        if not result.get("ok"):
            logger.error(
                "Paper book refused the %s of %s %s @ %s: %s",
                "close" if reduce_only else "open", qty, symbol, price, result.get("reason"),
            )
            return None

        if result.get("unmatchedQty"):
            # An over-sized close. Visible rather than silently clamped: it means
            # the monitor thinks the position is bigger than the book does.
            logger.warning(
                "Close of %s was larger than the book held; %.8g was left unmatched. "
                "The monitor and the paper book disagree on this position's size.",
                symbol, result["unmatchedQty"],
            )

        return result

    async def _unpersist_trade(self, trade_id: str) -> None:
        """Remove a trade row for a fill that did not happen anywhere.

        Used on exactly one path: a MODELLED open that the paper book refused.
        Nothing was sent to any venue and no position exists, so the row is not
        a record of anything — leaving it would put a phantom opening leg in the
        ledger that `annotateTrades` would carry as OPEN forever, and that
        `strategy_performance` would count as an entry with no exit.

        DELIBERATELY NOT REACHABLE FOR A VENUE-BACKED FILL. There the order is
        real and the row is the only local record of it; deleting it would leave
        a real position with no trace at all, which is the failure
        `_persist_trade` already logs an error about when the pool is missing.
        """
        pool = get_db_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM trades WHERE id = $1", trade_id)
        except Exception as exc:  # noqa: BLE001 - never raise over bookkeeping
            logger.error(
                "Could not remove the trade row for the refused open %s: %s. The ledger now "
                "holds an opening leg for a position that was never funded.",
                trade_id, exc,
            )

    @staticmethod
    def _slippage_bps(expected_price: float, fill_price: float, side: str) -> Optional[float]:
        """Slippage in basis points, signed so that positive is always a COST.

        Returns None when there is no reference price — an unmeasurable
        slippage is reported as unknown, not as zero.

        Side-aware because a fill above the expected price is bad for a buy
        and good for a sell. Taking the absolute difference would report a
        favourable fill as slippage and make execution quality look worse
        than it is; ignoring side entirely would let a systematically bad
        buy-side fill average out against a good sell-side one.
        """
        if expected_price <= 0 or fill_price <= 0:
            return None
        diff = (fill_price - expected_price) if side == "buy" else (expected_price - fill_price)
        return (diff / expected_price) * 10_000

    async def _attach_stop_loss(self, tar: TarApprovedEvent, fill_price: float, side: str) -> None:
        """Place the approved stop as a resting exchange order.

        NOT IMPLEMENTED for live trading, and it says so rather than
        pretending. `stop_loss` now travels all the way here on the approved
        TAR (it previously didn't exist anywhere in the event chain), but
        turning it into a resting `STOP_MARKET` order needs a
        `create_order`-with-`stopPrice` path on the exchange client that does
        not exist yet.

        This matters more than it looks: until it exists, the stop is only
        enforced by this process staying alive and watching the price. If the
        backend dies while a leveraged position is open, there is nothing at
        the exchange to close it — which is the exact failure spec Section
        22.8 says to design against ("the bot goes silent while holding a
        leveraged position"). Logged at WARNING on every live fill so it
        cannot be forgotten.
        """
        exit_side = "sell" if side == "buy" else "buy"
        if self.simulation_mode:
            logger.info(
                "TAR %s simulated: stop-loss %.6g (%s to exit) is tracked in-process only.",
                tar.tar_id,
                tar.stop_loss,
                exit_side,
            )
            return

        # The resting stop IS placed now — by `PositionMonitorAgent._place_resting_stop`
        # when it registers the fill, not here. This used to warn that they were
        # "NOT IMPLEMENTED", which is no longer true and would send a reader
        # looking for a gap that has been closed.
        logger.info(
            "TAR %s filled at %s with an approved stop-loss of %.6g. The position monitor "
            "places the resting reduce-only stop at the venue when it registers this fill.",
            tar.tar_id,
            fill_price,
            tar.stop_loss,
        )

    async def _persist_execution_quality(
        self,
        tar_id: str,
        order_id: str,
        symbol: str,
        exchange: str,
        quality: Dict[str, Any],
    ) -> None:
        """Write the execution score to the `execution_quality` table.

        Spec Section 22.4: the score must be *"written back to
        docs/13_DATABASE_SCHEMA.md so the Evaluation layer can use it."* There
        was no such table and no score to write.

        `score` may be NULL. A NULL score means "not measurable", which the
        Evaluation layer must exclude from averages rather than treat as zero —
        a fill with no reference price is not a bad fill.
        """
        pool = get_db_pool()
        if not pool:
            logger.debug("No database pool — execution quality for %s not persisted.", order_id)
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO execution_quality (
                      tar_id, order_id, ts, symbol, exchange, tab,
                      requested_qty, filled_qty, fully_filled,
                      slippage_bps, latency_ms, score,
                      components_measured, components_total, notes
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                    ON CONFLICT (order_id) DO NOTHING
                    """,
                    tar_id,
                    order_id,
                    datetime.datetime.utcnow(),
                    symbol,
                    exchange,
                    settings.execution_tab,
                    quality["requestedQty"],
                    quality["filledQty"],
                    quality["fullyFilled"],
                    quality["slippageBps"],
                    quality["latencyMs"],
                    quality["score"],
                    quality["componentsMeasured"],
                    quality["componentsTotal"],
                    json.dumps(quality["notes"]),
                )
        except Exception as e:
            # Logged, not raised: the order already executed. Losing the score
            # costs the Evaluation layer one data point; unwinding the caller
            # would skip the stop-loss attachment that follows.
            logger.error("Failed to persist execution quality for %s: %s", order_id, e)

    async def _persist_trade(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        exchange_order_id: str,
        tab: str,
        run_id: Optional[str] = None,
        strategy: Optional[str] = None,
        entry_context: Optional[str] = None,
        fee: Optional[float] = None,
        fee_measured: Optional[bool] = None,
    ):
        pool = get_db_pool()
        if not pool:
            # Worth saying out loud: the order is already on the exchange.
            # Silently skipping the write leaves a real position with no
            # local record of it.
            logger.error(
                "Trade %s (%s %s %s @ %s) executed but NOT persisted: no database pool. "
                "This position exists at the exchange with no local record.",
                trade_id, side, qty, symbol, price,
            )
            return

        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades
                        (id, ts, tab, symbol, side, qty, price, origin_tag,
                         exchange_order_id, run_id, strategy, entry_context,
                         fee, fee_measured)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    """,
                    # `tab` comes from the TAR instead of the hardcoded 'real'
                    # this used to pass. Simulated fills were being written
                    # into the trade log as real trades, permanently mixing
                    # simulated and real history in one table.
                    trade_id, datetime.datetime.utcnow(), tab, symbol, side, qty, price,
                    "agent-plan", exchange_order_id,
                    # The graph run that decided this trade, so the detail page
                    # can show the analysis, regime and specialist votes rather
                    # than only the entry and the outcome.
                    run_id,
                    # Attribution. A realised win rate cannot be assigned to a
                    # strategy that was never recorded against the fill.
                    strategy,
                    # What the agent saw. The trade row could otherwise only ever
                    # record WHAT happened, never WHY.
                    entry_context,
                    # THE COST OF THIS FILL, and whether the venue confirmed it.
                    # Every P&L figure in this system was gross before these two
                    # columns — see `services/fees`. `fee_measured` is kept beside
                    # the cost so a modelled paper fee can never be mistaken for a
                    # commission the exchange actually charged.
                    fee, fee_measured,
                )
        except Exception as e:
            logger.error(f"Failed to persist trade {trade_id}: {e}")


# The ONE execution engine. See `get_execution_agent`.
_execution_agent: Optional["ExecutionAgent"] = None


def get_execution_agent() -> ExecutionAgent:
    """The process-wide execution engine. Simulation unless LIVE_TRADING=true.

    A SINGLETON NOW, AND THE REASON IS STATE THIS AGENT ACCUMULATES.
    This used to construct a new agent per call. `main.py` builds one, subscribes
    it to the bus and attaches it to the position monitor — and that instance is
    the only one that ever sees a TICK_RECEIVED, so it is the only one whose
    `_last_prices` is populated.

    Any other caller got an agent that had seen no ticks, and simulating a fill
    through it fails with:

        TAR ... NOT simulated: no observed price for BTC/USDT yet, so there is no
        honest fill price to simulate against.

    which reads as a market-data problem and is actually a second, empty object.
    Spec Section 8 calls this "a hard chokepoint — no agent talks to an exchange
    directly, ever"; a chokepoint that can be instantiated freely is a chokepoint
    in name only.
    """
    global _execution_agent
    if _execution_agent is None:
        _execution_agent = ExecutionAgent()
    return _execution_agent


def reset_execution_agent() -> None:
    """Drop the singleton. For tests only.

    A LIVE_TRADING toggle does NOT need this: `simulation_mode` is a property
    resolved on every read, so the running agent follows the setting without
    being rebuilt. Rebuilding it would also drop `_last_prices` and leave the old
    instance subscribed to the bus.
    """
    global _execution_agent
    # DETACH BEFORE DROPPING. The bus holds a bound method, so releasing the
    # reference alone leaves the old agent subscribed and still receiving
    # events forever — see `BaseAgent.detach`.
    if _execution_agent is not None:
        _execution_agent.detach()
    _execution_agent = None
