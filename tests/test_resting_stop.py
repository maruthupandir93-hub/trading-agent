"""The resting stop-loss at the VENUE — placement, movement, and cancellation.

WHY THIS MATTERS MORE THAN THE IN-PROCESS STOP
----------------------------------------------
Every stop in this system was enforced by `_check_price` reacting to ticks inside
this process. That works while the process is alive and does nothing at all while
it is not: a crash, a deploy or an OOM left a REAL position open with no stop
anywhere in the world. Restoring the watch list narrowed the window from "forever,
silently" to "the length of the restart". Only an order resting at the exchange
closes it.

THE THREE FAILURES PINNED HERE ALL LOOK LIKE HOUSEKEEPING AND ARE NOT:

  placement   no resting order  -> a dead process is an unprotected position
  movement    a tightened stop that does not move at the venue leaves the
              exchange protecting the position at the OLD, wider level while the
              operator is told it moved
  cancellation a stop left resting after its position closes is a live
              reduce-only order that, on a now-flat account, becomes an order to
              OPEN the opposite position
"""

from __future__ import annotations

import pytest

from backend.agents.position_monitor import (
    PositionMonitorAgent,
    get_position_monitor,
    reset_position_monitor,
)
from backend.services.venue import OrderResult


class _FakeVenue:
    """Records what it was asked to do. No network, no account."""

    id = "binance"

    def __init__(self, *, place_ok=True, cancel_ok=True, creds=True):
        self.placed = []
        self.cancelled = []
        self._place_ok = place_ok
        self._cancel_ok = cancel_ok
        self._creds = creds
        self._n = 0

    def has_credentials(self):
        return self._creds

    async def place_stop_loss(self, *, symbol, side, qty, stop_price, client_order_id=None):
        self.placed.append(
            {"symbol": symbol, "side": side, "qty": qty, "stop": stop_price, "coid": client_order_id}
        )
        if not self._place_ok:
            return OrderResult(ok=False, error="venue refused the stop")
        self._n += 1
        return OrderResult(ok=True, order_id=f"stop-{self._n}")

    async def cancel_order(self, order_id, symbol):
        self.cancelled.append((order_id, symbol))
        return self._cancel_ok


@pytest.fixture
def venue(monkeypatch):
    fake = _FakeVenue()
    import backend.services.venue as venue_mod

    monkeypatch.setattr(venue_mod, "get_venue", lambda: fake)
    reset_position_monitor()
    yield fake
    reset_position_monitor()


def tracked(monitor: PositionMonitorAgent, *, tab="real", side="buy", stop=68_000.0):
    from backend.agents.position_monitor import _Tracked
    import datetime

    pos = _Tracked(
        tar_id="tar-1", symbol="BTC/USDT", side=side, tab=tab, qty=0.5,
        entry_price=70_000.0, stop_loss=stop, take_profit=75_000.0,
        opened_at=datetime.datetime.utcnow(), peak_price=70_000.0, stop_order_id=None,
    )
    monitor._open["tar-1"] = pos
    return pos


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_real_position_gets_a_resting_stop_on_the_EXIT_side(venue):
    # A long is closed by SELLING. Placing it on the entry side would add to the
    # position at the stop instead of closing it.
    monitor = get_position_monitor()
    pos = tracked(monitor, side="buy")

    await monitor._place_resting_stop(pos)

    assert len(venue.placed) == 1
    assert venue.placed[0]["side"] == "sell"
    assert venue.placed[0]["stop"] == 68_000.0
    assert pos.stop_order_id == "stop-1"


@pytest.mark.asyncio
async def test_a_short_gets_its_stop_on_the_buy_side(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor, side="sell", stop=72_000.0)

    await monitor._place_resting_stop(pos)
    assert venue.placed[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_a_paper_position_gets_NO_venue_order(venue):
    """Correct rather than an omission: there is no venue order behind a
    simulated fill, so there is nothing to rest."""
    monitor = get_position_monitor()
    pos = tracked(monitor, tab="paper")

    await monitor._place_resting_stop(pos)

    assert venue.placed == []
    assert pos.stop_order_id is None


@pytest.mark.asyncio
async def test_a_refused_stop_leaves_the_position_TRACKED_and_says_so(venue, caplog):
    """The position is already open and the money has moved.

    Refusing to track it would leave it open AND unwatched, which is strictly
    worse — so the failure is loud rather than fatal.
    """
    venue._place_ok = False
    monitor = get_position_monitor()
    pos = tracked(monitor)

    with caplog.at_level("CRITICAL"):
        await monitor._place_resting_stop(pos)

    assert pos.stop_order_id is None
    assert "tar-1" in monitor._open  # still watched in-process
    assert any("NO RESTING STOP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_no_credentials_means_no_call_rather_than_an_error(venue):
    venue._creds = False
    monitor = get_position_monitor()
    pos = tracked(monitor)

    await monitor._place_resting_stop(pos)
    assert venue.placed == []


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tightening_CANCELS_before_placing_the_new_stop(venue):
    """Order matters and the reverse is dangerous.

    Two live reduce-only stops on one position means the second, after the first
    fires and flattens the account, becomes an order to OPEN the opposite
    position. A brief window with no stop is recoverable; that is not.
    """
    monitor = get_position_monitor()
    pos = tracked(monitor)
    await monitor._place_resting_stop(pos)
    assert pos.stop_order_id == "stop-1"

    pos.stop_loss = 69_000.0
    await monitor._replace_resting_stop(pos)

    assert venue.cancelled == [("stop-1", "BTC/USDT")]
    assert venue.placed[-1]["stop"] == 69_000.0
    assert pos.stop_order_id == "stop-2"


@pytest.mark.asyncio
async def test_a_paper_position_is_never_replaced_at_a_venue(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor, tab="paper")
    await monitor._replace_resting_stop(pos)
    assert venue.placed == [] and venue.cancelled == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_resting_stop_is_cancelled_when_the_position_closes(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor)
    await monitor._place_resting_stop(pos)

    await monitor._cancel_resting_stop(pos, "close by stop-loss")

    assert venue.cancelled == [("stop-1", "BTC/USDT")]
    assert pos.stop_order_id is None


@pytest.mark.asyncio
async def test_a_failed_cancel_is_CRITICAL_and_keeps_the_id(venue, caplog):
    """The order may still be live, and the id is the only way to find it again.

    Clearing it on failure would lose the one handle on an order that will open a
    reversed position if price reaches it.
    """
    venue._cancel_ok = False
    monitor = get_position_monitor()
    pos = tracked(monitor)
    await monitor._place_resting_stop(pos)

    with caplog.at_level("CRITICAL"):
        await monitor._cancel_resting_stop(pos, "close")

    assert pos.stop_order_id == "stop-1"
    assert any("COULD NOT CANCEL" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancelling_when_there_is_no_resting_stop_is_a_no_op(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor)
    await monitor._cancel_resting_stop(pos, "close")
    assert venue.cancelled == []
