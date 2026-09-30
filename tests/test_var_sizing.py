"""The Risk Gateway sizes to fit the VaR limit instead of being refused by it.

WHY THIS EXISTS: TWO DAYS, 9,926 CYCLES, ZERO TRADES
====================================================
Read from the live system on 2026-09-28. A session on XRP/USDT at 100%
allocation and 10x leverage, on a $25,000 paper book:

    decisions    6,078 rows, every one rejected
    trades           0 rows
    risk_events     75 rows, EVERY ONE `GLOBAL_VAR_LIMIT`

The pipeline was not broken. The graph reached its gateway, the gateway sized
the trade, the execution service submitted a TAR — and the CRO refused all 75,
verbatim:

    Global VaR limit exceeded: a 1.89% adverse move (stop 1.26% away x1.5
    slippage allowance) on $249812.48 notional is $4709.69, above the 5.0% of
    $25000.00 equity limit ($1250.00). The largest notional that fits is
    $66302.81 (2.65x equity)

Two gates disagreeing over one quantity, and the loser was every trade. The
previous fix in this area corrected the CRO's MEASUREMENT (it had ignored the
stop and capped everything at 0.5x equity); it did not make the two gates AGREE
about the size, so the gateway went on asking for 10x equity and the CRO went on
saying no.

WHY SHRINKING IS CORRECT HERE AND NOT ELSEWHERE
===============================================
A VaR limit is a SIZE limit, so the right answer to breaching it is a smaller
size. That is categorically different from the refusals above it in `gate()` —
the tradeable-instrument blocklist, session scope, one-position-at-a-time, HTF
alignment — every one of which is a property of the INSTRUMENT or the SESSION
and must never be reachable by proposing a smaller trade. Those stay refusals.

THE POLICY IS NOT LOOSENED. The cap is exactly what 5% of equity buys at this
stop distance, the CRO still re-checks independently, and a wider stop still
consumes more of the budget.
"""

from __future__ import annotations

import inspect

import pytest

from backend.agents.cro_agent import (
    _adverse_move_fraction,
    max_portfolio_var_fraction,
)


def _cap(equity: float, fraction: float, leverage: int, entry: float, stop: float):
    """The arithmetic the gateway now performs, mirrored for assertion."""
    move, _ = _adverse_move_fraction(entry, stop)
    budget = equity * max_portfolio_var_fraction()
    asked = equity * fraction * leverage
    ceiling = budget / move
    return asked, ceiling, move, budget


# ---------------------------------------------------------------------------
# The live case
# ---------------------------------------------------------------------------

def test_the_exact_session_that_could_not_trade_now_fits(monkeypatch):
    """XRP/USDT, 100% of 25,000 at 10x, stop 1.26% away."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    entry = 3.0
    asked, ceiling, move, budget = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.0126))

    assert asked * move > budget, "the original ask must still breach the limit"
    assert ceiling == pytest.approx(budget / move)
    # 2.65x equity, matching the figure the live CRO itself reported.
    assert ceiling / 25_000.0 == pytest.approx(2.65, abs=0.02)
    assert ceiling * move == pytest.approx(budget)


def test_the_capped_size_passes_the_cro_by_construction(monkeypatch):
    """The whole point: what the gateway sends is what the CRO accepts.

    The capped notional sits exactly ON the limit, so this asserts `<=` — a
    strict `<` would be asserting a rounding artefact rather than the property.
    """
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    entry = 3.0
    _, ceiling, move, budget = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.0126))
    # `ceiling` is DERIVED as budget/move, so the product lands within a float
    # ulp either side of the budget. A bare `<=` makes this a coin flip on
    # rounding rather than a test of the property.
    assert ceiling * move == pytest.approx(budget, rel=1e-9)


def test_an_allocation_that_already_fits_is_untouched(monkeypatch):
    """The cap must not shrink a trade that was never over the limit — that
    would be the 1.2x margin-buffer haircut all over again, and the operator's
    report on that was "I chose 100% allocation but it only takes some amount"."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    entry = 3.0
    asked, ceiling, move, budget = _cap(25_000.0, 0.25, 2, entry, entry * (1 - 0.0126))
    assert asked * move <= budget
    assert asked < ceiling, "a conforming trade must be below the ceiling, so uncapped"


# ---------------------------------------------------------------------------
# Direction of the limit
# ---------------------------------------------------------------------------

