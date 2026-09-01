"""The Monitor stage: does a position opened by the pipeline ever get closed?

Before `agents/position_monitor.py` existed the answer was no. The approved
stop-loss reached the Execution Engine, was logged, and nothing compared price
against it — while POSITION_CLOSED (consumed by the CEO and the Reflection
agent) had no publisher at all, so the learning half of the chain was dead too.
"""

import uuid

import pytest

from backend.agents.execution_agent import ExecutionAgent
from backend.agents.position_monitor import PositionMonitorAgent
from backend.core import system_state
from backend.core.message_bus import MessageBus, get_message_bus
from backend.models.events import (
    OrderFilledEvent,
    TarApprovedEvent,
    TickReceivedEvent,
)

SYMBOL = "BTC/USDT"
ENTRY = 60_000.0
STOP = 59_000.0
TARGET = 62_000.0


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr("backend.core.message_bus._bus", MessageBus())
    system_state.resume("test setup")
    system_state.exit_observation_mode("test setup")
    yield
    system_state.resume("test teardown")
    system_state.exit_observation_mode("test teardown")


def _monitor():
    execution = ExecutionAgent(simulation_mode=True)
    monitor = PositionMonitorAgent(execution_agent=execution)
    return monitor, execution


async def _open_position(monitor, execution, tar_id=None, side="buy", qty=0.01):
    tar_id = tar_id or uuid.uuid4()
    # Price must be observed before the fill, as in the real chain.
    tick = TickReceivedEvent(symbol=SYMBOL, price=ENTRY, volume=1.0, exchange="test")
    await execution.handle_event(tick)
    await monitor.handle_event(tick)

    await monitor.handle_event(
        TarApprovedEvent(
            tar_id=tar_id, symbol=SYMBOL, direction="LONG" if side == "buy" else "SHORT",
            approved_size=qty, approved_leverage=1, cro_rationale="test",
            stop_loss=STOP if side == "buy" else TARGET,
            take_profit=TARGET if side == "buy" else STOP,
            tab="paper",
        )
    )
    await monitor.handle_event(
        OrderFilledEvent(
            tar_id=tar_id, exchange="simulated_exchange", order_id="o1",
            symbol=SYMBOL, side=side, tab="paper",
            fill_price=ENTRY, fill_quantity=qty, slippage_bps=0.0, fee=0.0,
        )
    )
    return tar_id


async def _tick(monitor, execution, price):
    tick = TickReceivedEvent(symbol=SYMBOL, price=price, volume=1.0, exchange="test")
    await execution.handle_event(tick)  # so the simulated close has a price
    await monitor.handle_event(tick)


@pytest.mark.asyncio
async def test_position_is_tracked_after_a_fill():
    monitor, execution = _monitor()
    await _open_position(monitor, execution)
    assert monitor.open_position_count == 1


@pytest.mark.asyncio
async def test_stop_loss_closes_the_position():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    await _tick(monitor, execution, STOP - 1)

    assert monitor.open_position_count == 0
    assert len(closes) == 1
    assert closes[0].exit_reason == "stop-loss"
    assert closes[0].realized_pnl < 0


@pytest.mark.asyncio
async def test_take_profit_closes_the_position():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    await _tick(monitor, execution, TARGET + 1)

    assert closes[0].exit_reason == "take-profit"
    assert closes[0].realized_pnl > 0


@pytest.mark.asyncio
async def test_price_between_stop_and_target_does_nothing():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    await _tick(monitor, execution, ENTRY + 100)

    assert closes == []
    assert monitor.open_position_count == 1


@pytest.mark.asyncio
async def test_short_position_stop_is_above_entry():
    """Sign errors here are the classic way a stop becomes a guaranteed loss."""
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution, side="sell")
    # For a short, stop_loss was set to TARGET (above entry).
    await _tick(monitor, execution, TARGET + 1)

    assert len(closes) == 1
    assert closes[0].exit_reason == "stop-loss"
    assert closes[0].realized_pnl < 0, "a short stopped out above entry must lose money"


@pytest.mark.asyncio
async def test_short_position_profits_when_price_falls():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution, side="sell")
    await _tick(monitor, execution, STOP - 1)

    assert closes[0].exit_reason == "take-profit"
    assert closes[0].realized_pnl > 0


@pytest.mark.asyncio
async def test_a_large_adverse_gap_closes_at_the_stop():
    """A tick far below the stop still closes, and is labelled a stop-loss.

    Note this does NOT test stop/target precedence: for a valid long the stop
    is below the target, so no single price can satisfy both conditions. The
    precedence case is covered by
    `test_stop_wins_when_stop_and_target_are_misconfigured` below.
    """
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    await _tick(monitor, execution, STOP - 5_000)
    assert closes[0].exit_reason == "stop-loss"
    assert closes[0].realized_pnl < 0


