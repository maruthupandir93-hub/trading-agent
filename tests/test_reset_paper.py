"""Resetting the paper book — and the one case where it must refuse.

WHY THIS ENDPOINT EXISTS AT ALL
-------------------------------
Deleting `agent_positions` / `agent_paper_account` / `monitored_positions` in SQL
against a RUNNING backend looks like it works and does not last: the book lives in
a module-level dict and the watch list lives in the monitor, and both re-persist
themselves over the top on the next write. The reset therefore has to clear MEMORY
first. `test_the_reset_clears_memory_not_just_storage` is the assertion that keeps
that ordering.

THE REFUSAL IS THE IMPORTANT TEST
---------------------------------
`clear_all` stops watching without closing anything. On paper that is correct — the
whole book is being discarded. On a real book it would leave a live position open
at the venue with nothing enforcing its stop, which is the worst action in this
codebase. The route must refuse while `LIVE_TRADING` is on, and it must refuse
BEFORE it has cleared anything.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.api import admin
from backend.agents.position_monitor import get_position_monitor, reset_position_monitor
from backend.services import portfolio_store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_position_monitor()
    # No database in unit tests: `save_watch_list` and `_persist` both return
    # False, which is the "memory only" path and is exercised deliberately.
    monkeypatch.setattr(portfolio_store, "_portfolio", {
        "paper": {"cash": 23_970.0, "positions": [{"symbol": "SOL/USDT", "qty": 10.0, "avgCost": 103.0}]},
        "real": {"positions": []},
    })
    yield
    reset_position_monitor()


def _request(**over):
    return admin.ResetPaperRequest(**{"confirm": "RESET PAPER", **over})


@pytest.mark.asyncio
async def test_the_reset_clears_memory_not_just_storage(monkeypatch):
    """The point of the whole route: the in-memory book is what re-persists."""
    monkeypatch.setattr(admin.settings, "_live_trading", False)

    result = await admin.reset_paper(_request(startingCash=10_000.0))

    assert result["status"] == "success"
    assert result["positionsCleared"] == 1
    assert portfolio_store._portfolio["paper"]["cash"] == 10_000.0
    assert portfolio_store._portfolio["paper"]["positions"] == []


@pytest.mark.asyncio
async def test_the_watch_list_is_emptied(monkeypatch):
    monkeypatch.setattr(admin.settings, "_live_trading", False)

    monitor = get_position_monitor()
    await monitor.track_manual_position(
        symbol="SOL/USDT", side="buy", qty=1.0, entry_price=100.0,
        stop_loss=95.0, take_profit=110.0, tab="paper",
    )
    assert len(monitor.snapshot_open()) == 1

    result = await admin.reset_paper(_request())

    assert result["watchedCleared"] == 1
    assert monitor.snapshot_open() == []


@pytest.mark.asyncio
async def test_it_refuses_while_live_trading_is_on_and_clears_nothing(monkeypatch):
    """THE safety assertion in this file.

    Forgetting a real position leaves it open on the exchange with nothing
    enforcing its stop. The refusal must also come BEFORE any clearing — a route
    that wiped the book and then raised would be worse than one that never ran.
    """
    monkeypatch.setattr(admin.settings, "_live_trading", True)

    monitor = get_position_monitor()
    await monitor.track_manual_position(
        symbol="BTC/USDT", side="buy", qty=0.1, entry_price=70_000.0,
        stop_loss=68_000.0, take_profit=75_000.0, tab="real",
    )

    with pytest.raises(HTTPException) as exc:
        await admin.reset_paper(_request())

    assert exc.value.status_code == 409
    assert "LIVE_TRADING" in str(exc.value.detail)
    # Nothing was touched.
    assert len(monitor.snapshot_open()) == 1
    assert portfolio_store._portfolio["paper"]["positions"] != []


@pytest.mark.asyncio
async def test_the_confirmation_phrase_is_required(monkeypatch):
    """The route is reachable over HTTP and this is irreversible."""
    monkeypatch.setattr(admin.settings, "_live_trading", False)

    with pytest.raises(HTTPException) as exc:
        await admin.reset_paper(_request(confirm="yes"))
    assert exc.value.status_code == 400
    assert portfolio_store._portfolio["paper"]["positions"] != []


@pytest.mark.asyncio
async def test_the_trade_log_is_kept_unless_asked_for(monkeypatch):
    """The trade log is the audit trail; wiping it is a separate decision."""
    monkeypatch.setattr(admin.settings, "_live_trading", False)

    result = await admin.reset_paper(_request(clearTradeLog=False))
    assert result["tradesDeleted"] is None


@pytest.mark.asyncio
async def test_the_real_book_is_left_alone(monkeypatch):
    """A reset of the PAPER book must not silently edit the other one."""
    monkeypatch.setattr(admin.settings, "_live_trading", False)
    portfolio_store._portfolio["real"] = {"positions": [{"symbol": "ETH/USDT", "qty": 2.0}]}

    await admin.reset_paper(_request())

    assert portfolio_store._portfolio["real"]["positions"] == [{"symbol": "ETH/USDT", "qty": 2.0}]


@pytest.mark.asyncio
async def test_a_watch_list_that_did_not_persist_is_reported_as_such(monkeypatch):
    """False means a restart may resurrect the rows, so it cannot read as success."""
    monkeypatch.setattr(admin.settings, "_live_trading", False)

    result = await admin.reset_paper(_request())
    # No database in this test, so the write could not land — and the response
    # says so rather than claiming the reset was durable.
    assert result["watchListPersisted"] is False
