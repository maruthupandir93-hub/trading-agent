"""The reflection must name a REAL cause, which means it must SEE the entry context.

WHY THIS FILE EXISTS
====================
The operator's complaint: every loss produced the same lesson —
"Check if losses cluster in this regime before changing weighting." Two causes,
both fixed and both pinned here:

  1. The model was often never reached (429 rate limiting -> fallback). That is
     `test_rate_limit.py`.
  2. Even when reached, the prompt was thin: symbol, side, pnl, exit reason — and
     NOT the RSI/ATR/structure/regime/HTF-trend/BTC snapshot the Risk Gateway
     already records at entry. A model given only the outcome cannot distinguish
     "stopped inside the noise band in a range" from "counter-trend against the
     4h", so it retreats to a generic category. This file pins that the rich
     context now reaches the prompt, and that the analysis runs on the reasoning
     tier.
"""

from __future__ import annotations

import pytest

from backend.graphs.nodes.reflection import (
    LESSON_NODE,
    _lesson_prompt,
    rule_based_lesson,
)
from backend.graphs.state import TradeReflection

ENTRY_CTX = (
    "SOL/USDT @ 15m: RSI(14)=41.0, ATR(14)=0.52, structure trend=Bearish, "
    "regime=Range, volatility=LOW (28th pct), context: 1h/4h trend=Bearish, "
    "BTC Bearish (-1.2%), rel.strength +0.3%, strategy=TrendFollowing"
)


def _receipt_state(entry_context):
    reflection = TradeReflection()
    reflection.outcome = "Failure"
    reflection.realized_pnl = -25.44
    state = {
        "symbol": "SOL/USDT",
        "closed_trade": {
            "symbol": "SOL/USDT", "side": "buy", "pnl": -25.44,
            "entry_price": 100.5, "exit_price": 99.9, "exit_reason": "stop-loss",
            "held_seconds": 900, "strategies": ["TrendFollowing"],
            "entry_context": entry_context,
        },
    }
    return state, reflection


def test_the_prompt_includes_the_entry_context():
    state, reflection = _receipt_state(ENTRY_CTX)
    prompt = _lesson_prompt(state, reflection)
    assert "RSI(14)=41.0" in prompt
    assert "1h/4h trend=Bearish" in prompt
    assert "BTC Bearish" in prompt
    # This is the fact that lets the model say "long against the 4h downtrend".
    assert "structure trend=Bearish" in prompt


def test_a_missing_entry_context_is_stated_not_faked():
    """Invariant 6. The prompt must tell the model the context is unavailable, not
    leave it to reason as though nothing was missing."""
    state, reflection = _receipt_state(None)
    prompt = _lesson_prompt(state, reflection)
    assert "NOT RECORDED" in prompt
    assert "RSI" not in prompt  # nothing invented


def test_the_system_prompt_demands_a_cause_not_a_category():
    from backend.graphs.nodes.reflection import _LESSON_SYSTEM_PROMPT

    # The exact bad example the operator kept seeing is named as what NOT to do.
    assert "losses cluster in this regime" in _LESSON_SYSTEM_PROMPT
    assert "LIKELY CAUSE" in _LESSON_SYSTEM_PROMPT
    assert "noise band" in _LESSON_SYSTEM_PROMPT


def test_the_lesson_runs_on_the_reasoning_tier():
    """Connecting an outcome to its context is judgment, and Section 39.6 reserves
    the strongest model for judgment. It runs after the close, off the critical
    path, so the slower tier costs nothing a fill waits on."""
    import inspect

    from backend.graphs.nodes import reflection

    source = inspect.getsource(reflection.write_lesson)
    assert "ModelTier.REASONING" in source
    assert "ModelTier.NARRATIVE" not in source


def test_the_deterministic_floor_still_exists_for_a_loss():
    """The rule-based lesson stays the floor: most deployments run without an LLM,
    and a failed model call must never end the learning pipeline for that trade."""
    reflection = TradeReflection()
    reflection.outcome = "Failure"
    assert rule_based_lesson(reflection)  # non-empty


def test_the_lesson_node_still_only_writes_the_lesson():
    """The tier changed; the contract must not have. The node still may not reach
    `reflection` (and through it the calibration delta that feeds sizing)."""
    from backend.graphs.nodes.reflection import register_reflection_nodes
    from backend.graphs.registry import get_contract

    register_reflection_nodes()  # idempotent; the caller owns the guard
    contract = get_contract(LESSON_NODE)
    assert contract is not None
    assert set(contract.writes) == {"reflection_lesson", "llm_calls_made", "llm_tokens_used"}
    assert "reflection" not in contract.writes


def test_the_closed_event_carries_the_context_to_the_reflection():
    """The receipt in `reflection_agent` reads these off the event. If the event
    ever stops carrying them, the prompt silently goes thin again."""
    from backend.models.events import PositionClosedEvent

    event = PositionClosedEvent(
        trade_id="t1", symbol="SOL/USDT", side="buy", tab="paper",
        entry_price=100.5, exit_price=99.9, quantity=10.0, realized_pnl=-25.44,
        exit_reason="stop-loss", entry_context=ENTRY_CTX,
        strategy="TrendFollowing", run_id="run-1",
    )
    assert event.entry_context == ENTRY_CTX
    assert event.strategy == "TrendFollowing"
    assert event.run_id == "run-1"
