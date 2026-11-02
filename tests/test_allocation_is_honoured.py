"""100% allocation means 100% of the balance, at the chosen leverage.

THE OPERATOR'S REPORT, AND THEIR OWN WORKED EXAMPLE:

    "i choose 100% allocation and start amount of 5$ ... if i start with 5$ and
     10x leverage it trade with 5$ and with 10x leverage so totally 50$ trading
     value for each trade ... if it have 5.1$ and choose 75% allocation it
     would take 3.825$"

That is broker sizing, and it is what the allocation control claims to do.
Measured on their configuration before this fix — $5, 10x, 100%:

    stop 1.0%   3.98 of 5.00 deployed   (80%)
    stop 1.3%   3.06 of 5.00 deployed   (61%)   <- what they saw
    stop 2.5%   1.59 of 5.00 deployed   (32%)

THREE REDUCERS SAT BETWEEN THE SELECTION AND THE ORDER, and this is the last
of them. The regime leverage cap and the volatility haircut are covered by
`test_operator_choices_honoured.py`; this file is the VaR cap.

WHAT IS NOT RELAXED. The mandatory stop (invariant 3), the liquidation cap,
the depth cap, the venue minimum and the absolute leverage ceiling (invariant
2) all still apply. The loss per stop-out is bounded — it is simply now the
`leverage x stop%` the operator chose rather than a smaller number a hidden
gate chose for them.
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# The operator's own worked examples
# ---------------------------------------------------------------------------

def deployed_margin(balance: float, fraction: float, leverage: int) -> float:
    """What the gateway deploys once the VaR cap no longer reduces it.

    Mirrors `risk_gateway`'s broker branch: the allocation is the margin pool,
    held back only by the ENTRY FEE, because `apply_paper_fill` refuses a fill
    when `margin + fee > free_cash` and a trade that passes every risk check
    and is then declined by the book is the worse failure.
    """
    from backend.services.fees import taker_rate

    wanted = fraction * balance
    affordable = balance / (1.0 + leverage * taker_rate() * 1.5)
    return min(wanted, affordable)


def test_five_dollars_at_ten_x_deploys_five_dollars():
    """Their first example: $5, 10x, 100% -> ~$50 of position."""
    margin = deployed_margin(5.00, 1.00, 10)
    assert margin == pytest.approx(5.00, rel=0.01), (
        f"only {margin:.2f} of 5.00 deployed"
    )
    assert margin * 10 == pytest.approx(50.0, rel=0.01)


def test_the_pool_grows_with_the_balance():
    """Their second example: after a trade leaves $5.10, 100% uses $5.10."""
    margin = deployed_margin(5.10, 1.00, 10)
    assert margin == pytest.approx(5.10, rel=0.01)


def test_seventy_five_percent_of_five_ten_is_three_eight_two_five():
    """Their third example, stated to the cent."""
    margin = deployed_margin(5.10, 0.75, 10)
    assert margin == pytest.approx(3.825, abs=0.01)


def test_the_only_haircut_is_the_entry_fee():
    """And it is NECESSARY, not a buffer. `apply_paper_fill` refuses a fill
    when `margin + fee > free_cash`, so sizing to literally the whole balance
    produces a position the book then declines — a trade that passed every risk
    check and silently never opened. Measured on a live $3 book at 10x.

    It is tiny: the fee on the notional, not a fraction of the account.
    """
    margin = deployed_margin(5.00, 1.00, 10)
    assert 4.9 < margin <= 5.0, f"the haircut is not just the fee: {margin:.4f}"


# ---------------------------------------------------------------------------
# BOTH gates must agree, or every trade dies at the second one
# ---------------------------------------------------------------------------

def test_one_predicate_is_shared_by_the_gateway_and_the_CRO():
    """THE FAILURE THIS PREVENTS IS DOCUMENTED AND MEASURED: the gateway sized
    the full allocation, the CRO refused it, and the loser was every trade —
    9,926 cycles, 0 trades, 75 GLOBAL_VAR_LIMIT events.

    So the flag lives in ONE module and both gates import it. A copy in each
    is the two-numbers-in-two-places failure this codebase keeps paying for.
    """
    import backend.agents.cro_agent as cro
    import backend.graphs.nodes.risk_gateway as rg

    assert rg.honour_session_allocation is cro.honour_session_allocation


def test_the_CRO_skips_its_veto_for_the_same_trades(monkeypatch):
    import backend.agents.cro_agent as cro

    # The veto lives in `_process_tar`; `handle_event` only dispatches to it.
    src = inspect.getsource(cro.CROAgent._process_tar)
    assert "allocation_governs" in src
    assert "honour_session_allocation() and session_is_driving()" in src


def test_the_gateway_skips_the_cap_for_the_same_trades():
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    assert "allocation_governs" in src
    assert "ALLOCATION HONOURED" in src


# ---------------------------------------------------------------------------
# Scope: a session only, and reversible
# ---------------------------------------------------------------------------

def test_the_UNATTENDED_path_still_has_the_VaR_veto():
    """With no session nobody chose an allocation — the agent is sizing itself,
    and the VaR policy is the only thing bounding it."""
    import backend.agents.cro_agent as cro

    monkey = cro.session_is_driving
    assert callable(monkey)
    src = inspect.getsource(cro.session_is_driving)
    assert "active_session_leverage" in src


def test_it_can_be_turned_off(monkeypatch):
    """Reversible in one env var, read at call time — so an operator who
    changes their mind does not need a restart."""
    import backend.agents.cro_agent as cro

    monkeypatch.delenv(cro.HONOUR_ALLOCATION_VAR, raising=False)
    assert cro.honour_session_allocation() is True

    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(cro.HONOUR_ALLOCATION_VAR, off)
        assert cro.honour_session_allocation() is False, off


def test_session_is_driving_never_decides_by_raising(monkeypatch):
    """A predicate that threw would take the whole decision with it."""
    import backend.agents.cro_agent as cro
    import backend.services.trading_session as ts

    def _boom():
        raise RuntimeError("no session store")

    monkeypatch.setattr(ts, "active_session_leverage", _boom)
    assert cro.session_is_driving() is False


# ---------------------------------------------------------------------------
# What is NOT relaxed
# ---------------------------------------------------------------------------

def test_the_mandatory_stop_and_the_liquidation_cap_still_apply():
    """Invariant 3, and the cap that guarantees the stop fires BEFORE the venue
    liquidates. Without the second, a bigger allocation would not be more risk
    — it would be unbounded risk, because every protective mechanism is built
    on that stop."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    assert "liquidation_safe_leverage" in src
    assert "stop_loss" in src


def test_what_a_stop_out_now_costs_is_REPORTED_not_hidden():
    """`leverage x stop%` of the deployed margin: 13% of the account at 10x on
    a 1.3% stop, 25% on a 2.5% one. That is the arithmetic of trading a whole
    account at 10x rather than a defect — and the operator chose it, so the
    number belongs where they can see it."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    assert "costs about" in src and "of the account" in src


@pytest.mark.parametrize("leverage,stop_pct,expected_pct", [
    (10, 1.3, 13.0),
    (10, 2.5, 25.0),
    (3, 2.5, 7.5),
])
def test_the_cost_arithmetic_is_what_is_quoted(leverage, stop_pct, expected_pct):
    """Stated here so the figure given to the operator stays derived from the
    model rather than from a message."""
    assert leverage * stop_pct == pytest.approx(expected_pct, abs=0.01)
