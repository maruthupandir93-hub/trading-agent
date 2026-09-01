"""Recent volatility readings — IN MEMORY ONLY, deliberately not a table.

WHY THIS IS NOT IN POSTGRES
---------------------------
The obvious place for "volatility history, for learning" is a `volatility_history`
table, and the original spec asked for exactly that. The operator's database is
size-constrained and this graph runs the volatility node on EVERY analysis cycle
and EVERY monitoring tick per open position — that is thousands of rows a day, of
which only the handful attached to an actual trade carry any lesson.

So the persistent record lives in the FRONTEND instead, capped at 15 entries:
`.data/volatility-history.json`, written by `lib/volatilityHistoryStore.server.ts`.
This module is only the window through which the frontend can see the readings
that have happened since the process started.

CONSEQUENCE, STATED PLAINLY: this buffer does not survive a restart, and it holds
`MAX_ENTRIES` readings and no more. Anything the frontend has not collected before
either of those happens is gone. That is an accepted trade, not an oversight — the
alternative was unbounded growth in a database the operator is trying to keep
small. If a durable record of every reading is ever wanted, it belongs in the
frontend store's cap, not here.

WRITE-ONLY WITH RESPECT TO THE GRAPH
------------------------------------
`record()` is called by `volatility_analysis` and NOTHING in the graph ever reads
this buffer back. That is what keeps the node replay-safe (Section 39.4): a
resumed checkpoint recomputes the same reading from the same candles, and the fact
that a previous run also appended here changes nothing about the state it
produces. It is telemetry in exactly the same category as the `logger.debug` line
beside it.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# Sized so a frontend polling every few seconds cannot miss a reading between
# polls, while staying trivially bounded. It is NOT the retention policy — the
# frontend's 15-entry file is.
MAX_ENTRIES = 200

# The graph runs on the asyncio loop, but `record()` is reachable from any
# thread that runs a node (the backtest engine uses a worker), and a deque's
# append is atomic while a snapshot read is not. The lock costs nothing at this
# volume and removes the question.
_lock = threading.Lock()
_entries: Deque[Dict[str, Any]] = deque(maxlen=MAX_ENTRIES)


def record(
    *,
    run_id: str,
    symbol: str,
    timeframe: str,
    ts: float,
    reading: Dict[str, Any],
) -> None:
    """Append one reading. Never raises — telemetry must not fail a graph run.

    `run_id` is the identity the frontend stores against, so a reading can be
    matched to the run that produced it and, through that run, to the trade it
    informed. It is NOT deduplicated here: the same run analysing two symbols
    produces two genuinely different readings.
    """
    entry = {
        "id": f"{run_id}:{symbol}",
        "runId": run_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "ts": ts,
        **reading,
    }
    with _lock:
        _entries.append(entry)


def recent(limit: int = 50, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first. `limit` bounds the response, not the buffer."""
    with _lock:
        snapshot = list(_entries)
    snapshot.reverse()
    if symbol:
        snapshot = [e for e in snapshot if e.get("symbol") == symbol]
    return snapshot[:limit]


def size() -> int:
    with _lock:
        return len(_entries)


def reset() -> None:
    """Test hook. One test's readings must not become the next one's history."""
    with _lock:
        _entries.clear()
