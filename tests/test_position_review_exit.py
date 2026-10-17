"""F44: the Supervisor's "the panel now argues against my position" EXIT.

This system claims two exit reasons. One -- the stop or the target -- is
enforced on every tick by `PositionMonitorAgent` and is authoritative. The
other is this: a fresh panel reading that contradicts a position we are
already in.

The second could barely run. `_after_opportunity` convened the panel only when
`strategy_scoring` had produced a fresh ENTRY thesis, and `supervise` returned
immediately when there was none -- so the branch whose whole job is to notice
that an OPEN position has become wrong required a cycle that was already
hunting a NEW one. Measured live, most cycles end at node 9 with no setup, so
most cycles could not reach it.

The position was never unprotected. What was missing is the second opinion
about it.

These tests drive the real router and the real node. They assert the ENTRY path
is unchanged as carefully as they assert the new path works, because the
Supervisor is the gate invariant 1 rests on and a loosened entry would be a far
worse bug than the one being fixed.
"""

from __future__ import annotations

import pytest

from backend.graphs.analysis import _after_opportunity
from backend.graphs.nodes.supervisor import ACTION_EXIT, supervise
from backend.graphs.state import (
    DebateVerdict,
    PortfolioStateSnapshot,
    TradeThesis,
    TriggerReason,
    new_state,
)


def _state(*, holding: bool, thesis, verdict=None, positions=None):
    st = new_state(
        run_id="r1",
        symbol="SOL/USDT",
        trigger=TriggerReason(kind="test", symbol="SOL/USDT", detail="unit"),
        started_at=0.0,
        holding_position=holding,
    )
    st["trade_thesis"] = thesis
    st["debate_verdict"] = verdict
    if positions is not None:
        st["portfolio_state"] = PortfolioStateSnapshot(
            tab="paper", equity=1000.0, cash=900.0, open_positions=positions
        )
    return st


def _thesis(direction="LONG"):
    return TradeThesis(
        direction=direction,
        strategy="Breakout",
        entry_price=100.0,
        stop_loss=97.5,
        take_profit=105.0,
        supporting_evidence=["a"],
        contradicting_evidence=[],
    )


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def test_the_router_still_skips_the_panel_on_a_quiet_cycle_with_nothing_held():
    """The cost argument that put this router here is unchanged.

    Seven specialists and a portfolio read on every cycle that found nothing is
    the spend this router exists to avoid, and most cycles find nothing.
    """
    assert _after_opportunity(_state(holding=False, thesis=None)) == "no_opportunity"


def test_the_router_convenes_the_panel_for_a_held_position_with_no_setup():
    assert _after_opportunity(_state(holding=True, thesis=None)) == "analyse"


def test_a_thesis_still_convenes_the_panel_whether_or_not_we_hold():
    for holding in (True, False):
        assert _after_opportunity(_state(holding=holding, thesis=_thesis())) == "analyse"


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

def test_no_thesis_and_nothing_held_is_still_the_old_no_thesis_answer():
    out = supervise(_state(holding=False, thesis=None))
    assert out is not None
    assert "decision" not in out
    assert any("no thesis" in u for u in out["unavailable"])


def test_a_confident_opposing_panel_EXITS_a_held_long_with_no_fresh_thesis():
    """The whole point of F44: this run produced no setup and still acts."""
    verdict = DebateVerdict(
        direction="SHORT",
        confidence=0.9,
        directional_confidence=0.9,
        participants=["market", "orderflow"],
        supporting=["momentum has rolled over"],
        contradicting=[],
        coverage=1.0,
    )
    state = _state(
        holding=True,
        thesis=None,
        verdict=verdict,
        positions=[{"symbol": "SOL/USDT", "qty": 5.0, "side": "buy"}],
    )

    out = supervise(state)

    assert out is not None and "decision" in out, out
    decision = out["decision"]
    assert decision.action == ACTION_EXIT
    # Every narrative answer must be populated -- an unexplained decision is the
    # failure this node's ten answers exist to prevent -- and none may claim a
    # thesis that does not exist.
    for field_name in (
        "what_happened",
        "what_is_happening",
        "why",
        "what_could_happen_next",
        "downside",
        "portfolio_impact",
        "trade_wait_or_exit",
    ):
        value = getattr(decision, field_name)
        assert isinstance(value, str) and value.strip(), field_name


def test_a_held_position_the_panel_does_NOT_contradict_says_so_rather_than_nothing():
    """"No thesis" alone would read as the run having failed."""
    verdict = DebateVerdict(
        direction="LONG",
        confidence=0.9,
        directional_confidence=0.9,
        participants=["market"],
        coverage=1.0,
    )
    out = supervise(
        _state(
            holding=True,
            thesis=None,
            verdict=verdict,
            positions=[{"symbol": "SOL/USDT", "qty": 5.0, "side": "buy"}],
        )
    )

    assert out is not None
    assert "decision" not in out
    text = " ".join(out["unavailable"])
    assert "open position" in text and "stop" in text


def test_a_review_run_can_never_produce_an_ENTRY():
    """Invariant 1's gate must not be loosened by the route that reaches it.

    A run with no thesis has nothing to enter, and the node must not invent one
    from the panel's direction alone.
    """
    verdict = DebateVerdict(
        direction="LONG",
        confidence=0.99,
        directional_confidence=0.99,
        participants=["market", "orderflow", "liquidity"],
        coverage=1.0,
    )
    out = supervise(_state(holding=True, thesis=None, verdict=verdict, positions=[]))

    assert out is not None
    assert "decision" not in out, "a review run produced a decision with no thesis"


def test_the_consultation_node_does_not_spend_a_model_call_on_a_review_run():
    """A held position must not buy a second opinion every decision interval."""
    import asyncio

    from backend.graphs.nodes.consultation import consult_externally

    state = _state(holding=True, thesis=None)
    out = asyncio.run(consult_externally(state))

    assert out is not None
    assert out["consultation"]["consulted"] is False
    assert "no decision" in out["consultation"]["skipReason"]


@pytest.mark.parametrize("side,opposing", [("buy", "SHORT"), ("sell", "LONG")])
def test_both_directions_can_be_exited_by_a_review_run(side, opposing):
    """Shorts were once invisible here because direction was read off the sign
    of the quantity. The review route must not reintroduce a one-sided exit."""
    verdict = DebateVerdict(
        direction=opposing,
        confidence=0.9,
        directional_confidence=0.9,
        participants=["market"],
        coverage=1.0,
    )
    out = supervise(
        _state(
            holding=True,
            thesis=None,
            verdict=verdict,
            positions=[{"symbol": "SOL/USDT", "qty": 5.0, "side": side}],
        )
    )
    assert out is not None and "decision" in out
    assert out["decision"].action == ACTION_EXIT
