from typing import Literal, Dict, Any, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from uuid import UUID, uuid4

EventType = Literal[
    'TICK_RECEIVED',
    'FEATURES_COMPUTED',
    'MARKET_STRUCTURE_ANALYZED',
    'LIQUIDITY_ANALYZED',
    'FUNDING_ANALYZED',
    'NEWS_ANALYZED',
    'MACRO_ANALYZED',
    'SIGNAL_GENERATED',
    'DEBATE_CONCLUDED',
    'CONFIDENCE_CALIBRATED',
    'RISK_EVALUATED',
    'STRESS_TESTED',
    'TAR_SUBMITTED',
    'TAR_APPROVED',
    'TAR_REJECTED',
    'ORDER_ROUTED',
    'ORDER_FILLED',
    'POSITION_CLOSED',
    'REFLECTION_COMPLETED',
    # Phase 31 / LangGraph spec Section 14. Published when a market condition
    # crosses a threshold and a reasoning run is warranted — or when one was
    # detected and deliberately SUPPRESSED by debounce or the rate ceiling.
    # Suppressions are published too: "we saw it and chose not to act" is a
    # different fact from "we never saw it", and only one of those is a bug.
    'TRIGGER_FIRED',
    # Phase 29 / LangGraph spec Section 12. The INERT boundary crossing:
    #
    #   LangGraph -> ExecutionRequest -> Risk Gateway -> Execution Service
    #             -> Exchange -> Order Confirmation -> Event Bus -> Monitoring
    #
    # Published by the analysis graph when the Risk Gateway approved a plan, and
    # consumed by `services/execution_service.py`. Deliberately NOT a TAR: this
    # event carries no authority and nothing about it is an instruction to trade.
    # A graph may say "here is an approved plan"; only the CRO may say "execute".
    'EXECUTION_PLAN_READY',
    # Phase 39.5 / LangGraph spec Section 39.5. One pair per graph NODE.
    #
    #     "LangGraph supports streaming state updates, node transitions ... For a
    #      trading dashboard this matters more than in most agent applications —
    #      'the AI is currently in multi_agent_analysis, 4 of 6 specialists
    #      reporting' is exactly the kind of live visibility that makes a 24/7
    #      autonomous system trustworthy to watch, versus a black box that
    #      occasionally reports a trade after the fact."
    #
    # WHY THESE HAD TO EXIST AS BUS EVENTS AND NOT ONLY AS A STREAM.
    # `graphs/runtime.stream_run` already yielded per-node progress, but it is an
    # async generator with exactly one caller: the `/api/graphs/stream` WebSocket.
    # The browser CANNOT open that socket (https page, no TLS on the backend —
    # see CLAUDE.md), so on the real deployment nothing consumed it and the
    # autonomous loop did not use it at all.
    #
    # Meanwhile `lib/realtime/store.ts` routes GRAPH_NODE_STARTED /
    # GRAPH_NODE_COMPLETED / GRAPH_NODE_FAILED into its `nodes` slice and derives
    # `currentNode` from them — and nothing in this backend had ever published
    # one. So the Dashboard's "Agent status — decision pipeline", the home page's
    # Execution Cycle stepper and the Agent Ensemble were wired to an event that
    # did not exist, and sat frozen while 21 nodes ran.
    #
    # Published from `wrap_node`, which every graph's every node already goes
    # through, so a new node is observable without remembering to instrument it.
    'GRAPH_NODE_STARTED',
    'GRAPH_NODE_COMPLETED',
    'GRAPH_NODE_FAILED',
]

class BaseEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_type: EventType

# 1. TICK_RECEIVED
class TickReceivedEvent(BaseEvent):
    event_type: Literal['TICK_RECEIVED'] = 'TICK_RECEIVED'
    symbol: str
    price: float
    volume: float
    exchange: str

