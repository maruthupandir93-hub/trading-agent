"""Which symbols get a live tick feed — and why an open position must always.

WHY THIS IS A SAFETY TEST
=========================
`PositionMonitorAgent` enforces every stop-loss by reacting to `TICK_RECEIVED`,
and `live_market_data` is the ONLY publisher of that event. The subscription list
was hardcoded:

    symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

A position in any other instrument therefore received no ticks, `_check_price`
never ran for it, and its stop could never fire. Worse, nothing said so: the
monitor listed the position as watched and the dashboard showed its stop, so the
operator had every reason to believe it was protected.

The set is now derived from what needs watching and reconciled on a timer. These
tests pin the two properties that make that safe:

  1. an OPEN POSITION's symbol is always in the set, whatever else is
  2. a symbol with an open position is never unsubscribed
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services import live_market_data as lmd


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """No monitor, no session, no book — each test supplies what it needs."""
    monkeypatch.setattr(lmd, "_watchers", {})
    monkeypatch.setattr(lmd, "_exchange", None)

    import backend.agents.position_monitor as pm
    import backend.services.trading_session as ts
    import backend.services.portfolio_store as ps

    class _EmptyMonitor:
        def snapshot_open(self):
            return []

    monkeypatch.setattr(pm, "get_position_monitor", lambda: _EmptyMonitor())
    monkeypatch.setattr(ts, "active_session", lambda: None)

    async def empty_portfolio():
        return {"paper": {"positions": []}, "real": {"positions": []}}

    monkeypatch.setattr(ps, "get_portfolio", empty_portfolio)
    yield


def _with_positions(monkeypatch, symbols):
    import backend.agents.position_monitor as pm

    class _Monitor:
        def snapshot_open(self):
            return [{"symbol": s, "qty": 1.0} for s in symbols]

    monkeypatch.setattr(pm, "get_position_monitor", lambda: _Monitor())


# ---------------------------------------------------------------------------
# What needs a feed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_defaults_are_always_watched():
    """So a fresh process is never a blank dashboard."""
    wanted = await lmd._symbols_needing_ticks()
    assert set(lmd.DEFAULT_SYMBOLS) <= wanted


@pytest.mark.asyncio
async def test_an_open_position_outside_the_defaults_gets_a_feed(monkeypatch):
    """THE BUG. Without this the position's stop can never fire."""
    _with_positions(monkeypatch, ["DOGE/USDT"])

    wanted = await lmd._symbols_needing_ticks()

    assert "DOGE/USDT" in wanted, (
        "an open position with no tick feed has a stop-loss that cannot fire, and "
        "nothing anywhere reports that it is unenforceable"
    )


@pytest.mark.asyncio
async def test_the_running_sessions_symbol_gets_a_feed_before_it_opens(monkeypatch):
    """Waiting for the fill would leave a new position's first moments unwatched."""
    import backend.services.trading_session as ts

    class _Session:
        symbol = "AVAX/USDT"

    monkeypatch.setattr(ts, "active_session", lambda: _Session())

    assert "AVAX/USDT" in await lmd._symbols_needing_ticks()


@pytest.mark.asyncio
async def test_a_book_position_the_monitor_is_not_tracking_still_gets_a_feed(monkeypatch):
    """A restored book, or a manual trade opened with no stop.

    It still needs marking for equity — `book_equity` returns None for a position
    it cannot price, which blanks the whole account figure.
    """
    import backend.services.portfolio_store as ps

    async def book():
        return {"paper": {"positions": [{"symbol": "LINK/USDT", "qty": 2.0}]}, "real": {"positions": []}}

    monkeypatch.setattr(ps, "get_portfolio", book)

    assert "LINK/USDT" in await lmd._symbols_needing_ticks()


@pytest.mark.asyncio
async def test_a_failure_reading_positions_does_not_shrink_the_set(monkeypatch):
    """A reconcile that cannot read the monitor must keep the existing feeds.

    Returning a smaller set on a transient error would unsubscribe a live
    position — the exact failure this module was rewritten to prevent.
    """
    import backend.agents.position_monitor as pm

    def boom():
        raise RuntimeError("monitor unavailable")

    monkeypatch.setattr(pm, "get_position_monitor", boom)

    wanted = await lmd._symbols_needing_ticks()
    assert set(lmd.DEFAULT_SYMBOLS) <= wanted


