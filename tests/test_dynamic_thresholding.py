"""The regime confidence gate must be expressed in the units the debate emits.

WHY THIS FILE EXISTS
--------------------
`algorithms/dynamic_thresholding.get_required_confidence` returned absolute
thresholds of 0.60-0.99. `algorithms/debate.score_debate` emits coverage-scaled
weighted evidence, which lands in the 0.15-0.44 band. The two scales had nothing
to do with each other, so `agents/supervisor_agent` could not approve a trade in
ANY market condition. Read from the live `decisions` table, one 65-minute window:

    3,063 decisions     outcome = 'rejected' on every single one
        0 trades

    Bull Trend      n=1730   observed confidence max 0.44   required 0.60
    Range           n=513    observed confidence max 0.36   required 0.75
    Low Volatility  n=176    observed confidence max 0.18   required 0.70

The bug was invisible from the outside. A gate that cannot be passed and a market
that offers nothing produce exactly the same log line — "Confidence 0.38 does not
meet the threshold 0.60" reads as ordinary selectivity, and it was printed 1,730
times.

`graphs/nodes/supervisor.MIN_CONFIDENCE_TO_TRADE` had already been rescaled, with
`test_the_trade_threshold_is_reachable_by_the_real_scorers` guarding it. That test
is why the graph path could trade while this one could not — and this file is the
same guard for the other path.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from backend.algorithms.dynamic_thresholding import (
    BASE_CONFIDENCE_TO_TRADE,
    BLOCKED_REGIMES,
    REGIME_STRICTNESS,
    UNREACHABLE,
    get_regime_risk_multiplier,
    get_required_confidence,
)

# The highest confidence this system has been OBSERVED to produce, per regime,
# across 2,419 live rejections. Not the arithmetic ceiling — the arithmetic
# ceiling is ~0.57 and unreachable in practice, and using it here would let the
# gate drift back above what the scorers actually emit while the test still
# passed. That is precisely how the original bug survived.
OBSERVED_CEILING = {
    "Bull Trend": 0.44,
    "Range": 0.36,
    "Low Volatility": 0.18,
}


def test_the_anchor_matches_the_graph_supervisor():
    """Two constants that must agree, in two files, kept in sync by this test.

    `algorithms/` is a leaf layer and must not import from `graphs/`, so the
    anchor cannot be read directly and is stated twice. That duplication is only
    safe because this test fails when they drift — the same arrangement, and the
    same hazard, as the ATR multipliers in `lib/riskManager.ts` and
    `core/risk_manager.py`.
    """
    from backend.graphs.nodes.supervisor import MIN_CONFIDENCE_TO_TRADE

    assert BASE_CONFIDENCE_TO_TRADE == MIN_CONFIDENCE_TO_TRADE, (
        f"the regime gate is anchored on {BASE_CONFIDENCE_TO_TRADE} while the graph "
        f"supervisor trades at {MIN_CONFIDENCE_TO_TRADE}. Two supervisors applying "
        f"different bars to the same debate scale is how one of them came to be "
        f"unreachable in the first place."
    )


@pytest.mark.parametrize("regime,ceiling", sorted(OBSERVED_CEILING.items()))
def test_a_trending_regime_gate_is_reachable_by_the_real_scorers(regime, ceiling):
    """The bar must sit below what the debate can actually produce.

    Low Volatility is deliberately excluded from the reachable set: its observed
    ceiling (0.18) sits below its own gate, and that is the operator's stated
    policy — this system loses money in chop, `VERY_LOW` is already in
    `BLOCKED_REGIMES` on the risk side, and the operator was asked directly
    whether to lower the Range threshold and chose to keep the selectivity.
    It is asserted as blocked below rather than silently ignored.
    """
    required = get_required_confidence(regime)
    if regime == "Low Volatility":
        pytest.skip("Low Volatility is intentionally near-prohibitive; see below")
    assert required < ceiling, (
        f"regime {regime!r} requires {required:.3f} but the debate has never been "
        f"observed above {ceiling:.2f}. This gate cannot be passed, and a gate that "
        f"cannot be passed reports itself as ordinary selectivity."
    )


def test_low_volatility_stays_near_prohibitive_and_that_is_deliberate():
    """Documents the one regime the rescaling does NOT open up.

    Asserted rather than left implicit so that a future change which makes Low
    Volatility tradeable is a deliberate edit to this test, not a side effect.
    """
    assert get_required_confidence("Low Volatility") > OBSERVED_CEILING["Low Volatility"]


def test_blocked_regimes_cannot_be_satisfied_by_any_confidence():
    """`score_debate` caps confidence at 1.0, so the sentinel must exceed it.

    An explicit sentinel, not a large number. The old table expressed this intent
    as 0.99 with the comment "effectively disables trading" — and nothing then
    distinguished "deliberately impossible" from "a number nobody rechecked after
    the scale moved", which is how the reachable regimes became unreachable too.
    """
    assert UNREACHABLE > 1.0
    for regime in BLOCKED_REGIMES:
        assert get_required_confidence(regime) > 1.0, regime


def test_every_regime_the_detector_can_return_is_covered():
    """No regime may fall through to a default nobody chose.

    `Unknown` did exactly that: `regime_agent.detect_market_regime` returns it and
    the old table never listed it, so it silently took the 0.80 default — blocked,
    but by accident rather than by decision.

    The regime vocabulary is read out of `regime_agent`'s own source rather than
    hardcoded here, so a newly added regime fails this test instead of quietly
    inheriting `UNLISTED_STRICTNESS`.
    """
    src = pathlib.Path("backend/agents/regime_agent.py").read_text(encoding="utf-8")
    returned = {
        node.value.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert returned, "could not read any regime literal out of regime_agent"

    covered = set(REGIME_STRICTNESS) | set(BLOCKED_REGIMES)
    missing = returned - covered
    assert not missing, (
        f"regime_agent can return {sorted(missing)}, which dynamic_thresholding does "
        f"not list. It would take the unlisted default — a bar nobody chose for it."
    )


def test_the_relative_strictness_of_the_old_table_is_preserved():
    """This was a UNIT fix, not a policy change, and the ordering proves it.

    Each regime's share of Bull Trend's bar must still match the ratio the old
    absolute table encoded. If a future edit wants to make Range easier than Bull
    Trend, that is a trading-policy decision and it should have to change this
    test to say so.
    """
    old_absolute = {
        "Bull Trend": 0.60,
        "Bear Trend": 0.65,
        "Accumulation": 0.65,
        "Low Volatility": 0.70,
        "Range": 0.75,
        "Distribution": 0.80,
        "High Volatility": 0.85,
        "Panic": 0.95,
        "Euphoria": 0.95,
    }
    base = get_required_confidence("Bull Trend")
    for regime, old in old_absolute.items():
        old_ratio = old / old_absolute["Bull Trend"]
        new_ratio = get_required_confidence(regime) / base
        assert new_ratio == pytest.approx(old_ratio, abs=0.01), (
            f"{regime} was {old_ratio:.3f}x Bull Trend's bar and is now "
            f"{new_ratio:.3f}x. The rescaling must not reorder the regimes."
        )


def test_the_risk_multiplier_was_not_rescaled_along_with_the_gate():
    """Sizing shares no units with the confidence gate and must not have moved.

    `get_regime_risk_multiplier` is a fraction OF `RISK_PER_TRADE` and was always
    on the correct 0-1 scale. Changing it alongside the gate would have smuggled a
    sizing change into a bug fix.
    """
    assert get_regime_risk_multiplier("Bull Trend") == 1.0
    assert get_regime_risk_multiplier("Range") == 0.5
    assert get_regime_risk_multiplier("Liquidity Crisis") == 0.0
    assert get_regime_risk_multiplier("Unknown") == 0.5