# 2. FEATURES_COMPUTED
class FeaturesComputedEvent(BaseEvent):
    event_type: Literal['FEATURES_COMPUTED'] = 'FEATURES_COMPUTED'
    symbol: str
    timeframe: str
    features: Dict[str, Any]

class MarketStructureAnalyzedEvent(BaseEvent):
    event_type: Literal['MARKET_STRUCTURE_ANALYZED'] = 'MARKET_STRUCTURE_ANALYZED'
    symbol: str
    structure_data: Dict[str, Any]

class LiquidityAnalyzedEvent(BaseEvent):
    event_type: Literal['LIQUIDITY_ANALYZED'] = 'LIQUIDITY_ANALYZED'
    symbol: str
    liquidity_data: Dict[str, Any]

class FundingAnalyzedEvent(BaseEvent):
    event_type: Literal['FUNDING_ANALYZED'] = 'FUNDING_ANALYZED'
    symbol: str
    funding_data: Dict[str, Any]

class NewsAnalyzedEvent(BaseEvent):
    event_type: Literal['NEWS_ANALYZED'] = 'NEWS_ANALYZED'
    symbol: str
    news_data: Dict[str, Any]

class MacroAnalyzedEvent(BaseEvent):
    event_type: Literal['MACRO_ANALYZED'] = 'MACRO_ANALYZED'
    symbol: str
    macro_data: Dict[str, Any]

# 3. SIGNAL_GENERATED
class SignalGeneratedEvent(BaseEvent):
    event_type: Literal['SIGNAL_GENERATED'] = 'SIGNAL_GENERATED'
    agent_id: str
    symbol: str
    strategy: str
    direction: Literal['LONG', 'SHORT', 'NEUTRAL']
    confidence: float
    rationale: str

# 4. DEBATE_CONCLUDED
class DebateConcludedEvent(BaseEvent):
    event_type: Literal['DEBATE_CONCLUDED'] = 'DEBATE_CONCLUDED'
    symbol: str
    winning_direction: Literal['LONG', 'SHORT', 'NEUTRAL']
    consensus_confidence: float
    participants: List[str]
    supervisor_rationale: str

class ConfidenceCalibratedEvent(BaseEvent):
    event_type: Literal['CONFIDENCE_CALIBRATED'] = 'CONFIDENCE_CALIBRATED'
    symbol: str
    calibrated_confidence: float
    breakdown: Dict[str, float]

class RiskEvaluatedEvent(BaseEvent):
    event_type: Literal['RISK_EVALUATED'] = 'RISK_EVALUATED'
    symbol: str
    risk_score: float
    warnings: List[str]

class StressTestedEvent(BaseEvent):
    event_type: Literal['STRESS_TESTED'] = 'STRESS_TESTED'
    symbol: str
    passed: bool
    results: Dict[str, Any]

# 5. TAR_SUBMITTED (Trade Authorization Request)
#
# `stop_loss` and `tab` are REQUIRED fields, deliberately with no defaults.
#
# CLAUDE.md invariant 3 says every position requires a computed stop-loss.
# Previously the TAR events carried no stop at all, so the Execution Engine
# routed a bare market order and nothing downstream could enforce the
# invariant even in principle — the information simply wasn't in the message.
# Making it a required Pydantic field means a TAR that cannot state its stop
# fails construction, at the Supervisor, before the CRO ever sees it. An
# Optional field with a None default would have let the old behaviour back in
# silently.
#
# `tab` is required for the same reason: the leverage ceiling and the trade's
# persisted destination both depend on whether this is paper or real money,
# and defaulting that is how a paper trade ends up recorded as a real one.
class TarSubmittedEvent(BaseEvent):
    event_type: Literal['TAR_SUBMITTED'] = 'TAR_SUBMITTED'
    tar_id: UUID = Field(default_factory=uuid4)
    symbol: str
    direction: Literal['LONG', 'SHORT']
    requested_size: float
    requested_leverage: int
    strategy: str
    supervisor_rationale: str
    stop_loss: float
    tab: Literal['paper', 'real']
    take_profit: Optional[float] = None
    # THE GRAPH RUN THAT PRODUCED THIS TRADE.
    #
    # `ExecutionPlanReadyEvent` already carried it, described as "for
    # traceability back to the reasoning that produced this" — and then the TAR
    # dropped it one hop later, so by the time `ExecutionAgent` wrote the trade
    # row there was nothing left to trace to. That is why the trade-detail
    # page's "How this trade happened" could only ever show the entry and the
    # outcome: the middle of the story was not lost, it was never carried.
    run_id: Optional[str] = None
    # WHICH STRATEGY CHOSE THIS TRADE. Carried for the same reason and needed for
    # a second one: without it, no realised win rate can be attributed to a
    # strategy, and `historical_success_rate` stays None forever — which is
    # exactly why strategy scoring has never used its own results.
    strategy: Optional[str] = None
    # What the agent saw at decision time. See `ExecutionPlan.entry_context`.
    entry_context: Optional[str] = None
    entry_price: Optional[float] = None

