'use client';

// ---------------------------------------------------------------------
// The instrument the operator is actually trading right now.
//
// WHY THIS EXISTS
//
// The pipeline views seed from "the most recent run of the decision graph",
// whatever symbol that was. The agent analyses whatever it was triggered on —
// a monitoring concern, a research scan, a manual run — so an operator with a
// session running on SOL/USDT would watch the diagram fill in with a BTC/USDT
// cycle the moment anything triggered one, with nothing on screen saying so.
//
// The fix is two-part and this is the first half: the pages that show a pipeline
// ask what the session is on and pin the view to it. The second half is that
// `usePipeline` now labels the symbol in every case, so an UNPINNED view is still
// readable rather than silently ambiguous.
//
// POLLED SLOWLY, ON PURPOSE. A session's symbol is fixed for its whole life — it
// is chosen once, before the session starts. Polling it at the panel's 5s rate
// would triple the request count on `/api/session` for a value that cannot
// change while a session is running.
// ---------------------------------------------------------------------

import { useBackend } from './useRealtime';

const POLL_MS = 20_000;

type SessionShape = { active?: { symbol?: string | null } | null };

/**
 * The running session's symbol, or null when nothing is running.
 *
 * Null is not an error and callers must treat it as "no pin" rather than as a
 * failure — most of the time there is no session, and the pipeline should then
 * show whatever the agent last did.
 */
export function useActiveSessionSymbol(): string | null {
  const res = useBackend<SessionShape>('/api/session', { intervalMs: POLL_MS });
  const symbol = res.data?.active?.symbol;
  return typeof symbol === 'string' && symbol.length > 0 ? symbol : null;
}
