"""The CRO's VaR check measures the loss the STOP allows, not a flat 10% of notional.

WHAT WAS WRONG, AND HOW IT WAS FOUND
====================================
Found by running a real execution plan through the real execution plane, not by
reading the code. The Risk Gateway sized a 25%-allocation, 3x session exactly as
CLAUDE.md's broker-style sizing describes, and the CRO refused it:

    Global VaR limit exceeded: a 10% adverse move on $18815.50 notional is
    $1881.55, above the 5% of $25088.49 equity limit ($1254.42)

The stop on that same trade sat 1.71% from entry. The real worst case was about
$483 — roughly a quarter of what the check asserted.

`implied_var = notional * 0.10` is the right shape for a position with NO stop,
and this system has no such positions: invariant 3 makes a computed stop
mandatory, the Risk Gateway hard-rejects without one, and the constraint
immediately above this one re-verifies the stop is on the correct side of entry.

The consequence was a flat cap of `0.05 / 0.10 = 0.5x equity` notional, whatever
the leverage — so two gates over one quantity disagreed:

    25% allocation at 3x  ->  0.75x equity  ->  gateway sized it, CRO refused
   100% allocation at 3x  ->  3.00x equity  ->  gateway sized it, CRO refused

which is why "I chose 100% allocation but it only takes some amount" was true:
the size was being decided by a limit the operator could not see. (The other
half of that answer is that every trade was actually coming from the event path,
which ignores the session's allocation and leverage entirely and always sizes at
1x — see `test_single_trade_originator.py`.)

WHAT DID NOT CHANGE
===================
The 5% policy. It is still enforced against equity, and it is still the default.
What changed is that the number compared against it is derived from the trade's
own bounded loss instead of from a constant that ignores the stop.
"""

from __future__ import annotations

import pytest

from backend.agents.cro_agent import (
    STOP_SLIPPAGE_MULTIPLIER,
    WORST_CASE_ADVERSE_MOVE,
    _adverse_move_fraction,
    max_portfolio_var_fraction,
)


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------

def test_the_adverse_move_comes_from_the_stop_distance():
    """The live case: entry 121.51, stop 123.69 on a short — 1.79% away."""
    move, basis = _adverse_move_fraction(121.51, 123.6891)
    distance = (123.6891 - 121.51) / 121.51
    assert move == pytest.approx(distance * STOP_SLIPPAGE_MULTIPLIER)
    assert "stop" in basis and "slippage" in basis
    assert move < WORST_CASE_ADVERSE_MOVE, (
        "a 1.8% stop must not be judged as a 10% loss — that is the error that "
        "capped every leveraged position at 0.5x equity"
    )


def test_a_wider_stop_consumes_more_of_the_limit():
    """The correct direction. A wider stop IS more risk, and must cost more.

    The old flat constant could not express this at all: a 0.5% stop and a 5%
    stop produced an identical VaR figure.
    """
    tight, _ = _adverse_move_fraction(100.0, 99.5)     # 0.5%
    wide, _ = _adverse_move_fraction(100.0, 95.0)      # 5.0%
    assert wide > tight


def test_a_stop_wider_than_the_worst_case_is_capped_there():
    """Beyond the flat worst case the old assumption is the more conservative of
    the two, so the estimate stops there rather than growing without bound."""
    move, _ = _adverse_move_fraction(100.0, 50.0)      # a 50% "stop"
    assert move == pytest.approx(WORST_CASE_ADVERSE_MOVE)


@pytest.mark.parametrize("stop", [None, 0.0, -5.0])
def test_an_unbounded_position_is_still_judged_as_unbounded(stop):
    """The fallback is the whole safety argument. If a position somehow reaches
    the CRO with no usable stop, it must be measured as if it has none — never
    given the benefit of a stop that is not there."""
    move, basis = _adverse_move_fraction(100.0, stop)
    assert move == pytest.approx(WORST_CASE_ADVERSE_MOVE)
    assert "no usable stop" in basis or "at entry" in basis


def test_a_stop_at_the_entry_price_is_not_treated_as_zero_risk():
    """A zero distance would otherwise imply zero VaR and approve any size."""
    move, _ = _adverse_move_fraction(100.0, 100.0)
    assert move == pytest.approx(WORST_CASE_ADVERSE_MOVE)


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def test_the_limit_defaults_to_the_spec_value(monkeypatch):
    """Spec Section 18. Unchanged unless someone changes it on purpose."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    assert max_portfolio_var_fraction() == pytest.approx(0.05)


def test_the_limit_is_read_at_call_time(monkeypatch):
    """The `simulation_mode` rule: a module-level getenv would freeze the value at
    import, so an operator raising the limit would see no effect until a restart."""
    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.10")
    assert max_portfolio_var_fraction() == pytest.approx(0.10)


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-0.2", "1.5", "nan"])
def test_an_unusable_limit_falls_back_to_the_spec_value(monkeypatch, bad):
    """A config typo must not silently disable a risk limit — nor, by becoming
    0, halt all trading while looking like a market condition."""
    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", bad)
    assert max_portfolio_var_fraction() == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# What the two gates now agree about
# ---------------------------------------------------------------------------

def test_a_broker_sized_session_trade_now_fits(monkeypatch):
    """The exact trade the CRO refused live, recomputed."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    equity, entry, stop = 25_088.49, 121.51, 123.6891
    notional = 18_815.50                      # 25% of equity at 3x

    move, _ = _adverse_move_fraction(entry, stop)
    assert notional * move <= equity * max_portfolio_var_fraction(), (
        "the gateway sizes this and the CRO must not then refuse it — two gates "
        "over one quantity have to agree about what mode they are in"
    )


def test_an_oversized_position_is_still_refused(monkeypatch):
    """The limit still bites. 100% allocation at 10x on a tight stop is a real
    refusal, not a rounding artefact — and the operator's own note in CLAUDE.md
    says a stop-out at 10x costs ~10% of the account."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    equity, entry, stop = 25_000.0, 100.0, 101.71
    notional = equity * 10                    # 100% allocation at 10x

    move, _ = _adverse_move_fraction(entry, stop)
    assert notional * move > equity * max_portfolio_var_fraction()


def test_the_refusal_tells_the_operator_what_would_fit():
    """A limit that only says "no" leaves the operator guessing at an allocation,
    and that guess is what produced the original complaint. The affordable
    notional is arithmetic over numbers already in hand, so stating it invents
    nothing (invariant 6)."""
    import inspect

    from backend.agents.cro_agent import CROAgent

    src = inspect.getsource(CROAgent._process_tar)
    assert "largest notional that fits" in src
    assert "MAX_PORTFOLIO_VAR_FRACTION if you accept" in src


def test_the_approval_rationale_reports_the_basis_it_used():
    """The rationale is the audit trail for an approval. Reporting a VaR figure
    without saying it came from the stop would make two runs with different
    stops look identical."""
    import inspect

    from backend.agents.cro_agent import CROAgent

    src = inspect.getsource(CROAgent._process_tar)
    assert "({move_basis})" in src
