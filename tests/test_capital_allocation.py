"""Session capital allocation — 'trade with 25 / 50 / 75 / 100% of my balance'.

The operator picks, per session, how much of the account the agent may deploy. It
does two things, both in the Risk Gateway: it SIZES each trade against that
fraction of the account, and it CAPS the total margin committed at once at that
fraction. It is not a leverage source — the leverage ceiling and mandatory stop
are untouched — so 100% means 'use the whole account as margin', never 'use more
leverage'. 1.0 (no session) is the pre-feature behaviour exactly.
"""

from __future__ import annotations

import pytest

from backend.graphs.nodes.risk_gateway import _deployed_margin, gate
from backend.graphs.state import (
    MarketSnapshot,
    PortfolioStateSnapshot,
    TechnicalAnalysis,
    TradeDecision,
    TradeThesis,
    TradingState,
    new_state,
)
from backend.graphs.triggers import TriggerReason


def _candles():
    out = []
    price = 100.0
    for i in range(60):
        out.append({"time": i, "open": price, "high": price + 1, "low": price - 1,
                    "close": price, "volume": 1000.0})
    return out


def _state(**over) -> TradingState:
    st = new_state(
        run_id="cap-test", symbol="BTC/USDT",
        trigger=TriggerReason(kind="manual", symbol="BTC/USDT", detail="t"),
        started_at=0.0,
    )
    st.update(
        decision=TradeDecision(action="TRADE", direction="LONG", probability=None),
        trade_thesis=TradeThesis(direction="LONG", strategy="Trend", entry_price=100.0,
                                 stop_loss=98.0, take_profit=104.0),
        technical_analysis=TechnicalAnalysis(atr=1.3),
        market_data=MarketSnapshot(symbol="BTC/USDT", price=100.0,
                                   candles={"15m": _candles()}),
        portfolio_state=PortfolioStateSnapshot(tab="paper", equity=10_000.0,
                                               cash=10_000.0, open_positions=[]),
    )
    st.update(over)
    return st


@pytest.fixture(autouse=True)
def _empty_ledger(monkeypatch):
    monkeypatch.setattr(
        "backend.services.ai_memory.get_memory_stats",
        lambda: {"trade_ledger": []},
    )
    # THE SCOPE GATES ARE TURNED OFF FOR THIS FILE, DELIBERATELY.
    #
    # Every test here drives `gate()` to check SIZING — how the capital fraction
    # and leverage turn into a quantity. The session-scope and one-position-at-a-
    # time gates run BEFORE sizing and would refuse these states for reasons that
    # have nothing to do with what is being measured, so leaving them on would
    # make this file assert "the gate rejected" over and over while testing none
    # of the arithmetic it exists to pin.
    #
    # Their own behaviour is covered in `tests/test_session_scope.py`, including
    # the fact that they run ahead of sizing.
    monkeypatch.setenv("SESSION_ONLY_TRADING", "false")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "99")


def _session(monkeypatch, fraction: float, leverage: int = 1) -> None:
    """Pin the running session's allocation AND leverage as the gateway sees them.

    Both are needed now: broker-style sizing (allocation = margin pool, leverage
    turns it into notional) only runs when a session is driving the trade, which
    `active_session_leverage()` returning non-None is what signals.
    """
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_capital_fraction", lambda: fraction
    )
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_session_leverage", lambda: leverage
    )


# ---------------------------------------------------------------------------
# _deployed_margin — the capital already in play
# ---------------------------------------------------------------------------


def test_deployed_margin_reads_marginLocked():
    positions = [{"symbol": "SOL/USDT", "qty": 10, "avgCost": 100, "marginLocked": 250.0},
                 {"symbol": "ETH/USDT", "qty": 1, "avgCost": 2000, "marginLocked": 400.0}]
    assert _deployed_margin(positions) == 650.0


def test_deployed_margin_falls_back_to_notional_over_leverage():
    """A venue position without marginLocked: margin = notional / leverage."""
    positions = [{"symbol": "SOL/USDT", "qty": 2, "avgCost": 100, "leverage": 4}]  # 200/4
    assert _deployed_margin(positions) == 50.0


def test_deployed_margin_treats_unknown_leverage_as_1x_conservatively():
    """Over-counting margin caps the pool SOONER, never later."""
    positions = [{"symbol": "SOL/USDT", "qty": 2, "avgCost": 100}]  # no leverage -> 1x -> 200
    assert _deployed_margin(positions) == 200.0


def test_deployed_margin_ignores_malformed_rows():
    assert _deployed_margin([{"garbage": True}, {}]) == 0.0


# ---------------------------------------------------------------------------
# active_capital_fraction
# ---------------------------------------------------------------------------


def test_no_session_is_full_allocation(monkeypatch):
    import backend.services.trading_session as ts

    monkeypatch.setattr(ts, "active_session", lambda: None)
    assert ts.active_capital_fraction() == 1.0


def test_a_bad_fraction_clamps_to_full(monkeypatch):
    import backend.services.trading_session as ts

    class _S:
        capital_fraction = 5.0  # nonsense
    monkeypatch.setattr(ts, "active_session", lambda: _S())
    assert ts.active_capital_fraction() == 1.0

    class _S2:
        capital_fraction = 0.75
    monkeypatch.setattr(ts, "active_session", lambda: _S2())
    assert ts.active_capital_fraction() == 0.75


