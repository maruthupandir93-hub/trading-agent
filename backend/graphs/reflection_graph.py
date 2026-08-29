"""Graph 5 — Trade Reflection (spec Section 16 / Phase 33), on `TradingState`.

    Trade Closed -> Memory -> Execution Quality -> Outcome -> Lesson -> Store

WHAT CHANGED, AND WHY IT WAS THE LAST AUDIT ITEM
------------------------------------------------
This graph ran on its own `ReflectionState`. It was therefore the ONLY graph in
the system that did not go through `build_graph`, which meant no `NodeContract`
validation, no declared-write enforcement, and no run tracing. Spec Section 4 is
explicit that there should be one shared state:

    "Don't let every agent maintain its own ad-hoc state. Create one strongly
     typed TradingState that every node reads from and writes back to."

It was tolerable while every node here was deterministic. It stopped being
tolerable when a model started writing the lesson: an unconstrained node with an
LLM in it is precisely what the contract layer exists to prevent. A node on the
old state could have written the confidence calibration delta — which feeds
position sizing — and nothing would have objected.

THREE CONSEQUENCES OF THE MOVE
------------------------------
1. **`collect_context` is gone, not ported.** It read a symbol's memory, which is
   what `memory_loader` already does and does better — all seven Section 15
   stores, a typed `MemoryContext`, and per-store `unavailable` reasons rather
   than a bare dict. The graph reuses that node now. Deleting a duplicate is the
   engineering principle the spec states outright.

2. **The lesson is contract-isolated.** `reflection_lesson` is the only field the
   LLM node may write; `reflection` sits in `DETERMINISTIC_ONLY_FIELDS`.

3. **Runs are traced.** Every reflection now produces a `RunTrace` like every
   other graph, so "why did this trade produce that lesson" is answerable from
   the trace store rather than only from logs.

`produces_decision=False`: this graph's job is not to decide. Without that flag
`finish_run` would label every successful reflection "no decision produced".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from langgraph.graph import END

from backend.graphs.builder import GraphConfig, build_graph
from backend.graphs.nodes.memory_loader import (
    MEMORY_LOADER_NODE,
    register_memory_node,
)
from backend.graphs.nodes.reflection import (
    EXECUTION_NODE,
    LESSON_NODE,
    OUTCOME_NODE,
    STORE_NODE,
    register_reflection_nodes,
    rule_based_lesson,
)
from backend.graphs.runtime import finish_run, start_run
from backend.graphs.state import TradingState, TriggerReason
from backend.llm.budget import RunBudget

logger = logging.getLogger(__name__)

GRAPH_NAME = "trade_reflection"

_nodes_registered = False


def _ensure_nodes() -> None:
    """Register this graph's nodes once.

    Guarded by `get_contract`, not by a module flag alone — `clear_registry()`
    resets the flag for the graph modules it knows about, and a node that checked
    only its own flag would refuse to re-register after a clear and leave the
    graph unbuildable.
    """
    global _nodes_registered
    if _nodes_registered:
        return
    from backend.graphs.registry import get_contract

    if get_contract(MEMORY_LOADER_NODE) is None:
        register_memory_node()
    if get_contract(EXECUTION_NODE) is None:
        register_reflection_nodes()

    _nodes_registered = True


def reflection_config() -> GraphConfig:
    """Strictly linear. Every stage needs the one before it.

    No conditional edges and no fan-out: execution quality needs the receipt,
    the outcome needs the P&L, the lesson needs both, and the store needs the
    lesson. There is nothing here that can usefully run in parallel, and a
    superstep split would only add ways for a partial reflection to be stored.
    """
    _ensure_nodes()
    return GraphConfig(
        name=GRAPH_NAME,
        nodes=[MEMORY_LOADER_NODE, EXECUTION_NODE, OUTCOME_NODE, LESSON_NODE, STORE_NODE],
        entry=MEMORY_LOADER_NODE,
        edges=[
            (MEMORY_LOADER_NODE, EXECUTION_NODE),
            (EXECUTION_NODE, OUTCOME_NODE),
            (OUTCOME_NODE, LESSON_NODE),
            (LESSON_NODE, STORE_NODE),
            (STORE_NODE, END),
        ],
    )


async def run_reflection_graph(
    receipt: Dict[str, Any],
    checkpointer: Any = None,
    budget: Optional[RunBudget] = None,
) -> Dict[str, Any]:
    """Reflect on one closed trade. Never raises.

    NO CHECKPOINTER IS USED BY DEFAULT, and that is deliberate rather than an
    omission. A reflection is a single short pass over a trade that has already
    closed — there is no position to resume reasoning about and nothing a restart
    would need to continue. Graph 4 (monitoring) checkpoints because a POSITION
    outlives a process; this does not.

    `thread_scope` is the trade, so if a checkpointer is ever supplied the thread
    maps to the thing being reflected on rather than to a run id nothing can
    correlate.
    """
    _ensure_nodes()

    symbol = receipt.get("symbol") or "UNKNOWN"
    trade_id = receipt.get("trade_id") or receipt.get("tradeId") or "unknown"

    state, ctx, thread_id = start_run(
        graph=GRAPH_NAME,
        symbol=symbol,
        trigger=TriggerReason(
            kind="manual",
            symbol=symbol,
            detail=f"trade {trade_id} closed",
        ),
        thread_scope=f"trade:{trade_id}",
        budget=budget,
    )

    # Injected here rather than loaded by a node, so the graph has exactly one
    # source of truth for which trade it is reflecting on and cannot pick a
    # different one mid-run — the same reasoning as `monitored_position`.
    state["closed_trade"] = receipt

    try:
        graph = build_graph(reflection_config(), ctx, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}} if checkpointer else None
        final: TradingState = await (
            graph.ainvoke(state, config=config) if config else graph.ainvoke(state)
        )
    except Exception as e:
        logger.error("Reflection graph failed for trade %s: %s", trade_id, e)
        finish_run(ctx, None, outcome="failed",
                   no_decision_reason=f"graph error: {e}", produces_decision=False)
        return {"ok": False, "symbol": symbol, "error": str(e), "runId": ctx.run_id}

    trace = finish_run(ctx, final, produces_decision=False)
    return {"ok": True, "runId": ctx.run_id, **summarise_reflection(final),
            "traceOutcome": trace.outcome}


def summarise_reflection(state: TradingState) -> Dict[str, Any]:
    """The reflection's output shape.

    `lessonSource` is reported alongside the lesson, never folded into it. A
    template string and a model's analysis are different kinds of artefact and a
    reader must be able to tell which one they are looking at.
    """
    reflection = state.get("reflection")
    draft = state.get("reflection_lesson")

    return {
        "symbol": state.get("symbol"),
        "tradeId": None if reflection is None else reflection.trade_id,
        "outcome": None if reflection is None else reflection.outcome,
        "realizedPnl": None if reflection is None else reflection.realized_pnl,
        "executionQuality": None if reflection is None else reflection.execution_quality,
        "executionQualityDetail": (
            None if reflection is None else reflection.execution_quality_detail
        ),
        "attribution": [] if reflection is None else list(reflection.attribution),
        "confidenceCalibrationDelta": (
            None if reflection is None else reflection.confidence_calibration_delta
        ),
        "lesson": None if draft is None else draft.text,
        "lessonSource": None if draft is None else draft.source,
        "lessonDetail": None if draft is None else draft.detail,
        "ruleBasedLesson": None if draft is None else draft.rule_based,
        "unavailable": list(state.get("unavailable") or []),
    }


def lesson_from(state: TradingState) -> str:
    """The lesson text, with the deterministic one as the floor.

    Never returns an empty string: `ReflectionCompletedEvent.lesson_learned` feeds
    the HypothesisAgent, and an empty lesson would end the learning pipeline for
    that trade rather than degrade it.
    """
    draft = state.get("reflection_lesson")
    if draft is not None and draft.text:
        return draft.text

    reflection = state.get("reflection")
    if reflection is not None:
        return rule_based_lesson(reflection)
    return "No lesson generated."
