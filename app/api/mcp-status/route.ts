// Scope note (read this before assuming more than it does): this route
import { writeAuthFailure } from '@/lib/apiAuth';
import { checkOutboundUrl } from '@/lib/ssrfGuard';
// only checks whether an MCP server URL is reachable over HTTP. It does
// NOT implement the MCP JSON-RPC handshake (initialize/tools-list) and
// does NOT wire any tool-calling into the chat loop — that's a separate,
// much larger piece of work than a status check. See the MCP Manager UI
// copy for the same disclosure to the person using the app.

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request) {
  // F34: this route CHANGES STORED STATE, and `middleware.ts` excludes `/api`.
  // Without this the public deployment accepted the write from anyone.
  const unauthorized = writeAuthFailure(req);
  if (unauthorized) return unauthorized;

  let body: { url?: string };
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  // F36: THIS FETCHES A URL THE CALLER CHOOSES, FROM THE SERVER.
  //
  // Returning the status and latency of any address the Vercel function can
  // reach is a blind port scanner — including loopback, the private network
  // and the cloud metadata service at 169.254.169.254. Authentication (F34)
  // stops a stranger using it; this stops it being one at all.
  const check = checkOutboundUrl(body.url);
  if (!check.ok) {
    return Response.json({ reachable: false, error: check.reason });
  }
  const url = check.url.toString();

  const started = Date.now();
  try {
    const res = await fetch(url, {
      method: 'GET',
      signal: AbortSignal.timeout(4000),
      headers: { 'User-Agent': 'Mozilla/5.0 (QUANT-terminal MCP status check)' },
    });
    return Response.json({ reachable: true, status: res.status, latencyMs: Date.now() - started });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'unreachable';
    return Response.json({ reachable: false, error: message, latencyMs: Date.now() - started });
  }
}
