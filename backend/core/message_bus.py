import asyncio
import contextvars
import logging
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from pydantic import BaseModel, ValidationError

# Import the new strongly-typed events
from backend.models.events import (
    BaseEvent,
    EventType
)

logger = logging.getLogger(__name__)

# Subscribing to this topic receives EVERY published event. Spec Section 20
# requires that "everything must be observable", and the alternative — making
# each observer enumerate all 18 EventType values — silently misses any event
# type added later, which is exactly when an observability gap matters most.
WILDCARD_TOPIC = "*"


class _Fanout:
    """The deferral queue owned by one in-flight top-level publish.

    `active` is what makes an abandoned queue safe. A subscriber that spawns a
    task inherits this context (asyncio copies it into the new task), so that
    task's publish would otherwise append to a queue whose drain loop has already
    finished — and the event would be silently lost. When the loop exits it marks
    itself inactive, and a publish finding an inactive queue starts its own
    delivery instead.
    """

    __slots__ = ("queue", "active")

    def __init__(self) -> None:
        self.queue: Deque[Tuple[str, Any]] = deque()
        self.active: bool = True


# The fan-out in flight ON THIS TASK, or None. A ContextVar rather than an
# attribute on the bus, because the rule below is per-causal-chain and NOT
# per-process — see `publish`.
_fanout: contextvars.ContextVar[Optional[_Fanout]] = contextvars.ContextVar(
    "message_bus_fanout", default=None
)

# Topics delivered IMMEDIATELY, even from inside another delivery.
#
# WHY AN EXEMPTION EXISTS AT ALL
# ------------------------------
# The deferral rule below exists so an event cannot overtake the event that
# caused it. That matters when a consumer's CORRECTNESS depends on the order —
# the monitor must see TAR_APPROVED before ORDER_FILLED or the position is
# recorded unprotected.
#
# Graph node transitions are not that. They are progress telemetry about work
# happening DURING an event's delivery, and nothing downstream joins them to
# another event. Deferring them is actively harmful: the analysis graph runs as a
# subscriber of TRIGGER_FIRED and takes ~20 seconds, so its 46 node events would
# all be held until the run finished and then arrive in one burst — a "live"
# pipeline view that shows nothing while the pipeline is running and everything
# once it is over, which is the opposite of what it is for.
#
# Their subscribers are the event buffer and the WebSocket bridge, neither of
# which publishes, so delivering them inline cannot recurse.
IMMEDIATE_TOPICS: frozenset = frozenset({
    "GRAPH_NODE_STARTED",
    "GRAPH_NODE_COMPLETED",
    "GRAPH_NODE_FAILED",
})


