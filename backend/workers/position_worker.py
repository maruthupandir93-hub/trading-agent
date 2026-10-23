"""Phase 30 driver — runs Graph 4 for every open position (spec Section 13).

    "A trade doesn't end when the order fills — build a persistent monitoring
     workflow."

WHY THIS IS A SEPARATE WORKER FROM `monitor_worker.py`
-----------------------------------------------------
`ContinuousMonitorWorker` says of itself:

    "This is a *reporting* loop, not a second decision loop ... Separating
     'observe and report' from 'act' matters: two loops that can both act on the
     same position will eventually act twice on it."

Putting Graph 4 inside it would break exactly that. So this is a distinct worker,
and it is the ONLY driver of the monitoring graph.

WHY ONE DRIVER AND NOT ALSO A TRIGGER SUBSCRIPTION
--------------------------------------------------
Section 14 prefers event triggers over polling, and `TriggerEvaluator` already has
`evaluate_position`. Subscribing the graph to those triggers as WELL as running
this loop would give one position two concurrent monitoring runs — racing the
shared checkpointer and, worse, both reaching an EXIT and publishing two closes.

So this loop is the single path. The right way to make it more responsive is to
make this worker interruptible by a trigger, not to add a second driver.

WHY A FIVE-MINUTE INTERVAL IS ENOUGH
------------------------------------
Because this loop is not what protects capital. `PositionMonitorAgent` enforces
the stop and target on EVERY TICK; if price collapses, that fires in milliseconds
and does not wait for this. What this loop decides is slower by nature — has the
regime flipped, can the stop be trailed, has the position gone stale — and none of
those change meaningfully inside five minutes.

A one-minute interval would mean twelve times the market I/O for decisions that
would almost always come back HOLD.

DEFAULT OFF, LIKE PHASE 29
--------------------------
`POSITION_MONITORING_ENABLED` (default **false**). Off, the graph still runs and
the decision is logged in full — the reasoning is completely observable — but
nothing is applied. Turning it on is an explicit operator decision.

Note what "on" permits: tightening stops, reducing, and exiting. Every one of those
REDUCES risk. The dangerous direction (opening) is gated separately and by
different flags. Even so this defaults off, because an over-eager EXIT rule closing
a working position is a real harm, not a safe one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ENV_ENABLE = "POSITION_MONITORING_ENABLED"

# See the module docstring. Not a tuning knob to lower casually: the mechanical
# stop is what makes this interval safe.
DEFAULT_INTERVAL_SECONDS = 300


def monitoring_enabled() -> bool:
    """Read at CALL TIME, so flipping the flag does not need a restart."""
    return os.getenv(ENV_ENABLE, "false").strip().lower() == "true"


class PositionMonitoringWorker:
    """Runs Graph 4 for each open position, then applies the decision — or not."""

    def __init__(
        self,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        monitor_agent: Any = None,
        checkpointer: Any = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self._monitor_agent = monitor_agent
        self._checkpointer = checkpointer
        self._running = False
        self.cycles_run = 0
        self.last_cycle: Optional[Dict[str, Any]] = None
        # "ALIVE BUT REPEATEDLY FAILING" WAS INVISIBLE (review Issue 11).
        #
        # `start()` catches every cycle exception so one bad pass cannot kill
        # the loop — which is right, and it meant a loop failing on EVERY pass
        # looked identical to a healthy one from outside: `cycles_run` simply
        # stopped advancing, and nothing reported why. The review asks for
        # three INDEPENDENT states to be monitored rather than one standing in
        # for the others, and this is the third of them.
        self.last_cycle_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None
        self.consecutive_failures = 0

    def attach(self, monitor_agent: Any = None, checkpointer: Any = None) -> None:
        if monitor_agent is not None:
            self._monitor_agent = monitor_agent
        if checkpointer is not None:
            self._checkpointer = checkpointer

    async def start(self) -> None:
        self._running = True
        logger.info(
            "Position monitoring worker started (every %ds, %s=%s, checkpointer=%s).",
            self.interval_seconds, ENV_ENABLE, monitoring_enabled(),
            type(self._checkpointer).__name__ if self._checkpointer else "none",
        )
        while self._running:
            try:
                self.last_cycle = await self.run_cycle()
                self.cycles_run += 1
                self.last_cycle_at = time.time()
                self.consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let one bad cycle kill the loop. A monitoring loop that dies
                # silently is worse than one that never started, because the operator
                # keeps believing something is watching.
                #
                # RECORDED, not only logged. The log is the right place for the
                # detail and the wrong place for the STATE: nobody reads a log
                # to find out whether the thing is working, and a loop failing
                # on every pass looked exactly like a healthy idle one.
                self.last_error = str(e)
                self.last_error_at = time.time()
                self.consecutive_failures += 1
                logger.error(
                    "Position monitoring cycle failed (%d consecutive): %s",
                    self.consecutive_failures, e,
                )
            await asyncio.sleep(self.interval_seconds)

    # Consecutive failures past which the worker is reported unhealthy rather
    # than merely having had a bad pass. One failure is a transient; three in a
    # row is a loop that is not doing its job.
    UNHEALTHY_AFTER = 3

    def health(self) -> Dict[str, Any]:
        """Is the GRAPH-BASED position review actually working?

        This is one of the three independent states the review asks for, and
        the one that had no answer at all. It is NOT the mechanical stop and
        NOT the exchange-side stop — see `backend/api/monitoring` for why
        conflating them is the failure being fixed.
        """
        failing = self.consecutive_failures >= self.UNHEALTHY_AFTER
        return {
            "running": self._running,
            "actingOnDecisions": monitoring_enabled(),
            "cyclesRun": self.cycles_run,
            "lastCycleAt": self.last_cycle_at,
            "consecutiveFailures": self.consecutive_failures,
            "lastError": self.last_error,
            "lastErrorAt": self.last_error_at,
            "healthy": self._running and not failing,
            "note": (
                f"the review loop has failed {self.consecutive_failures} time(s) in a "
                f"row: {self.last_error}"
                if failing else
                "the graph review loop is running"
                if self._running else
                "the graph review loop is NOT running. The MECHANICAL stop is a "
                "separate mechanism and may still be enforcing every stop — do not "
                "read this as 'nothing is watching'."
            ),
        }

    def stop(self) -> None:
        self._running = False
        logger.info(
            "Position monitoring worker stopped after %d cycle(s).", self.cycles_run
        )

    async def run_cycle(self) -> Dict[str, Any]:
        """One monitoring pass over every open position."""
        from backend.graphs.monitoring import (
            apply_decision,
            positions_from_agent,
            run_monitoring_graph,
        )
        from backend.graphs.state import TriggerReason

        if self._monitor_agent is None:
            return {"positions": 0, "note": "no monitor agent attached", "decisions": []}

        positions = positions_from_agent(self._monitor_agent)
        if not positions:
            return {"positions": 0, "note": "no open positions", "decisions": []}

        acting = monitoring_enabled()
        decisions: List[Dict[str, Any]] = []

        # Sequential, deliberately. An EXIT on one position changes the exposure the
        # next position's `portfolio_risk` dimension reads, so running them at once
        # would have each decide against a book that was already stale. They also
        # share a checkpointer.
        for position in positions:
            trigger = TriggerReason(
                kind="scheduled",
                symbol=position.symbol,
                detail=f"position monitoring sweep every {self.interval_seconds}s",
            )
            result = await run_monitoring_graph(
                position, trigger, checkpointer=self._checkpointer
            )

            if not result.get("ok"):
                decisions.append({
                    "symbol": position.symbol,
                    "error": result.get("error"),
                    "applied": False,
                })
                continue

            decision = result.get("decision") or {}
            record = {
                "symbol": position.symbol,
                "tarId": position.tar_id,
                "action": decision.get("action"),
                "reason": decision.get("reason"),
                "rMultiple": (result.get("position") or {}).get("rMultiple"),
                "dimensionsAvailable": result.get("dimensionsAvailable"),
                "dimensionsTotal": result.get("dimensionsTotal"),
            }

            if acting:
                record["applied"] = await apply_decision(
                    result, position, self._monitor_agent
                )
            else:
                record["applied"] = {
                    "applied": False,
                    "action": decision.get("action"),
                    "reason": (
                        f"{ENV_ENABLE} is not 'true', so the decision was computed and "
                        f"logged but NOT applied. The reasoning above is complete; only "
                        f"the action was withheld."
                    ),
                }
                if decision.get("action") not in (None, "HOLD"):
                    # Logged at warning: an operator running with the flag off should
                    # be able to see what the system WOULD have done, and a decision
                    # to exit that was withheld is worth noticing.
                    logger.warning(
                        "WOULD have %s %s (%s) — %s is off, so nothing was applied.",
                        decision.get("action"), position.symbol,
                        decision.get("reason"), ENV_ENABLE,
                    )

            decisions.append(record)

        summary = {
            "positions": len(positions),
            "acting": acting,
            "decisions": decisions,
            "note": (
                "Graph 4 ran for each position. Actions were applied."
                if acting else
                f"Graph 4 ran for each position; {ENV_ENABLE} is off so no action "
                f"was applied. Stop-loss enforcement is unaffected — that runs in "
                f"PositionMonitorAgent on every tick and is never gated by this flag."
            ),
        }
        actions = [d.get("action") for d in decisions]
        logger.info(
            "Position monitoring cycle: %d position(s), decisions %s (acting=%s)",
            len(positions), actions or "none", acting,
        )
        return summary


_worker: Optional[PositionMonitoringWorker] = None


def get_position_worker(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> PositionMonitoringWorker:
    global _worker
    if _worker is None:
        _worker = PositionMonitoringWorker(interval_seconds)
    return _worker


def reset_for_tests() -> None:
    global _worker
    _worker = None
