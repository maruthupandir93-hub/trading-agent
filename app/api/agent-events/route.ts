// The agent event stream, polled by lib/agentEventStream.ts.
//
// WHAT THIS REPLACES
//
// The browser opened a WebSocket to `ws://<backend>:8000/api/dashboard/agent-events`.
// With the frontend on https and the backend without a TLS certificate, the
// browser refuses that connection outright — mixed content. A WebSocket also
// cannot be proxied through a Vercel serverless function, so there is no way to
// tunnel it: the connection simply never opened, and the terminal, the debate
// visualizer and the trade history table all sat empty with no error.
//
// The backend now buffers every bus event with a monotonic sequence number
// (backend/services/event_buffer.py) and serves them at
// GET /api/dashboard/events?cursor=N. This route is the same-origin https hop
// that makes that reachable from the browser.
//
// CURSOR: omit it on the first call — that returns no events and the current
// head, because a new client should not be shown a ten-minute backlog as though
// it were happening now. Pass the returned cursor back on each subsequent call.
//
// The WebSocket on the backend is deliberately NOT removed. It still works for
// anything that can reach the host directly, and it becomes the better transport
// again the moment the backend has a TLS hostname.

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const cursor = searchParams.get('cursor') ?? undefined;
  const limit = searchParams.get('limit') ?? undefined;

  return proxyToBackend('/api/dashboard/events', { cursor, limit });
}