# 6. TAR_APPROVED / TAR_REJECTED
class TarApprovedEvent(BaseEvent):
    event_type: Literal['TAR_APPROVED'] = 'TAR_APPROVED'
    tar_id: UUID
    symbol: str
    direction: str
    approved_size: float
    approved_leverage: int
    cro_rationale: str
    # Carried through from the TAR so the Execution Engine can attach the
    # protective stop to the position it just opened. If this did not travel
    # with the approval, Execution would have to re-derive it and could
    # legitimately arrive at a different number than the one Risk approved.
    stop_loss: float
    tab: Literal['paper', 'real']
    take_profit: Optional[float] = None
    # ATTRIBUTION. THESE THREE WERE MISSING, AND THAT ONE OMISSION DISABLED THE
    # LEARNING LOOP, THE "HOW THIS TRADE HAPPENED" VIEW AND RUN TRACEABILITY —
    # on every trade the agent has ever made.
    #
    # `TarSubmittedEvent` declares all three and `cro_agent` passes all three to
    # this constructor. Pydantic IGNORES unknown keyword arguments by default, so
    # nothing raised: the CRO believed it forwarded them, and the event that came
    # out the other side simply did not have them.
    #
    # It stayed hidden because every reader is defensive —
    # `getattr(tar, "strategy", None)` in `execution_agent` and in the position
    # monitor. A defensive read of a field that does not exist is
    # indistinguishable from a field that is legitimately absent, so the whole
    # chain reported None and looked like it was working.
    #
    # Measured on the live database before the fix: `strategy`, `run_id` and
    # `entry_context` were NULL on all 30 trade rows, opening and closing alike,
    # and `strategy_performance.aggregate()` returned {} on an account that had
    # traded for three days.
    run_id: Optional[str] = None
    strategy: Optional[str] = None
    entry_context: Optional[str] = None

class TarRejectedEvent(BaseEvent):
    event_type: Literal['TAR_REJECTED'] = 'TAR_REJECTED'
    tar_id: UUID
    rule_breached: str
    cro_rationale: str

# 7. ORDER_ROUTED
class OrderRoutedEvent(BaseEvent):
    event_type: Literal['ORDER_ROUTED'] = 'ORDER_ROUTED'
    tar_id: UUID
    exchange: str
    order_id: str
    order_type: str
    price: Optional[float]
    quantity: float

# 8. ORDER_FILLED
#
# `symbol`, `side` and `tab` are required. They were absent, and
# `agents/reflection_agent.py` worked around that by hardcoding
# `"symbol": "BTC/USDT"` with the comment "Mocking symbol since
# OrderFilledEvent doesn't carry it (only tar_id)". Every reflection in the
# system was therefore attributed to BTC/USDT regardless of what actually
# traded — and those reflections feed the accuracy figure the Confidence
# Agent calibrates against, so fabricated attribution propagated into
# position sizing. Carrying the fields removes the reason to invent them.
class OrderFilledEvent(BaseEvent):
    event_type: Literal['ORDER_FILLED'] = 'ORDER_FILLED'
    tar_id: UUID
    exchange: str
    order_id: str
    symbol: str
    side: Literal['buy', 'sell']
    tab: Literal['paper', 'real']
    fill_price: float
    fill_quantity: float
    slippage_bps: float
    fee: float


