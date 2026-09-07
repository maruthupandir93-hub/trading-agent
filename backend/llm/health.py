"""Process-wide LLM health: fallback rate, failure classes, and dead-model detection.

WHY THIS EXISTS
===============
The reasoning layer degraded silently once already and it took a human noticing
that every "lesson learned" read the same to find it. The cause was three things
at once — a model that had gone end-of-life (HTTP 410), a shared-key rate limit,
and a thin prompt — and NONE of them raised: the provider fails closed by design,
so every LLM node quietly fell back to its deterministic floor and the system kept
running while getting measurably dumber.

Fail-closed is correct. What was missing is a NUMBER an operator (or an alert) can
watch: how often are LLM calls actually failing, and why. This module is that
number. It records the outcome of every `complete()` call and reports:

  * the fallback rate over a rolling window (failures / total),
  * a breakdown by failure CLASS (rate-limited, timeout, model-EOL, auth, empty),
  * and, derived from real outcomes rather than an extra probe, which configured
    models look DEAD — a model whose recent calls returned 410/404 is not slow or
    throttled, it is gone, and that is the one an operator must act on now.

DERIVED FROM REAL CALLS, NOT A SEPARATE PROBE
=============================================
The audit suggested "ping each configured model and confirm it's alive." That
works, but a scheduled probe spends the same 40/min key budget the trade path
needs, and it tests a different request than the ones that matter. Recording the
outcomes of the calls the system ALREADY makes costs nothing and reflects reality:
if narration and reflection are succeeding, the model is alive by definition; if
they are returning 410, this says so with the model named.

IT NEVER CHANGES BEHAVIOUR
==========================
This is measurement only. It records and reports; it does not retry, degrade, or
gate anything. A bug here can make a dashboard wrong, never a trade.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

# The rolling window. Sized for "recent behaviour" rather than all-time: a model
# that died an hour ago and was fixed should stop showing as failing once healthy
# calls push the failures out. 500 calls is a few hours of normal traffic.
_WINDOW = 500

# A model is flagged DEAD only after this many EOL/not-found responses in the
# window, so a single fluke 404 (a transient routing blip) does not cry wolf. Two
# is enough to distinguish "gone" from "hiccup" without waiting long.
_DEAD_THRESHOLD = 2


def classify(status_code: Optional[int], error: Optional[str], *, timed_out: bool = False,
             empty: bool = False) -> str:
    """The failure class, for the breakdown. One word an operator can act on.

    The classes map to DIFFERENT operator responses, which is the whole point of
    separating them: a 429 needs patience or a higher tier, a 410 needs a new
    model id in `.env`, a 401 needs a new key, a timeout needs a slower-tier
    timeout or a faster model.
    """
    if empty:
        return "empty_completion"
    if timed_out:
        return "timeout"
    if status_code == 410:
        return "model_eol"          # the gpt-oss-120b case: gone, needs replacing
    if status_code == 404:
        return "model_not_found"    # wrong id, or not enabled for this account
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limited"
    if status_code is not None and status_code >= 400:
        return f"http_{status_code}"
    return "other"


# The classes that mean a model is GONE rather than merely unhappy right now.
_DEAD_CLASSES = frozenset({"model_eol", "model_not_found"})


@dataclass
class _Outcome:
    ts: float
    model: Optional[str]
    tier: Optional[str]
    ok: bool
    error_class: Optional[str]
    error: Optional[str]


@dataclass
class LLMHealthTracker:
    """Records call outcomes and answers "is the reasoning layer healthy?"."""

    _outcomes: Deque[_Outcome] = field(default_factory=lambda: deque(maxlen=_WINDOW))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(
        self,
        *,
        model: Optional[str],
        tier: Optional[str],
        ok: bool,
        error_class: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record one completed call. Called on every `complete()` return path.

        `ok=False` is a call that produced no usable text — which is exactly a
        node fallback, because a node that gets no text uses its deterministic
        floor. So the failure rate here IS the fallback rate.
        """
        with self._lock:
            self._outcomes.append(_Outcome(
                ts=time.time(), model=model, tier=tier, ok=ok,
                error_class=error_class if not ok else None,
                error=(error[:200] if (error and not ok) else None),
            ))

    def snapshot(self) -> Dict[str, object]:
        """The health report, for the monitoring API."""
        with self._lock:
            outcomes = list(self._outcomes)

        total = len(outcomes)
        if total == 0:
            return {
                "callsTracked": 0,
                "fallbackRatePct": 0.0,
                "healthy": True,
                "byFailureClass": {},
                "deadModels": [],
                "note": (
                    "No LLM calls recorded yet this process. Either the market has "
                    "been quiet (no analysis has needed narration) or no provider is "
                    "configured — this is not a fault."
                ),
            }

        failures = [o for o in outcomes if not o.ok]
        by_class: Dict[str, int] = {}
        for o in failures:
            key = o.error_class or "other"
            by_class[key] = by_class.get(key, 0) + 1

        # Dead models: derived from real outcomes. Count EOL/not-found per model.
        dead_counts: Dict[str, int] = {}
        last_error: Dict[str, str] = {}
        for o in failures:
            if o.error_class in _DEAD_CLASSES and o.model:
                dead_counts[o.model] = dead_counts.get(o.model, 0) + 1
                if o.error:
                    last_error[o.model] = o.error
        dead_models = [
            {"model": m, "occurrences": c, "lastError": last_error.get(m)}
            for m, c in dead_counts.items() if c >= _DEAD_THRESHOLD
        ]

        fallback_rate = len(failures) / total * 100.0

        # "Healthy" is a judgement the dashboard can render as one green/red dot:
        # any dead model is unhealthy (a configured model is gone), and a fallback
        # rate this high means the reasoning layer is mostly not reasoning.
        healthy = not dead_models and fallback_rate < 50.0

        return {
            "callsTracked": total,
            "fallbackRatePct": round(fallback_rate, 1),
            "healthy": healthy,
            "byFailureClass": by_class,
            "deadModels": dead_models,
            "note": _note(healthy, fallback_rate, dead_models),
        }

    def reset(self) -> None:
        with self._lock:
            self._outcomes.clear()


