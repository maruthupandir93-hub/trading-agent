"""A fixed profit target — so a trade ends as a win or a stop, never a scratch.

THE PROBLEM, FROM THE LIVE LEDGER
---------------------------------
Of 4,003 closed trades, 2,136 (53.4%) realised less than 0.001, and 1,121 of
those exited tagged "stop-loss". That is not the market — it is the system's own
scale-out. The partial banks half at +1R and moves the RUNNER's stop to
break-even, so the runner's single most likely outcome is an exit at ~0.00.

The operator described it exactly: "one trade starts and takes a profit and
holding and sometimes the price is reverse and go to loss and when it reach the
0.00 it finishes".

A fixed percentage target closes the WHOLE position at once. There is no runner
left at break-even, so the scratch cannot happen.

WHAT THE PERCENTAGE MEANS. A favourable move of that much in PRICE. With leverage
it is amplified against the margin — at 3x a 2% move is ~6% of margin, at 10x
~20%. Price rather than equity so the setting means the same thing at every
leverage the session might use.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core.message_bus import MessageBus
from backend.models.events import OrderFilledEvent, TarApprovedEvent


@pytest.fixture
def bus(monkeypatch):
    private = MessageBus()
    import backend.core.message_bus as mb

    monkeypatch.setattr(mb, "get_message_bus", lambda: private)
    for module in ("backend.core.agent_base", "backend.agents.position_monitor"):
        try:
            mod = __import__(module, fromlist=["get_message_bus"])
            if hasattr(mod, "get_message_bus"):
                monkeypatch.setattr(mod, "get_message_bus", lambda: private)
        except (ImportError, AttributeError):
            pass
    return private


class FakeExecution:
    def __init__(self, fill=None):
        self.closes = []
        self._fill = fill

    async def close_position(self, **kw):
        self.closes.append(kw)
        return self._fill if self._fill is not None else kw.get("_px", 100.0)


@pytest.fixture
def monitor(bus):
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    return agent


def _open(monitor, entry=100.0, stop=97.5, side="buy", qty=1.0):
    tar_id = uuid.uuid4()
    asyncio.run(monitor.handle_event(TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT", direction="long" if side == "buy" else "short",
        approved_size=qty, approved_leverage=3, cro_rationale="ok",
        stop_loss=stop, tab="paper",
        take_profit=(entry + 5000) if side == "buy" else (entry - 5000),
    )))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="x", order_id=str(uuid.uuid4()), symbol="SOL/USDT",
        side=side, tab="paper", fill_price=entry, fill_quantity=qty,
        slippage_bps=1.0, fee=0.0,
    )))
    return monitor._open[str(tar_id)]


def _tick(monitor, price):
    asyncio.run(monitor._check_price("SOL/USDT", price))


# ---------------------------------------------------------------------------
# The target itself
# ---------------------------------------------------------------------------

def test_it_is_off_by_default(monkeypatch):
    """0 preserves the previous ATR-target + scale-out behaviour exactly."""
    import backend.agents.position_monitor as pm

    assert pm.PROFIT_TARGET_PCT == 0.0


def test_the_whole_position_closes_at_the_target(monkeypatch, monitor):
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=102.0)
    monitor.attach_execution(ex)

    pos = _open(monitor, entry=100.0)
    _tick(monitor, 101.0)                      # +1%, not yet
    assert not ex.closes

    _tick(monitor, 102.0)                      # +2% — bank it
    assert len(ex.closes) == 1
    assert ex.closes[0]["qty"] == pytest.approx(1.0), "the WHOLE position, not half"
    assert ex.closes[0]["reason"] == "profit-target"


def test_a_short_reaches_the_target_on_a_FALL(monkeypatch, monitor):
    """This agent shorts perpetuals. A long-only target would leave every short
    running to its ATR target while longs banked at 2%."""
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=98.0)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=102.5, side="sell")
    _tick(monitor, 99.0)
    assert not ex.closes
    _tick(monitor, 98.0)                       # -2% is +2% for a short
    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "profit-target"


def test_an_adverse_move_never_triggers_it(monkeypatch, monitor):
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=98.0)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=90.0)
    _tick(monitor, 98.0)                       # -2%: a LOSS, not a target
    assert [c for c in ex.closes if c.get("reason") == "profit-target"] == []


# ---------------------------------------------------------------------------
# The scratch it exists to remove
# ---------------------------------------------------------------------------

def test_the_scale_out_is_bypassed_while_the_target_is_on(monkeypatch, monitor):
    """THE WHOLE POINT. Leaving both on would scale out at +1R, move the stop to
    break-even, and reintroduce exactly the ~0.00 exit this removes.

    Entry 100, stop 97.5 -> 1R = 2.5, so +1R is 102.5. A 2% target fires at 102.0
    FIRST, closing everything, so no runner is ever left at break-even.
    """
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.5)
    ex = FakeExecution(fill=102.0)
    monitor.attach_execution(ex)

    pos = _open(monitor, entry=100.0, stop=97.5, qty=1.0)
    _tick(monitor, 102.0)

    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "profit-target"
    assert pos.partial_done is not True, "no scale-out should have happened"


def test_with_the_target_off_the_scale_out_still_runs(monkeypatch, monitor):
    """Turning the target off must restore the old behaviour completely, not
    leave the partial disabled as a side effect."""
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 0.0)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.5)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_R", 1.0)
    ex = FakeExecution(fill=102.5)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=97.5, qty=1.0)
    _tick(monitor, 102.5)                      # +1R
    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "partial-tp"


def test_the_stop_still_wins_a_tick_that_spans_both(monkeypatch, monitor):
    """Invariant: a candle through the stop AND the target is assumed to have hit
    the stop first. Assuming the favourable one would overstate performance."""
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=97.5)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=97.5)
    _tick(monitor, 97.0)                       # through the stop
    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "stop-loss"
