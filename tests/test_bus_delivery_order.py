"""An event must reach every subscriber before the events it causes.

THE BUG
-------
`MessageBus.publish` used to deliver inline, awaiting each subscriber in turn. A
subscriber that published from inside its own handler therefore ran that nested
delivery to completion *before* the outer event reached the subscribers still
queued behind it.

Two subscribers on `TAR_APPROVED` turned that into a safety failure, and it was
deterministic rather than a race — `main.py` builds `ExecutionAgent` before
`PositionMonitor`, so the subscription order was fixed:

    publish(TAR_APPROVED)
      -> ExecutionAgent          places the order, publishes ORDER_FILLED inline
           -> PositionMonitor._register_fill runs with an EMPTY pending map,
              because the monitor has not been handed the approval yet
           -> "UNPROTECTED POSITION ... will NOT be monitored"
      -> PositionMonitor         finally receives TAR_APPROVED. Too late.

Every position the agent opened was recorded unprotected, no stop was
enforceable on any of them, and the position never entered the watch list — so
the paper book and the positions view stayed empty while the event log showed a
completed fill.

WHY THE TEST IS AT THE BUS AND NOT AT THE AGENTS
------------------------------------------------
Reordering the two constructions in `main.py` would also have made the symptom go
away, and would have broken again silently the next time startup was reordered.
The hazard is that a nested publish jumps the queue; that is a property of the
bus, so it is pinned here with two plain subscribers rather than with the real
agents, whose setup would obscure what is actually being asserted.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.message_bus import MessageBus, WILDCARD_TOPIC


@pytest.mark.asyncio
async def test_a_nested_publish_waits_for_the_event_that_caused_it():
    """The exact TAR_APPROVED -> ORDER_FILLED shape, with the real ordering."""
    bus = MessageBus()
    order: list[str] = []
    pending: set[str] = set()
    unprotected: list[str] = []

    async def executor(event):
        # Subscribed FIRST, exactly like ExecutionAgent in main.py.
        order.append("executor:approved")
        await bus.publish("ORDER_FILLED", {"tar_id": event["tar_id"]})

    async def monitor_approved(event):
        # Subscribed SECOND, exactly like PositionMonitor.
        order.append("monitor:approved")
        pending.add(event["tar_id"])

    async def monitor_filled(event):
        order.append("monitor:filled")
        if event["tar_id"] not in pending:
            unprotected.append(event["tar_id"])

    bus.subscribe("TAR_APPROVED", executor)
    bus.subscribe("TAR_APPROVED", monitor_approved)
    bus.subscribe("ORDER_FILLED", monitor_filled)

    await bus.publish("TAR_APPROVED", {"tar_id": "t1"})

    # The whole point: the monitor sees the approval before it sees the fill.
    assert order == ["executor:approved", "monitor:approved", "monitor:filled"], order
    assert unprotected == [], (
        "the fill was delivered before the approval that authorised it, so the "
        "position would have been recorded UNPROTECTED and never monitored"
    )


@pytest.mark.asyncio
async def test_the_outermost_publish_still_drains_everything_before_returning():
    """A caller awaiting `publish` keeps the guarantee it always had.

    Only a NESTED publish returns early. If the outer one did too, a caller that
    publishes and then reads state a subscriber writes would silently see the
    old value — which would trade this bug for a subtler one.
    """
    bus = MessageBus()
    seen: list[str] = []

    async def first(_event):
        await bus.publish("SECOND", {})

    async def second(_event):
        await bus.publish("THIRD", {})

    async def third(_event):
        seen.append("third")

    bus.subscribe("FIRST", first)
    bus.subscribe("SECOND", second)
    bus.subscribe("THIRD", third)

    await bus.publish("FIRST", {})

    assert seen == ["third"], "the outer publish returned before the chain finished"


@pytest.mark.asyncio
async def test_events_caused_by_one_event_are_delivered_in_the_order_raised():
    """Causation order, not reverse order.

    A depth-first bus delivers the LAST-raised nested event first once the stack
    unwinds. Breadth-first keeps the order a reader would expect from the log.
    """
    bus = MessageBus()
    seen: list[str] = []

    async def fan_out(_event):
        await bus.publish("A", {})
        await bus.publish("B", {})

    bus.subscribe("ROOT", fan_out)
    bus.subscribe("A", lambda _e: seen.append("A"))
    bus.subscribe("B", lambda _e: seen.append("B"))

    await bus.publish("ROOT", {})
    assert seen == ["A", "B"], seen


@pytest.mark.asyncio
async def test_a_failing_subscriber_does_not_wedge_the_bus():
    """`_delivering` is cleared in `finally`.

    Without that, one escaped exception would leave the flag set and every
    subsequent publish would enqueue and never be delivered — the bus would go
    silent for the rest of the process's life, which is far worse than the
    original bug.
    """
    bus = MessageBus()
    seen: list[str] = []

    async def boom(_event):
        raise RuntimeError("subscriber failed")

    bus.subscribe("X", boom)
    bus.subscribe("X", lambda _e: seen.append("x"))

    await bus.publish("X", {})
    await bus.publish("X", {})

    # Both publishes delivered: `_deliver` swallows the raiser and the other
    # subscriber still ran, twice.
    assert seen == ["x", "x"], seen


@pytest.mark.asyncio
async def test_wildcard_observers_still_see_every_event_including_nested_ones():
    """The event buffer that feeds the dashboard is a wildcard subscriber.

    If queueing had dropped nested events from the wildcard fan-out, the whole
    live view would have gone quiet — the failure this change was made to fix.
    """
    bus = MessageBus()
    seen: list[str] = []

    async def cause(_event):
        await bus.publish("CAUSED", {})

    bus.subscribe("CAUSE", cause)
    bus.subscribe(WILDCARD_TOPIC, lambda e: seen.append("event"))

    await bus.publish("CAUSE", {})
    assert len(seen) == 2, f"wildcard saw {len(seen)} of 2 events"


# ---------------------------------------------------------------------------
# The deferral must be scoped to ONE causal chain
# ---------------------------------------------------------------------------
#
# THE REGRESSION THESE GUARD AGAINST, WHICH WAS SHIPPED AND CAUGHT
# ----------------------------------------------------------------
# The first version of the ordering fix used a single `self._delivering` flag on
# the bus, so ANY publish anywhere in the process queued while ANY delivery was in
# flight. That is fine until a subscriber is slow — and this system has a very slow
# one: the analysis graph is subscribed to TRIGGER_FIRED and runs for ~20 seconds.
#
# Measured on the running backend: 8 node events published, 0 delivered. The bus
# was frozen for the whole duration of every graph run, and the live pipeline view
# it was built to feed stayed empty. A process-wide stall is strictly worse than
# the out-of-order delivery it replaced.
#
# The flag is a ContextVar now, so "am I inside a delivery?" is answered per causal
# chain rather than per process.


@pytest.mark.asyncio
async def test_a_slow_subscriber_does_not_block_an_unrelated_publisher():
    """The whole point of scoping the deferral to one chain."""
    bus = MessageBus()
    delivered: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_event):
        started.set()
        await release.wait()          # a 20-second graph run, in miniature
        delivered.append("slow")

    bus.subscribe("SLOW", slow)
    bus.subscribe("FAST", lambda _e: delivered.append("fast"))

    slow_task = asyncio.create_task(bus.publish("SLOW", {}))
    await started.wait()

    # The slow subscriber is mid-flight. An unrelated publish must still land.
    await asyncio.wait_for(bus.publish("FAST", {}), timeout=1.0)
    assert delivered == ["fast"], (
        "an unrelated publish was blocked behind a slow subscriber — the bus is "
        "serialising the whole process instead of one causal chain"
    )

    release.set()
    await slow_task
    assert delivered == ["fast", "slow"]


@pytest.mark.asyncio
async def test_graph_node_telemetry_is_delivered_during_the_work_it_reports_on():
    """Node events must NOT be held until the run that emits them finishes.

    The analysis graph runs as a subscriber and publishes one pair of node events
    per node as it goes. Deferring those behind the graph's own completion would
    make the live pipeline view show nothing for the whole run and then everything
    at once — which is precisely the black box Section 39.5 exists to replace.
    """
    bus = MessageBus()
    seen: list[str] = []
    bus.subscribe("GRAPH_NODE_STARTED", lambda _e: seen.append("node"))

    mid_run: list[int] = []

    async def long_graph_run(_event):
        for _ in range(3):
            await bus.publish("GRAPH_NODE_STARTED", {})
            # Observed from INSIDE the run: the telemetry must already be through.
            mid_run.append(len(seen))
            await asyncio.sleep(0)

    bus.subscribe("TRIGGER_FIRED", long_graph_run)
    await bus.publish("TRIGGER_FIRED", {})

    assert mid_run == [1, 2, 3], (
        f"node events were deferred until the run finished (saw {mid_run}); the "
        f"live pipeline view would stay frozen for the whole run"
    )


@pytest.mark.asyncio
async def test_ordering_still_holds_for_events_whose_order_matters():
    """The exemption must not have re-opened the original hole.

    TAR_APPROVED -> ORDER_FILLED is not exempt, so it must still be deferred.
    """
    bus = MessageBus()
    order: list[str] = []
    pending: set[str] = set()

    async def executor(event):
        order.append("executor:approved")
        await bus.publish("ORDER_FILLED", {"tar_id": event["tar_id"]})

    bus.subscribe("TAR_APPROVED", executor)
    bus.subscribe("TAR_APPROVED", lambda e: (order.append("monitor:approved"), pending.add(e["tar_id"])))
    bus.subscribe("ORDER_FILLED", lambda e: order.append(
        "monitor:filled" if e["tar_id"] in pending else "monitor:UNPROTECTED"))

    await bus.publish("TAR_APPROVED", {"tar_id": "t1"})
    assert order == ["executor:approved", "monitor:approved", "monitor:filled"], order
