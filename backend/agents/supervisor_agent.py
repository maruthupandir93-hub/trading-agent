"""Supervisor AI — the arbitration layer (spec Section 22.7).

The Supervisor is the only agent that may submit a Trade Authorization
Request (TAR). It never executes: it hands an approved decision to the CRO,
which may veto, and only then does Execution see it.

WHAT WAS WRONG BEFORE
---------------------
`handle_event` built every TAR like this:

    tar = TarSubmittedEvent(
        symbol=event.symbol,
        direction="LONG",          # Hardcoded for this simple event chain demo
        requested_size=0.1,
        requested_leverage=1,
        ...
    )

So regardless of what the Debate concluded, every trade was a LONG; the size
was a constant 0.1 units of whatever the symbol was (0.1 BTC and 0.1 DOGE
are not comparable risks); and there was no stop-loss anywhere in the chain,
which made CLAUDE.md invariant 3 unenforceable downstream — Execution simply
had no stop to attach.

WHAT IT DOES NOW — AND WHEN IT REFUSES
--------------------------------------
It consumes DEBATE_CONCLUDED to learn the actual direction and confidence,
then treats STRESS_TESTED as the final gate. It submits a TAR only when it
can state, from real data, all of: a direction, an entry price, a computed
stop, and a size derived from that stop. Any one of those missing is a
refusal to submit, not a default value — a fabricated direction or an
invented stop is worse than no trade, because it looks like a decision.

Every refusal is logged with the specific missing input, so "why did nothing
trade?" has an answer (spec Section 5: every agent must explain every
decision).
"""

import datetime
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.core.agent_base import BaseAgent
from backend.core.config import settings
from backend.core.db import get_db_pool
from backend.core.risk_manager import (
    ATR_STOP_MULTIPLIER,
    ATR_TARGET_MULTIPLIER,
    calculate_atr,
    calculate_position_size,
    compute_stop_loss_take_profit,
    kelly_risk_fraction,
    max_leverage_ceiling,
    validate_trade,
)
from backend.core.system_state import may_open_new_position
from backend.models.events import (
    BaseEvent,
    DebateConcludedEvent,
    EventType,
    StressTestedEvent,
    TarRejectedEvent,
    TarSubmittedEvent,
)
from backend.services.market_data import fetch_klines, get_price
from backend.services.portfolio_store import get_portfolio

logger = logging.getLogger(__name__)

# ATR needs `period + 1` candles (14 + 1). Ask for more than the minimum so a
# short feed hiccup doesn't take the stop calculation offline, but treat
# anything under the true minimum as "no stop available".
KLINE_LIMIT = 100
MIN_KLINES_FOR_ATR = 15

# A debate conclusion older than this is not used. A direction derived from
# market conditions half an hour ago is not evidence about now, and silently
# acting on a stale one is how a system ends up trading yesterday's thesis.
DEBATE_STALENESS_SECONDS = 600


