import pytest
from backend.agents.regime_agent import detect_market_regime
from backend.core.risk_manager import (
    ATR_STOP_MULTIPLIER,
    MAX_MARGIN_FRACTION_PER_TRADE,
    calculate_position_size,
    position_size_detail,
)
from backend.core.knowledge_graph import KnowledgeGraph

def test_regime_detection_insufficient_data():
    klines = [{"close": 100} for _ in range(10)]
    assert detect_market_regime(klines) == "Unknown"

def test_knowledge_graph_implications():
    kg = KnowledgeGraph()
    implications = kg.query_implications("High Funding")
    assert "High Liquidation Risk" in implications
    assert "Lower Position Size" in implications

def test_position_sizing_is_capped_by_the_margin_ceiling():
    """The MARGIN cap binds here, not the risk budget.

    THIS TEST USED TO ASSERT A NOTIONAL CAP, AND THAT CAP WAS THE BUG.
    The old rule was `equity * 0.5 / price` — half the account's NOTIONAL,
    ignoring leverage. Measured on the operator's live ledger it bound on EVERY
    trade by about 6x, which had two consequences:

      1. `RISK_PER_TRADE` did nothing. Actual risk was ~0.3% while it said 2%.
      2. Widening the stop would have INCREASED risk rather than holding it:
         same quantity over twice the distance is twice the loss.

    The cap is now on margin — what actually leaves the account — and scales with
    leverage, because at 5x a $5,000 position locks $1,000. It still binds for a
    tight stop, which is its job: a very tight stop must not justify an
    unbounded position.
    """
    equity = 1000
    price = 60000
    atr = 1000
    leverage = 1.0

    qty_by_risk = (equity * 0.02) / (atr * ATR_STOP_MULTIPLIER)
    qty_by_margin = (equity * MAX_MARGIN_FRACTION_PER_TRADE * leverage) / price
    assert qty_by_margin < qty_by_risk, "this test assumes the margin cap binds"

    qty = calculate_position_size(equity, price, atr, 0.02, leverage=leverage)
    assert qty == pytest.approx(qty_by_margin)


def test_the_margin_cap_scales_with_leverage():
    """At 5x the same margin funds 5x the notional.

    The old notional cap got STRICTER as leverage rose — the exact opposite of
    how margin works, and why every leveraged trade was sized as though it were
    unleveraged.
    """
    equity, price, atr = 1000, 60000, 1000

    at_1x = position_size_detail(equity=equity, price=price, atr=atr,
                                 risk_per_trade_percent=0.02, leverage=1.0)
    at_5x = position_size_detail(equity=equity, price=price, atr=atr,
                                 risk_per_trade_percent=0.02, leverage=5.0)

    # The CEILING scales with leverage, which is the property under test.
    assert at_5x["maxQtyByMargin"] == pytest.approx(at_1x["maxQtyByMargin"] * 5)

    # And the consequence that matters: at 1x the ceiling binds, at 5x it stops
    # binding and RISK sizing takes over. That handover is the whole fix — the
    # old notional cap never released, so the risk budget never governed.
    assert at_1x["capBound"] is True
    assert at_5x["capBound"] is False
    assert at_5x["dollarRisk"] == pytest.approx(equity * 0.02)


def test_a_wider_stop_holds_dollar_risk_constant_when_risk_sizing_governs():
    """THE PROPERTY THE WHOLE SIZING FIX EXISTS FOR.

    Widening the stop must shrink the position so the loss at the stop is
    unchanged. Under the old notional cap it did the opposite: quantity was
    pinned, so going from a 1.5-ATR to a 3.0-ATR stop doubled risk per trade
    from $32 to $64 while looking like a safety improvement.
    """
    equity, price, atr, risk = 10_000, 100.0, 0.43, 0.005

    narrow = position_size_detail(
        equity=equity, price=price, atr=atr,
        risk_per_trade_percent=risk, leverage=5.0, stop_multiplier=1.5,
    )
    wide = position_size_detail(
        equity=equity, price=price, atr=atr,
        risk_per_trade_percent=risk, leverage=5.0, stop_multiplier=3.0,
    )

    assert not narrow["capBound"] and not wide["capBound"], (
        "this property only holds while RISK sizing governs; if the cap binds, "
        "widening the stop increases risk instead"
    )
    assert wide["qty"] < narrow["qty"]
    assert wide["dollarRisk"] == pytest.approx(narrow["dollarRisk"])
    assert wide["dollarRisk"] == pytest.approx(equity * risk)


def test_position_sizing_respects_the_risk_budget_when_cash_is_ample():
    """Isolates the risk-based branch, which the capped case above hides.

    With a stop far enough away, the risk budget is the binding constraint
    and the position is sized so that hitting the stop costs exactly
    `risk_per_trade` of equity — which is the whole point of the function.
    """
    equity = 100_000
    price = 100.0
    atr = 10.0        # stop distance 15.0, i.e. 15% away
    risk_pct = 0.02

    qty = calculate_position_size(equity, price, atr, risk_pct)

    # Not capped by margin at this equity level and this stop width.
    assert qty < (equity * MAX_MARGIN_FRACTION_PER_TRADE) / price
    # Hitting the stop loses exactly the risk budget.
    loss_at_stop = qty * (atr * ATR_STOP_MULTIPLIER)
    assert loss_at_stop == pytest.approx(equity * risk_pct)


def test_position_sizing_returns_zero_when_atr_is_unavailable():
    """No volatility estimate means no size — not an unbounded one."""
    assert calculate_position_size(1000, 60000, 0.0, 0.02) == 0.0
    assert calculate_position_size(1000, 0.0, 1000, 0.02) == 0.0
