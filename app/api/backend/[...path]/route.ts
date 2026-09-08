// ---------------------------------------------------------------------
// Catch-all same-origin proxy to the FastAPI backend.
//
// THE PROBLEM THIS SOLVES, AND ITS SIZE
//
// `lib/realtime/useRealtime.ts::useBackend` is used by EIGHTEEN pages and two
// components, and it fetched `backendUrl(path)` — `http://<host>:8000/...` —
// straight from the browser. `components/shell/TopBar.tsx` (pause/resume),
// `components/shell/EmergencyStopModal.tsx`, `components/PolymarketPanel.tsx`
// and the live-trading toggle in `app/(terminal)/settings/page.tsx` did the same
// thing directly.
//
// On the real deployment NONE of those requests are allowed to leave the page.
// The frontend is served from Vercel over https; the backend has no TLS
// certificate, so it is plain http; and a browser on an https page refuses
// http:// subresource requests outright. That is mixed content — a browser
// policy, not a CORS setting, not something a header on the backend can permit.
//
// So the dashboard, positions, orders, risk, learning, system, markets, intel,
// polymarket, replay, strategies, execution and exposure pages could not read
// the backend at all, the pause button did nothing, and the EMERGENCY STOP
// silently failed. The pages that appeared to work were the ones reading
// Next.js's own local routes.
//
// HOW THE PROXY FIXES IT
//
//     Browser --https--> Vercel (this route) --http--> backend
//
// The browser's request is same-origin https, so no policy applies. The second
// hop is made by a server, and mixed-content rules govern browsers only. This is
// why the backend needs no certificate — and equally why the browser must never
// be pointed at it directly again.
//
// WHY A CATCH-ALL RATHER THAN A ROUTE PER ENDPOINT
//
// There are ~75 backend paths. Hand-writing a proxy for each is 75 files that
// must stay in step with `BACKEND_PATHS`, and the failure mode of forgetting one
// is a page that silently shows nothing. One file forwards whatever it is given.
//
// WHAT IT DOES NOT DO: it adds no logic, no caching and no reshaping. Anything
// this file decided would be a second place where the API's behaviour is
// defined.
// ---------------------------------------------------------------------

import { backendOrigin } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// ---------------------------------------------------------------------
// WHO IS ALLOWED TO USE THIS PROXY.
//
// This route attaches the backend's `TRADES_API_KEY` to every forwarded request
// (below), so a caller who reaches it can invoke the backend's write routes —
// enable live trading, switch venue, start/stop an autonomous session, reset the
// paper book, emergency-stop — with the server's own credential. It therefore
// must authenticate the INCOMING caller before it does that, or the key is handed
// to anyone on the internet who knows the URL.
//
// The app already has a gate: `middleware.ts` challenges with HTTP Basic auth
// when `DASHBOARD_PASSWORD` is set. But its matcher EXCLUDES `/api`, so this
// proxy was never behind it — the middleware comment even claimed these routes
// "have their own TRADES_API_KEY auth", which was backwards: the proxy SUPPLIES
// that key, it does not check one. So the check is enforced here, independently,
// as defense in depth.
//
//   * DASHBOARD_PASSWORD set  -> every request must carry the matching Basic auth.
//     The browser, already authenticated by the middleware on a page load, sends
//     it automatically on these same-origin calls, so the dashboard keeps working
//     and an unauthenticated caller is refused.
//   * DASHBOARD_PASSWORD unset -> reads (GET) are allowed so the dashboard can
//     render, but STATE-CHANGING methods are REFUSED. An open write proxy on a
//     public, real-money system is the exposure; leaving it open "so nothing
//     breaks" would not be a fix. Set DASHBOARD_PASSWORD to restore writes.
// ---------------------------------------------------------------------

function incomingPasswordOk(req: Request): boolean {
  const required = process.env.DASHBOARD_PASSWORD;
  if (!required) return false; // caller must handle the unset case separately
  const header = req.headers.get('authorization') ?? '';
  const [scheme, value] = header.split(' ');
  if (scheme !== 'Basic' || !value) return false;
  try {
    const decoded = Buffer.from(value, 'base64').toString('utf-8');
    const idx = decoded.indexOf(':');
    // "any username as long as the password matches", same rule as middleware.ts.
    const pwd = idx >= 0 ? decoded.slice(idx + 1) : decoded;
    return pwd === required;
  } catch {
    return false;
  }
}