def _note(healthy: bool, fallback_rate: float, dead_models: List[dict]) -> str:
    if dead_models:
        names = ", ".join(d["model"] for d in dead_models)
        return (
            f"A configured model appears DEAD: {names} is returning end-of-life / "
            f"not-found responses. Update LLM_MODEL_* (or the consultation model) in "
            f".env — the provider is silently falling back to the deterministic floor "
            f"on every call to it. This is exactly the gpt-oss-120b EOL that degraded "
            f"the learning once before."
        )
    if fallback_rate >= 50.0:
        return (
            f"{fallback_rate:.0f}% of recent LLM calls failed and fell back to the "
            f"deterministic floor. Check the failure breakdown: sustained rate_limited "
            f"means raise LLM_MAX_RPM or slow the trigger rate; timeout means the tier "
            f"is pointed at too slow a model."
        )
    if fallback_rate > 0:
        return (
            f"{fallback_rate:.0f}% of recent LLM calls fell back — within normal range "
            f"for a rate-limited free tier. The reasoning layer is working."
        )
    return "Every recent LLM call succeeded. The reasoning layer is healthy."


_tracker: Optional[LLMHealthTracker] = None


def get_health_tracker() -> LLMHealthTracker:
    global _tracker
    if _tracker is None:
        _tracker = LLMHealthTracker()
    return _tracker


def llm_health_status() -> Dict[str, object]:
    """The health report, for `GET /api/monitoring`."""
    return get_health_tracker().snapshot()


def reset_health_tracker() -> None:
    get_health_tracker().reset()
