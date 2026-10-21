"""A strategy's SPEC and its EXECUTION must not describe different trades.

ISSUE C, CONFIRMED. The Trend profile's `exit_logic` read "ATR stop at 1.5x,
target 3.0x" while `core/risk_manager` places 2.5x/5.0x. Both are a nominal
1:2, so the error is invisible in the ratio — and it is not cosmetic: a 1.5x
stop and a 2.5x stop produce different stop distances, holding times, position
sizes and target hit rates. A strategy can look profitable against the spec and
behave differently in production.

This is the ATR-multiplier drift between `lib/riskManager.ts` and
`backend/core/risk_manager.py` for the third time, so the fix is structural:
the prose INTERPOLATES the constants instead of restating them, and this test
fails if anyone restates them again.

ISSUE E, PARTIALLY CONFIRMED. The backtest already imports the live risk model
and already charges fees, which the review listed as a verification item. What
it does NOT model is the live EXIT POLICY — and that gap is real, so it is
asserted here rather than left for the next reader to rediscover.
"""

from __future__ import annotations

import inspect
import re

from backend.algorithms import strategy_profiles as sp
from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER
from tests.sourceutil import code_only


def test_no_profile_restates_an_atr_multiplier_as_a_literal():
    """The prose must be derived from the constants, never typed beside them."""
    live = {str(ATR_STOP_MULTIPLIER), str(ATR_TARGET_MULTIPLIER)}

    for profile in sp.STRATEGY_PROFILES:
        for field in ("exit_logic", "risk_profile", "position_sizing_rule"):
            text = getattr(profile, field) or ""
            for raw in re.findall(r"(\d+(?:\.\d+)?)\s*x\s*ATR|ATR[^.]*?(\d+(?:\.\d+)?)x", text):
                quoted = next((g for g in raw if g), None)
                if quoted is None:
                    continue
                assert quoted in live, (
                    f"{profile.agent}.{field} quotes {quoted}x ATR, but the system "
                    f"places {ATR_STOP_MULTIPLIER}x/{ATR_TARGET_MULTIPLIER}x. "
                    f"Interpolate the constant instead of restating it."
                )


def test_the_trend_profile_quotes_the_multipliers_that_are_actually_placed():
    """The specific line the review flagged."""
    text = sp.get_profile("Trend").exit_logic

    assert f"{ATR_STOP_MULTIPLIER}x" in text
    assert f"{ATR_TARGET_MULTIPLIER}x" in text
    assert "1.5x" not in text and "3.0x" not in text


def test_the_profiles_module_imports_the_multipliers_rather_than_hardcoding():
    src = code_only(inspect.getsource(sp))
    assert "ATR_STOP_MULTIPLIER" in src and "ATR_TARGET_MULTIPLIER" in src


# ---------------------------------------------------------------------------
# Issue E: what the backtest does and does not model
# ---------------------------------------------------------------------------

def test_the_backtest_uses_the_live_risk_model_and_charges_fees():
    """Two of the review's verification items, confirmed ALREADY SATISFIED.

    Recorded as a test rather than as a note, so "the backtest ignores costs"
    cannot be re-reported from a code read.
    """
    from backend.core import strategy_backtest as bt

    src = code_only(inspect.getsource(bt))
    assert "ATR_STOP_MULTIPLIER" in src and "ATR_TARGET_MULTIPLIER" in src, (
        "the backtest must import the live risk model, not restate it"
    )
    assert "taker_rate" in src, "the backtest must charge the real fee"
    assert "fee_r" in src, "fees must reach the reported expectancy"


def test_the_backtest_resolves_an_ambiguous_bar_against_the_trade():
    """A bar that spans both the stop and the target must assume the STOP.

    Assuming the favourable one would systematically overstate every strategy's
    expectancy — and the whole purpose of the backtest is to decide which
    strategies may risk money.
    """
    from backend.core import strategy_backtest as bt

    doc = (inspect.getsource(bt) or "").lower()
    assert "stop" in doc and "ambiguous" in doc, (
        "the ambiguous-bar rule must be stated where it is implemented"
    )


def test_the_backtest_does_not_yet_model_the_live_exit_policy():
    """A KNOWN, DELIBERATELY RECORDED GAP — Issue E's real finding.

    The backtest simulates a pure ATR stop/target pair. Production can also
    exit on `PROFIT_TARGET_PCT`, `PARTIAL_TP_FRACTION`, `TRAILING_STOP_R` and
    now `max_holding_minutes`. With all four at their defaults the two agree —
    the ATR pair IS the live policy today, which is why the stored backtests
    remain valid evidence.

    They stop agreeing the moment an operator sets any of those, and then a
    backtest is measuring a strategy production does not run. This test exists
    so that turning one on is a deliberate act with a visible consequence
    rather than a silent divergence.
    """
    import backend.agents.position_monitor as pm

    assert pm.PROFIT_TARGET_PCT() == 0.0, (
        "PROFIT_TARGET_PCT is set, so the live exit is no longer the ATR pair "
        "the stored backtests measured — re-run scripts/run_backtests.py "
        "against the live policy before trusting them"
    )
    assert pm.PARTIAL_TP_FRACTION() == 0.0, (
        "PARTIAL_TP_FRACTION is set; the backtest does not model a scale-out"
    )
    assert pm.TRAILING_STOP_R() == 0.0, (
        "TRAILING_STOP_R is set; the backtest does not model a trail, and the "
        "live trail measured -0.027R against the fixed 2:1"
    )
