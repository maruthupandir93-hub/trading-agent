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

    def __init__(self, *, place_ok=True, cancel_ok=True, creds=True, tp_ok=True):
        self.placed = []
        self.tps_placed = []
        self.cancelled = []
        self._place_ok = place_ok
        self._tp_ok = tp_ok
        self._cancel_ok = cancel_ok
        self._creds = creds
        self._n = 0
        self._tpn = 0

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

    async def place_take_profit(self, *, symbol, side, qty, take_profit_price, client_order_id=None):
        self.tps_placed.append(
            {"symbol": symbol, "side": side, "qty": qty, "tp": take_profit_price, "coid": client_order_id}
        )
        if not self._tp_ok:
            return OrderResult(ok=False, error="venue refused the take-profit")
        self._tpn += 1
        return OrderResult(ok=True, order_id=f"tp-{self._tpn}")

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


# ---------------------------------------------------------------------------
# The resting TAKE-PROFIT — the mirror of the stop, added so a favourable move
# during a restart is captured instead of missed. Same three failures, same
# reduce-only safety that lets both rest at once.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_position_gets_a_resting_take_profit_on_the_exit_side(monkeypatch, venue):
    monitor = get_position_monitor()
    pos = tracked(monitor, side="buy")           # long -> TP is a SELL

    # The ATR target is the only target when the percentage exit is off, which is
    # the arrangement this test was written for.
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 0.0)
    await monitor._place_resting_tp(pos)

    assert len(venue.tps_placed) == 1
    tp = venue.tps_placed[0]
    assert tp["side"] == "sell"                  # exit side, mirrors the stop
    assert tp["tp"] == 75_000.0
    assert pos.tp_order_id == "tp-1"


@pytest.mark.asyncio
async def test_the_resting_tp_sits_where_this_monitor_WOULD_ACTUALLY_EXIT(monkeypatch, venue):
    """A REAL-vs-PAPER divergence, and it only bit real money.

    `pos.take_profit` is the Risk Gateway's 5x-ATR level. But since
    PROFIT_TARGET_PCT became the default exit, `_check_price` closes at a fixed
    PERCENTAGE instead — and that is much nearer. Measured on a live 3x SOL/USDT
    short: the 2%-of-margin target was a 0.667% move to 120.56, while the ATR
    target sat at 116.96, 5.4x further away.

    So on paper the position closed at the percentage target, and a real one did
    too WHILE THE PROCESS WAS ALIVE — but the resting order, whose entire purpose
    is the window when it is NOT alive, sat at the ATR level. A real trade that
    reached its target during a deploy would sail straight through it and ride
    back, while the paper book booked the win. Same settings, same symbol,
    different outcome, real money only.
    """
    monitor = get_position_monitor()
    pos = tracked(monitor, side="buy")           # entry 70,000, ATR target 75,000
    pos.leverage = 2
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_BASIS", "account")

    await monitor._place_resting_tp(pos)

    # 2% of margin at 2x is a 1% PRICE move: 70,000 -> 70,700, well inside the
    # ATR target. The exchange must enforce the exit this process would take.
    assert venue.tps_placed[0]["tp"] == pytest.approx(70_700.0)


@pytest.mark.asyncio
async def test_the_resting_tp_never_sits_BEYOND_the_atr_target(monkeypatch, venue):
    """Whichever level is reached FIRST wins, and it is not always the percentage.

    At low leverage the percentage target can be further away than the ATR one —
    at 1x, a 2% account target is a 2% price move, and a tight ATR target may sit
    inside it. Taking the percentage unconditionally would move the resting order
    AWAY from entry, which is the take-profit equivalent of widening a stop.
    """
    monitor = get_position_monitor()
    pos = tracked(monitor, side="buy")
    pos.leverage = 1
    pos.take_profit = 70_350.0                   # a 0.5% ATR target, tighter than 2%
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_BASIS", "account")

    await monitor._place_resting_tp(pos)
    assert venue.tps_placed[0]["tp"] == pytest.approx(70_350.0)


