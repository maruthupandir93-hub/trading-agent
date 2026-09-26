import logging
from typing import Dict, Any, List, Optional
import os
from backend.core.agent_base import BaseAgent
from backend.models.events import (
    EventType,
    BaseEvent,
    OrderFilledEvent,
    PositionClosedEvent,
    ReflectionCompletedEvent,
)
from backend.core.db import get_db_pool
import datetime

logger = logging.getLogger(__name__)

# The legacy analyze_reflection function has been replaced by the LangGraph
# reflection_graph.py, which maintains determinism but executes as a proper graph.

# Kept so existing callers (`services/ai_memory`, and this module's own
# `_reflect_on_close`) keep working, but modified to invoke the graph.
async def analyze_mistake(receipt: Dict[str, Any]) -> str:
    """Run the reflection graph over one closed trade and return its lesson.

    THE SIGNATURE IS UNCHANGED ON PURPOSE. `services/ai_memory.py` and
    `_reflect_on_close` below both call this and want a lesson string; the Phase 33
    migration onto `TradingState` is invisible to them.

    What changed underneath: the graph now runs through `build_graph`, so every
    node is contract-checked and the run is traced. It also builds and invokes per
    call rather than reusing a module-level compiled app.

    THAT IS A DELIBERATE TRADE, NOT A REGRESSION. The previous version cached the
    compiled graph to avoid a LangGraph compile per call. Compiling per run is what
    `run_reflection_graph` does for every other graph in the system, because a run
    needs its own `RunContext` — the tracing, the budget and the contract wrapper
    are all bound to it, and a shared compiled app would share one run's context
    across every trade. This runs once per CLOSED TRADE, so the compile cost is
    paid at a rate measured in trades per day.

    A failed run returns the deterministic lesson rather than raising: the caller
    publishes `ReflectionCompletedEvent`, and an exception here would end the
    learning pipeline for that trade instead of degrading it.
    """
    from backend.graphs.reflection_graph import run_reflection_graph

    result = await run_reflection_graph(receipt)

    if not result.get("ok"):
        logger.error(
            "Reflection graph failed for %s: %s", receipt.get("symbol"), result.get("error")
        )
        return "No lesson generated — the reflection run failed. See the run trace."

    lesson = result.get("lesson") or "No lesson generated."
    logger.info(
        "Reflection produced a %s lesson for %s: %s",
        result.get("lessonSource", "rules"), receipt.get("symbol"), lesson,
    )
    return lesson


# Confidence calibration bounds. Capped so one large trade cannot dominate the
# series — an uncapped delta would let a single outsized win push calibration far
# enough that the next several trades could not correct it.
CALIBRATION_CAP = 5.0
# Dollars of realised P&L per point of calibration movement.
CALIBRATION_SCALE = 100.0


def calibration_delta(realized_pnl: float) -> float:
    """Confidence calibration movement from one closed trade.

    Shared by `ReflectionAgent` and `graphs/reflection_graph.py`. It lived inline in
    both, copied verbatim — and this number feeds `ConfidenceAgent`, which feeds
    position sizing, so two copies that could drift is a real hazard rather than a
    tidiness point.

    It replaced a constant `-5.0` applied to every trade including winners, which
    drove calibration monotonically downward forever regardless of performance.

    A KNOWN LIMITATION, stated rather than hidden: this is driven by P&L MAGNITUDE,
    not by whether the prediction was correct. A lucky win on a wrong-direction read
    still raises confidence. Spec Section 16's example ties calibration to
    prediction correctness ("Prediction: Correct · Entry: Too early"), which needs
    the predicted direction recorded at entry and compared at close — that data is
    not currently carried on `POSITION_CLOSED`. Fixing it properly means extending
    that event, not adjusting this formula.
    """
    return max(-CALIBRATION_CAP, min(CALIBRATION_CAP, realized_pnl / CALIBRATION_SCALE))


class ReflectionAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "Reflection Agent"

    @property
    def purpose(self) -> str:
        # "particularly losses" removed: Section 12 requires a reflection on
        # every completed trade, and learning only from losses biases the system
        # toward explaining failure.
        return "Analyzes every closed trade — win or loss — to produce a reflection the learning pipeline can build on."

    @property
    def permissions(self) -> List[str]:
        # Note what is absent: this agent may write memory but has no
        # permission to alter strategy or risk configuration. CLAUDE.md
        # invariant 5 — learning produces understanding, never a deployment.
        return ["READ_TRADES", "WRITE_MEMORY"]

    @property
    def inputs(self) -> List[str]:
        return [
            "POSITION_CLOSED events (real symbol, side, exit reason and realized P&L)",
            "ORDER_FILLED events (observed only; an opening fill produces no reflection)",
        ]

    @property
    def outputs(self) -> List[str]:
        return [
            "REFLECTION_COMPLETED events carrying the lesson and a derived calibration delta",
            "Rows in the `reflections` table",
            "NO writes to strategy or risk configuration — deliberately outside its permissions",
        ]

    @property
    def category(self) -> str:
        return "learning"

    @property
    def events_consumed(self) -> List[EventType]:
        # REFLECTION_COMPLETED removed: this agent PUBLISHES that event, and
        # subscribing to its own output was both a latent feedback loop and
        # misleading in the contract — handle_event never acted on it.
        return ["POSITION_CLOSED", "ORDER_FILLED"]

    @property
    def events_published(self) -> List[EventType]:
        return ["REFLECTION_COMPLETED"]


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
        return "REFLECTION_DETERMINISTIC_V1"

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
        """Reflect on CLOSED positions only.

        WHAT THIS REPLACED. The previous implementation triggered on
        ORDER_FILLED — which fires when a position OPENS — and built its
        reflection from three hardcoded values:

            "symbol": "BTC/USDT",   # Mocking symbol since OrderFilledEvent
                                    # doesn't carry it (only tar_id)
            "side": "buy",
            "pnl": -50.0,           # Mock negative PNL to trigger reflection
            ...
            confidence_calibration_delta=-5.0

        So every reflection recorded a $50 loss on BTC/USDT for a trade that
        had just been entered and had no outcome yet. Two consequences beyond
        the obvious: the `reflections` table filled with fabricated losses,
        and those rows feed the win-rate the Confidence Agent calibrates
        against — so invented outcomes propagated into live position sizing.

        Reflection now waits for POSITION_CLOSED, which carries the real
        symbol, side and realized P&L as required fields. An opening fill
        produces no reflection, because an open position has not taught us
        anything yet.
        """
        if event.event_type == "POSITION_CLOSED" and isinstance(event, PositionClosedEvent):
            # COUNT IT BEFORE REASONING ABOUT IT.
            #
            # `ai_memory.global_stats` is what `algorithms/probability`,
            # `ConfidenceAgent` and `supervisor_agent._measured_win_rate` all read
            # to answer "how often has this system been right?". Its only writer
            # was `trading_agent_tick`, a legacy path the autonomous system never
            # runs — so on 2026-09-25 the file read total_trades 0 while Postgres
            # held 11 closed trades, and every one of those three readers reported
            # "unmeasurable" indefinitely.
            #
            # Here rather than in `position_monitor` because this agent is already
            # the POSITION_CLOSED subscriber that owns learning, and the close path
            # itself must not grow a synchronous file write.
            #
            # BEFORE the reflection, not after: the reflection makes an LLM call
            # that can fail, and the count is a fact that must not depend on a
            # model answering. `record_closed_trade` never raises.
            from backend.services.ai_memory import record_closed_trade

            await record_closed_trade(
                symbol=event.symbol,
                side=event.side,
                pnl=event.realized_pnl,
                strategy=getattr(event, "strategy", None),
                # WHICH BOOK — the daily-loss gate and Kelly sizing both read
                # this back, and neither may count the other book's outcomes.
                tab=getattr(event, "tab", "paper"),
            )
            await self._reflect_on_close(event)
            return

        if event.event_type == "ORDER_FILLED" and isinstance(event, OrderFilledEvent):
            # An entry is not a lesson. Logged at debug so the absence of a
            # reflection here is explainable rather than mysterious.
            logger.debug(
                "Order %s filled for %s — no reflection generated: an opening fill has no "
                "outcome to learn from. Awaiting POSITION_CLOSED.",
                event.order_id,
                event.symbol,
            )
            return

    async def _reflect_on_close(self, event: PositionClosedEvent) -> None:
        receipt = {
            "symbol": event.symbol,
            "side": event.side,
            "pnl": event.realized_pnl,
            "entry_price": event.entry_price,
            "exit_price": event.exit_price,
            "quantity": event.quantity,
            "exit_reason": event.exit_reason,
            "strategies": list(event.strategies),
            "held_seconds": event.held_seconds,
            # The snapshot of WHAT THE AGENT SAW at entry — RSI, ATR, structure,
            # regime, volatility, higher-timeframe trend, BTC benchmark. This is
            # the difference between "check if losses cluster" and "stopped out
            # 0.6% away while 15m ATR was 0.5% AND the entry was counter to the 4h
            # downtrend". The reflection prompt reads it directly.
            "entry_context": getattr(event, "entry_context", None),
            "strategy": getattr(event, "strategy", None),
            "run_id": getattr(event, "run_id", None),
        }

        # The GRAPH is called directly here rather than through
        # `analyze_mistake`, which returns only the lesson string.
        #
        # `_persist_reflection` now records WHO wrote the lesson, and that fact
        # only exists in the graph's full result. Squeezing it back out of a bare
        # string was the alternative, and a parser over prose to recover a field
        # the producer already had is how provenance silently becomes wrong.
        #
        # `analyze_mistake` keeps its string signature for `services/ai_memory.py`,
        # which genuinely only wants the text.
        from backend.graphs.reflection_graph import run_reflection_graph

        result = await run_reflection_graph(receipt)
        note = result.get("lesson") or "No lesson generated."
        lesson_source = result.get("lessonSource")
        lesson_detail = result.get("lessonDetail")
        if not result.get("ok"):
            logger.error(
                "Reflection graph failed for %s: %s", event.symbol, result.get("error")
            )

        # Extracted to `calibration_delta` below and shared with
        # `graphs/reflection_graph.py`, which had copied the expression verbatim.
        # Two copies of a rule that feeds position sizing is one too many.
        delta = calibration_delta(event.realized_pnl)

        rationale = (
            f"{event.symbol} {event.side} closed at {event.exit_price:.6g} from "
            f"{event.entry_price:.6g} ({event.exit_reason}), realized "
            f"{'+' if event.realized_pnl >= 0 else ''}{event.realized_pnl:.2f}."
        )
        self.record_decision("reflected", rationale, receipt, acted=True)

        await self._persist_reflection(
            event.trade_id, event.symbol, note,
            lesson_source=lesson_source, lesson_detail=lesson_detail,
        )

        await self.publish(
            ReflectionCompletedEvent(
                trade_id=event.trade_id,
                pnl=event.realized_pnl,
                lesson_learned=note,
                confidence_calibration_delta=delta,
            )
        )

    async def _persist_reflection(
        self,
        trade_id: str,
        symbol: str,
        content: str,
        lesson_source: Optional[str] = None,
        lesson_detail: Optional[str] = None,
    ):
        """Write the reflection, INCLUDING who wrote the lesson.

        `lesson_source` distinguishes a model-written lesson from one of the three
        deterministic templates. The Evaluation layer and the learning dashboard
        both read this table, and without the column they would average a canned
        string and a reasoned finding together as if they were the same kind of
        observation.

        NULL is left as NULL rather than defaulted to 'rules': rows written before
        the column existed have genuinely unknown provenance, and asserting one
        would be a fabricated fact about the system's own history.
        """
        pool = get_db_pool()
        if not pool: return

        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO reflections
                        (trade_id, ts, symbol, content, exit_context_used,
                         lesson_source, lesson_detail)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (trade_id) DO UPDATE SET
                        content = $4,
                        lesson_source = $6,
                        lesson_detail = $7
                    """,
                    trade_id, datetime.datetime.utcnow(), symbol, content,
                    "Auto-generated reflection", lesson_source, lesson_detail,
                )
        except Exception as e:
            logger.error(f"Failed to persist reflection for {trade_id}: {e}")

def get_reflection_agent() -> ReflectionAgent:
    return ReflectionAgent()