# ---------------------------------------------------------------------------
# The gateway: sizing scales, and the pool caps
# ---------------------------------------------------------------------------


def test_a_smaller_allocation_makes_a_smaller_trade(monkeypatch):
    """A smaller allocation deploys proportionally less margin.

    Compared below the margin buffer (25% vs 50%, both pool-bound) so the ratio is
    exact. At 100% the 1.2x margin buffer caps deployment at ~83%, so the ratio to a
    sub-buffer allocation is not the raw fraction — that is asserted separately.
    """
    _session(monkeypatch, 0.5, leverage=1)
    half = gate(_state())
    assert half["risk_assessment"].approved is True
    half_size = half["execution_plan"].size

    _session(monkeypatch, 0.25, leverage=1)
    quarter = gate(_state())
    assert quarter["risk_assessment"].approved is True
    quarter_size = quarter["execution_plan"].size

    assert quarter_size < half_size
    assert quarter_size == pytest.approx(half_size * 0.5, rel=0.02)


def test_leverage_multiplies_the_notional(monkeypatch):
    """The operator's leverage is honoured: 5x deploys 5x the notional of 1x.

    This is the fix for 'my leverage did nothing'. On the same 50% allocation and
    the same account, a 5x session's position is five times a 1x session's — the
    Binance/Bybit concept, where allocated margin times leverage is the notional.
    """
    _session(monkeypatch, 0.5, leverage=1)
    one_x = gate(_state())
    assert one_x["risk_assessment"].approved is True

    _session(monkeypatch, 0.5, leverage=5)
    five_x = gate(_state())
    assert five_x["risk_assessment"].approved is True
    assert five_x["execution_plan"].leverage == 5
    assert five_x["execution_plan"].size == pytest.approx(one_x["execution_plan"].size * 5, rel=0.02)


def test_full_allocation_deploys_the_WHOLE_account(monkeypatch):
    """100% MEANS 100%, less only the entry fee.

    This used to assert 83.3% — the account divided by the 1.2x margin buffer —
    and that haircut was the operator's bug report: "I chose 100% allocation but
    it only takes some amount." A control that silently delivers five-sixths of
    what it says is worse than one that refuses.

    The buffer was a proxy for "keep the stop reachable before a margin call".
    That is now guaranteed directly by `liquidation_safe_leverage`, which caps
    leverage until the stop provably sits inside the liquidation distance, and by
    isolated margin, which bounds a position to its own margin. Withholding a
    sixth of the capital was approximating a guarantee that now exists.

    ONLY THE ENTRY FEE IS RESERVED, because `apply_paper_fill` refuses a fill when
    `margin + fee > free_cash` — sizing to literally the whole balance would pass
    every risk check and then silently fail to open.
    """
    from backend.services.fees import taker_rate

    _session(monkeypatch, 1.0, leverage=1)
    out = gate(_state())
    assert out["risk_assessment"].approved is True

    reserve = 10_000.0 * taker_rate() * 1.5
    assert out["execution_plan"].size == pytest.approx((10_000.0 - reserve) / 100.0, rel=0.001)
    # and that is essentially the whole account, not five-sixths of it
    assert out["execution_plan"].size * 100.0 > 9_900.0


def test_no_session_uses_risk_based_sizing_not_the_pool(monkeypatch):
    """With no session running, the autonomous 1x risk-based path runs unchanged."""
    # active_session_leverage is the REAL function here (no session) -> None.
    import backend.services.trading_session as ts
    monkeypatch.setattr(ts, "active_session", lambda: None)
    out = gate(_state())
    assert out["risk_assessment"].approved is True
    assert "CapitalPool" not in out["risk_assessment"].checks


def test_a_fully_deployed_pool_rejects_new_trades(monkeypatch):
    """At 50%, once half the account is committed no new trade opens."""
    _session(monkeypatch, 0.5, leverage=1)
    # Account capital = cash 4000 + deployed 6000 = 10000; pool = 50% = 5000.
    # Deployed 6000 >= 5000 -> reject.
    st = _state(portfolio_state=PortfolioStateSnapshot(
        tab="paper", equity=10_000.0, cash=4_000.0,
        open_positions=[{"symbol": "SOL/USDT", "qty": 60, "avgCost": 100, "marginLocked": 6_000.0}],
    ))
    out = gate(st)
    assert out["risk_assessment"].approved is False
    assert "CapitalPool" in out["risk_assessment"].checks
    assert "pool is fully deployed" in " ".join(out["risk_assessment"].rejection_reasons)


def test_room_left_in_the_pool_still_trades(monkeypatch):
    """The complement: with the pool not yet full, a trade is approved."""
    _session(monkeypatch, 0.75, leverage=1)
    st = _state(portfolio_state=PortfolioStateSnapshot(
        tab="paper", equity=10_000.0, cash=9_000.0,
        open_positions=[{"symbol": "SOL/USDT", "qty": 10, "avgCost": 100, "marginLocked": 1_000.0}],
    ))
    out = gate(st)
    assert out["risk_assessment"].approved is True


# ---------------------------------------------------------------------------
# The session carries it
# ---------------------------------------------------------------------------


def test_the_session_dataclass_defaults_to_full():
    from backend.services.trading_session import TradingSession

    s = TradingSession(id="x", symbol="SOL/USDT", leverage=3, start_equity=100.0,
                       target_equity=110.0, floor_equity=50.0)
    assert s.capital_fraction == 1.0