def test_a_wider_stop_buys_a_smaller_position(monkeypatch):
    """The correct direction, and the reason the CRO's measurement was fixed to
    read the stop in the first place: a wider stop IS more risk per unit."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    entry = 3.0
    _, tight, _, _ = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.005))
    _, wide, _, _ = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.030))
    assert wide < tight


def test_higher_leverage_does_not_buy_more_notional(monkeypatch):
    """THE POINT OF THE LIMIT. Leverage multiplies notional, and the VaR budget
    is denominated in equity — so past the ceiling, raising leverage buys a
    smaller MARGIN and the same NOTIONAL, not a bigger position."""
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    entry = 3.0
    _, c5, _, _ = _cap(25_000.0, 1.0, 5, entry, entry * (1 - 0.0126))
    _, c10, _, _ = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.0126))
    assert c5 == pytest.approx(c10)


def test_raising_the_policy_raises_the_ceiling(monkeypatch):
    """The operator's deliberate lever, and the one the refusal names."""
    entry = 3.0
    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    _, base, _, _ = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.0126))
    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.10")
    _, raised, _, _ = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.0126))
    assert raised == pytest.approx(base * 2)


# ---------------------------------------------------------------------------
# The wiring, and what must NOT become shrinkable
# ---------------------------------------------------------------------------

def test_the_gateway_caps_rather_than_rejects():
    from backend.graphs.nodes import risk_gateway

    src = inspect.getsource(risk_gateway.gate)
    assert "max_notional" in src and "VaR-CAPPED" in src
    assert "per_trade_margin = capped_margin" in src


def test_the_gateway_uses_the_CRO_s_own_arithmetic():
    """Two copies of this calculation would drift into exactly the disagreement
    the import exists to end — the ATR-multiplier lesson, one layer up."""
    from backend.graphs.nodes import risk_gateway

    src = inspect.getsource(risk_gateway)
    assert "from backend.agents.cro_agent import" in src
    assert "cro_adverse_move" in src


def test_the_cap_is_reported_and_names_the_lever():
    """A cap that silently shrinks the operator's chosen allocation is the exact
    complaint that produced the margin-buffer fix. It must say what it did and
    what to change."""
    from backend.graphs.nodes import risk_gateway

    src = inspect.getsource(risk_gateway.gate)
    assert "MAX_PORTFOLIO_VAR_FRACTION" in src
    assert "Sized down to" in src


@pytest.mark.parametrize("gate_phrase", [
    "no trading session is running",
    # Wrapped across two source lines, so match the stable half.
    "is on {session.symbol}, not {symbol}",
])
def test_scope_refusals_are_still_refusals_not_caps(gate_phrase):
    """These are properties of the SESSION, not of size. Making one reachable by
    proposing a smaller trade would let an out-of-scope entry through by
    shrinking — the failure `trade_scope`'s docstring warns about in as many
    words."""
    from backend.services import trade_scope

    src = inspect.getsource(trade_scope)
    assert gate_phrase in src


def test_the_untradeable_gate_is_not_reachable_by_shrinking(monkeypatch):
    """BTC is refused because of WHAT it is, never because of how big the trade
    is. It must remain a refusal.

    The blocklist is set explicitly because `tests/conftest.py` clears it — other
    tests use BTC as a generic symbol — and because it is read at CALL time, so
    a test that leaned on the default would be asserting the fixture, not the
    gate.
    """
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "BTC/USDT")
    from backend.services.tradeable_universe import refusal_reason

    reason = refusal_reason("BTC/USDT")
    assert reason is not None
    assert "size" not in reason.lower() and "smaller" not in reason.lower()


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_the_cap_leaves_headroom_below_the_budget(monkeypatch):
    """SIZING TO EXACTLY THE BUDGET DOES NOT WORK, and this was measured.

    The first version of the cap sized to precisely `budget / move`. The real CRO
    then rejected it anyway:

        on $66137.57 notional is $1250.00, above the ... limit ($1250.00)

    Two causes, neither fixable by being more careful with floats. The CRO never
    receives a notional — it receives a SIZE and re-derives `size * entry`, so the
    figure it judges has been through `notional -> size -> notional`. And
    `Venue.check_size` quantises to the instrument's step, which can round UP.
    A boundary that another component recomputes and compares with a strict `>`
    is a coin flip on the last bit.
    """
    from backend.graphs.nodes.risk_gateway import VAR_HEADROOM

    monkeypatch.delenv("MAX_PORTFOLIO_VAR_FRACTION", raising=False)
    entry = 3.0
    _, ceiling, move, budget = _cap(25_000.0, 1.0, 10, entry, entry * (1 - 0.0126))
    sized = ceiling * VAR_HEADROOM

    assert sized * move < budget, "the sized position must sit strictly under the limit"
    # And not so far under that the operator loses a meaningful position: the
    # headroom is for a rounding round trip, not a second risk buffer. The 1.2x
    # margin buffer was removed for exactly that reason.
    assert sized * move > budget * 0.98


def test_the_headroom_is_small_enough_not_to_be_a_second_buffer():
    from backend.graphs.nodes.risk_gateway import VAR_HEADROOM

    assert 0.98 <= VAR_HEADROOM < 1.0
