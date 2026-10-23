"""Run tracing — spec Section 39.7.

    "Tracing tells you what a run did — every node, every tool call, every model
     generation. It's necessary for debugging and for feeding the Explainability
     requirements ... but it's not a substitute for the durable-execution and
     idempotency work in 39.1-39.3. Build both."

That distinction is respected here: this module records what happened. It is
NOT the recovery mechanism — the checkpointer (`runtime.py`) is, and the
execution plane's idempotency is separate again.

WHY LOCAL RECORDS RATHER THAN LANGSMITH
---------------------------------------
Section 39.7 says "LangSmith or an equivalent". LangSmith would ship every trace
— including market data, position sizes and decision rationale — to a third
party. That is a data decision the operator should make deliberately, not one
introduced as a side effect of adding tracing. So: local records by default, with
the shape kept exporter-friendly so wiring LangSmith later is a small change
rather than a rewrite.

FAILED RUNS ARE TRACED TOO
--------------------------
A trace store containing only successful runs cannot answer "why did nothing
trade?" — which is the question this system's operator asks most often, and the
same reason `supervisor_agent` persists refusals alongside approvals.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRACE_DIR = os.path.join(_ROOT, ".data", "graph_traces")

# Keep the most recent N run files. Traces are debugging aids, not the audit
# trail — the `decisions` table is that, and it is unbounded on purpose. An
# unbounded trace directory on a 24/7 system is a disk-space incident.
MAX_TRACE_FILES = 500

# PER GRAPH, not in total (F12). The old single bound let
# `position_monitoring` — which runs once per tick per open position — evict
# every `trade_analysis` trace within minutes, leaving the one view that
# explains a decision with a single row in it.
MAX_TRACES_PER_GRAPH = 250


@dataclass
class NodeTrace:
    node: str
    started_at: float
    duration_ms: float
    # Which state keys this node actually wrote. Compared against its contract
    # when debugging a contract violation.
    wrote: List[str] = field(default_factory=list)
    llm_calls: int = 0
    llm_tokens: int = 0
    error: Optional[str] = None
    # True when the node ran but could not produce its output (missing input,
    # dead feed) rather than raising. Distinguished because they need different
    # responses: one is a data problem, one is a bug.
    unavailable: bool = False


@dataclass
class RunTrace:
    run_id: str
    graph: str
    symbol: str
    thread_id: str
    trigger: str
    started_at: float
    finished_at: Optional[float] = None
    nodes: List[NodeTrace] = field(default_factory=list)
    # Terminal outcome: "completed" | "failed" | "interrupted" | "aborted_budget"
    outcome: str = "running"
    # Set when the run ended without producing a decision. This is the field
    # that answers "why did nothing trade?".
    no_decision_reason: Optional[str] = None
    llm_budget: Dict[str, Any] = field(default_factory=dict)
    unavailable: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at) * 1000.0

    def node_names(self) -> List[str]:
        return [n.node for n in self.nodes]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["durationMs"] = self.duration_ms
        return d


def record_run(trace: RunTrace) -> None:
    """Persist one run trace. Never raises.

    Tracing must not be able to break a run. A trace write that failed loudly
    would mean a full disk could stop the reasoning layer, and the reasoning
    layer is what monitors open positions.
    """
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        # THE GRAPH IS IN THE FILENAME, so trimming can keep a quota PER GRAPH
        # (F12). `position_monitoring` runs once per tick per open position
        # while `trade_analysis` runs once per decision, so a single global
        # cap of 500 meant monitoring traces evicted the analysis ones almost
        # immediately: `GET /api/graphs/runs?graph=trade_analysis&limit=200`
        # returned ONE run. Combined with the decision table being flooded
        # (F10), there was effectively no record of why the agent traded or
        # did not.
        graph_tag = _safe_tag(getattr(trace, "graph", "") or "graph")
        path = os.path.join(
            TRACE_DIR,
            f"{int(trace.started_at * 1000)}_{graph_tag}_{trace.run_id}.json",
        )

        # Atomic write — a crash mid-write otherwise leaves a truncated file
        # that breaks the reader on the next `list_recent_runs()`.
        fd, tmp = tempfile.mkstemp(dir=TRACE_DIR, prefix=".trace.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(trace.to_dict(), fh, indent=2, default=str)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

        _trim_old_traces()
    except Exception as e:
        logger.error("Could not write graph trace for run %s: %s", trace.run_id, e)


def _safe_tag(value: str) -> str:
    """A filename-safe graph name."""
    return "".join(c if c.isalnum() or c in "-" else "-" for c in str(value))[:40] or "graph"


def _trim_old_traces() -> None:
    """Keep a quota PER GRAPH, not one global pool (F12).

    A single cap let the busiest graph starve every other one.
    `position_monitoring` runs once per TICK PER OPEN POSITION and
    `trade_analysis` once per decision, so the analysis traces — the ones that
    answer "why did it trade?" — were evicted within minutes and the runs
    endpoint returned a single row.

    Files not matching the current naming scheme (written before the graph tag
    existed) fall into one shared bucket and are trimmed together, so an
    upgrade does not leave them unbounded.
    """
    try:
        files = sorted(f for f in os.listdir(TRACE_DIR) if f.endswith(".json"))
    except Exception:
        # Trimming is housekeeping. A failure here must not surface.
        return

    by_graph: Dict[str, List[str]] = {}
    for name in files:
        parts = name.split("_", 2)
        tag = parts[1] if len(parts) == 3 else "_legacy"
        by_graph.setdefault(tag, []).append(name)

    for tag, names in by_graph.items():
        excess = len(names) - MAX_TRACES_PER_GRAPH
        for name in (names[:excess] if excess > 0 else []):
            try:
                os.unlink(os.path.join(TRACE_DIR, name))
            except OSError:
                pass


def list_recent_runs(limit: int = 50, graph: Optional[str] = None) -> List[Dict[str, Any]]:
    """Recent run traces, newest first. Used by the monitoring API.

    `graph` FILTERS BY FILENAME, BEFORE ANY FILE IS READ, and that is the point
    rather than an optimisation.

    The caller used to over-fetch the newest 200 traces and filter the parsed
    results. `position_monitoring` runs once per tick per open position while
    `trade_analysis` runs once per decision interval, so the newest 200 are
    almost always ALL monitoring — and a request for `trade_analysis` came back
    EMPTY while 250 of its traces sat on disk. The per-graph retention cap
    (F12) kept the files; nothing made them reachable.

    The graph tag has been in the filename since F12 (`<ts>_<graph>_<runid>`),
    so selecting the right files first is exact. It also stops the reader
    opening 200 JSON files to discard most of them.
    """
    if not os.path.isdir(TRACE_DIR):
        return []
    out: List[Dict[str, Any]] = []
    wanted = _safe_tag(graph) if graph else None
    try:
        candidates = []
        for f in os.listdir(TRACE_DIR):
            if not f.endswith(".json"):
                continue
            if wanted is not None:
                parts = f.split("_", 2)
                # A legacy file (no graph tag) cannot be attributed, so it is
                # excluded from a FILTERED request rather than guessed into
                # one. An unfiltered request still returns it.
                if len(parts) != 3 or parts[1] != wanted:
                    continue
            candidates.append(f)
        names = sorted(candidates, reverse=True)[:limit]
        for name in names:
            try:
                with open(os.path.join(TRACE_DIR, name), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                # Skip an unreadable trace rather than failing the whole listing.
                continue
    except Exception as e:
        logger.error("Could not list graph traces: %s", e)
    return out
