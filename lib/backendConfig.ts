// ---------------------------------------------------------------------
// How the browser reaches the FastAPI backend: THROUGH THIS APP, never directly.
//
// THE RULE, AND WHY IT IS ABSOLUTE
//
// The frontend is served from Vercel over https. The backend has no TLS
// certificate, so it speaks plain http. A browser on an https page REFUSES to
// issue http:// requests from it — mixed content. That is a browser policy: no
// CORS header, no backend setting and no fetch option can permit it.
//
// So a browser fetch to the backend's own origin cannot succeed on the real
// deployment, and it fails in the worst way — the request never leaves the page,
// so the UI sees a network error indistinguishable from "the server is down".
// That is exactly what happened: eighteen pages using `useBackend`, the
// pause/resume button, the Polymarket panel and the EMERGENCY STOP all silently
// did nothing while the backend was running perfectly.
//
// Everything therefore goes to `/api/backend/<path>` on THIS origin, which
// `app/api/backend/[...path]/route.ts` forwards server-to-server. Mixed-content
// rules govern browsers, not servers, so that second hop is unrestricted — and
// it is why the backend needs no certificate for any of this to work.
//
// WHICH SERVER OWNS WHAT
//
// Next.js route handlers under `app/api/` read the JSON stores in `.data/` and
// work with no external dependency. The FastAPI equivalents read Postgres and
// run the agent. Anything the Next.js layer already serves is fetched from the
// same origin with a plain relative URL. Anything that exists ONLY in FastAPI
// goes through the proxy path below.
//
// This distinction has bitten before, so it is worth restating: `/api/health`
// and `/api/trades` are real routes of BOTH servers, and they are different
// routes. Using a Next path against the FastAPI host returns a 404 that reads as
// missing data rather than as a wrong address.
// ---------------------------------------------------------------------

/**
 * FastAPI origin — SERVER-SIDE USE ONLY.
 *
 * Kept for the proxy route and for `lib/api/backendProxy.server.ts`, which run
 * on Vercel's servers where http is fine. DO NOT call this from a component or
 * a hook: the resulting request is mixed content and will be blocked. Use
 * `backendProxyPath()` instead.
 */
export const BACKEND_BASE =
  process.env.BACKEND_INTERNAL_URL?.replace(/\/$/, '') ||
  process.env.NEXT_PUBLIC_BACKEND_URL?.replace(/\/$/, '') ||
  'http://localhost:8000';

/**
 * The same-origin path the BROWSER should fetch for a given backend path.
 *
 *   backendProxyPath('/api/admin/pause')  ->  '/api/backend/admin/pause'
 *
 * The `/api` prefix is stripped because the proxy route re-adds it — its own
 * path already contains one, and `/api/backend/api/admin/pause` would forward to
 * `/api/api/admin/pause`.
 */
