"""Phase 33 nodes — Trade Reflection, on the shared `TradingState`.

    Trade Closed -> Memory -> Execution Quality -> Outcome -> Lesson -> Store

WHAT THIS MIGRATION FIXED
-------------------------
`graphs/reflection_graph.py` ran on its own `ReflectionState`, which meant it was
the one graph in the system that did NOT go through `build_graph` — so it had no
`NodeContract` validation, no declared-write enforcement and no run tracing. It
was the last open item of the Sections 14-41 audit, and it mattered more once a
model started writing the lesson: an unconstrained node with an LLM in it is
exactly the combination the contract layer exists to prevent.

Three things came out of the move, none of them cosmetic:

1. **`collect_context` was deleted, not ported.** It fetched memory for a symbol
   — which is precisely what `memory_loader` already does, and does better: all
   seven Section 15 stores, a typed `MemoryContext`, and per-store `unavailable`
   reasons instead of a bare dict. Two implementations of "read this symbol's
   memory" is the duplication the spec's engineering principles forbid, and the
   reflection graph now reuses the node rather than keeping its own copy.

2. **The lesson is contract-isolated.** `reflection_lesson` is the only field the
   LLM node may write. `reflection` is in `DETERMINISTIC_ONLY_FIELDS`, so a model
   cannot reach `confidence_calibration_delta` — which feeds confidence
   calibration and therefore position sizing. Before this, that separation rested
   on the node body being written carefully.

3. **Every write is now declared and enforced.** A node returning a key it did
   not declare raises `NodeContractViolation` instead of silently mutating state.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from backend.graphs.contracts import NodeContract
from backend.graphs.registry import register_node
from backend.graphs.state import LessonDraft, TradeReflection, TradingState

logger = logging.getLogger(__name__)

EXECUTION_NODE = "reflection_execution_quality"
OUTCOME_NODE = "reflection_outcome"
LESSON_NODE = "reflection_lesson"
STORE_NODE = "reflection_store"


def _receipt(state: TradingState) -> Dict[str, Any]:
    return state.get("closed_trade") or {}


def _reflection(state: TradingState) -> TradeReflection:
    """The reflection built so far, or a new one.

    Returns a COPY rather than mutating in place. These nodes run sequentially so
    in-place mutation would work, but a node that mutates shared state instead of
    returning a delta is invisible to `validate_node_output` — the contract layer
    can only check what a node RETURNS.
    """
    import copy

    existing = state.get("reflection")
    return copy.deepcopy(existing) if existing is not None else TradeReflection()


# ---------------------------------------------------------------------------
# Execution quality — measured, or honestly absent
# ---------------------------------------------------------------------------


async def assess_execution_quality(state: TradingState) -> Dict[str, Any]:
    """Read the MEASURED execution score, or report that there is none.

    This was once `state["execution_quality"] = "Good"` unconditionally — every
    trade in the system's history graded itself Good, the same class of bug as
    slippage hardcoded to 0.0 giving every fill a perfect score.

    It matters more than it looks: the lesson and the confidence calibration both
    read this, so a permanent "Good" means execution is never identified as the
    cause of a loss and the system can never learn that it is filling badly.

    `execution_quality` in the `execution_quality` table is deliberately NULLABLE
    — a fill with no reference price is not a bad fill. That distinction is
    preserved here: 'unavailable' is NOT 'Poor'.
    """
    receipt = _receipt(state)
    reflection = _reflection(state)
    reflection.trade_id = receipt.get("trade_id") or receipt.get("tradeId")
    pnl = receipt.get("pnl")
    reflection.realized_pnl = float(pnl) if pnl is not None else None

    order_id = receipt.get("orderId") or receipt.get("order_id")

    if not order_id:
        reflection.execution_quality = "unavailable"
        reflection.execution_quality_detail = (
            "no order id on the trade receipt, so the persisted execution score "
            "cannot be looked up"
        )
        return {"reflection": reflection}

    try:
        from backend.core.db import get_db_pool

        pool = get_db_pool()
        if pool is None:
            reflection.execution_quality = "unavailable"
            reflection.execution_quality_detail = (
                "no database pool — execution_quality lives in Postgres, which is "
                "not provisioned by default. This is NOT a good fill."
            )
            return {"reflection": reflection}

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT score, slippage_bps, latency_ms, fully_filled, notes "
                "FROM execution_quality WHERE order_id = $1",
                str(order_id),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Execution quality lookup failed for %s: %s", order_id, exc)
        reflection.execution_quality = "unavailable"
        reflection.execution_quality_detail = f"lookup failed: {exc}"
        return {"reflection": reflection}

    if row is None or row["score"] is None:
        # A NULL score means not measurable, which the Evaluation layer must
        # exclude from averages rather than treat as zero.
        reflection.execution_quality = "unavailable"
        reflection.execution_quality_detail = (
            f"no measurable score for order {order_id}"
            + ("" if row is None else " (score is NULL — no reference price)")
        )
        return {"reflection": reflection}

    score = float(row["score"])
    reflection.execution_quality = (
        "Good" if score >= 0.7 else "Fair" if score >= 0.4 else "Poor"
    )
    reflection.execution_quality_detail = (
        f"score {score:.3f} (slippage {row['slippage_bps']} bps, "
        f"latency {row['latency_ms']} ms, fully filled={row['fully_filled']})"
    )
    return {"reflection": reflection}


# ---------------------------------------------------------------------------
# Outcome classification and attribution
# ---------------------------------------------------------------------------


def classify_outcome(state: TradingState) -> Dict[str, Any]:
    """Classify the trade and attribute it, deterministically.

    Kept as rules rather than handed to the model on purpose: "did this trade make
    money" is arithmetic, and the attribution is a reproducible mapping from the
    strategies that were active. Both must be comparable across trades, which
    prose is not.
    """
    receipt = _receipt(state)
    reflection = _reflection(state)

    pnl = float(receipt.get("pnl", 0.0) or 0.0)
    won = pnl >= 0
    strategies = receipt.get("strategies") or []

    reflection.outcome = "Success" if won else "Failure"

    attribution: List[str] = []
    if not won:
        if "trend" in strategies and "mean_reversion" not in strategies:
            attribution.append("Trend entry failed, possible mean reversion or false breakout.")
        elif "breakout" in strategies:
            attribution.append("Breakout failed, possible false breakout.")
    reflection.attribution = attribution

    # Computed HERE, by a deterministic node, and never by the model.
    #
    # Delegated to `reflection_agent`, which owns the formula. Two copies of a
    # calibration rule that feeds position sizing would drift — this graph
    # originally copied the expression verbatim.
    from backend.agents.reflection_agent import calibration_delta

    reflection.confidence_calibration_delta = calibration_delta(pnl)

    return {"reflection": reflection}


# ---------------------------------------------------------------------------
# The lesson — the one node a model may write
# ---------------------------------------------------------------------------

_LESSON_SYSTEM_PROMPT = (
    "You write one lesson from a trade that has ALREADY closed. You are not "
    "deciding anything and no trade depends on your answer.\n"
    "Rules:\n"
    "- Use ONLY the facts supplied. Never introduce an indicator, price, level or "
    "market condition that is not in the input.\n"
    "- Anything marked 'unavailable' is UNKNOWN. Say it is unknown. Never treat "
    "an unavailable execution score as a good or bad fill, and never guess why.\n"
    "- One lesson, and it must be TESTABLE — a specific claim someone could "
    "check against history. 'Manage risk better' is not a lesson. 'Trend entries "
    "taken within 30 minutes of a funding flip lost more often' is.\n"
    "- A single trade is weak evidence. Say what should be CHECKED, not what "
    "should be changed.\n"
    "- Never recommend a change to position size, leverage, or which strategy is "
    "enabled. Those are not yours to propose.\n"
    "- 1 to 3 sentences. No preamble, no headings, no restating the numbers back."
)


def rule_based_lesson(reflection: TradeReflection) -> str:
    """The deterministic lesson. Always available, never removed.

    Two reasons it stays the floor rather than being replaced by the model:

    1. `ReflectionCompletedEvent.lesson_learned` drives the HypothesisAgent, which
       queues research. A reflection producing no lesson would silently end the
       learning pipeline — the dead-end the hypothesis agent was added to close.
    2. Most deployments run with no LLM configured. If the only lesson path needed
       a model, "self-learning" would be a feature that is off by default.
    """
    if reflection.outcome == "Success":
        return (
            "No strict recommendation from a single winning trade. Continue monitoring "
            "repeatability."
        )
    if reflection.attribution:
        return f"Check if {reflection.attribution[0]} correlates with this regime."
    return "Check if losses cluster in this regime before changing weighting."


def _lesson_prompt(state: TradingState, reflection: TradeReflection) -> str:
    """Build the prompt from measured facts only.

    Deliberately no raw candles and no price history: the question is "what does
    this outcome suggest we check", and handing over the market would invite the
    model to form its own view of what happened — which a single closed trade
    cannot support and which is not what is being asked.
    """
    receipt = _receipt(state)
    pnl = reflection.realized_pnl

    lines = [
        f"Symbol: {receipt.get('symbol', state.get('symbol', 'unknown'))}",
        f"Side: {receipt.get('side', 'unknown')}",
        f"Outcome: {reflection.outcome} "
        f"(realized P&L {pnl:+.2f})" if pnl is not None else f"Outcome: {reflection.outcome}",
        f"Entry: {receipt.get('entry_price')}  Exit: {receipt.get('exit_price')}",
        f"Exit reason: {receipt.get('exit_reason', 'unknown')}",
        f"Held: {receipt.get('held_seconds')} seconds",
        f"Strategies active: {', '.join(receipt.get('strategies') or []) or 'none recorded'}",
        f"Execution quality: {reflection.execution_quality or 'unavailable'}"
        f" — {reflection.execution_quality_detail or 'no detail'}",
        "Deterministic attribution: "
        + ("; ".join(reflection.attribution) if reflection.attribution else "none"),
    ]

    memory = state.get("memory_context")
    if memory is not None:
        if getattr(memory, "unavailable", None):
            # Listed explicitly. A model that does not know what is missing will
            # reason as though nothing is.
            lines.append("UNAVAILABLE (do not speculate about these): "
                         + "; ".join(map(str, memory.unavailable))[:400])
        for label, value in (("Recent lessons", getattr(memory, "semantic", None)),
                             ("Past risk events", getattr(memory, "risk_events", None)),
                             ("Strategy performance", getattr(memory, "strategy_performance", None))):
            if value:
                lines.append(f"{label}: {str(value)[:400]}")

    return "\n".join(lines)


async def write_lesson(state: TradingState) -> Dict[str, Any]:
    """Write the lesson — from a model when one is configured, from rules otherwise.

    WRITES `reflection_lesson` AND NOTHING ELSE. `reflection` is in
    `DETERMINISTIC_ONLY_FIELDS`, so `NodeContract` refuses at registration if this
    node ever declares it — which is what stops a model reaching
    `confidence_calibration_delta` and, through it, position sizing.

    The lesson is UNDERSTANDING, not deployment (invariant 5). It lands in
    semantic memory and reaches the HypothesisAgent, which proposes research.
    Nothing on that path writes production strategy config, and the prompt
    forbids recommending a sizing or strategy change so the text cannot smuggle
    one in.
    """
    from backend.llm.provider import (
        DEFAULT_TEMPERATURE,
        ModelTier,
        get_provider,
        request_budget,
    )

    reflection = state.get("reflection") or TradeReflection()
    fallback = rule_based_lesson(reflection)

    provider = get_provider()
    if not provider.available:
        return {
            "reflection_lesson": LessonDraft(
                text=fallback,
                source="rules",
                detail=(
                    f"no LLM provider configured (provider '{provider.name}' reports "
                    f"unavailable), so the deterministic lesson was used"
                ),
                rule_based=fallback,
            )
        }

    result = await provider.complete(
        system=_LESSON_SYSTEM_PROMPT,
        user=_lesson_prompt(state, reflection),
        tier=ModelTier.NARRATIVE,
        # 300 tokens of LESSON, plus scratchpad room. See `request_budget`.
        max_tokens=request_budget(300),
        temperature=DEFAULT_TEMPERATURE,
    )

    delta: Dict[str, Any] = {
        # Budget accounting happens whether or not the call succeeded: a failed
        # call still consumed a request.
        "llm_calls_made": (state.get("llm_calls_made") or 0) + 1,
        "llm_tokens_used": (state.get("llm_tokens_used") or 0) + result.total_tokens,
    }

    if not result.ok:
        # Degrades to the rule-based lesson, never to nothing. A failed model call
        # must not end the learning pipeline for that trade.
        logger.warning(
            "Reflection lesson fell back to rules for %s: %s",
            _receipt(state).get("symbol"), result.error,
        )
        delta["reflection_lesson"] = LessonDraft(
            text=fallback, source="rules",
            detail=f"model call failed ({result.error}); deterministic lesson used",
            rule_based=fallback,
        )
        return delta

    delta["reflection_lesson"] = LessonDraft(
        text=result.text,
        source="model",
        detail=(
            f"{result.model or 'model'}, {result.total_tokens} tokens"
            + (f", {result.latency_ms:.0f}ms" if result.latency_ms is not None else "")
        ),
        rule_based=fallback,
    )
    return delta


# ---------------------------------------------------------------------------
# Store — the only node with side effects
# ---------------------------------------------------------------------------


async def store_lesson(state: TradingState) -> Dict[str, Any]:
    """Write the lesson into semantic memory and link it to its strategies.

    DECLARES NO WRITES. Its whole job is the side effect, and declaring a write it
    does not make would be a contract that describes something untrue.

    Provenance is stored WITH the lesson rather than beside it: these rows are read
    back months later by the learning dashboard and by the memory queries the
    roadmap describes, and a template string and a model's analysis carry very
    different weight as evidence. Once in the graph there is no way to tell them
    apart after the fact.
    """
    from backend.services.semantic_memory import add_relationship, upsert_entity

    receipt = _receipt(state)
    reflection = state.get("reflection") or TradeReflection()
    draft = state.get("reflection_lesson") or LessonDraft()

    lesson_text = draft.text or rule_based_lesson(reflection)
    lesson_id = f"lesson_{int(time.time())}"

    try:
        await upsert_entity(
            entity_id=lesson_id,
            entity_type="TradeLesson",
            properties={
                "lesson": lesson_text,
                "outcome": reflection.outcome or "unknown",
                "trade_symbol": receipt.get("symbol", state.get("symbol", "UNKNOWN")),
                "lesson_source": draft.source,
                "lesson_detail": draft.detail or "",
                "execution_quality": reflection.execution_quality or "unavailable",
            },
        )

        for strategy in receipt.get("strategies") or []:
            await upsert_entity(strategy, "Strategy", {"name": strategy})
            await add_relationship(strategy, lesson_id, "has_lesson", weight=1.0)
    except Exception as exc:  # noqa: BLE001
        # Recorded, not raised. The reflection is already complete and its lesson
        # is already in state; losing the memory write costs a future query, not
        # this run.
        logger.error("Reflection could not store lesson %s: %s", lesson_id, exc)

    return {}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_reflection_nodes() -> None:
    """Register unconditionally. The caller owns the idempotence check.

    No module-level flag — see `nodes/consultation.py` for the bug that pattern
    causes when `registry.clear_registry()` runs.
    """
    register_node(
        NodeContract(
            name=EXECUTION_NODE,
            reads=("closed_trade", "reflection"),
            writes=("reflection",),
            purpose="Read the measured execution score, or report that there is none.",
            deterministic=True,
            phase=33,
        ),
        assess_execution_quality,
    )

    register_node(
        NodeContract(
            name=OUTCOME_NODE,
            reads=("closed_trade", "reflection"),
            writes=("reflection",),
            purpose=(
                "Classify the outcome, attribute it, and compute the confidence "
                "calibration delta. Deterministic — the delta feeds position sizing."
            ),
            deterministic=True,
            phase=33,
        ),
        classify_outcome,
    )

    register_node(
        NodeContract(
            name=LESSON_NODE,
            reads=("closed_trade", "reflection", "memory_context", "symbol",
                   "llm_calls_made", "llm_tokens_used"),
            # `reflection_lesson` ONLY. `reflection` is deterministic-only, so
            # NodeContract raises at registration if this ever declares it.
            writes=("reflection_lesson", "llm_calls_made", "llm_tokens_used"),
            purpose="Write one testable lesson from the closed trade. Cannot change any number.",
            deterministic=False,
            may_call_llm=True,
            phase=33,
        ),
        write_lesson,
    )

    register_node(
        NodeContract(
            name=STORE_NODE,
            reads=("closed_trade", "reflection", "reflection_lesson", "symbol"),
            # Side effects only.
            writes=(),
            purpose="Persist the lesson to semantic memory and link it to its strategies.",
            deterministic=True,
            phase=33,
        ),
        store_lesson,
    )
