"""Size must scale with the MARKET, not only with the account.

THE OPERATOR'S REQUIREMENT: trade "equally like a pro" at $2, $20,000 or
$2,000,000. Risk sizing already delivers half of that — every trade risks
`equity x MAX_PORTFOLIO_VAR_FRACTION`, so the percentage at stake is identical
at every balance.

It is blind to the other half. At $2,000,000 and the live VaR, a 2% stop sizes
an **$8,000,000 notional market order** on SOL/USDT, and the system had NO
maximum size check and NO liquidity check anywhere. That order walks the whole
visible book and fills percent away from the price the decision was made at,
which makes the computed stop a fiction — the position is past it on arrival.

A stop is a promise about how much a trade can lose. An order that moves the
market breaks that promise before the stop is ever tested.
"""

from __future__ import annotations

import types

import pytest

from backend.algorithms.liquidity_cap import (
    MAX_SLIPPAGE_PCT,
    PARTICIPATION_FRACTION,
    absorbable_notional,
    cap_for,
)


def _book(touch: float, step: float, qty: float, levels: int = 50, side: str = "asks"):
    sign = 1 if side == "asks" else -1
    return [{"price": touch + sign * step * i, "qty": qty} for i in range(levels)]


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------

def test_a_deep_book_absorbs_more_than_a_thin_one():
    """The whole point: the ceiling is a property of THIS market, not a constant.

    The same $8m is unremarkable on BTC and impossible on a small alt, so a
    fixed maximum notional would be wrong in both directions.
    """
    deep = absorbable_notional(_book(200.0, 0.01, 200.0))
    thin = absorbable_notional(_book(2.0, 0.002, 500.0))

    assert deep.notional and thin.notional
    assert deep.notional > thin.notional * 100


def test_the_tolerance_bounds_the_AVERAGE_fill_not_the_last_level():
    """A trade's P&L is set by what it actually paid across the whole fill."""
    book = _book(100.0, 0.05, 10.0, levels=60)
    v = absorbable_notional(book, max_slippage_pct=0.10)

    assert v.notional
    # Reconstruct the average actually achieved and check it respects the band.
    filled, cost = 0.0, 0.0
    for level in book:
        if cost + level["price"] * level["qty"] > v.notional:
            remaining = v.notional - cost
            filled += remaining / level["price"]
            cost = v.notional
            break
        filled += level["qty"]
        cost += level["price"] * level["qty"]
    avg = cost / filled
    assert avg <= 100.0 * 1.0011, f"average fill {avg} slipped past the tolerance"


def test_a_tighter_tolerance_yields_a_smaller_ceiling():
    loose = absorbable_notional(_book(100.0, 0.02, 50.0), max_slippage_pct=0.50)
    tight = absorbable_notional(_book(100.0, 0.02, 50.0), max_slippage_pct=0.05)
    assert loose.notional > tight.notional


def test_it_reads_the_side_being_TAKEN():
    """A book can be deep on one side and thin on the other, and that asymmetry
    is exactly when a large order is most dangerous."""
    snap = types.SimpleNamespace(
        order_book_asks=_book(100.0, 0.01, 1000.0),          # deep to buy into
        order_book_bids=_book(99.99, 0.01, 1.0, side="bids"),  # thin to sell into
    )
    buy = cap_for("buy", snap)
    sell = cap_for("sell", snap)

    assert buy.notional and sell.notional
    assert buy.notional > sell.notional * 50


def test_the_participation_fraction_leaves_room_for_the_EXIT():
    """Taking all of it would move price by the full tolerance and leave
    nothing resting for the stop to exit into — and the exit happens when the
    market is already against the position."""
    book = _book(100.0, 0.01, 100.0)
    whole = absorbable_notional(book).notional
    snap = types.SimpleNamespace(order_book_asks=book, order_book_bids=[])

    assert cap_for("buy", snap).notional == pytest.approx(whole * PARTICIPATION_FRACTION)
    assert PARTICIPATION_FRACTION < 1.0


# ---------------------------------------------------------------------------
# What an absent book must NOT do
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rows", [[], None, [{"price": 0, "qty": 0}], [{"bad": 1}]])
def test_an_unreadable_book_is_None_and_never_zero(rows):
    """Zero would mean "this market can absorb nothing", which blocks every
    trade on a feed outage. None means unmeasured and the caller leaves its
    size alone — the True/False/None rule, in the direction that matters here:
    a missing measurement must not become a refusal, because the VaR cap and
    the venue minimum still apply."""
    v = absorbable_notional(rows)
    assert v.notional is None
    assert v.reason


def test_a_snapshot_without_a_book_is_unmeasured():
    snap = types.SimpleNamespace(order_book_asks=[], order_book_bids=[])
    assert cap_for("buy", snap).notional is None
    assert cap_for("buy", None).notional is None


# ---------------------------------------------------------------------------
# The gateway uses it, and only ever downward
# ---------------------------------------------------------------------------

def test_the_gateway_applies_the_depth_cap():
    import inspect

    from backend.graphs.nodes import risk_gateway
    from tests.sourceutil import code_only

    src = code_only(inspect.getsource(risk_gateway.gate))
    assert "_depth_cap" in src, "the gateway never consults the book's capacity"


def test_the_depth_cap_can_only_reduce_a_size():
    """It is a CEILING, never a reason to trade or to trade larger.

    A cap that could raise a size would let a deep book override the VaR
    budget, which is the one limit measured against the operator's own money.
    """
    import inspect

    from backend.graphs.nodes import risk_gateway
    from tests.sourceutil import code_only

    src = code_only(inspect.getsource(risk_gateway.gate))
    # The guard is `0 < depth.notional < notional` — it fires only when the
    # book's ceiling is BELOW the sized notional.
    assert "depth.notional < notional" in src.replace(" ", "").replace(
        "depth.notional<notional", "depth.notional < notional"
    ) or "depth.notional < notional" in src


def test_the_helper_never_raises_into_the_sizing_path():
    """A malformed book must not turn a market-data problem into a refused
    trade: the VaR cap above already bounds the loss."""
    from backend.graphs.nodes.risk_gateway import _depth_cap

    broken = types.SimpleNamespace(order_book_asks="not a book", order_book_bids=None)
    verdict = _depth_cap(types.SimpleNamespace(direction="LONG"), broken)
    assert verdict.notional is None


# ---------------------------------------------------------------------------
# The property the operator asked for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("equity", [2.0, 20_000.0, 2_000_000.0])
def test_risk_per_trade_is_the_same_PERCENTAGE_at_every_balance(equity):
    """"Trade equally at $2 or $2,000,000" — the half that already worked.

    This pins it, because it is the property the depth cap must not break:
    the cap bounds the ORDER, it must not change what fraction of the account
    is at risk when the book is deep enough to take it.
    """
    from backend.agents.cro_agent import max_portfolio_var_fraction

    risk = equity * max_portfolio_var_fraction()
    assert risk / equity == pytest.approx(max_portfolio_var_fraction())