function unauthorized(detail: string): Response {
  return new Response(JSON.stringify({ error: detail }), {
    status: 401,
    headers: {
      'Content-Type': 'application/json',
      'WWW-Authenticate': 'Basic realm="TradingOS"',
    },
  });
}

// Long enough for a slow backend call; the backend's own upstream timeout is
// 12s, so anything past this is the backend itself being unresponsive rather
// than an exchange being slow.
const TIMEOUT_MS = 25_000;

type Ctx = { params: { path?: string[] } };

async function forward(req: Request, ctx: Ctx, method: 'GET' | 'POST' | 'PUT' | 'DELETE'): Promise<Response> {
  const segments = ctx.params.path ?? [];
  if (segments.length === 0) {
    return Response.json({ error: 'No backend path given.' }, { status: 400 });
  }

  // AUTHENTICATE BEFORE ATTACHING THE BACKEND KEY. See the note at the top.
  const passwordConfigured = Boolean(process.env.DASHBOARD_PASSWORD);
  if (passwordConfigured) {
    if (!incomingPasswordOk(req)) {
      return unauthorized('Authentication required for the backend proxy.');
    }
  } else if (method !== 'GET') {
    return unauthorized(
      'This action changes trading state and is refused because DASHBOARD_PASSWORD ' +
        'is not set. The backend proxy attaches the server API key, so it must not ' +
        'accept unauthenticated writes on a public deployment. Set DASHBOARD_PASSWORD ' +
        'in the frontend environment (Vercel) and reload to enable it.',
    );
  }

  // Rebuilt from the decoded segments rather than pasted from the raw URL, so a
  // caller cannot walk out of /api with `..` or smuggle a second host in.
  const safe = segments.map((s) => encodeURIComponent(s)).join('/');
  const incoming = new URL(req.url);
  const target = new URL(`${backendOrigin()}/api/${safe}`);
  incoming.searchParams.forEach((value, key) => target.searchParams.set(key, value));

  const headers: Record<string, string> = { Accept: 'application/json' };

  const contentType = req.headers.get('content-type');
  if (contentType) headers['Content-Type'] = contentType;

  // The shared secret both halves already use (backend/core/auth.py). Held
  // server-side and attached here, which is the point: the browser performs
  // state-changing actions like pause and emergency-stop without ever holding
  // the key, and the key never enters the client bundle.
  const key = process.env.TRADES_API_KEY;
  if (key) headers.Authorization = `Bearer ${key}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const body = method === 'GET' || method === 'DELETE' ? undefined : await req.text();
    const upstream = await fetch(target.toString(), {
      method,
      headers,
      body,
      signal: controller.signal,
      cache: 'no-store',
    });

    const text = await upstream.text();
    return new Response(text, {
      status: upstream.status,
      headers: { 'Content-Type': upstream.headers.get('content-type') ?? 'application/json' },
    });
  } catch (err) {
    const aborted = err instanceof Error && err.name === 'AbortError';
    const detail = aborted
      ? `timed out after ${TIMEOUT_MS / 1000}s`
      : err instanceof Error
        ? err.message
        : 'unknown error';

    // Names WHICH hop failed. "Vercel cannot reach the backend" and "the backend
    // cannot reach an exchange" are both a 502 to the browser and have entirely
    // different fixes — the first is a firewall or security-list rule, the
    // second is the backend's own region.
    return Response.json(
      {
        error:
          `Could not reach the trading backend at ${backendOrigin()} (${detail}). This is the ` +
          `Vercel-to-backend hop. Check that the backend is running and that its port is open ` +
          `to inbound traffic in the host firewall / cloud security list.`,
      },
      { status: 502 },
    );
  } finally {
    clearTimeout(timer);
  }
}

export async function GET(req: Request, ctx: Ctx) {
  return forward(req, ctx, 'GET');
}

export async function POST(req: Request, ctx: Ctx) {
  return forward(req, ctx, 'POST');
}

export async function PUT(req: Request, ctx: Ctx) {
  return forward(req, ctx, 'PUT');
}

export async function DELETE(req: Request, ctx: Ctx) {
  return forward(req, ctx, 'DELETE');
}
