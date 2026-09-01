// ---------------------------------------------------------------------
// Flow-diagram and execution-cycle logic — pure, no JSX. See `journey.ts` for
// why the split exists.
// ---------------------------------------------------------------------

import type { GraphNodeState, NodeStatus } from '../realtime/store';

export type FlowNode = {
  name: string;
  status: NodeStatus;
  /** One line on what the node produced. `null` when the stream did not say. */
  detail?: string | null;
  durationMs?: number | null;
  /** From the node contract. Surfaced because "which node may call a model" is the
   *  single most important property of this pipeline — the deterministic/LLM ratio
   *  is the number this project watches for drift. */
  mayCallLlm?: boolean;
};

/** Merge a declared node list with live statuses from the store.
 *
 *  WHY A DECLARED TOPOLOGY RATHER THAN ONLY THE NODES SEEN SO FAR
 *
 *  Rendering only nodes the stream has mentioned would make the diagram grow as a
 *  run progresses and vanish between runs — so a quiet system would look like a
 *  system with no pipeline. `/api/graphs/nodes` returns the full registered list
 *  with contracts, so the shape is known up front and statuses fill in.
 *
 *  A node the stream has said nothing about is IDLE, which is true, rather than
 *  absent, which is misleading. */
/** The three fields a diagram actually needs from a node state.
 *
 *  Deliberately narrower than `GraphNodeState`. The live stream's states carry a
 *  graph, run id and symbol; a replayed trace and a selected historical run do
 *  not, because the caller already knows which run it picked. Demanding the full
 *  type here would force those two call sites to invent identity fields nothing
 *  reads — the usual way a type stops describing the data and starts being
 *  satisfied with placeholders. */
export type NodeDisplayState = Pick<GraphNodeState, 'status' | 'detail' | 'durationMs'>;

export function mergeNodeStates(
  declared: { name: string; mayCallLlm?: boolean }[],
  live: Record<string, NodeDisplayState>,
): FlowNode[] {
  return declared.map((d) => {
    const l = live[d.name];
    return {
      name: d.name,
      status: l?.status ?? 'IDLE',
      detail: l?.detail ?? null,
      durationMs: l?.durationMs ?? null,
      mayCallLlm: d.mayCallLlm,
    };
  });
}

/* ===================================================================== */
/* Execution cycle                                                       */
/* ===================================================================== */

// The REAL pipeline. The reference's `execCycle()` uses six invented labels —
// Scan, Detect, Validate, Size, Fill, Settle — which are not stages this system
// has. Keeping them would have been easier and would have described a system that
// does not exist: an operator reading "Size" would look for a sizing stage, and
// sizing happens inside Validate. That matters, because the Risk Gateway is the
// only place in the reasoning layer that sizes.
export const EXEC_STAGES = [
  { key: 'trigger', label: 'Trigger', detail: 'a change passed the debounce and rate gate' },
  { key: 'analyse', label: 'Analyse', detail: 'regime, strategy, 9 specialists, debate' },
  { key: 'decide', label: 'Decide', detail: "the Supervisor's ten answers" },
  { key: 'validate', label: 'Validate', detail: 'Risk Gateway sizes, then validates' },
  { key: 'submit', label: 'Submit', detail: 'approved plan becomes a TAR for the CRO' },
  { key: 'fill', label: 'Fill', detail: 'ExecutionAgent — simulated unless live' },
] as const;

export type ExecStageKey = (typeof EXEC_STAGES)[number]['key'];

/** Map the routed store's current node onto a stage.
 *
 *  Kept in one place so "which stage is this node in?" is answerable without
 *  reading a component. */
export function stageForNode(node: string | null): ExecStageKey | null {
  if (!node) return null;
  if (node === 'supervisor' || node === 'trade_thesis_narrative') return 'decide';
  if (node === 'risk_gateway') return 'validate';
  return 'analyse';
}

/* ===================================================================== */
/* Seeding the pipeline from the LAST COMPLETED RUN                      */
/* ===================================================================== */

/** One node as `/api/graphs/runs` reports it in a finished run's trace. */
export type RunTraceNode = {
  node: string;
  started_at?: number | null;
  duration_ms?: number | null;
  wrote?: string[] | null;
  llm_calls?: number | null;
  error?: string | null;
  unavailable?: boolean | null;
};

