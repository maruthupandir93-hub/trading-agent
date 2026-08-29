"""External consultation node — spec Section 31 (Phase 48), "ask another model".

    Supervisor -> (uncertainty high) -> Consultation Router -> Model A / Model B
               -> Evidence Aggregator -> Supervisor

WHERE THIS NODE SITS, AND WHY THAT IS THE WHOLE SAFETY ARGUMENT
--------------------------------------------------------------
    ... debate -> supervisor -> risk_gateway -> external_consultation -> narrative

It runs AFTER the decision and AFTER the risk gateway.

That ordering is not a convenience. Section 31 says "the external AI response is
advisory evidence, not authority", and placing this node after both gates makes
that STRUCTURAL rather than documented: by the time it runs, `decision` and
`risk_assessment` are already written and no later node re-derives them. An
external model cannot influence what it cannot precede.

The alternative — consulting before the Supervisor, as the diagram in Section 31
draws it — would put external opinions in state while the decision was still
being made. Even with the Supervisor not reading them, that is one careless
`reads=` tuple away from an outside model steering a trade. The diagram describes
the intent; this ordering enforces it.

WHAT IT WRITES
--------------
`consultation` only — a plain dict from `ConsultationResult.aggregate()`, which
is deliberately built to contain nothing a gate can read. `NodeContract` enforces
the single write, so this node cannot touch `decision`, `confidence`,
`risk_assessment` or anything else.

WHEN IT RUNS
------------
Only when `should_consult()` says the decision is genuinely uncertain — inside a
confidence band, or when internal components disagree on direction. Consulting on
every run would spend the tens of thousands of tokens per cycle that Section 39.6
warns about, on the runs where the internal evidence is already one-sided and an
outside view would change nothing.

It is also OFF unless a panel is configured (`LLM_CONSULT_PANEL`). Absent, the
node records why and the run continues — a missing second opinion costs nothing
that any gate reads.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.graphs.contracts import NodeContract
from backend.graphs.registry import register_node
from backend.graphs.state import TradingState

logger = logging.getLogger(__name__)

NODE_NAME = "external_consultation"


def _internal_confidence(state: TradingState) -> Optional[float]:
    """The confidence the consultation gate reads.

    The debate verdict's DIRECTIONAL confidence, falling back to the state-level
    value.

    `directional_confidence` rather than the verdict's plain `confidence`, and
    `state.py` documents why: the plain field is driven to zero when the system is
    emergency-stopped, so gating on it would make "trading is halted" look
    identical to "the evidence is perfectly balanced" — and the second is exactly
    when a second opinion is worth taking. Only the directional value describes
    the market read.

    `TradeDecision` is deliberately NOT consulted here: it has no confidence
    field. An earlier version of this function read `decision.confidence` through
    `getattr(..., None)`, which always returned None and so was dead code that
    read as a live first choice.
    """
    verdict = state.get("debate_verdict")
    if verdict is not None:
        directional = getattr(verdict, "directional_confidence", None)
        if directional is not None:
            return float(directional)

    value = state.get("confidence")
    return float(value) if value is not None else None


def _directions_disagree(state: TradingState) -> bool:
    """True when the specialists genuinely split on direction.

    This is `should_consult`'s strongest trigger — the case an outside view is
    most likely to inform — so it is computed from the findings rather than
    inferred from a low confidence number, which can be low for several unrelated
    reasons (thin coverage, a binding risk concern, a halt).
    """
    findings = state.get("specialist_findings") or []
    stances = {
        getattr(f, "stance", None)
        for f in findings
        if getattr(f, "available", False) and getattr(f, "stance", None)
    }
    # Only a genuine long-vs-short split counts. A mix of a direction and
    # 'neutral' is not disagreement about direction.
    directional = {s for s in stances if str(s).lower() in ("long", "short", "bullish", "bearish")}
    return len(directional) > 1


def _internal_view(state: TradingState) -> str:
    """The already-formed view, as facts the panel can react to.

    Deliberately no raw candles and no price history. The question is "does this
    reasoning hold up", not "what do you think the market will do" — handing over
    the market would invite a fresh opinion instead of a review of ours, and a
    fresh opinion from a model with no risk context is the least useful answer
    available.
    """
    lines: List[str] = [f"Symbol: {state.get('symbol', 'unknown')}"]

    decision = state.get("decision")
    if decision is not None:
        lines.append(f"Action decided: {getattr(decision, 'action', None)}")
        if getattr(decision, "rationale", None):
            lines.append(f"Rationale: {decision.rationale}")

    verdict = state.get("debate_verdict")
    if verdict is not None:
        lines.append(f"Debate direction: {getattr(verdict, 'direction', None)}")
        conf = getattr(verdict, "directional_confidence", None)
        lines.append(f"Directional confidence: {conf if conf is not None else 'not measured'}")
        coverage = getattr(verdict, "coverage", None)
        if coverage is not None:
            # Coverage matters to a reviewer: a verdict from half the panel should
            # not be defended as though it came from all of it.
            lines.append(f"Panel coverage: {coverage:.2f} of the possible directional weight")
        for label, items in (("Supporting", getattr(verdict, "supporting", None)),
                             ("Contradicting", getattr(verdict, "contradicting", None)),
                             ("Absent specialists", getattr(verdict, "absent", None))):
            if items:
                lines.append(f"{label}: {'; '.join(map(str, items))[:500]}")

    risk = state.get("risk_assessment")
    if risk is not None:
        lines.append(f"Risk gateway verdict: {getattr(risk, 'verdict', None)}")
        if getattr(risk, "reasons", None):
            lines.append(f"Risk reasons: {'; '.join(map(str, risk.reasons))[:400]}")

    unavailable = state.get("unavailable") or []
    if unavailable:
        # Named, so the panel knows what is missing rather than reasoning as
        # though nothing is.
        lines.append("UNAVAILABLE (do not speculate about these): "
                     + "; ".join(map(str, unavailable))[:500])

    return "\n".join(lines)


async def consult_externally(state: TradingState) -> Optional[Dict[str, Any]]:
    """Take a second opinion when the decision is genuinely uncertain.

    Never raises. Every path either writes `consultation` or writes nothing, and
    a failure is recorded inside the result rather than propagated — this node
    runs after the decision, so aborting the run here would discard work that is
    already complete and correct.
    """
    from backend.llm.provider import build_consultation_panel
    from backend.services.ai_consultation import consult, should_consult

    confidence = _internal_confidence(state)
    disagree = _directions_disagree(state)

    wanted, reason = should_consult(confidence, directions_disagree=disagree)
    if not wanted:
        # The reason is recorded even on a no, so "why wasn't a second opinion
        # taken?" has an answer without reading the code.
        return {"consultation": {"consulted": False, "skipReason": reason}}

    panel = build_consultation_panel()
    if not panel:
        return {
            "consultation": {
                "consulted": False,
                "skipReason": (
                    f"{reason}, but no consultation panel is configured "
                    f"(LLM_CONSULT_PANEL is unset), so no outside view was sought"
                ),
            }
        }

    symbol = state.get("symbol", "unknown")
    question = (
        f"Does the reasoning below support the stated action on {symbol}? "
        f"Answer AGREE, DISAGREE or UNCLEAR on the first line."
    )

    result = await consult(
        question=question,
        internal_view=_internal_view(state),
        providers=panel,
    )

    aggregate = result.aggregate()
    aggregate["triggerReason"] = reason
    aggregate["internalConfidence"] = confidence
    aggregate["directionsDisagreed"] = disagree

    logger.info(
        "External consultation for %s: %d/%d responded, stances=%s (%s)",
        symbol, len(result.responded), len(result.opinions),
        aggregate.get("stances"), reason,
    )

    return {
        "consultation": aggregate,
        # Budget accounting, same as every other LLM node. A consultation that
        # spent tokens must show in the run's total even though it changed nothing.
        "llm_calls_made": (state.get("llm_calls_made") or 0) + len(result.opinions),
        "llm_tokens_used": (state.get("llm_tokens_used") or 0) + result.total_tokens,
    }


def register_consultation_node() -> None:
    """Register unconditionally. The caller owns the idempotence check.

    NO MODULE-LEVEL `_registered` FLAG, deliberately, matching every other node
    module in this package — and the first version of this file had one, which
    broke the suite in a way worth recording.

    `registry.clear_registry()` resets the "already registered" flags of the four
    GRAPH modules by name. A node module holding its own flag is invisible to
    that reset: after a clear, `_ensure_nodes()` correctly sees the contract is
    missing and calls this function, which returns early because its private flag
    is still True — so the node is never re-registered and the graph build fails
    with a KeyError. That is exactly the failure `clear_registry`'s docstring
    describes, and it only appeared under a full-suite run where an earlier test
    had cleared the registry.

    `analysis._ensure_nodes` guards with `if get_contract(NODE_NAME) is None`,
    which is the single source of truth for whether registration is needed.
    """
    register_node(
        NodeContract(
            name=NODE_NAME,
            reads=("symbol", "decision", "debate_verdict", "risk_assessment",
                   "specialist_findings", "confidence", "unavailable",
                   "llm_calls_made", "llm_tokens_used"),
            # `consultation` ONLY. NodeContract enforces this, which is what stops
            # a future edit here from writing `decision` or `confidence` — the
            # granularity of enforcement is the state key, so a node that could
            # write the decision could overturn it.
            writes=("consultation", "llm_calls_made", "llm_tokens_used"),
            purpose=(
                "Take an advisory second opinion from distinct external models when the "
                "decision is genuinely uncertain. Runs after the decision and the risk "
                "gateway, so it cannot influence either."
            ),
            deterministic=False,
            may_call_llm=True,
            phase=48,
        ),
        consult_externally,
    )