export function backendProxyPath(path: string): string {
  const withoutApi = path.replace(/^\/api\//, '/');
  return `/api/backend${withoutApi.startsWith('/') ? withoutApi : `/${withoutApi}`}`;
}

/** Paths on the FastAPI backend, so a rename is a one-line change here. */
export const BACKEND_PATHS = {
  monitoring: '/api/monitoring',
  trades: '/api/execution',
  auditLog: '/api/execution/audit',
  dashboard: '/api/dashboard',
  adminStatus: '/api/admin/status',
  tradingMode: '/api/admin/trading-mode',
  liveTradingEnable: '/api/admin/live-trading/enable',
  liveTradingDisable: '/api/admin/live-trading/disable',
  pause: '/api/admin/pause',
  resume: '/api/admin/resume',
  emergencyStop: '/api/admin/emergency-stop',
  agents: '/api/ai/agents',
  researchQueue: '/api/research/queue',

  // --- The LangGraph reasoning layer (spec Section 39.5) -------------------
  //
  // These exist ONLY on FastAPI. Everything above has a Next.js equivalent
  // reading `.data/`, but the seven graphs run in the Python process and their
  // traces, node contracts and decisions have no JSON-store mirror â€” so unlike
  // the rest of this table, these genuinely require the backend to be running.
  //
  // Before these existed, the reasoning layer was computed, traced to disk and
  // completely unreachable from the dashboard: layers 1-3 of the recommended
  // stack were connected and layer 4 had no API surface at all.
  graphs: '/api/graphs',
  graphNodes: '/api/graphs/nodes',
  graphRuns: '/api/graphs/runs',
  graphPositions: '/api/graphs/positions',
  metaLearning: '/api/graphs/meta-learning',

  // --- Polymarket prediction-market feed (Phase 37) -----------------------
  //
  // FastAPI only, for the same reason as the graph paths above: the poller runs in
  // the Python process and its stores have no `.data/` mirror the Next.js layer
  // reads.
  //
  // `polymarketConfirm` is the human gate. `polymarket_store.confirm_mapping`
  // refuses to mark a mapping confirmed without `set_by_human=True`, and that route
  // is the only place in the codebase that passes it â€” so this path is what makes an
  // otherwise unreachable safety check actually usable.
  polymarket: '/api/polymarket',
  polymarketSignals: '/api/polymarket/signals',
  polymarketMappings: '/api/polymarket/mappings',
  polymarketConfirm: '/api/polymarket/mappings/confirm',
  polymarketSnapshots: '/api/polymarket/snapshots',
  polymarketSeries: '/api/polymarket/series',

  // --- Catalog: the three read-only views added to unblock BLOCKED routes ----
  //
  // Each exposes data that was already in the Python process with no route to it.
  // All read-only; see backend/api/catalog.py for what each honestly is NOT.
  catalog: '/api/catalog',
  catalogOrders: '/api/catalog/orders',
  catalogStrategies: '/api/catalog/strategies',
  catalogReplay: '/api/catalog/replay',

  // --- Everything else the new routes read -----------------------------------
  portfolio: '/api/dashboard/portfolio',
  exchangeStatus: '/api/exchange/status',
  marketPrices: '/api/market/prices',
  memoryStats: '/api/memory/stats',
  memoryReport: '/api/memory/report',
  memoryMistakes: '/api/memory/mistakes',
  researchDashboard: '/api/research/dashboard',
  executionAudit: '/api/execution/audit',
} as const;

/**
 * The same-origin path for the agent-event stream.
 *
 * THIS USED TO BE `agentEventsWsUrl()`, RETURNING A `ws://` URL, AND IT COULD
 * NEVER CONNECT ON THE DEPLOYED SITE — an https page may not open a `ws://`
 * socket, and a WebSocket cannot be proxied through a Vercel serverless
 * function either. The agent terminal, the debate visualizer and the trade
 * history table sat permanently empty with no error shown.
 *
 * `lib/agentEventStream.ts` now polls this route, which reads the backend's
 * cursor-based event buffer. The backend's WebSocket still exists and still
 * works for any client that can reach the host directly — it is the transport
 * to return to once the backend has a TLS hostname, and nothing but that is
 * stopping it.
 */
export const AGENT_EVENTS_PATH = '/api/agent-events';

/**
 * Live node-by-node graph progress (spec Section 39.5).
 *
 * The backend serves this as a WebSocket at `/api/graphs/stream`, which the
 * browser cannot open for the reason above. Exposed as a proxied HTTP path so a
 * consumer polls it instead of opening a socket that will be blocked.
 *
 * Each message is one NODE, carrying counts rather than the state itself — the
 * state holds candles, seven specialist findings and a portfolio snapshot, and
 * shipping it per node would push megabytes for a 20-node run.
 */
export function graphStreamPath(symbol: string): string {
  return `${backendProxyPath('/api/graphs/stream')}?symbol=${encodeURIComponent(symbol)}`;
}

/**
 * Absolute backend URL — SERVER-SIDE ONLY.
 *
 * Renamed from the old `backendUrl()` so that a browser-side call site is a
 * COMPILE ERROR rather than a request that is silently blocked at runtime. Every
 * former caller was in a component or a hook, and every one of them was broken
 * on the deployed site; they now use `backendProxyPath()`.
 *
 * If you are writing a component and reach for this, you want
 * `backendProxyPath()`.
 */
export function serverOnlyBackendUrl(path: string): string {
  return `${BACKEND_BASE}${path}`;
}