# 8b. POSITION_CLOSED — the event the learning pipeline actually needs.
#
# Spec Section 12 requires that "every COMPLETED trade" generates a
# reflection. There was no completion event: the Reflection agent listened to
# ORDER_FILLED, which fires when a position OPENS, and supplied a hardcoded
# `pnl: -50.0` ("Mock negative PNL to trigger reflection") because an opening
# fill has no P&L to report. So the learning system was reflecting on entries
# using an invented loss.
#
# `realized_pnl` is required and has no default, so a close cannot be
# announced without stating its actual outcome.
class PositionClosedEvent(BaseEvent):
    event_type: Literal['POSITION_CLOSED'] = 'POSITION_CLOSED'
    trade_id: str
    symbol: str
    side: Literal['buy', 'sell']
    tab: Literal['paper', 'real']
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    # Why it closed: 'take-profit' | 'stop-loss' | 'thesis-invalidated' |
    # 'manual' | 'liquidation'. Labelling a thesis-driven exit as a stop
    # misreports WHY the position closed, which is exactly the information
    # the reflection then learns from.
    exit_reason: str
    strategies: List[str] = Field(default_factory=list)
    held_seconds: Optional[float] = None
    # ATTRIBUTION, carried to the reflection so it can name a REAL cause instead
    # of the generic template. Without `entry_context` the reflection reasons over
    # symbol/side/pnl alone — which cannot distinguish "stopped inside the noise
    # band in a range" from "counter-trend entry against the 4h" — and falls back
    # to "check if losses cluster in this regime". These are the same three fields
    # the trade row already records; the reflection was the one reader not given
    # them.
    strategy: Optional[str] = None
    run_id: Optional[str] = None
    entry_context: Optional[str] = None

# 8c. TRIGGER_FIRED — Phase 31 (spec Section 14)
#
#     "Your agent should continuously ask 'did anything change?' — not just run
#      on a timer. Use event triggers, not polling. ... These generate graph
#      runs. This is far more efficient than 'every 5 minutes → run LLM.'"
#
# `acted` is required and has no default. A trigger that was detected but
# suppressed still gets published, because an operator asking "why didn't the
# system react to that move?" needs to distinguish a missed detection from a
# deliberate suppression. Defaulting `acted=True` would hide every suppression.
class TriggerFiredEvent(BaseEvent):
    event_type: Literal['TRIGGER_FIRED'] = 'TRIGGER_FIRED'
    symbol: str
    kind: str
    detail: str
    acted: bool
    observed_value: Optional[float] = None
    threshold: Optional[float] = None
    # Set exactly when acted is False.
    suppressed_reason: Optional[str] = None
    # The graph run this trigger started, when it started one.
    run_id: Optional[str] = None


