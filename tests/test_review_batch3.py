"""Batch 3: two of my own fixes that traded one bug for another.

#15 and #13 are both cases where an earlier fix in this project was right
about the problem and wrong about the remedy, and the comment confidently
asserted the remedy worked. Those are the expensive kind, because the next
reader trusts the comment.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# #15 — swallowing CancelledError means the task never stops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_cancel_aimed_at_the_delivering_task_actually_stops_it():
    """F64 caught `CancelledError` and continued, with a comment claiming "the
    cancellation still reaches the task that was cancelled". IT DOES NOT.

    `CancelledError` is how the loop ASKS a task to stop; a task that catches
    it without re-raising does not stop. So a cancelled price-feed watcher kept
    running, and once `_reconcile` re-subscribed the symbol TWO watchers
    published the same ticks — every stop evaluated twice on doubled prices.
    """
    from backend.core.message_bus import MessageBus

    bus = MessageBus()
    started = asyncio.Event()

    async def slow(event):
        started.set()
        await asyncio.sleep(10)

    bus.subscribe("T", slow)

    async def publisher():
        await bus.publish("T", {"x": 1})

    task = asyncio.create_task(publisher())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled(), (
        "the delivering task swallowed its own cancellation and kept running"
    )


@pytest.mark.asyncio
async def test_a_subscribers_own_cancellation_does_not_abort_the_chain():
    """The case F64 was written for, which must keep working.

    Rotating away from a coin cancels its watcher mid-delivery, and that must
    not cost the remaining subscribers the event — the Telegram alert, the CEO
    evaluation, the hypothesis and REFLECTION_COMPLETED were all lost.
    """
    from backend.core.message_bus import MessageBus

    bus = MessageBus()
    seen: list[str] = []

    async def cancels_itself(event):
        inner = asyncio.create_task(asyncio.sleep(10))
        inner.cancel()
        await inner                      # raises CancelledError, not ours

    async def behind(event):
        seen.append("behind")

    bus.subscribe("T", cancels_itself)
    bus.subscribe("T", behind)

    await bus.publish("T", {"x": 1})

    assert seen == ["behind"], "a subscriber's own cancel aborted the chain"


def test_the_bus_distinguishes_the_two_cases():
    import backend.core.message_bus as mb

    # `_deliver`, not `publish` — the fan-out loop that runs each
    # subscriber is where a cancellation can arrive.
    src = code_only(inspect.getsource(mb.MessageBus._deliver))
    assert "cancelling" in src, (
        "the bus cannot tell its own cancellation from a subscriber's, so one "
        "of the two behaviours is wrong"
    )
    assert "raise" in src


# ---------------------------------------------------------------------------
# #34 — a fire-and-forget alert must not be collected mid-flight
# ---------------------------------------------------------------------------

def test_telegram_sends_hold_a_strong_reference():
    """The event loop keeps only a WEAK reference to a running task, so a bare
    `create_task` can be garbage-collected before the HTTP call completes.

    Silent loss is the worst shape for this feature in particular: the channel
    exists for an operator who is NOT watching a dashboard, so a missing alert
    looks exactly like a quiet market.
    """
    import backend.services.telegram_notifier as tn

    src = code_only(inspect.getsource(tn))
    assert "_pending" in src and "add_done_callback" in src

    handler = code_only(inspect.getsource(tn.TelegramNotifier.handle_event))
    assert "create_task" not in handler, (
        "handle_event still spawns a bare task, which can be collected before "
        "it sends"
    )


# ---------------------------------------------------------------------------
# #13 — a restored pending approval must be able to expire
# ---------------------------------------------------------------------------

def test_a_pending_row_with_no_timestamp_is_treated_as_stale():
    """The filter read `if float(row.get("approved_at") or 0.0) and ...`, so a
    row WITHOUT the field short-circuited to False and could never expire.

    Combined with a restore path that did not set one, an approval that was
    never going to fill survived every boot — and `may_open_new_position`
    counts it, so it held the single allowed slot against every real entry.
    """
    from backend.agents.position_monitor import PositionMonitorAgent

    from backend.agents.position_monitor import _PENDING_TTL_S

    monitor = PositionMonitorAgent()
    monitor._pending["undated"] = {"symbol": "SOL/USDT"}          # no approved_at
    monitor._pending["fresh"] = {"symbol": "SOL/USDT", "approved_at": time.time()}

    # FIRST PASS: the undated row is ADOPTED, not dropped. Dropping it
    # immediately would mean a fill arriving a moment later found no pending
    # entry and was logged UNPROTECTED — the one outcome this map exists to
    # prevent, and the reason `test_a_pending_row_with_no_timestamp_is_kept`
    # asserts it survives.
    monitor._expire_stale_pending()
    assert "undated" in monitor._pending
    assert monitor._pending["undated"]["approved_at"] > 0, (
        "an undated approval was left undatable, so it can never expire"
    )

    # SECOND PASS, a TTL later: now it goes. That is the half the original
    # rule lacked — it short-circuited on the missing field, so the row was
    # IMMORTAL and held the single allowed position slot forever.
    monitor._pending["undated"]["approved_at"] = time.time() - _PENDING_TTL_S - 1
    monitor._expire_stale_pending()
    assert "undated" not in monitor._pending, "an undatable approval is immortal"
    assert "fresh" in monitor._pending, "a current approval was expired"


def test_the_restore_path_stamps_what_it_restores():
    """So the expiry above has something to measure."""
    import backend.agents.position_monitor as pm

    src = inspect.getsource(pm.PositionMonitorAgent.restore)
    stamp = src.index('"approved_at"')
    pending = src.index('self._pending[tar_id]')
    assert pending < stamp < src.index("pending += 1"), (
        "the restored pending row carries no approval time, so it can never "
        "be expired"
    )


# ---------------------------------------------------------------------------
# #10 — a book change must not be reported as a loss
# ---------------------------------------------------------------------------

def test_a_book_change_ends_the_session_rather_than_flooring_it():
    """`target_equity` and `floor_equity` are fixed at start from ONE book's
    equity. F30 made the loop follow LIVE_TRADING but kept comparing those
    thresholds against the other account, so a "$2 -> $5" paper session
    switched to real finished instantly as "floored" — a verdict about a loss
    that never happened, on an account it had never traded.
    """
    import backend.services.trading_session as ts

    src = inspect.getsource(ts._run_session)
    change = src.index("cycle_tab != tab")
    tail = src[change: change + 3000]

    assert '"book-changed"' in tail, "a book change still falls through to the floor check"
    assert "break" in tail
    # And it must NOT silently keep measuring.
    assert "tab = cycle_tab" not in tail, (
        "the loop still adopts the new book and compares the old thresholds "
        "against it"
    )


def test_the_reason_explains_rather_than_labels():
    """An operator reading "floored" goes looking for a loss. The reason has to
    name the toggle, both thresholds and what is safe to do next."""
    import backend.services.trading_session as ts

    src = inspect.getsource(ts._run_session)
    for phrase in ("live trading was toggled", "Start a new session", "monitor keeps watching"):
        assert phrase in src, f"the book-changed reason does not mention {phrase!r}"
