'use client';

// ---------------------------------------------------------------------
// One hook that answers "what is my agent doing?" — and answers it even
// between cycles.
//
// THE PROBLEM THIS SOLVES
// -----------------------
// Three pages render the LangGraph pipeline (dashboard, home's execution-cycle
// stepper, the agent page) and all three read `useGraphNodes()`, which is fed
// only by the live event stream. That stream is a catch-up feed: it opens with
// no cursor, and the backend deliberately answers that with the current head and
// NO backlog, so a page that has just loaded knows nothing at all.
//
// Between cycles — which is most of the time — every node therefore rendered
// IDLE under the heading "No graph node is running". True, and useless: the
// agent may have finished a full 23-node run seconds earlier, and the operator's
// whole reason for opening the page was to find out what it did.
//
// So this hook seeds from the last completed run's trace (`/api/graphs/runs`,
// which stores per-node duration, what each node wrote, and any error) and lets
// the live stream override it per node as a new cycle progresses.
//
// IT NEVER PRESENTS HISTORY AS LIVE. `source` says which it is, and `ageSeconds`
// says how old the seeded run is, so a caller can render "last completed cycle,
// 12s ago" instead of implying something is happening now. A diagram that
// animated a finished run would be the same fabrication as one animated on a
// timer, which is what this whole viz layer exists to avoid.
// ---------------------------------------------------------------------

import { useEffect, useMemo, useState } from 'react';

import { BACKEND_PATHS } from '../backendConfig';
import type { GraphNodeState } from './store';
import { useBackend, useCurrentNode, useGraphNodes } from './useRealtime';
import {
  mergeSeededAndLive,
  nodesFromRunTrace,
  type RunTrace,
} from '../viz/flow';

export type PipelineSource = 'live' | 'last-run' | 'none';

export type PipelineView = {
  /** Node name -> state, live where the stream has spoken, seeded otherwise. */
  nodes: Record<string, GraphNodeState>;
  /** The node the stream says is RUNNING right now. `null` between cycles. */
  currentNode: string | null;
  source: PipelineSource;
  /** The run the display is seeded from, when `source === 'last-run'`. */
  lastRun: RunTrace | null;
  /** Seconds since that run finished, recomputed on a timer. */
  ageSeconds: number | null;
  /** True while a cycle is actually in progress. */
  isRunning: boolean;
};

/** How often to re-check for a newly finished run.
 *
 *  10s, not 2s. The live stream is what makes an in-progress run visible; this
 *  poll only has to notice that a run FINISHED, and a finished run does not
 *  change. Polling it at stream frequency would triple the request rate for
 *  data that moves once a cycle.
 */
const RUNS_POLL_MS = 10_000;

/** The graph these pipeline views render. */
export const DECISION_GRAPH = 'trade_analysis';

export function usePipeline(graph: string = DECISION_GRAPH): PipelineView {
  // FILTERED BY GRAPH, and that is not optional.
  //
  // The monitoring graph runs once per tick per open position, so it outnumbers
  // every other graph enormously. Asking for "the most recent run" without a
  // filter returned a `position_monitoring` trace every time — twelve nodes whose
  // names do not appear in Graph 2, so `mergeNodeStates` matched none of them and
  // the diagram stayed exactly as empty as before the seeding was added.
  //
  // limit=1 because only the latest run seeds the diagram: merging two would show
  // nodes from different cycles side by side as though they were one.
  const runs = useBackend<{ runs: RunTrace[] }>(
    `${BACKEND_PATHS.graphRuns}?limit=1&graph=${encodeURIComponent(graph)}`,
    { intervalMs: RUNS_POLL_MS },
  );

  const liveNodes = useGraphNodes();
  const currentNode = useCurrentNode();

  const lastRun = runs.data?.runs?.[0] ?? null;
  const seeded = useMemo(() => nodesFromRunTrace(lastRun), [lastRun]);
  const nodes = useMemo(() => mergeSeededAndLive(seeded, liveNodes), [seeded, liveNodes]);

  const hasLive = Object.keys(liveNodes).length > 0;
  const source: PipelineSource = hasLive
    ? 'live'
    : Object.keys(seeded).length > 0
      ? 'last-run'
      : 'none';

  // Ticks so "12s ago" stays honest without an event to drive it. Kept out of
  // the realtime store for the same reason `useStreamAge` is: an age changes
  // with the clock, not with an event, and routing a tick just to update it
  // would re-render every subscriber once a second.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  const ageSeconds =
    lastRun && typeof lastRun.finished_at === 'number'
      ? Math.max(0, Math.round(now / 1000 - lastRun.finished_at))
      : null;

  return {
    nodes,
    currentNode,
    source,
    lastRun,
    ageSeconds,
    isRunning: currentNode !== null,
  };
}

/** One line describing where the pipeline display came from.
 *
 *  Exported so the three pages phrase it identically — the same state described
 *  two different ways on two pages is how an operator stops trusting either.
 */
export function pipelineSourceLabel(view: PipelineView): string {
  if (view.isRunning) {
    return `Live — ${view.currentNode} is running now.`;
  }
  if (view.source === 'live') {
    return 'Live — the stream has reported this cycle; no node is running right now.';
  }
  if (view.source === 'last-run' && view.lastRun) {
    const age = view.ageSeconds === null ? 'unknown' : `${view.ageSeconds}s`;
    return (
      `Last completed cycle (${view.lastRun.symbol ?? 'unknown symbol'}, ${age} ago). ` +
      `Not live — stages will fill in from the stream when the next cycle starts.`
    );
  }
  return (
    'No cycle has run yet in this backend process. The pipeline shape below is the ' +
    'registered node list, not activity.'
  );
}