# 8d. EXECUTION_PLAN_READY — Phase 29 (spec Section 12)
#
#     "LangGraph generates an execution request; execution happens OUTSIDE
#      LangGraph."
#
# The boundary object, as an event. It is a REQUEST, not an approval and not an
# order: no field on it grants authority, and the Execution Service re-validates
# everything on it rather than trusting that a gateway ran.
#
# `intent` is required and has no default. An open and a close travel completely
# different paths downstream — an open goes through the CRO, a close must NOT
# (CLAUDE.md invariant 4) — so a defaulted intent would decide the most
# safety-critical routing question in this system by omission.
class ExecutionPlanReadyEvent(BaseEvent):
    event_type: Literal['EXECUTION_PLAN_READY'] = 'EXECUTION_PLAN_READY'
    symbol: str
    intent: Literal['open', 'close']
    side: Literal['buy', 'sell']
    tab: Literal['paper', 'real']
    # Derived from decision identity, never from a thread id (Section 39.3). The
    # Execution Service keys its duplicate-submission guard off this.
    idempotency_basis: str
    # None on a close whose held quantity could not be read — the executor sizes
    # it. Never a guessed number.
    size: Optional[float] = None
    leverage: Optional[int] = None
    # Required for an OPEN, and the service rejects an open without it
    # (invariant 3). Always None on a close: the close IS the exit.
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    # For traceability back to the reasoning that produced this.
    run_id: Optional[str] = None
    strategy: Optional[str] = None
    rationale: Optional[str] = None
    entry_price: Optional[float] = None
    entry_context: Optional[str] = None


# 9. REFLECTION_COMPLETED
class ReflectionCompletedEvent(BaseEvent):
    event_type: Literal['REFLECTION_COMPLETED'] = 'REFLECTION_COMPLETED'
    trade_id: str
    pnl: float
    lesson_learned: str
    confidence_calibration_delta: float


# ---------------------------------------------------------------------------
# 10. GRAPH_NODE_* — per-node execution progress (Section 39.5)
# ---------------------------------------------------------------------------
#
# THE FIELD NAMES ARE THE FRONTEND'S CONTRACT, NOT A CHOICE.
# `lib/realtime/store.ts::route()` reads the base of each event with
# `str(e, 'node', 'node_name', 'name')`, `str(e, 'status')`,
# `num(e, 'duration_ms', 'durationMs')` and `str(e, 'detail', 'summary', 'out')`.
# Renaming any of these silently empties the pipeline view again, with no error
# anywhere — which is exactly the failure mode this event was added to fix. There
# is a test pinning the names.
#
# `status` is carried explicitly even though the store can infer it from the
# event type. The store prefers an explicit status when present, and stating it
# means a future SKIPPED or WAITING state does not need a fourth event type.
class GraphNodeStartedEvent(BaseEvent):
    event_type: Literal['GRAPH_NODE_STARTED'] = 'GRAPH_NODE_STARTED'
    node: str
    status: Literal['RUNNING'] = 'RUNNING'
    # The graph and run this node belongs to. Two graphs can run concurrently for
    # two symbols, and a viewer needs to know which pipeline moved.
    graph: str
    run_id: str
    symbol: Optional[str] = None


class GraphNodeCompletedEvent(BaseEvent):
    event_type: Literal['GRAPH_NODE_COMPLETED'] = 'GRAPH_NODE_COMPLETED'
    node: str
    status: Literal['COMPLETED'] = 'COMPLETED'
    graph: str
    run_id: str
    symbol: Optional[str] = None
    # Wall-clock milliseconds. Never None on a completion — the runtime measures
    # it — but Optional so a future caller cannot be forced to invent one.
    duration_ms: Optional[float] = None
    # The state keys this node wrote, joined. The store shows it as the node's
    # `detail` line. Keys, not values: the state carries candles and a portfolio
    # snapshot, and pushing those through an event stream once per node would put
    # megabytes on the wire for a 21-node run.
    detail: Optional[str] = None
    # Counts, not contents — the reasons are in the run trace.
    unavailable_count: int = 0


class GraphNodeFailedEvent(BaseEvent):
    event_type: Literal['GRAPH_NODE_FAILED'] = 'GRAPH_NODE_FAILED'
    node: str
    status: Literal['FAILED'] = 'FAILED'
    graph: str
    run_id: str
    symbol: Optional[str] = None
    duration_ms: Optional[float] = None
    # The exception text. Carried in `detail` because that is the field the store
    # already renders for a node, so a failure explains itself in the same place a
    # success does rather than needing its own display path.
    detail: Optional[str] = None