@pytest.mark.asyncio
async def test_stop_wins_when_stop_and_target_are_misconfigured():
    """The only case where precedence is reachable: an inverted stop/target.

    The CRO rejects an inverted stop, so this shouldn't occur in the pipeline —
    but if a bad configuration ever gets through, resolving in favour of the
    stop is the conservative reading. Assuming the favourable level would
    systematically overstate performance.
    """
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    tar_id = uuid.uuid4()
    tick = TickReceivedEvent(symbol=SYMBOL, price=ENTRY, volume=1.0, exchange="test")
    await execution.handle_event(tick)
    # Inverted: stop ABOVE target. For a long, hit_stop is `price <= stop` and
    # hit_target is `price >= target`, so any price in [61000, 62000] satisfies
    # BOTH at once — which is the only way the precedence branch is reachable.
    await monitor.handle_event(
        TarApprovedEvent(
            tar_id=tar_id, symbol=SYMBOL, direction="LONG",
            approved_size=0.01, approved_leverage=1, cro_rationale="misconfigured",
            stop_loss=62_000.0, take_profit=61_000.0, tab="paper",
        )
    )
    await monitor.handle_event(
        OrderFilledEvent(
            tar_id=tar_id, exchange="simulated_exchange", order_id="o1",
            symbol=SYMBOL, side="buy", tab="paper",
            fill_price=ENTRY, fill_quantity=0.01, slippage_bps=0.0, fee=0.0,
        )
    )
    await _tick(monitor, execution, 61_500.0)

    assert len(closes) == 1
    assert closes[0].exit_reason == "stop-loss", "an ambiguous breach must resolve to the stop"


@pytest.mark.asyncio
async def test_a_fill_with_no_approved_tar_is_not_silently_ignored():
    """An unprotected position must be flagged, not dropped."""
    monitor, execution = _monitor()
    await execution.handle_event(
        TickReceivedEvent(symbol=SYMBOL, price=ENTRY, volume=1.0, exchange="test")
    )
    await monitor.handle_event(
        OrderFilledEvent(
            tar_id=uuid.uuid4(), exchange="simulated_exchange", order_id="orphan",
            symbol=SYMBOL, side="buy", tab="paper",
            fill_price=ENTRY, fill_quantity=0.01, slippage_bps=0.0, fee=0.0,
        )
    )
    assert monitor.open_position_count == 0
    explanation = monitor.explain_decision()
    assert explanation["decision"] == "unprotected-fill"
    assert explanation["acted"] is False


@pytest.mark.asyncio
async def test_a_stop_still_fires_while_the_system_is_paused():
    """CLAUDE.md invariant 4. A stop-loss that stops firing the moment the
    system is paused is not a stop-loss."""
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    system_state.pause("operator paused while holding a position")
    await _tick(monitor, execution, STOP - 1)

    assert len(closes) == 1, "the stop must fire even while paused"
    assert closes[0].exit_reason == "stop-loss"


@pytest.mark.asyncio
async def test_a_stop_still_fires_during_an_emergency_stop():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    system_state.trigger_emergency_stop("test")
    await _tick(monitor, execution, STOP - 1)

    assert len(closes) == 1, "the stop must fire even during an emergency stop"


@pytest.mark.asyncio
async def test_a_stop_still_fires_in_observation_mode():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    system_state.enter_observation_mode("drawdown breach")
    await _tick(monitor, execution, STOP - 1)

    assert len(closes) == 1, "the stop must fire in observation mode"


@pytest.mark.asyncio
async def test_a_failed_close_keeps_the_position_tracked():
    """Dropping it would leave an open position with nothing watching it."""
    monitor, execution = _monitor()

    async def failing_close(**_kw):
        return None

    await _open_position(monitor, execution)
    execution.close_position = failing_close

    await _tick(monitor, execution, STOP - 1)
    assert monitor.open_position_count == 1, "a failed close must not drop the position"


@pytest.mark.asyncio
async def test_realized_pnl_uses_the_actual_fill_not_the_trigger_price():
    """The two differ by slippage; using the trigger would report the P&L we
    hoped for rather than the one we got."""
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution, qty=1.0)

    slipped_fill = STOP - 500.0

    async def slipping_close(**_kw):
        return slipped_fill

    execution.close_position = slipping_close
    await _tick(monitor, execution, STOP - 1)

    assert len(closes) == 1
    assert closes[0].exit_price == slipped_fill
    assert closes[0].realized_pnl == pytest.approx((slipped_fill - ENTRY) * 1.0)


