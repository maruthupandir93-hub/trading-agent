"""A replayable ring buffer of bus events, so the dashboard can POLL for them.

WHY THE WEBSOCKET ALONE WAS NOT ENOUGH
--------------------------------------
`api/dashboard.py` broadcasts every bus event to connected WebSocket clients and
keeps NO history. That is fine for a socket — a client is either attached and
receiving, or it is not.

It stops working the moment the browser cannot open the socket at all, which is
the situation this deployment is in: the frontend is served from Vercel over
https, this backend has no TLS certificate, and a browser on an https page
refuses to open `ws://`. A WebSocket also cannot be proxied through a Vercel
serverless function, so there is no way to tunnel it either.

Polling needs something a socket does not: the ability to ask "what happened
since I last asked?". Without a buffer, a poll can only ever return events that
happen to fire during the request itself, which for an event stream means
returning almost nothing.

WHY A CURSOR AND NOT A TIMESTAMP
--------------------------------
Several events can share a millisecond, and this codebase is naive-UTC in some
places and aware in others (see `services/position_store._as_naive_utc` for what
that already cost). A monotonic sequence number has neither problem: it is
unambiguous, it is totally ordered, and "give me everything after 412" has
exactly one correct answer.

The cursor also makes DROPS DETECTABLE. If a client asks for events after 100
and the oldest event still buffered is 340, it missed 240 of them. That is
reported as `missed` rather than silently returning what is left — a UI that
shows a gap without knowing it is a UI that lies about what the agent did.

BOUNDED ON PURPOSE
------------------
`maxlen` is a hard ceiling. This buffer exists so a browser can catch up over a
few seconds, NOT to be an event log — `decisions`, `trades` and `reflections` in
Postgres are the durable record. An unbounded buffer in a long-running process is
a memory leak with a nice name.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ~10 minutes of a busy system at a few events a second. Enough for a browser
# tab that was backgrounded to catch up; not enough to be mistaken for storage.
MAX_BUFFERED_EVENTS = 2000


class EventBuffer:
    def __init__(self, maxlen: int = MAX_BUFFERED_EVENTS) -> None:
        self._events: deque = deque(maxlen=maxlen)
        self._next_seq = 1
        # The bus publishes from the asyncio loop while HTTP handlers read from
        # the same loop, so a lock is not strictly required today. It is here
        # because `deque` mutation plus a counter increment is two operations,
        # and a future threaded caller reading between them would see a sequence
        # number that does not exist yet.
        self._lock = threading.Lock()
        self._dropped = 0

    def append(self, event: Dict[str, Any]) -> int:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append((seq, event))
            return seq

    def since(self, cursor: Optional[int], limit: int = 500) -> Tuple[List[Dict[str, Any]], int, bool]:
        """Events with seq > cursor.

        Returns `(events, next_cursor, missed)`.

        `cursor=None` means "I am new here": it returns NOTHING and the current
        head. A new client is not interested in a ten-minute backlog, and
        replaying one into a fresh terminal view looks like a burst of activity
        that is not happening now.

        `missed` is True when the requested cursor is older than the oldest
        event still buffered — the client fell behind and there is a real gap.
        """
        with self._lock:
            head = self._next_seq - 1
            if cursor is None:
                return [], head, False

            if not self._events:
                # Nothing buffered. Not a gap — just nothing to say. Handing
                # back the client's own cursor keeps it in step rather than
                # jumping it forward past events that may yet arrive.
                return [], max(cursor, head), False

            # The client expects `cursor + 1` next. If the buffer has already
            # discarded that, there is a real gap.
            oldest_seq = self._events[0][0]
            missed = (cursor + 1) < oldest_seq

            selected = [(seq, event) for seq, event in self._events if seq > cursor]
            chunk = selected[:limit]

            if not chunk:
                return [], max(cursor, head), missed

            # The cursor advances to the sequence number of the LAST event
            # actually handed over — read off the event itself, never computed
            # by arithmetic on the old cursor. Arithmetic is wrong the moment
            # the buffer has dropped anything, which is exactly when it matters.
            return [event for _, event in chunk], chunk[-1][0], missed

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "buffered": len(self._events),
                "capacity": self._events.maxlen,
                "head": self._next_seq - 1,
                "oldest": self._events[0][0] if self._events else None,
                "droppedSinceStart": self._dropped,
            }

    def clear(self) -> None:
        """For tests."""
        with self._lock:
            self._events.clear()
            self._next_seq = 1
            self._dropped = 0


_buffer: Optional[EventBuffer] = None


def get_event_buffer() -> EventBuffer:
    global _buffer
    if _buffer is None:
        _buffer = EventBuffer()
    return _buffer
