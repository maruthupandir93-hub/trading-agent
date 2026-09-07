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


def _fraction(monkeypatch, value: float) -> None:
    """Pin the running session's allocation as the gateway sees it."""
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_capital_fraction", lambda: value
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
    """25% must size a quarter of what 100% sizes, on the same setup."""
    _fraction(monkeypatch, 1.0)
    full = gate(_state())
    assert full["risk_assessment"].approved is True
    full_size = full["execution_plan"].size

    _fraction(monkeypatch, 0.25)
    quarter = gate(_state())
    assert quarter["risk_assessment"].approved is True
    quarter_size = quarter["execution_plan"].size

    assert quarter_size < full_size
    # Sizing is linear in equity here (the margin cap binds proportionally), so a
    # quarter allocation is about a quarter of the size.
    assert quarter_size == pytest.approx(full_size * 0.25, rel=0.05)


def test_full_allocation_is_byte_for_byte_the_old_behaviour(monkeypatch):
    """1.0 must not change sizing at all — the feature is opt-in."""
    _fraction(monkeypatch, 1.0)
    out = gate(_state())
    # No CapitalPool check appears at full allocation.
    assert "CapitalPool" not in out["risk_assessment"].checks


def test_a_fully_deployed_pool_rejects_new_trades(monkeypatch):
    """At 50%, once half the account is committed no new trade opens."""
    _fraction(monkeypatch, 0.5)
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
    _fraction(monkeypatch, 0.75)
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