def _debate_confidence(debate: Optional[Dict[str, Any]]) -> Optional[float]:
    """The debate's confidence as a PERCENT, or None when there was no debate.

    `decisions.debate_confidence_pct` is a percent column and `score_debate`
    emits a 0-1 fraction, so the conversion belongs here, at the storage
    boundary — the same rule `position_store._as_naive_utc` follows for
    timestamps. Writing 0.38 into a column every reader scales as a percentage
    would report 0.38% where 38% was meant, and a number that is wrong by 100x
    but still plausible is worse than a missing one.

    None rather than 0.0 when absent, because "no debate had run" and "the
    debate found nothing" are different facts and only one of them is evidence
    about the market (invariant 6).
    """
    if not debate:
        return None
    value = debate.get("confidence")
    if value is None:
        return None
    try:
        return round(float(value) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def _debate_direction(debate: Optional[Dict[str, Any]]) -> Optional[str]:
    """The debate's verdict ('LONG' / 'SHORT' / 'NEUTRAL'), or None if it never ran."""
    if not debate:
        return None
    value = debate.get("direction") or debate.get("winning_dir")
    return str(value) if value else None


class SupervisorAgent(BaseAgent):
    def __init__(self) -> None:
        # symbol -> most recent debate conclusion
        self._debates: Dict[str, Dict[str, Any]] = {}
        super().__init__()

    @property
    def name(self) -> str:
        return "Supervisor AI"

    @property
    def purpose(self) -> str:
        return "Orchestrates all specialized agents. No single agent can execute directly; the Supervisor is the final authority to generate a TAR."

    @property
    def permissions(self) -> List[str]:
        # Note what is absent: no EXECUTE_TRADES. The Supervisor hands
        # approved decisions to the CRO and never touches the exchange.
        return ["READ_MARKET_DATA", "READ_PORTFOLIO", "INVOKE_DEBATE", "SUBMIT_TAR"]

    @property
    def inputs(self) -> List[str]:
        return [
            "DEBATE_CONCLUDED events (trade direction and confidence)",
            "STRESS_TESTED events (the final gate)",
            "15m klines via services/market_data.fetch_klines (for ATR / stop derivation)",
            "Live price via services/market_data.get_price",
            "Portfolio equity via services/portfolio_store.get_portfolio",
            "Operator kill switch via core/system_state",
        ]

    @property
    def outputs(self) -> List[str]:
        return [
            "TAR_SUBMITTED events carrying direction, size, leverage, stop-loss and tab",
            "Rows in the `decisions` table with outcome 'pending-approval'",
            "Rows in the `decisions` table with outcome 'declined' — every refusal is recorded, "
            "so 'why didn't it trade?' has an answer",
        ]

    @property
    def category(self) -> str:
        return "orchestration"

    @property
    def events_consumed(self) -> List[EventType]:
        # DEBATE_CONCLUDED added: without it the Supervisor had no source for
        # trade direction, which is why direction was hardcoded to LONG.
        #
        # TAR_REJECTED added: the CRO published it and NOBODY consumed it, so a
        # vetoed trade vanished. The Supervisor — the agent that submitted the
        # TAR — never learned its request was refused, and the rejection never
        # reached the decision record. Spec Section 22.3 requires the breached
        # rule to be logged "to the trade's explainability record"; without a
        # consumer that record was never updated.
        return ["DEBATE_CONCLUDED", "STRESS_TESTED", "TAR_REJECTED"]

    @property
    def events_published(self) -> List[EventType]:
        return ["TAR_SUBMITTED"]

    @property
    def responsibilities(self) -> List[str]:
        return [
            "Arbitrate between specialist agents, weighing evidence and confidence rather than taking a simple vote.",
            "Produce a structured decision record for every decision, including refusals.",
            "Submit TARs to the CRO. Never execute, never bypass the CRO veto.",
        ]

    @property
    def dependencies(self) -> List[str]:
        return ["MessageBus", "MarketData", "PortfolioStore", "RiskManager"]

    @property
    def memory_ttl(self) -> str:
        return f"Debate conclusions cached in-process for {DEBATE_STALENESS_SECONDS}s; decisions persisted to the `decisions` table indefinitely."

    @property
    def knowledge_sources(self) -> List[str]:
        return ["Debate conclusions (event bus)", "Market klines", "Portfolio state"]

    @property
    def prompt_reference(self) -> str:
        return "SUPERVISOR_DETERMINISTIC_V1"

    @property
    def apis_used(self) -> List[str]:
        return ["Market data (klines, price)"]

    @property
    def database_tables(self) -> List[str]:
        return ["decisions (write)"]

    @property
    def metrics_reported(self) -> List[str]:
        return ["TARs submitted", "Refusals by cause", "Events processed"]

    @property
    def failure_recovery_strategy(self) -> str:
        return (
            "Fails closed: any missing input (no debate, no price, no stop, unknown equity) "
            "results in no TAR being submitted rather than a TAR built from defaults."
        )

    @property
    def health_status(self) -> str:
        return "Active"

    # -----------------------------------------------------------------
    async def handle_event(self, event: BaseEvent) -> None:
        if event.event_type == "DEBATE_CONCLUDED" and isinstance(event, DebateConcludedEvent):
            self._debates[event.symbol] = {
                "direction": event.winning_direction,
                "confidence": event.consensus_confidence,
                "participants": event.participants,
                "rationale": event.supervisor_rationale,
                "ts": event.timestamp,
            }
            logger.debug(
                "Supervisor recorded debate for %s: %s @ %.1f%% confidence",
                event.symbol,
                event.winning_direction,
                event.consensus_confidence,
            )
            return

        if event.event_type == "TAR_REJECTED" and isinstance(event, TarRejectedEvent):
            await self._note_rejection(event)
            return

        if event.event_type == "STRESS_TESTED" and isinstance(event, StressTestedEvent):
            await self._consider_trade(event)

    async def _note_rejection(self, event: TarRejectedEvent) -> None:
        """Record that the CRO vetoed a TAR this agent submitted.

        The Supervisor does not retry, argue, or resubmit — the CRO's veto is
        final (spec Section 22.7: "You never bypass the CRO's veto"). This
        exists so the veto is recorded against the decision rather than
        disappearing, and so the operator can see how often and why the risk
        layer is refusing trades. A rising rejection rate for one rule is a
        signal that the Supervisor's sizing or the strategy is misconfigured.
        """
        rationale = f"CRO vetoed this TAR. Rule breached: {event.rule_breached}. {event.cro_rationale}"
        logger.info("Supervisor acknowledged CRO veto of TAR %s (%s)", event.tar_id, event.rule_breached)
        self.record_decision(
            "vetoed-by-cro",
            rationale,
            {"tar_id": str(event.tar_id), "rule_breached": event.rule_breached},
            acted=False,
        )
        await self._update_decision_outcome(str(event.tar_id), "rejected-by-risk", rationale)

    async def _update_decision_outcome(self, decision_id: str, outcome: str, rationale: str) -> None:
        """Update an existing decision row in place.

        An UPDATE rather than a second INSERT: the TAR already has a row with
        outcome 'pending-approval', and inserting another would make one
        decision look like two in every count and rate calculation.
        """
        pool = get_db_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE decisions SET outcome = $2, rationale = $3 WHERE id = $1",
                    decision_id,
                    outcome,
                    rationale,
                )
        except Exception as e:
            logger.error("Failed to update decision %s: %s", decision_id, e)

    async def _refuse(
        self, symbol: str, cause: str, debate: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record and log a decision NOT to trade.

        `debate` is OPTIONAL because the refusal paths genuinely differ: a system
        that is paused, or a symbol with no live price, is refused before any
        debate has been scored, and there is no confidence to report. Passing it
        where it exists is what lets a later reader compare the confidence that
        was reached against the bar it had to clear — the exact comparison that
        exposed `dynamic_thresholding`'s unreachable scale.

        Refusals are persisted alongside approvals. A decision log that only
        contains the trades that happened cannot answer "why didn't it act
        on that setup?", which is the question an operator asks most often.

        `outcome="rejected"`, NOT "declined". `db/schema.sql`'s `decisions` table
        constrains this column to

            approved-executed | approved-not-executed | rejected |
            pending-approval  | manually-approved     | manually-rejected

        and "declined" is not among them, so EVERY refusal violated the check
        constraint and was never stored. The failure was logged by
        `_persist_decision` and swallowed, so the system kept running and the
        decision log silently contained only the trades that happened — precisely
        the situation this method's own docstring exists to prevent.

        Found by enabling the autonomy gates and reading the server log: a steady
        stream of `violates check constraint "decisions_outcome_check"` behind
        ordinary-looking "Supervisor declined to submit a TAR" lines. Most decisions
        ARE refusals, so most of the audit trail was being dropped.

        `tests/test_decision_audit.py` now asserts every outcome literal the code
        writes is one the schema accepts, so a new value cannot reintroduce this.
        """
        logger.info("Supervisor declined to submit a TAR for %s: %s", symbol, cause)
        await self._persist_decision(
            decision_id=str(uuid.uuid4()),
            symbol=symbol,
            side="buy",
            qty=0.0,
            price=0.0,
            outcome="rejected",
            rationale=f"No TAR submitted: {cause}",
            # THE CAUSE IS THE STRUCTURED REASON. Recorded in both columns on
            # purpose rather than picking one: `rationale` is the human sentence
            # an operator reads on the Decisions page, `rejection_reasons` is the
            # machine-readable list everything else groups by. Writing only the
            # first is what made 3,063 consecutive refusals un-analysable.
            #
            # Filled here, in the one method every refusal path goes through,
            # rather than at the 16 call sites — a reason that has to be passed
            # twice is a reason that will eventually be passed once.
            rejection_reasons=[cause],
            debate_confidence_pct=_debate_confidence(debate),
            debate_recommendation=_debate_direction(debate),
        )

    async def _consider_trade(self, event: StressTestedEvent) -> None:
        symbol = event.symbol

        if not event.passed:
            await self._refuse(symbol, f"failed stress tests ({event.results})")
            return

        # Operator kill switch. Checked here as well as in Execution, because
        # the cheapest place to stop is before a TAR enters the pipeline.
        if not may_open_new_position():
            await self._refuse(symbol, "system is paused or emergency-stopped by the operator")
            return

        # ---- ONE ORIGINATOR OF ENTRIES, NOT TWO -------------------------------
        #
        # THE OPERATOR'S COMPLAINT, AND THE MEASUREMENT THAT CONFIRMED IT.
        #
        # "in the trade history page ... each trade is market data and directly
        # execute, all agent doesn't working together". That is literally what the
        # ledger showed. Read live from Postgres:
        #
        #     23 trade rows. strategy, run_id AND entry_context NULL on EVERY one.
        #     5 of the 12 opening rows join `decisions` on the TAR id, and every
        #     one of those rationales begins "Debate concluded LONG at ...".
        #
        # Those are THIS path's rows. It reaches a trade from a four-to-five leg
        # technical debate (Structure, Momentum, Trend, Volume, StrategyEnsemble)
        # and submits. It never runs the 24-node graph, so no trade it opens has:
        #
        #     the 9-specialist panel      (orderflow, liquidity, news, funding,
        #                                  portfolio, risk, prediction, event_risk,
        #                                  market — coverage-scaled and
        #                                  constraint-dampened)
        #     regime detection + strategy scoring   -> `trades.strategy`
        #     the Risk Gateway's entry snapshot     -> `trades.entry_context`
        #     the run trace link                    -> `trades.run_id`
        #
        # Which is exactly why the trade-detail journey renders Market Data, then
        # an unknown middle, then Execution: the middle was never recorded because
        # the nodes that record it never ran.
        #
        # AND THE TWO PATHS DISAGREE, MEASURABLY. On one live SOL/USDT run taken
        # while writing this, the full panel reached NEUTRAL at 0.067 confidence
        # (the portfolio constraint binding at 0.40 because a position was already
        # open) and the Supervisor node returned DO_NOT_TRADE. This path's own
        # debate put the same symbol at 0.23-0.24 and traded it. The shortcut is
        # not a faster route to the same answer; it is a different, less informed
        # answer that wins because it is cheaper — the graph takes ~5s to reach its
        # gateway and this path takes milliseconds, so it claims the single
        # allowed position first and the graph's run is then refused for holding
        # one.
        #
        # SO: WHEN THE GRAPH PATH IS ENABLED, IT IS THE ONLY ORIGINATOR OF
        # ENTRIES. This is a refusal, not a silent return, so the reason lands in
        # `decisions` and "why did this not trade?" stays answerable.
        #
        # EXITS ARE UNTOUCHED — invariant 4. `_consider_trade` only ever opens;
        # closes belong to `PositionMonitorAgent` and never come through here.
        #
        # REVERSIBLE IN ONE LINE: GRAPH_EXECUTION_ENABLED=false hands this path
        # back its old role, and it keeps every gate it has. Read at call time for
        # the `simulation_mode` reason — a frozen import would mean an operator
        # flipping the flag saw no change until a restart.
        from backend.services.execution_service import execution_enabled

        if execution_enabled():
            await self._refuse(
                symbol,
                "entries are originated by the 24-node analysis graph while "
                "GRAPH_EXECUTION_ENABLED=true, so this event-driven path does not "
                "submit its own. It reaches a trade from the debate alone, without "
                "the specialist panel, regime detection, strategy scoring or the "
                "Risk Gateway's entry snapshot — so a trade it opened carried no "
                "strategy, no run_id and no entry context, and the learning loop "
                "and the trade-journey view both had nothing to read",
            )
            return

        # --- direction: from the debate, never assumed ------------------
        #
        # LOADED BEFORE THE SCOPE GATE, AND THE ORDER IS LOAD-BEARING.
        #
        # The scope gate used to sit ABOVE this line and pass `debate` to
        # `self._refuse(...)`. Python makes `debate` a local of this whole method
        # because it is assigned here, so every one of those refusals raised
        #
        #     UnboundLocalError: cannot access local variable 'debate' where it
        #     is not associated with a value
        #
        # rather than recording a refusal. That is why the live `decisions` table
        # holds 1,995 rejections and NOT ONE of them is a scope rejection: the
        # gate stopped the trade by crashing, so no row was ever written and the
        # operator had no way to see the limit working. Proven at runtime before
        # this was moved, not inferred from reading.
        debate = self._debates.get(symbol)
        if debate is None:
            await self._refuse(
                symbol,
                "no debate conclusion available for this symbol, so trade direction is unknown "
                "(previously this defaulted to LONG)",
                debate,
            )
            return

        age = (datetime.datetime.utcnow() - debate["ts"].replace(tzinfo=None)).total_seconds()
        if age > DEBATE_STALENESS_SECONDS:
            await self._refuse(
                symbol,
                f"the only debate conclusion for this symbol is {age:.0f}s old "
                f"(limit {DEBATE_STALENESS_SECONDS}s)",
                debate,
            )
            return

        direction = debate["direction"]
        if direction not in ("LONG", "SHORT"):
            await self._refuse(symbol, f"debate concluded {direction} — no directional trade to make", debate)
            return

        # ---- SCOPE: may we open anything at all, in this instrument, now? -----
        #
        # THIS PATH HAD NONE OF THESE CHECKS, AND IT IS THE PATH THAT TRADED.
        #
        # Every gate written over this project's life — the tradeable-instrument
        # blocklist, session scope, one-position-at-a-time — lives in
        # `graphs/nodes/risk_gateway`, which this method never touches. It did not
        # matter while `dynamic_thresholding` demanded 0.60-0.99 confidence on a
        # scale whose ceiling was 0.44: this supervisor refused everything, 2,948
        # decisions, zero trades.
        #
        # Rescaling those thresholds to the debate's real units was correct — an
        # unreachable gate is a bug. But it unblocked THIS path, and the ledger
        # shows what that meant: 4,080 fills in five days, 856 closes on BTC
        # (which is on the untradeable list), up to three symbols at once, and no
        # session ever started.
        #
        # ENTRIES ONLY. `_consider_trade` opens; closes are the position monitor's
        # and never come through here, so invariant 4 is untouched.
        from backend.services.trade_scope import entry_refusal

        try:
            from backend.agents.position_monitor import get_position_monitor

            held = [p.get("symbol") for p in get_position_monitor().snapshot_open()]
        except Exception as exc:  # noqa: BLE001
            # FAIL CLOSED. If the book cannot be read we cannot know whether the
            # concurrency limit is already met, and opening anyway is how "one
            # position at a time" becomes three.
            await self._refuse(symbol, f"could not read the open-position book ({exc})", debate)
            return

        scope_refusal = entry_refusal(symbol, held)
        if scope_refusal is not None:
            await self._refuse(symbol, scope_refusal, debate)
            return
            
        side = "buy" if direction == "LONG" else "sell"

        # --- price: real, or nothing ------------------------------------
        price = get_price(symbol)
        if price <= 0:
            await self._refuse(symbol, "no live price available (market data feed returned 0)", debate)
            return

        # --- stop: computed from real volatility, or nothing ------------
        klines = await fetch_klines(symbol, "15m", limit=KLINE_LIMIT)
        if len(klines) < MIN_KLINES_FOR_ATR:
            await self._refuse(
                symbol,
                f"only {len(klines)} candle(s) available, need {MIN_KLINES_FOR_ATR} to compute ATR "
                f"and therefore a stop-loss",
                debate,
            )
            return
            
        # --- PHASE 38 & 39: Regime Detection and Dynamic Thresholding -----
        from backend.agents.regime_agent import detect_market_regime
        from backend.algorithms.dynamic_thresholding import get_required_confidence, get_regime_risk_multiplier
        
        regime = detect_market_regime(klines)
        required_confidence = get_required_confidence(regime)
        
        if debate.get("confidence", 0) < required_confidence:
            await self._refuse(symbol, f"Confidence {debate.get('confidence', 0):.2f} does not meet the threshold {required_confidence:.2f} required for regime '{regime}'", debate)
            return
            
        # --- PHASE 37: Bayesian Expected Value Evaluation ------------------
        from backend.algorithms.bayesian_engine import calculate_trade_probabilities
        bayesian_probs = calculate_trade_probabilities(debate)
        if bayesian_probs["expected_value"] <= 0:
            await self._refuse(symbol, f"Bayesian evaluation rejected trade: Expected Value is {bayesian_probs['expected_value']:.3f} (P(Profit)={bayesian_probs['p_profit']:.2f})", debate)
            return
        # Record the probabilities into the debate dict so it can be logged downstream
        debate["bayesian_probs"] = bayesian_probs

        atr = calculate_atr(klines)
        sltp = compute_stop_loss_take_profit(price, atr, side)
        if sltp is None:
            await self._refuse(symbol, f"ATR computed as {atr}, so no stop-loss could be derived", debate)
            return

        # --- size: from the stop distance and real equity ---------------
        tab = settings.execution_tab
        portfolio = await get_portfolio()
        equity = self._equity_for(portfolio, tab)
        if equity <= 0:
            await self._refuse(
                symbol,
                f"equity for the '{tab}' tab is unknown, so a risk-based position size "
                f"cannot be computed",
                debate,
            )
            return

        # --- PHASE 40: Position Sizing AI -----------------------------
        from backend.core.risk_manager import calculate_dynamic_risk
        
        regime_multiplier = get_regime_risk_multiplier(regime)
        base_risk = settings.RISK_PER_TRADE
        ev = bayesian_probs["expected_value"]
        
        final_risk_fraction = calculate_dynamic_risk(base_risk, regime_multiplier, ev)

        if final_risk_fraction <= 0:
            await self._refuse(symbol, f"dynamic sizing returned zero risk for EV {ev:.3f} in regime {regime}", debate)
            return

        size = calculate_position_size(equity, price, atr, final_risk_fraction)
        if size <= 0:
            await self._refuse(
                symbol,
                f"risk-based sizing returned {size} for equity ${equity:.2f} at ATR {atr:.6g} "
                f"— the smallest position consistent with the risk budget rounds to zero",
                debate,
            )
            return

        # --- CIO: correlated-exposure cap (spec Section 18) -------------
        # Consulted BEFORE the TAR is built, so a correlated trade is sized
        # down here rather than rejected downstream. The CRO keeps the final
        # veto; this is an allocation constraint, not a second approval.
        from backend.agents.cio_agent import get_cio_agent

        exposure = await get_cio_agent().check_exposure(
            symbol=symbol, side=side, proposed_notional=size * price, equity=equity
        )
        if not exposure["allowed"]:
            await self._refuse(symbol, f"CIO exposure limit: {exposure['detail']}", debate)
            return
        if exposure["max_notional"] < size * price:
            # Size down to the permitted notional rather than declining.
            reduced = exposure["max_notional"] / price
            logger.info(
                "CIO reduced %s position from %.8g to %.8g units: %s",
                symbol, size, reduced, exposure["detail"],
            )
            size = reduced
            if size <= 0:
                await self._refuse(symbol, f"CIO exposure limit leaves no room: {exposure['detail']}", debate)
                return

        # This path requests no leverage. Clamped to the ceiling regardless,
        # so the value in the TAR can never exceed it even if this changes.
        requested_leverage = min(1, max_leverage_ceiling(tab))

        # --- final risk validation before the CRO ----------------------
        # Run here as well as in the CRO so a TAR that cannot pass basic
        # checks is never submitted. The CRO remains the authority; this is
        # not a substitute for its veto.
        validation = validate_trade(
            {
                "qty": size,
                "price": price,
                "equityUsd": equity,
                "klines": klines,
                "side": side,
                "tab": tab,
                "requestedLeverage": requested_leverage,
            }
        )
        if not validation.approved:
            await self._refuse(symbol, "pre-submission risk checks failed: " + "; ".join(validation.rejection_reasons), debate)
            return

        rationale = (
            # x100: `score_debate` emits a 0-1 FRACTION and this line renders a
            # PERCENT. `:.0f` on 0.23 is "0", so every rationale this path has
            # ever written says "at 0% confidence" — including the five that
            # became live trades. A confidence of zero is also exactly what a
            # broken gate would look like, so the one line an operator reads to
            # audit a trade asserted the opposite of what the gate measured.
            f"Debate concluded {direction} at {debate['confidence'] * 100:.0f}% confidence "
            f"({', '.join(debate['participants']) or 'no participants recorded'}); "
            f"stress tests passed; stop at {sltp['stopLoss']:.6g} "
            f"({abs(price - sltp['stopLoss']) / price * 100:.2f}% away). "
            # `sizing['rule']` / `sizing['detail']` referenced a dict that does not
            # exist in this method — a NameError that crashed a trade the instant
            # it was approved and about to submit. Sizing here is the risk fraction
            # this method actually computed (regime-adjusted risk-per-trade), which
            # is the number worth recording.
            f"Sizing: {final_risk_fraction * 100:.2f}% risk of ${equity:.2f} equity "
            f"(regime multiplier {regime_multiplier:.2f}) -> {size:.8g} units."
        )

        tar = TarSubmittedEvent(
            symbol=symbol,
            direction=direction,
            requested_size=size,
            requested_leverage=requested_leverage,
            # STRATEGY IS None HERE, AND THAT IS THE HONEST VALUE.
            #
            # This used to be the string "Event-Driven Multi-Agent Pipeline" — a
            # description of the PATH, not a strategy profile. It landed in
            # `trades.strategy` on every fill this supervisor produced, and
            # `services/strategy_performance` aggregates exactly that column: so
            # 2,426 closed trades accumulated under one label that matches none of
            # the eleven real strategies, every profile's
            # `historical_success_rate` stayed None, and the 0.2 track-record
            # weight in strategy scoring stayed permanently neutral. The learning
            # loop looked wired and was measuring a name.
            #
            # This path runs a DEBATE; it does not select a strategy profile, so
            # it has nothing to attribute. `position_monitor` already applies the
            # same rule to a manual position — "a human's click was not chosen by
            # an algorithm, and crediting one would poison the measurement it
            # feeds". A pipeline label poisons it the same way.
            strategy=None,
            supervisor_rationale=rationale,
            stop_loss=sltp["stopLoss"],
            take_profit=sltp["takeProfit"],
            entry_price=price,
            tab=tab,
        )
        logger.info("Supervisor submitting TAR %s: %s %s %s @ %s", tar.tar_id, direction, size, symbol, price)

        await self._persist_decision(
            decision_id=str(tar.tar_id),
            symbol=symbol,
            side=side,
            qty=size,
            price=price,
            outcome="pending-approval",
            rationale=rationale,
            # PASSED HERE TOO, AND THEY WERE NOT. `_refuse` fills these on every
            # rejection, so the live table had a confidence on all 1,995 refusals
            # and NULL on all 5 decisions that became trades — the analysis columns
            # were populated on exactly the rows nobody needs them for. Measured
            # before this line existed.
            debate_confidence_pct=_debate_confidence(debate),
            debate_recommendation=_debate_direction(debate),
        )
        await self.publish(tar)

    @staticmethod
    def _measured_win_rate() -> Optional[float]:
        """Win rate from the real trade ledger, or None below a usable sample.

        None rather than a default, because `kelly_risk_fraction` treats None as
        "use the fixed fraction" — a made-up win rate would feed Kelly a number
        nobody measured, and Kelly is at its most dangerous when its probability
        estimate is optimistic.
        """
        try:
            from backend.config import settings
            from backend.services.ai_memory import stats_for_tab

            # THE BOOK BEING TRADED, not both books added together. Paper fills
            # are simulated against an observed price with no real slippage and
            # no partial fills, so a paper win rate is optimistic relative to a
            # real one — and this number goes straight into Kelly, which is at
            # its most dangerous when its probability estimate is optimistic.
            stats = stats_for_tab(settings.execution_tab) or {}
        except Exception:
            return None

        total = stats.get("total_trades") or 0
        wins = stats.get("wins") or 0
        # 20 trades is not a statistical threshold, it is a floor to stop a
        # three-win streak reading as a 100% win rate.
        if total < 20:
            return None
        return wins / total

    @staticmethod
    def _equity_for(portfolio: Dict[str, Any], tab: str) -> float:
        """Equity for a tab: cash plus the marked value of open positions.

        Returns 0.0 for the real tab when no cash figure is declared. That is
        deliberate — it makes the Supervisor refuse rather than size a real
        trade against an assumed balance. The real tab has no `cash` key in
        `portfolio_store` today, so real-money sizing needs an operator-
        declared starting capital first (the same gap the TypeScript side
        closed with TradingControls' realStartingCapitalUsd).
        """
        book = portfolio.get(tab) or {}
        cash = book.get("cash")
        if cash is None:
            return 0.0
        equity = float(cash)
        for pos in book.get("positions", []):
            live = get_price(pos.get("symbol", ""))
            # Fall back to entry cost only for marking existing holdings —
            # this is not inventing a price, it is stating the position at
            # book value when no live quote is available.
            mark = live if live > 0 else float(pos.get("avgCost", 0.0))
            equity += float(pos.get("qty", 0.0)) * mark
        return equity

    async def _persist_decision(
        self,
        decision_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        outcome: str,
        rationale: str,
        rejection_reasons: Optional[List[str]] = None,
        debate_confidence_pct: Optional[float] = None,
        debate_recommendation: Optional[str] = None,
    ) -> None:
        """Write one row to the decision audit trail.

        `rejection_reasons` USED TO BE OMITTED FROM THIS INSERT ENTIRELY, and the
        column is `jsonb NOT NULL DEFAULT '[]'` — so every row took the default
        and the structured reason was empty on 100% of them. Read from the live
        table:

            decisions with EMPTY rejection_reasons: 3063 of 3063

        The cause survived only inside the free-text `rationale`, so the Decisions
        page and every analytic over "why is this agent not trading?" saw nothing
        at all. Diagnosing the unreachable-threshold bug above required regex over
        prose because of this; the column exists precisely so that is not
        necessary.

        `debate_confidence_pct` and `debate_recommendation` were dropped the same
        way. They are what makes a rejection ANALYSABLE rather than merely logged
        — the confidence-versus-threshold gap is the number that showed the gate
        was impossible, and it was reconstructible only by parsing a sentence.

        A NULL confidence and an ABSENT one are not distinguished here, and do not
        need to be: the column is nullable and every refusal that has a debate
        passes it. A refusal raised BEFORE the debate exists (paused system, no
        price) genuinely has no confidence to report, and NULL is the honest value
        rather than 0.0 — which would read as "the debate was certain of nothing"
        instead of "no debate had happened yet".
        """
        pool = get_db_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO decisions (id, ts, symbol, side, tab, origin_tag, requested_qty, requested_price, outcome, urgency, rationale,
                                           rejection_reasons, debate_confidence_pct, debate_recommendation)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    """,
                    decision_id,
                    datetime.datetime.utcnow(),
                    symbol,
                    side,
                    # Was hardcoded 'real'; now reflects the actual execution
                    # mode so the decision log doesn't label paper decisions
                    # as real ones.
                    settings.execution_tab,
                    "agent-plan",
                    qty,
                    price,
                    outcome,
                    "normal",
                    rationale,
                    # json.dumps, not the list: asyncpg binds jsonb from a JSON
                    # string. Passing the list raises
                    # "invalid input for query argument" and the except below
                    # would swallow it into the same silence this fix is undoing.
                    json.dumps(list(rejection_reasons or [])),
                    debate_confidence_pct,
                    debate_recommendation,
                )
        except Exception as e:
            logger.error(f"Failed to persist decision: {e}")


# Export an instance or a factory
# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
#
# MEMOISED, and it was not before. `get_supervisor()` used to be `return SupervisorAgent()`, so
# every call built a NEW agent — and `BaseAgent.__init__` subscribes on construction,
# with nothing ever unsubscribing. So each call added a permanent duplicate handler to
# the global bus, and the agent then processed every matching event once per call ever
# made.
#
# Latent in production, because `main.py` calls this exactly once at startup. It
# became live the moment `HistoricalBacktestEngine` also called it: running a backtest
# in-process left a SECOND agent handling every live event for the rest of the
# process's life — for the supervisor, two trade-authorization requests per debate.
#
# Found by an independent end-to-end verification of the Phase 38 bus-isolation work,
# not by the test suite, which had constructed one engine per test and never checked
# what accumulated across them.
#
# Every other accessor of this shape already memoises — `cio_agent`,
# `hypothesis_agent`, `get_exchange_client`, `get_polymarket_client`. These two were
# the exceptions.

_instance: Optional[SupervisorAgent] = None


def get_supervisor() -> SupervisorAgent:
    global _instance
    if _instance is None:
        _instance = SupervisorAgent()
    return _instance


def reset_supervisor() -> None:
    """For tests. Drops the singleton WITHOUT unsubscribing it.

    Deliberate: a test that wants a clean bus should build its own `MessageBus`, which
    is what `isolated_bus` does. Silently unsubscribing here would make this function
    mutate global routing as a side effect of asking for a fresh object.
    """
    global _instance
    _instance = None


async def request_trade_authorization(
    task_id: str, task: Dict[str, Any], symbol: str, price: float, intended_side: str
) -> Dict[str, Any]:
    """Authorization path for the task-based `trading_agent.py`.

    THIS USED TO BE A RUBBER STAMP. Verbatim:

        return {
            "approved": True,
            "optimal_qty": task.get("qty", 0.1),
            "tp_sl": {"takeProfit": price * 1.05, "stopLoss": price * 0.95},
            "receipt": "Legacy Approval Stub",
        }

    It approved every trade unconditionally and handed back a fabricated
    ±5% stop and target that had nothing to do with the instrument's actual
    volatility — on a stablecoin pair a 5% stop is never hit, on a small-cap
    it is hit by noise. `trading_agent.py` then wrote those numbers to
    `dynamic_sl_price` and traded against them.

    It now runs the real risk pipeline and returns `approved: False` with a
    reason when any required input is missing.
    """
    if not may_open_new_position():
        return {
            "approved": False,
            "reason": "system is paused or emergency-stopped by the operator",
            "optimal_qty": 0.0,
            "tp_sl": None,
            "receipt": "Blocked: operator kill switch active",
        }

    if price <= 0:
        return {
            "approved": False,
            "reason": "no live price available",
            "optimal_qty": 0.0,
            "tp_sl": None,
            "receipt": "Blocked: no price",
        }

    tab = task.get("tab") or settings.execution_tab
    klines = await fetch_klines(symbol, "15m", limit=KLINE_LIMIT)
    if len(klines) < MIN_KLINES_FOR_ATR:
        return {
            "approved": False,
            "reason": f"only {len(klines)} candle(s) available; need {MIN_KLINES_FOR_ATR} to compute a stop-loss",
            "optimal_qty": 0.0,
            "tp_sl": None,
            "receipt": "Blocked: no computable stop-loss",
        }

    atr = calculate_atr(klines)
    sltp = compute_stop_loss_take_profit(price, atr, intended_side)
    if sltp is None:
        return {
            "approved": False,
            "reason": f"ATR computed as {atr}; no stop-loss could be derived",
            "optimal_qty": 0.0,
            "tp_sl": None,
            "receipt": "Blocked: no computable stop-loss",
        }

    portfolio = await get_portfolio()
    equity = SupervisorAgent._equity_for(portfolio, tab)
    if equity <= 0:
        return {
            "approved": False,
            "reason": f"equity for the '{tab}' tab is unknown; cannot size by risk",
            "optimal_qty": 0.0,
            "tp_sl": None,
            "receipt": "Blocked: unknown equity",
        }

    size = calculate_position_size(equity, price, atr, settings.RISK_PER_TRADE)
    requested_leverage = min(float(task.get("leverage") or 1), max_leverage_ceiling(tab))

    validation = validate_trade(
        {
            "qty": size,
            "price": price,
            "equityUsd": equity,
            "klines": klines,
            "side": intended_side,
            "tab": tab,
            "requestedLeverage": requested_leverage,
        }
    )
    if not validation.approved:
        return {
            "approved": False,
            "reason": "; ".join(validation.rejection_reasons),
            "optimal_qty": 0.0,
            "tp_sl": None,
            "receipt": "Blocked by risk checks: " + "; ".join(validation.rejection_reasons),
        }

    return {
        "approved": True,
        "optimal_qty": size,
        "tp_sl": {"takeProfit": sltp["takeProfit"], "stopLoss": sltp["stopLoss"]},
        "requested_leverage": requested_leverage,
        "receipt": (
            f"Approved for task {task_id}: {size:.8g} {symbol} at {price}, stop {sltp['stopLoss']:.6g} "
            f"(ATR {atr:.6g}), risking {settings.RISK_PER_TRADE * 100:.1f}% of ${equity:.2f} equity, "
            f"leverage {requested_leverage}x (ceiling {max_leverage_ceiling(tab)}x)."
        ),
    }
