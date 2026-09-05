"""The closing trade row must carry attribution, or the agent cannot learn.

WHY THIS FILE EXISTS
====================
`strategy_performance.aggregate` reads:

    WHERE pnl IS NOT NULL AND strategy IS NOT NULL

Only a CLOSE carries a `pnl`. Only an OPEN carried a `strategy`. **The
intersection was always empty**, so that query could never return a single row,
every profile's `historical_success_rate` stayed None forever, and the 0.2
track-record weight in strategy scoring was permanently neutral (0.5). The
learning loop was wired end to end and structurally incapable of producing a
number.

It was invisible from the outside. The panel said "No strategy has closed a trade
yet", which is exactly what an honest, empty, WORKING loop also says.

Confirmed against the live database before fixing:

    12 closed trades, strategy NULL on all 12
    aggregate() -> {} on an account that had been trading for three days

The chain already carried the attribution — plan -> TAR -> CRO -> TAR_APPROVED.
`PositionMonitorAgent.handle_event` was the hop that dropped it: it copied four
fields out of the approval into `_pending` and the approval was the LAST place
those three existed, because `OrderFilledEvent` does not carry them.

THE SECOND BUG IN HERE IS SAFETY-CRITICAL AND WAS FOUND THE SAME WAY.
`_watch_rows` never emitted `stop_order_id`, and `save_watch_list` binds by name
from `_FIELDS` — so the column was written NULL on every row it ever stored. The
schema comment explained precisely why the column mattered ("a restart must be
able to CANCEL it") and nothing had ever put a value in it.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from backend.agents.position_monitor import (
    PositionMonitorAgent,
    get_position_monitor,
    reset_position_monitor,
)
from backend.models.events import OrderFilledEvent, TarApprovedEvent
from backend.services.position_store import _FIELDS


@pytest.fixture(autouse=True)
def _clean():
    reset_position_monitor()
    yield
    reset_position_monitor()


def _approval(tar_id: str, **over) -> TarApprovedEvent:
    kw = dict(
        tar_id=uuid.UUID(tar_id),
        symbol="SOL/USDT",
        direction="LONG",
        approved_size=10.0,
        approved_leverage=3,
        cro_rationale="ok",
        stop_loss=95.0,
        take_profit=110.0,
        tab="paper",
        run_id="run-abc",
        strategy="TrendFollowing",
        entry_context="SOL/USDT @ 15m: RSI(14)=41.0, regime=Trending Bullish",
    )
    kw.update(over)
    return TarApprovedEvent(**kw)


def _fill(tar_id: str) -> OrderFilledEvent:
    return OrderFilledEvent(
        tar_id=uuid.UUID(tar_id),
        order_id="o-1",
        symbol="SOL/USDT",
        side="buy",
        fill_quantity=10.0,
        fill_price=100.0,
        exchange="binance",
        tab="paper",
        slippage_bps=0.0,
        fee=0.0,
    )


TAR = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# The attribution chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_approval_is_the_LAST_place_attribution_exists():
    """`OrderFilledEvent` does not carry strategy, run_id or entry_context.

    So anything the TAR handler does not keep is gone by the time the position
    closes — there is nowhere later to recover it from. This asserts the premise
    rather than assuming it, because if a future fill event DID carry them, the
    reasoning below would change.
    """
    fill = _fill(TAR)
    for field in ("strategy", "run_id", "entry_context"):
        assert getattr(fill, field, None) is None


@pytest.mark.asyncio
async def test_the_pending_approval_keeps_attribution():
    monitor = get_position_monitor()
    await monitor.handle_event(_approval(TAR))

    pending = monitor._pending[TAR]
    assert pending["strategy"] == "TrendFollowing"
    assert pending["run_id"] == "run-abc"
    assert "RSI(14)=41.0" in pending["entry_context"]


@pytest.mark.asyncio
async def test_attribution_reaches_the_TRACKED_position():
    monitor = get_position_monitor()
    await monitor.handle_event(_approval(TAR))
    await monitor.handle_event(_fill(TAR))

    pos = monitor._open[TAR]
    assert pos.strategy == "TrendFollowing"
    assert pos.run_id == "run-abc"
    assert pos.entry_context is not None


@pytest.mark.asyncio
async def test_the_CLOSING_row_is_written_with_the_strategy(monkeypatch):
    """THE test in this file — the row `strategy_performance` actually reads.

    Everything above is plumbing; this is the assertion that the learning loop
    can produce a number at all.
    """
    monitor = get_position_monitor()
    await monitor.handle_event(_approval(TAR))
    await monitor.handle_event(_fill(TAR))
    pos = monitor._open[TAR]

    captured: dict = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    # Patched on the SOURCE module: `_persist_closed_trade` imports `get_db_pool`
    # inside the function, so a name bound on `position_monitor` is never read.
    import backend.core.db as db

    monkeypatch.setattr(db, "get_db_pool", lambda: _Pool())
    await monitor._persist_closed_trade(pos, exit_price=110.0, realized=42.0, reason="target")

    assert "strategy" in captured["sql"], "the closing INSERT does not name the column"
    assert "TrendFollowing" in captured["args"], (
        "the closing row carries no strategy, so `strategy_performance`'s "
        "`WHERE pnl IS NOT NULL AND strategy IS NOT NULL` can never match it"
    )
    assert "run-abc" in captured["args"]


@pytest.mark.asyncio
async def test_a_manual_position_closes_with_NO_strategy_rather_than_a_made_up_one():
    """A human's click was not chosen by an algorithm.

    Attributing it to one would credit or blame a strategy for an outcome it had
    no part in — and that number then steers future selection.
    """
    monitor = get_position_monitor()
    await monitor.track_manual_position(
        symbol="SOL/USDT", side="buy", qty=1.0, entry_price=100.0,
        stop_loss=95.0, take_profit=110.0, tab="paper",
    )
    pos = next(iter(monitor._open.values()))
    assert pos.strategy is None


# ---------------------------------------------------------------------------
# Persistence — the fields must survive a restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_persisted_field_is_actually_emitted_by_the_watch_rows():
    """THE BUG THIS CATCHES, generalised.

    `save_watch_list` binds `row.get(f) for f in _FIELDS`. A field named in
    `_FIELDS` but never SET by `_watch_rows` is silently written as NULL — no
    error, no warning, and the column looks present in the schema. That is
    exactly what happened to `stop_order_id`, whose whole purpose was surviving a
    restart so the orphaned venue stop could be cancelled.
    """
    monitor = get_position_monitor()
    await monitor.handle_event(_approval(TAR))
    await monitor.handle_event(_fill(TAR))
    monitor._open[TAR].stop_order_id = "venue-stop-9"

    rows = monitor._watch_rows()
    assert rows, "no rows produced"
    for row in rows:
        missing = [f for f in _FIELDS if f not in row]
        assert not missing, (
            f"{missing} are in _FIELDS but never set by _watch_rows, so they persist "
            f"as NULL on every row"
        )


@pytest.mark.asyncio
async def test_the_stop_order_id_actually_reaches_the_stored_row():
    monitor = get_position_monitor()
    await monitor.handle_event(_approval(TAR))
    await monitor.handle_event(_fill(TAR))
    monitor._open[TAR].stop_order_id = "venue-stop-9"

    open_row = [r for r in monitor._watch_rows() if r["status"] == "open"][0]
    assert open_row["stop_order_id"] == "venue-stop-9"
    assert open_row["strategy"] == "TrendFollowing"


@pytest.mark.asyncio
async def test_a_pending_approval_stores_attribution_too():
    """A restart between approval and fill must not lose it — that window is the
    entire reason pending rows are persisted at all."""
    monitor = get_position_monitor()
    await monitor.handle_event(_approval(TAR))

    pending_row = [r for r in monitor._watch_rows() if r["status"] == "pending"][0]
    assert pending_row["strategy"] == "TrendFollowing"
    assert pending_row["run_id"] == "run-abc"
    # No fill yet, so no venue stop exists to record.
    assert pending_row["stop_order_id"] is None


@pytest.mark.asyncio
async def test_restoring_rebuilds_attribution(monkeypatch):
    """Otherwise the gap reopens one restart at a time."""
    import backend.services.position_store as store

    stored = [{
        "tar_id": TAR, "status": "open", "symbol": "SOL/USDT", "tab": "paper",
        "side": "buy", "qty": 10.0, "entry_price": 100.0, "stop_loss": 95.0,
        "take_profit": 110.0, "peak_price": 101.0,
        "opened_at": datetime.datetime.utcnow(),
        "stop_order_id": "venue-stop-9", "strategy": "MeanReversion",
        "run_id": "run-xyz", "entry_context": "ctx",
    }]

    async def _load():
        return stored

    monkeypatch.setattr(store, "load_watch_list", _load)
    monitor = PositionMonitorAgent()
    await monitor.restore()

    pos = monitor._open[TAR]
    assert pos.strategy == "MeanReversion"
    assert pos.run_id == "run-xyz"
    assert pos.stop_order_id == "venue-stop-9"
