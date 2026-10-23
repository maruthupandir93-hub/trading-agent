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
    // REDIRECTS ARE NOT FOLLOWED, AND THAT IS THE SECOND HALF OF THE GUARD.
    //
    // `checkOutboundUrl` vets the address the caller NAMED. `fetch` follows up
    // to twenty redirects by default and vets none of them, so a public host
    // an attacker controls could answer 302 to http://169.254.169.254/ and
    // this route would faithfully report the status and latency of the cloud
    // metadata service — the exact probe the guard above exists to refuse,
    // reached by asking politely.
    //
    // A redirect is REPORTED rather than chased. The question this route
    // answers is "is this MCP server reachable", and a server that redirects
    // elsewhere has answered it: the status is real and the operator can see
    // where it pointed. Re-vetting and re-fetching the target would be a
    // second request this route has no reason to make.
    const res = await fetch(url, {
      method: 'GET',
      redirect: 'manual',
      signal: AbortSignal.timeout(4000),
      headers: { 'User-Agent': 'Mozilla/5.0 (QUANT-terminal MCP status check)' },
    });
    const location = res.status >= 300 && res.status < 400 ? res.headers.get('location') : null;
    return Response.json({
      reachable: true,
      status: res.status,
      latencyMs: Date.now() - started,
      ...(location
        ? {
            redirectedTo: location,
            note:
              'This server redirected. The redirect was NOT followed — a caller-chosen ' +
              'URL that redirects into a private address would otherwise turn this ' +
              'reachability check into a probe of our own network.',
          }
        : {}),
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'unreachable';
    return Response.json({ reachable: false, error: message, latencyMs: Date.now() - started });
  }
}