@pytest.mark.asyncio
async def test_ticks_for_other_symbols_do_not_close_the_position():
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    other = TickReceivedEvent(symbol="ETH/USDT", price=1.0, volume=1.0, exchange="test")
    await monitor.handle_event(other)

    assert closes == []
    assert monitor.open_position_count == 1


@pytest.mark.asyncio
async def test_a_zero_price_tick_is_ignored_not_treated_as_a_collapse():
    """A zero tick is missing data. Treating it as a price would stop out every
    open position at once."""
    monitor, execution = _monitor()
    closes = []
    get_message_bus().subscribe("POSITION_CLOSED", lambda e: closes.append(e))

    await _open_position(monitor, execution)
    await monitor.handle_event(
        TickReceivedEvent(symbol=SYMBOL, price=0.0, volume=0.0, exchange="test")
    )

    assert closes == []
    assert monitor.open_position_count == 1


@pytest.mark.asyncio
async def test_monitor_may_close_but_not_open():
    """A monitoring loop must not become a second entry path around Risk."""
    monitor, _ = _monitor()
    assert "CLOSE_POSITIONS" in monitor.permissions
    assert "ROUTE_ORDERS" not in monitor.permissions
    assert "TAR_SUBMITTED" not in monitor.events_published
    assert monitor.events_published == ["POSITION_CLOSED"]


# ---------------------------------------------------------------------------
# The closing trade, and its realized P&L
# ---------------------------------------------------------------------------
#
# WHY THIS WAS MISSING AND WHAT IT COST
# -------------------------------------
# `execution_agent._persist_trade` writes the OPENING fill, and an opening fill
# has no P&L — the trade has not finished. Nothing wrote the CLOSE. Measured
# against the live database: of 2,626 rows in `trades`, 2,623 (every `agent-plan`
# row) had `pnl IS NULL`.
#
# `lib/api/portfolio.realised()` counts only rows where `typeof t.pnl === 'number'`,
# so it counted three. The P&L dashboard read "no trade carries a pnl", the win
# rate was `null`, and biggest-win and max-drawdown were blank — on a system that
# had executed thousands of trades.


class _RecordingConn:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, *args):
        self._rows.append((sql, args))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingPool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        return _RecordingConn(self._rows)


@pytest.mark.asyncio
async def test_a_closed_position_is_persisted_with_its_realized_pnl(monkeypatch):
    """The row the P&L dashboard, win rate and trade log all read."""
    rows: list = []
    # Patched at the SOURCE, not on the position_monitor namespace.
    # `_persist_closed_trade` does `from backend.core.db import get_db_pool` inside
    # the function, so it resolves the name from `backend.core.db` at call time —
    # a monkeypatch on `pm.get_db_pool` would bind a name nothing reads.
    monkeypatch.setattr("backend.core.db.get_db_pool", lambda: _RecordingPool(rows))

    monitor, execution = _monitor()
    await _open_position(monitor, execution)

    # Through the stop. The tick goes to BOTH agents, as it does on the real bus:
    # the executor prices the simulated exit from its own `_last_prices`, so a tick
    # delivered only to the monitor would fill the close at the ENTRY price and
    # report a realized P&L of exactly zero.
    stop_tick = TickReceivedEvent(symbol=SYMBOL, price=STOP - 100, volume=1.0, exchange="test")
    await execution.handle_event(stop_tick)
    await monitor.handle_event(stop_tick)

    inserts = [r for r in rows if "INSERT INTO trades" in r[0]]
    assert inserts, "closing the position wrote no trade row — the P&L dashboard stays empty"

    args = inserts[0][1]
    # (id, ts, tab, symbol, side, qty, price, pnl, origin_tag, note)
    assert args[3] == SYMBOL
    assert args[4] == "sell", "the exit of a long is a SELL; recording it as a buy reads as two opens"
    assert isinstance(args[7], float), "the closing row must carry a realized pnl"
    assert args[7] < 0, "a long stopped out below entry realized a loss"
    assert args[8] == "agent-close"


@pytest.mark.asyncio
async def test_a_missing_database_does_not_break_the_close(monkeypatch, caplog):
    """The position IS closed and the money has already moved.

    Raising here would leave the caller believing the close failed and retrying an
    exit for a position that is already flat — the double-exit the
    persist-before-publish ordering exists to prevent.
    """
    import logging

    monkeypatch.setattr("backend.core.db.get_db_pool", lambda: None)

    monitor, execution = _monitor()
    await _open_position(monitor, execution)

    stop_tick = TickReceivedEvent(symbol=SYMBOL, price=STOP - 100, volume=1.0, exchange="test")
    await execution.handle_event(stop_tick)
    with caplog.at_level(logging.ERROR):
        await monitor.handle_event(stop_tick)

    assert monitor.open_position_count == 0, "the close must still happen"
    assert "did NOT persist" in caplog.text