@pytest.mark.asyncio
async def test_a_short_resting_tp_takes_the_HIGHER_of_the_two(monkeypatch, venue):
    """Direction flips which comparison means "nearer to entry"."""
    monitor = get_position_monitor()
    pos = tracked(monitor, side="sell", stop=72_000.0)
    pos.take_profit = 65_000.0                   # ATR target, 7.1% away
    pos.leverage = 5
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_BASIS", "account")

    await monitor._place_resting_tp(pos)

    # 2% at 5x is a 0.4% move DOWN from 70,000 -> 69,720, nearer than 65,000.
    assert venue.tps_placed[0]["tp"] == pytest.approx(69_720.0)


@pytest.mark.asyncio
async def test_a_short_take_profit_is_a_buy(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor, side="sell")          # short -> TP is a BUY
    await monitor._place_resting_tp(pos)
    assert venue.tps_placed[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_a_paper_position_gets_no_resting_take_profit(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor, tab="paper")
    await monitor._place_resting_tp(pos)
    assert venue.tps_placed == []
    assert pos.tp_order_id is None


@pytest.mark.asyncio
async def test_a_position_with_no_take_profit_gets_no_tp_order(venue):
    """Some entries are stop-only. That is not a failure to place a TP."""
    monitor = get_position_monitor()
    pos = tracked(monitor)
    pos.take_profit = None
    await monitor._place_resting_tp(pos)
    assert venue.tps_placed == []


@pytest.mark.asyncio
async def test_a_refused_take_profit_does_not_reject_the_position(venue):
    """A missed target is only an upside not captured while down — unlike a
    refused STOP, it is a WARNING, and the position stays tracked."""
    venue._tp_ok = False
    monitor = get_position_monitor()
    pos = tracked(monitor)
    await monitor._place_resting_tp(pos)
    assert pos.tp_order_id is None
    assert "tar-1" in monitor._open           # still tracked


@pytest.mark.asyncio
async def test_a_fill_places_BOTH_a_resting_stop_and_a_take_profit(venue):
    """The whole point: on a real fill the position gets both resting legs."""
    import uuid
    from backend.models.events import OrderFilledEvent, TarApprovedEvent

    monitor = get_position_monitor()
    tar = uuid.uuid4()
    await monitor.handle_event(TarApprovedEvent(
        tar_id=tar, symbol="BTC/USDT", direction="LONG", approved_size=0.5,
        approved_leverage=3, cro_rationale="ok", stop_loss=68_000.0,
        take_profit=75_000.0, tab="real",
    ))
    await monitor.handle_event(OrderFilledEvent(
        tar_id=tar, order_id="o1", symbol="BTC/USDT", side="buy", tab="real",
        fill_price=70_000.0, fill_quantity=0.5, slippage_bps=0.0, fee=0.0,
        exchange="binance",
    ))

    assert len(venue.placed) == 1               # the stop
    assert len(venue.tps_placed) == 1           # the take-profit
    pos = monitor._open[str(tar)]
    assert pos.stop_order_id == "stop-1"
    assert pos.tp_order_id == "tp-1"


@pytest.mark.asyncio
async def test_both_resting_orders_are_cancelled_on_close(venue):
    monitor = get_position_monitor()
    pos = tracked(monitor)
    pos.stop_order_id = "stop-1"
    pos.tp_order_id = "tp-1"

    await monitor._cancel_resting_stop(pos, "close")
    await monitor._cancel_resting_tp(pos, "close")

    assert ("stop-1", "BTC/USDT") in venue.cancelled
    assert ("tp-1", "BTC/USDT") in venue.cancelled
    assert pos.stop_order_id is None
    assert pos.tp_order_id is None


@pytest.mark.asyncio
async def test_the_tp_order_id_survives_a_watch_row_round_trip(venue):
    """`tp_order_id` must be emitted by _watch_rows (bound by name from _FIELDS),
    or it persists as NULL on every row exactly as stop_order_id once did."""
    from backend.services.position_store import _FIELDS

    monitor = get_position_monitor()
    pos = tracked(monitor)
    pos.stop_order_id = "stop-1"
    pos.tp_order_id = "tp-1"

    open_row = [r for r in monitor._watch_rows() if r["status"] == "open"][0]
    assert "tp_order_id" in _FIELDS
    assert open_row["tp_order_id"] == "tp-1"