export type RunTrace = {
  run_id: string;
  graph: string;
  symbol?: string | null;
  trigger?: string | null;
  started_at?: number | null;
  finished_at?: number | null;
  outcome?: string | null;
  nodes: RunTraceNode[];
};

/** Convert a finished run's trace into the same shape the live stream produces.
 *
 *  WHY THIS EXISTS — THE PIPELINE LOOKED PERMANENTLY DEAD
 *  ------------------------------------------------------
 *  The event stream is a CATCH-UP feed, not a history: `agentEventStream` opens
 *  with `cursor = null`, and the backend answers that with the current head and
 *  NO backlog, deliberately — a new tab must not be shown ten minutes of old
 *  events as though they were happening now.
 *
 *  The consequence nobody had accounted for is that a FRESHLY LOADED PAGE knows
 *  nothing. Every node renders IDLE and the diagram says "No graph node is
 *  running", which is literally true and completely misleading: the agent may
 *  have completed a full 23-node cycle four seconds earlier. Between cycles —
 *  which is most of the time — the operator's answer to "what is my agent doing?"
 *  was a blank pipeline.
 *
 *  `/api/graphs/runs` already stores the whole trace of every run: per node, its
 *  duration, what it wrote, whether it errored and whether it reported itself
 *  unavailable. That is strictly MORE than the live stream carries. So the
 *  pipeline is seeded from the last completed run and the live stream overrides
 *  it the moment a new cycle starts.
 *
 *  IT IS LABELLED AS HISTORY, NOT PASSED OFF AS LIVE. The caller renders
 *  "last completed cycle, Ns ago" whenever the display is seeded rather than
 *  streaming. Showing a finished run as though it were in progress would be the
 *  same class of lie as the animated-on-a-timer diagram this whole module was
 *  written to replace.
 */
export function nodesFromRunTrace(run: RunTrace | null | undefined): Record<string, GraphNodeState> {
  if (!run || !Array.isArray(run.nodes)) return {};

  const out: Record<string, GraphNodeState> = {};
  for (const n of run.nodes) {
    if (!n || typeof n.node !== 'string') continue;

    // A node that raised is FAILED. A node that ran but reported an input it
    // could not measure still COMPLETED — "degraded" and "broken" are different
    // facts and the run trace is careful to keep them apart, so this must be too.
    const status: NodeStatus = n.error ? 'FAILED' : 'COMPLETED';

    const wrote = Array.isArray(n.wrote) ? n.wrote.filter((w) => typeof w === 'string') : [];
    const detail = n.error
      ? n.error
      : wrote.length > 0
        ? wrote.join(', ')
        : 'no state written';

    out[n.node] = {
      name: n.node,
      status,
      durationMs: typeof n.duration_ms === 'number' ? Math.round(n.duration_ms) : null,
      detail,
      at: typeof n.started_at === 'number' ? n.started_at * 1000 : Date.now(),
      // Carried from the trace so a seeded node knows which run and which
      // INSTRUMENT it describes. Without it the diagram cannot say whether the
      // cycle it is showing is the coin the operator is actually trading.
      graph: typeof run.graph === 'string' ? run.graph : null,
      runId: typeof run.run_id === 'string' ? run.run_id : null,
      symbol: typeof run.symbol === 'string' ? run.symbol : null,
    };
  }
  return out;
}

/** Live states win over seeded ones, per node.
 *
 *  Merged per NODE rather than all-or-nothing: a run in progress has reported
 *  three nodes and the previous run reported twenty-three, and the useful display
 *  is the three live ones over the twenty-three historical. Taking the whole live
 *  object only when it is non-empty would blank out the other twenty for the
 *  duration of every run — replacing a stale-but-complete picture with a fresh
 *  and nearly empty one.
 */
export function mergeSeededAndLive(
  seeded: Record<string, GraphNodeState>,
  live: Record<string, GraphNodeState>,
): Record<string, GraphNodeState> {
  if (Object.keys(live).length === 0) return seeded;
  if (Object.keys(seeded).length === 0) return live;
  return { ...seeded, ...live };
}