# ---------------------------------------------------------------------------
# Reconciling the running watchers
# ---------------------------------------------------------------------------

class _FakeExchange:
    async def watch_ticker(self, symbol):
        await asyncio.sleep(3600)  # never returns; the task just stays alive

    async def close(self):
        pass


class _Bus:
    async def publish(self, topic, event):
        pass


@pytest.mark.asyncio
async def test_reconcile_starts_a_watcher_for_a_newly_opened_position(monkeypatch):
    monkeypatch.setattr(lmd, "_exchange", _FakeExchange())

    await lmd._reconcile(_Bus())
    assert lmd.watched_symbols() == set(lmd.DEFAULT_SYMBOLS)

    # A position opens on a symbol nothing was watching.
    _with_positions(monkeypatch, ["DOGE/USDT"])
    await lmd._reconcile(_Bus())

    assert "DOGE/USDT" in lmd.watched_symbols()

    for task in lmd._watchers.values():
        task.cancel()


@pytest.mark.asyncio
async def test_a_symbol_with_an_open_position_is_NEVER_unsubscribed(monkeypatch):
    """The property that makes the timer safe.

    A reconcile that dropped a live position's feed would silently disarm its
    stop, which is worse than never having subscribed at all — the position looks
    protected the whole time.
    """
    monkeypatch.setattr(lmd, "_exchange", _FakeExchange())
    _with_positions(monkeypatch, ["DOGE/USDT"])

    await lmd._reconcile(_Bus())
    assert "DOGE/USDT" in lmd.watched_symbols()

    # Reconcile again with the position still open — it must survive.
    await lmd._reconcile(_Bus())
    assert "DOGE/USDT" in lmd.watched_symbols()

    for task in lmd._watchers.values():
        task.cancel()


@pytest.mark.asyncio
async def test_a_symbol_is_dropped_once_nothing_needs_it(monkeypatch):
    """The other direction: a closed position's feed is released rather than
    watched forever, which is what bounds the socket's subscription count."""
    monkeypatch.setattr(lmd, "_exchange", _FakeExchange())
    _with_positions(monkeypatch, ["DOGE/USDT"])
    await lmd._reconcile(_Bus())
    assert "DOGE/USDT" in lmd.watched_symbols()
    lmd._live_prices["DOGE/USDT"] = 0.15  # as a real tick would have

    # Position closed.
    _with_positions(monkeypatch, [])
    await lmd._reconcile(_Bus())

    assert "DOGE/USDT" not in lmd.watched_symbols()
    assert lmd.watched_symbols() == set(lmd.DEFAULT_SYMBOLS)

    # ...and its cached price goes with it. `get_live_price` carries no
    # timestamp, so a value left behind is served forever as a LIVE websocket
    # price for a symbol nothing is watching.
    assert lmd.get_live_price("DOGE/USDT") == 0.0

    for task in lmd._watchers.values():
        task.cancel()


@pytest.mark.asyncio
async def test_a_dead_watcher_is_restarted_rather_than_left_subscribed(monkeypatch):
    """A crashed watcher leaves its symbol unwatched while still appearing in the
    map — the position would look covered and receive nothing."""
    monkeypatch.setattr(lmd, "_exchange", _FakeExchange())
    await lmd._reconcile(_Bus())

    dead = lmd._watchers["BTC/USDT"]
    dead.cancel()
    try:
        await dead
    except asyncio.CancelledError:
        pass
    assert dead.done()

    await lmd._reconcile(_Bus())

    assert "BTC/USDT" in lmd.watched_symbols()
    assert lmd._watchers["BTC/USDT"] is not dead, "the dead task was left in place"

    for task in lmd._watchers.values():
        task.cancel()


def test_the_symbol_list_is_not_hardcoded_at_the_call_site():
    """A regression guard on the shape.

    Reverting to a literal list restores a silent stop-loss failure for every
    instrument outside it, and nothing fails visibly.
    """
    import inspect

    source = inspect.getsource(lmd.start_live_data_feed)
    assert "_reconcile" in source
    assert "symbols = [" not in source