class MessageBus:
    """
    Level 20: Event-Driven Architecture (Section 6 implementation)
    A centralized pub/sub system enabling microservice decoupling.
    Agents can broadcast strongly-typed events and other agents can listen.
    """
    def __init__(self):
        # We route by EventType string, e.g. 'TICK_RECEIVED'
        self._subscribers: Dict[str, List[Callable]] = {}

    def subscribe(self, topic: str, callback: Callable) -> None:
        """Subscribe to a specific EventType string, or WILDCARD_TOPIC for all.

        IDEMPOTENT. Subscribing the same callable to the same topic twice is a no-op.

        WHY, RATHER THAN APPENDING BLINDLY
        ----------------------------------
        Handling one event twice with the same handler is never what anyone wants, and
        the consequences here are not cosmetic: the supervisor would evaluate every
        signal twice and could submit two trade requests for one decision.

        This codebase already treats double-subscription as dangerous and guards
        against it by hand in several places — `analysis._subscribed`,
        `execution_service._subscribed`, and `trigger_worker.subscribe`, whose
        docstring spells out that "subscribing twice would evaluate every tick twice,
        and the second evaluation would see the baseline the first one just reset — so
        half the triggers would silently vanish rather than duplicate, which is the
        harder bug to notice".

        Those guards are per-caller, so each new subscriber has to remember. The
        hazard belongs here instead.

        Found via `BaseAgent.rebind_bus`: restoring a simulation-rebound agent
        re-subscribed it on the global bus, taking `DEBATE_CONCLUDED` from one handler
        to two. Nothing raised.
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        if callback in self._subscribers[topic]:
            # Debug, not warning: an idempotent re-subscribe is the normal result of a
            # guard doing its job, and warning on it would train people to ignore it.
            logger.debug("Already subscribed to %s; not duplicating.", topic)
            return
        self._subscribers[topic].append(callback)
        logger.debug(f"Subscribed to topic: {topic}")

    def unsubscribe(self, topic: str, callback: Callable) -> bool:
        """Remove one subscription. Returns True if it was there.

        Added for `BaseAgent.rebind_bus`, and it turned out to be the missing half of
        the §6.4 fix rather than a convenience.

        Rebinding an agent to a simulation bus without unsubscribing it from the
        global one left it subscribed to BOTH. So during a backtest a live
        `TICK_RECEIVED` still reached the market-intelligence agent, which then
        published its result to the SIMULATION bus — live analysis silently stopped
        working for the duration and the simulation was polluted with live data. That
        is the same class of cross-contamination §6.4 was about, arriving from the
        other direction.

        Empty topic lists are removed, so `len(_subscribers)` stays an honest count of
        topics that actually have listeners. A monitoring view reading it would
        otherwise report topics served by nobody.
        """
        handlers = self._subscribers.get(topic)
        if not handlers or callback not in handlers:
            return False
        handlers.remove(callback)
        if not handlers:
            del self._subscribers[topic]
        logger.debug("Unsubscribed from topic: %s", topic)
        return True

    async def publish(self, topic: str, payload: Any) -> None:
        """Publish one event. An event reaches EVERY subscriber before the events it causes.

        THE BUG THIS ORDERING RULE EXISTS TO PREVENT
        --------------------------------------------
        This used to deliver inline: each subscriber was awaited in turn, and a
        subscriber that published from inside its own handler ran that nested
        delivery to completion before the OUTER event reached the subscribers
        still queued behind it.

        Two subscribers on TAR_APPROVED made that fatal, deterministically:

            ExecutionAgent   subscribes first  (main.py builds it first)
            PositionMonitor  subscribes second

            publish(TAR_APPROVED)
              -> ExecutionAgent.handle_event      places the order
                   -> publish(ORDER_FILLED)       NESTED, ran to completion here
                        -> PositionMonitor._register_fill
                           self._pending is EMPTY: the monitor has not been
                           handed the approval yet, it is still callback #2
                        -> "UNPROTECTED POSITION ... will NOT be monitored"
              -> PositionMonitor.handle_event(TAR_APPROVED)   too late; orphaned

        So EVERY position the agent opened was recorded unprotected, no stop was
        enforceable on any of them, and the position never entered the watch list
        -- which is also why the paper book and the positions view stayed empty
        while the event log showed a completed fill. Observed live on 2026-08-28
        with tar_id 7bb6f502 on BTC/USDT.

        WHY THE FIX IS HERE AND NOT IN main.py's ORDERING
        -------------------------------------------------
        Swapping the two constructions would work today and break the next time
        anyone reorders startup, with no test able to see it, and the failure
        would show up as an unmonitored leveraged position. The hazard is that a
        nested publish JUMPS THE QUEUE, and that belongs to the bus.

        THE SCOPE IS ONE CAUSAL CHAIN, NOT THE WHOLE PROCESS
        ---------------------------------------------------
        The first version of this fix used a single `self._delivering` flag, so
        ANY publish anywhere queued while ANY delivery was in flight. That turned
        a slow subscriber into a process-wide stall, and this system has a very
        slow subscriber: the analysis graph is subscribed to TRIGGER_FIRED and
        runs for ~20 seconds. Measured symptom -- 8 node events published, 0
        delivered, the whole bus frozen for the duration of every graph run.

        That is strictly worse than the bug it replaced, so the flag is now a
        ContextVar. asyncio copies the context into each task, so "am I inside a
        delivery?" is answered per causal chain: a nested publish from a
        subscriber defers, while an unrelated task publishing concurrently
        delivers immediately and cannot be blocked by someone else's slow handler.

        The outermost publish still returns only once its own chain is drained, so
        a caller that awaits it keeps the guarantee it always had: everything this
        event caused has been delivered. A NESTED publish returns before its own
        delivery, which is the intended semantics -- a subscriber announcing
        something must not be able to block on its own announcement.

        `IMMEDIATE_TOPICS` is exempt; see its comment for why telemetry must not
        be deferred behind the work it is reporting on.
        """
        if isinstance(payload, dict):
            logger.warning(f"Warning: publishing raw dict on {topic} instead of strong BaseEvent. Make sure you use the models in backend.models.events")

        pending = _fanout.get()

        if pending is not None and pending.active and topic not in IMMEDIATE_TOPICS:
            # A nested publish on this chain. Defer it behind the event that
            # caused it; the owning loop below will drain it.
            pending.queue.append((topic, payload))
            return

        if topic in IMMEDIATE_TOPICS and pending is not None and pending.active:
            # Telemetry from inside a delivery. Delivered inline, deliberately.
            await self._deliver(topic, payload)
            return

        own = _Fanout()
        own.queue.append((topic, payload))
        token = _fanout.set(own)
        try:
            while own.queue:
                next_topic, next_payload = own.queue.popleft()
                await self._deliver(next_topic, next_payload)
        finally:
            # Marked inactive BEFORE the context is reset, so a task that
            # inherited this context and publishes later starts its own delivery
            # rather than appending to a queue nobody will drain.
            own.active = False
            _fanout.reset(token)

    async def _deliver(self, topic: str, payload: Any) -> None:
        """Hand one event to every subscriber of it. Never raises."""
        # Topic subscribers first, then wildcard observers. Ordered this way
        # so a monitoring/WebSocket observer can never delay or fail ahead of
        # the agent that actually has to act on the event.
        callbacks = list(self._subscribers.get(topic, ()))
        if topic != WILDCARD_TOPIC:
            callbacks += list(self._subscribers.get(WILDCARD_TOPIC, ()))

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(payload)
                else:
                    callback(payload)
            except Exception as e:
                # Swallowed per-callback so one broken subscriber can't stop
                # the others from seeing the event. Logged with the callback
                # name because "Error executing callback" alone gave no way
                # to tell WHICH subscriber failed.
                logger.error(
                    "Error in subscriber %s for topic %s: %s",
                    getattr(callback, "__qualname__", repr(callback)),
                    topic,
                    e,
                )

_bus = MessageBus()

def get_message_bus() -> MessageBus:
    return _bus
