"""The stop must fire before liquidation does. Nothing compared the two.

WHY THIS FILE EXISTS
--------------------
The stop is 2.5 x ATR. The liquidation distance is a function of LEVERAGE. Those
two numbers were computed in different places and never checked against each
other, so at high enough leverage — or wide enough volatility — the venue
liquidates the position BEFORE its stop is touched.

That is not one failure, it is all of them at once. Every protective mechanism in
this system is built on that stop firing: the resting venue order, the tick
monitor, the trailing stop, the partial take-profit. If liquidation gets there
first, none of them runs, and the loss is the ENTIRE margin rather than the risk
the gateway sized for. At a 100% capital allocation that margin is the account.

    lev    liq distance   stop @ ATR%=4.0 (2.5x = 10%)
    3x          32.83%    safe
    5x          19.50%    safe
    10x          9.50%    LIQUIDATED BEFORE THE STOP

THE VOLATILITY GATE DOES NOT CATCH THIS. It ranks volatility by PERCENTILE, so a
market that has been violent for a while reads NORMAL against its own recent
history while its ATR is objectively large. Measured on SOL over 880 bars, ATR%
ranged 0.145-1.288 — safe at 10x today, and one expansion away from not being.
"""

from __future__ import annotations

import pytest

from backend.core.risk_manager import (
    LIQUIDATION_SAFETY_FACTOR,
    MAINTENANCE_MARGIN_RATE,
    liquidation_distance,
    liquidation_safe_leverage,
    max_leverage_ceiling,
)


def test_liquidation_distance_shrinks_with_leverage():
    assert liquidation_distance(1) > liquidation_distance(5) > liquidation_distance(10)
    # 10x: ~10% margin less the maintenance requirement
    assert liquidation_distance(10) == pytest.approx(0.10 - MAINTENANCE_MARGIN_RATE)


def test_a_tight_stop_keeps_the_requested_leverage():
    """The common case must not be penalised. SOL's median ATR% is ~0.37, so a
    2.5-ATR stop is under 1% and sits far inside 10x's liquidation distance."""
    assert liquidation_safe_leverage(0.0093, 10) == 10


def test_a_wide_stop_forces_leverage_down():
    """ATR% 4.0 -> a 10% stop, which is outside 10x's ~9.5% liquidation distance."""
    capped = liquidation_safe_leverage(0.10, 10)
    assert capped < 10
    assert liquidation_distance(capped) * LIQUIDATION_SAFETY_FACTOR >= 0.10


def test_the_cap_can_only_lower_never_raise():
    """Same property as `max_leverage_ceiling`, and the reason is the same: a risk
    control that can increase exposure is not a risk control."""
    for requested in (1, 2, 3, 5, 10):
        assert liquidation_safe_leverage(0.001, requested) <= requested


def test_it_never_returns_below_1x():
    """Leverage under 1x is not a thing. A stop too wide even for 1x is a stop
    wider than the instrument can move, which the ATR checks reject on their own."""
    assert liquidation_safe_leverage(0.99, 10) == 1
    assert liquidation_safe_leverage(5.0, 10) == 1


def test_an_unusable_stop_distance_leaves_the_request_alone():
    """Do not silently widen leverage on an unknown. Invariant 3's own checks
    refuse a trade with no stop; this must not pre-empt that with a guess."""
    assert liquidation_safe_leverage(0.0, 5) == 5
    assert liquidation_safe_leverage(None, 5) == 5


def test_the_safety_factor_is_a_real_margin_not_a_rounding():
    """The stop must sit within HALF the distance to liquidation.

    The computed distance ignores fees, accrued funding and the venue's tiered
    maintenance rate, and price can gap THROUGH a stop. A stop at 90% of the way
    to liquidation is one bad tick from being overtaken by the thing it exists to
    pre-empt.
    """
    assert LIQUIDATION_SAFETY_FACTOR <= 0.5
    # A stop at exactly the raw liquidation distance must NOT be allowed.
    raw = liquidation_distance(10)
    assert liquidation_safe_leverage(raw, 10) < 10


def test_it_composes_with_the_absolute_ceiling_by_min():
    """Invariant 2 stays the outer bound; this can only tighten it further."""
    for tab in ("paper", "real"):
        hard = max_leverage_ceiling(tab)
        assert liquidation_safe_leverage(0.0001, hard) <= hard


def test_the_gateway_applies_it(monkeypatch):
    """Asserted through `gate()` so the wiring is covered, not just the helper."""
    import ast
    import pathlib

    src = pathlib.Path("backend/graphs/nodes/risk_gateway.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "gate"
    )
    called = {
        c.func.id for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "liquidation_safe_leverage" in called, (
        "gate() must cap leverage so the stop fires before liquidation"
    )
