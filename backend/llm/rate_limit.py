"""A process-wide rate limiter for LLM calls, keyed by API key.

WHY THIS EXISTS
===============
`budget.py` caps calls PER RUN — it stops one graph looping on itself. It says so
itself: "Rate limiting across runs is the trigger layer's job." But the trigger
layer rate-limits trade ANALYSIS, not model CALLS, and those are not the same
thing. One analysis run makes several model calls (thesis narrative, external
consultation), a reflection makes one, and the consultation panel makes one more
— all against the SAME NVIDIA key, whose free tier allows 40 requests a minute.

Nothing coordinated them, so a busy minute sailed past 40, NVIDIA returned HTTP
429, `complete()` correctly returned `text=None`, and every LLM node fell back to
its deterministic floor. In the reflection node that floor is the string the
operator kept seeing:

    "Check if losses cluster in this regime before changing weighting."

It looked like the learning was shallow. It was actually the model never being
reached.

WHAT THIS DOES
==============
A sliding-window limiter: it remembers the timestamps of recent calls and, before
letting the next one through, WAITS until fewer than `rpm` sit inside the trailing
60 seconds. The operator asked for exactly this — "wait for 1 min after it" — and
a wait is the right response here because every LLM call in this system is OFF the
critical trading path: the trade is already on the bus at ~4.8s (see the analysis
streaming note in CLAUDE.md), and the reflection runs after the position has
closed. Nothing a human is waiting on blocks behind this.

KEYED BY THE API KEY, NOT THE PROVIDER
======================================
The main provider and the consultation panel are both configured against the same
NVIDIA key in this deployment. The 40/min ceiling is the KEY's, so both must draw
from one bucket — a limiter per provider-id would let two callers on one key spend
80/min between them and still be throttled by NVIDIA. The bucket key is the API
key (hashed, never stored in the clear), falling back to the provider id when
there is no key.

IT NEVER BREAKS THE FAIL-CLOSED CONTRACT
========================================
This only DELAYS a call; it never fabricates a result. If the wait would be
absurd (a misconfiguration, or the limiter and the server disagreeing badly), it
proceeds rather than hanging forever — the server's own 429 is still the backstop,
and `provider.complete` still returns `text=None` on it. A limiter that could
hang a run indefinitely would be worse than the throttling it prevents, because
this process is what enforces stop-losses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import deque
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)

# NVIDIA's free NIM tier is 40 requests/minute per key. Configurable because a
# paid tier or a different vendor has a different ceiling, and because a test
# needs to set it to something it can exercise quickly.
DEFAULT_RPM = int(os.getenv("LLM_MAX_RPM", "40") or "40")

# The window the ceiling is measured over. 60s because the limit is "per minute".
WINDOW_S = 60.0

# A single acquire will never sleep longer than this in one hop. It re-checks
# after waking, so the TOTAL wait can exceed it under sustained pressure — this
# just bounds one sleep so a wildly misconfigured window cannot wedge a task for
# minutes with no log. One full window plus a small margin is the natural cap:
# after that long, at least one slot has certainly aged out.
MAX_SINGLE_WAIT_S = WINDOW_S + 5.0


class AsyncRateLimiter:
    """A sliding-window limiter. `rpm` requests per trailing 60 seconds.

    Async-safe: several graph nodes reach `acquire()` concurrently, and the lock
    makes the window read-and-append atomic so two callers cannot both see 39
    used and both proceed to 41.
    """

    def __init__(self, rpm: int = DEFAULT_RPM, *, name: str = "llm") -> None:
        # A limiter with rpm <= 0 would block forever; treat it as "no limit"
        # rather than a deadlock, and say so, because that is a config error and
        # a hung reasoning layer is the exact failure this module prevents.
        if rpm <= 0:
            logger.warning(
                "Rate limiter %s configured with rpm=%d; treating as UNLIMITED. "
                "A non-positive rate would block every LLM call forever.", name, rpm,
            )
        self.rpm = rpm
        self.name = name
        self._calls: Deque[float] = deque()
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return time.monotonic()

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    async def acquire(self) -> float:
        """Block until a slot is free, then reserve it. Returns seconds waited.

        The reservation is the append at the end: a slot is taken the moment this
        returns, so a caller that then fails its HTTP request has still spent its
        slot — which is correct, because NVIDIA counted the request too.
        """
        if self.rpm <= 0:
            return 0.0

        waited_total = 0.0
        while True:
            async with self._lock:
                now = self._now()
                self._prune(now)
                if len(self._calls) < self.rpm:
                    self._calls.append(now)
                    return waited_total
                # The oldest call in the window is what must age out before a slot
                # frees. Wait exactly until it does, plus a hair so the recheck
                # sees it gone.
                wait = (self._calls[0] + WINDOW_S) - now + 0.01

            wait = min(max(wait, 0.0), MAX_SINGLE_WAIT_S)
            if waited_total == 0.0:
                # Logged once per acquire that actually blocks, at INFO — this is
                # expected backpressure, not an error, but an operator watching
                # "why is reflection slow" should be able to see it.
                logger.info(
                    "LLM rate limiter %s: %d/%d in the last %.0fs, waiting %.1fs for a slot",
                    self.name, len(self._calls), self.rpm, WINDOW_S, wait,
                )
            await asyncio.sleep(wait)
            waited_total += wait

    def snapshot(self) -> Dict[str, object]:
        """Current usage, for a status endpoint. Cheap and lock-free-ish."""
        now = self._now()
        self._prune(now)
        return {
            "name": self.name,
            "rpm": self.rpm,
            "usedInWindow": len(self._calls),
            "windowSeconds": WINDOW_S,
        }


# ---------------------------------------------------------------------------
# One limiter per API-key bucket
# ---------------------------------------------------------------------------

_limiters: Dict[str, AsyncRateLimiter] = {}
_registry_lock: Optional[asyncio.Lock] = None


def _bucket_key(*, api_key: Optional[str], provider_id: str) -> str:
    """A stable, non-secret id for the account a call bills against.

    The API key is hashed, never held in the clear: this dict is long-lived and a
    key sitting in a module global is exactly the kind of thing that ends up in a
    log or a repr. The provider id is the fallback for keyless endpoints (Ollama),
    where every caller genuinely shares one local server.
    """
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return "provider:" + provider_id


def get_rate_limiter(
    *, api_key: Optional[str], provider_id: str, rpm: int = DEFAULT_RPM
) -> AsyncRateLimiter:
    """The shared limiter for this account. Created once per bucket.

    Deliberately NOT guarded by an asyncio.Lock on first creation: building the
    limiter is a cheap synchronous dict insert, and the cost of a rare double
    build under concurrent first-calls (one throwaway deque) is far less than the
    cost of making every call site await a registry lock. The limiter each caller
    ends up sharing is the one that wins the last write, and they converge within
    one call.
    """
    key = _bucket_key(api_key=api_key, provider_id=provider_id)
    limiter = _limiters.get(key)
    if limiter is None:
        limiter = AsyncRateLimiter(rpm=rpm, name=f"{provider_id}:{key[-6:]}")
        _limiters[key] = limiter
    return limiter


def reset_rate_limiters() -> None:
    """Drop every limiter. For tests; also lets a config reload re-read the rpm."""
    _limiters.clear()


def rate_limiter_status() -> Dict[str, object]:
    """All active limiters, for the monitoring API."""
    return {"limiters": [lim.snapshot() for lim in _limiters.values()]}


# ---------------------------------------------------------------------------
# Retry-After parsing, for when the SERVER says 429 anyway
# ---------------------------------------------------------------------------
#
# The limiter keeps us under the ceiling, but it cannot see usage from OTHER
# processes on the same key (a second backend, a manual script, the frontend's
# own chat). So a 429 can still arrive, and when it does the server tells us how
# long to wait. Honouring that is the difference between one retried call and a
# thundering retry that keeps the key throttled.


def parse_retry_after(value: Optional[str], *, default: float = WINDOW_S) -> float:
    """Seconds to wait, from a `Retry-After` header. Bounded and never negative.

    The header is either a number of seconds or an HTTP date. Both are handled;
    an unparseable value falls back to `default` rather than raising, because a
    malformed header must not turn a throttle into a crash. Capped at one window
    plus a margin — a server asking us to wait ten minutes is one we would rather
    fail against and let the node degrade than hang a task on.
    """
    if not value:
        return default
    value = value.strip()
    try:
        secs = float(value)
        return max(0.0, min(secs, MAX_SINGLE_WAIT_S))
    except ValueError:
        pass
    # An HTTP-date form: compute the delta from now.
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when is not None:
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            delta = (when - now).total_seconds()
            return max(0.0, min(delta, MAX_SINGLE_WAIT_S))
    except Exception:  # noqa: BLE001
        pass
    return default
