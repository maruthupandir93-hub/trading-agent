"""Partial profit-taking — bank half at +1R, run the rest from break-even.

The operator's exact complaint: a position goes +1-2%, the stop trails to
break-even, price drifts back to entry, and it closes at 0.0 — the whole gain
evaporates. Scale-out fixes that: at +PARTIAL_TP_R the monitor closes
PARTIAL_TP_FRACTION of the position (a real, banked profit) and moves the runner's
stop to break-even. Worst case becomes "banked ~1% and the runner scratched", not
"gave it all back".

These are unit tests on the monitor with a fake execution engine — no exchange, no
DB — so they pin the DECISION logic: when a scale-out fires, how much it banks,
that the runner's stop moves to break-even, and that it fires exactly once.
"""

import asyncio
import uuid

import pytest

from backend.core.message_bus import MessageBus
from backend.models.events import OrderFilledEvent, TarApprovedEvent, TickReceivedEvent


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
    """Records closes; returns the fill price the test pins."""

    def __init__(self, fill_price):
        self.closes = []
        self._fill_price = fill_price

    async def close_position(self, **kwargs):
        self.closes.append(kwargs)
        return self._fill_price


@pytest.fixture
def monitor(bus, monkeypatch):
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    # THE FIXED PROFIT TARGET IS PINNED OFF. It defaults to 2% now and it
    # deliberately BYPASSES the scale-out: a target closes the whole position, so
    # no runner is left to scale out of — which is the point, since the runner
    # sitting at break-even is what closed 53.4% of this system's trades at ~0.00.
    #
    # This file tests the scale-out itself, so it opts out of the thing that
    # replaces it. Leaving it on would make every test here assert the target's
    # behaviour under the scale-out's name.
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 0.0)
    return agent


def _open(monitor, entry, stop, qty=0.05, side="buy", tab="paper"):
    """Register an approved fill and return its tar_id."""
    tar_id = uuid.uuid4()
    appr = TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT", direction="long" if side == "buy" else "short",
        approved_size=qty, approved_leverage=5, cro_rationale="ok",
        stop_loss=stop, tab=tab, take_profit=entry + 6000 if side == "buy" else entry - 6000,
    )
    asyncio.run(monitor.handle_event(appr))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="x", order_id=str(uuid.uuid4()), symbol="SOL/USDT",
        side=side, tab=tab, fill_price=entry, fill_quantity=qty, slippage_bps=1.0, fee=0.1,
    )))
    return str(tar_id)


def test_a_long_scales_out_half_at_plus_one_R_and_moves_stop_to_breakeven(monitor):
    # entry 70000, stop 68000 -> risk 2000 -> +1R is 72000.
    monitor.attach_execution(FakeExecution(fill_price=72_000.0))
    tar_id = _open(monitor, entry=70_000.0, stop=68_000.0, qty=0.05)

    # Below +1R: nothing banks.
    asyncio.run(monitor._check_price("SOL/USDT", 71_000.0))
    assert monitor._execution.closes == []

    # At +1R: bank half, move stop to break-even, stay open on the runner.
    asyncio.run(monitor._check_price("SOL/USDT", 72_000.0))

    assert len(monitor._execution.closes) == 1
    banked = monitor._execution.closes[0]
    assert banked["reason"] == "partial-tp"
    assert banked["qty"] == pytest.approx(0.025)          # half of 0.05
    pos = monitor.snapshot_open()[0]
    assert pos["qty"] == pytest.approx(0.025)             # runner is the other half
    assert float(pos["stopLoss"]) == pytest.approx(70_000.0)  # break-even
    assert monitor.open_position_count == 1               # NOT a full close


def test_the_scale_out_fires_only_once(monitor):
    monitor.attach_execution(FakeExecution(fill_price=72_000.0))
    _open(monitor, entry=70_000.0, stop=68_000.0, qty=0.05)

    asyncio.run(monitor._check_price("SOL/USDT", 72_000.0))
    assert len(monitor._execution.closes) == 1

    # Even higher — a second scale-out must NOT fire (partial_done, and the
    # break-even stop makes the R multiple undefined).
    asyncio.run(monitor._check_price("SOL/USDT", 73_500.0))
    assert len(monitor._execution.closes) == 1


def test_after_scaling_out_a_pullback_to_entry_closes_the_runner_at_breakeven(monitor):
    monitor.attach_execution(FakeExecution(fill_price=72_000.0))
    _open(monitor, entry=70_000.0, stop=68_000.0, qty=0.05)

    asyncio.run(monitor._check_price("SOL/USDT", 72_000.0))   # scale out, stop -> 70000
    assert monitor.open_position_count == 1

    # Price falls back to entry: the runner's break-even stop closes it. The banked
    # half is already realised, so the trade nets a profit instead of 0.0.
    monitor._execution._fill_price = 70_000.0
    asyncio.run(monitor._check_price("SOL/USDT", 70_000.0))
    assert monitor.open_position_count == 0
    # Two closes total: the partial, then the runner.
    assert [c["reason"] for c in monitor._execution.closes] == ["partial-tp", "stop-loss"]


def test_a_short_scales_out_at_plus_one_R_too(monitor):
    # short entry 70000, stop 72000 -> risk 2000 -> +1R is 68000 (price falls).
    monitor.attach_execution(FakeExecution(fill_price=68_000.0))
    _open(monitor, entry=70_000.0, stop=72_000.0, qty=0.05, side="sell")

    asyncio.run(monitor._check_price("SOL/USDT", 68_000.0))
    assert len(monitor._execution.closes) == 1
    pos = monitor.snapshot_open()[0]
    assert pos["qty"] == pytest.approx(0.025)
    assert float(pos["stopLoss"]) == pytest.approx(70_000.0)  # break-even
