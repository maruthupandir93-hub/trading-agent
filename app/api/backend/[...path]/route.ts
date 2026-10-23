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


/**
 * Has the operator deliberately opted into an open write surface?
 *
 * THE BACKEND HONOURS THIS AND THE FRONTEND DID NOT, so the escape hatch was
 * half-wired: on a genuinely local deployment the operator set
 * ALLOW_UNAUTHENTICATED_WRITES, the backend began accepting writes, and this
 * proxy went on refusing them with a message about DASHBOARD_PASSWORD. The
 * documented way out of the fail-closed default did not work end to end.
 *
 * Parsed EXACTLY as `backend/core/auth.open_writes_allowed` parses it —
 * trimmed, lowercased, and matching the same four truthy spellings. Two halves
 * of one application disagreeing about what a flag means is the shape this
 * codebase keeps paying for, and a security flag is the worst place for it:
 * "on" to one side and "off" to the other is a hole nobody can see from
 * either.
 */
function openWritesAllowed(): boolean {
  const raw = (process.env.ALLOW_UNAUTHENTICATED_WRITES ?? '').trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on';
}

/**
 * Field names that carry an exchange secret. Matched against the raw body
 * rather than a parsed one: a body this route cannot parse is exactly the
 * case where guessing its shape is wrong, and a substring test cannot be
 * defeated by nesting the field one level deeper.
 *
 * `apiSecret` alone would be enough for the panels that exist today.
 * `apiKey` and `passphrase` are included because a key without its secret is
 * still an identifier worth not broadcasting, and a future venue may use a
 * passphrase.
 */
const CREDENTIAL_KEYS = ['apiSecret', 'apiKey', 'passphrase', 'privateKey'];

function allowPlaintextSecrets(): boolean {
  const raw = (process.env.ALLOW_PLAINTEXT_EXCHANGE_SECRETS ?? '').trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on';
}

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
  } else if (method !== 'GET' && !openWritesAllowed()) {
    return unauthorized(
      'This action changes trading state and is refused because DASHBOARD_PASSWORD ' +
        'is not set. The backend proxy attaches the server API key, so it must not ' +
        'accept unauthenticated writes on a public deployment. Set DASHBOARD_PASSWORD ' +
        'in the frontend environment (Vercel) and reload to enable it.',
    );
  }

  // IT COULD WALK OUT OF /api, AND THE COMMENT HERE SAID IT COULD NOT.
  //
  // `encodeURIComponent` does NOT encode a dot — `.` and `..` are unreserved
  // characters and come back unchanged. So a request to
  // `/api/backend/%2e%2e/openapi.json` arrived as the segments ['..',
  // 'openapi.json'], was rebuilt as `<backend>/api/../openapi.json`, and `new
  // URL()` then normalised it to `<backend>/openapi.json` — the backend's full
  // OpenAPI schema, every route and every parameter, through a proxy whose
  // GETs are open when DASHBOARD_PASSWORD is unset.
  //
  // Encoding was never the right tool: the job is to refuse a traversal
  // segment, not to spell it differently. A path segment that is `.`, `..`, or
  // carries a separator is not something any backend route has, so it can only
  // be an attempt to leave the prefix.
  for (const segment of segments) {
    if (segment === '.' || segment === '..' || segment.includes('/') || segment.includes('\\')) {
      return Response.json(
        { error: 'Invalid backend path: a path segment may not traverse directories.' },
        { status: 400 },
      );
    }
  }
  const safe = segments.map((s) => encodeURIComponent(s)).join('/');
  const incoming = new URL(req.url);
  const target = new URL(`${backendOrigin()}/api/${safe}`);

  // BELT AND BRACES, because the check above is a blocklist and this is the
  // property that actually matters. If any future encoding quirk produces a
  // path outside the prefix, it stops here rather than reaching the backend.
  if (!target.pathname.startsWith('/api/')) {
    return Response.json(
      { error: 'Invalid backend path: it resolves outside /api.' },
      { status: 400 },
    );
  }
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

    // EXCHANGE SECRETS MAY NOT CROSS A PLAIN-HTTP HOP (review Issue 4).
    //
    // The header above says this hop being server-to-server "is what makes
    // plain http OK", and that is true of MARKET DATA and false of
    // CREDENTIALS. Vercel to the Oracle box is the public internet: nothing
    // about the request being made by a server encrypts it. `TradePanel`
    // posts the operator's apiKey and apiSecret in the body of
    // `/api/operator/exchange/order`, and those are trading keys on a real
    // account.
    //
    // REFUSED, not warned. A warning on a page nobody re-reads does not stop
    // a secret leaving, and this is the only point in the path that can see
    // both the secret and the scheme it is about to cross.
    //
    // The opt-out exists because a laptop talking to a backend on
    // 127.0.0.1 crosses no network at all, and refusing there would break
    // local development for a risk that does not exist. It is named for what
    // it permits, so switching it on is a decision rather than an accident.
    if (body && !allowPlaintextSecrets()) {
      const origin = backendOrigin();
      const encrypted = origin.startsWith('https://');
      const loopback = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/.test(origin);
      if (!encrypted && !loopback && CREDENTIAL_KEYS.some((k) => body.includes(k))) {
        return Response.json(
          {
            error:
              'Refused: this request carries exchange API credentials and the backend ' +
              'hop is plain HTTP, so they would cross the network in clear text. Put ' +
              'TLS on the backend (BACKEND_INTERNAL_URL starting https://), or set ' +
              'ALLOW_PLAINTEXT_EXCHANGE_SECRETS=true if that hop is genuinely a ' +
              'private link you trust. Market data and every other route are ' +
              'unaffected — only credential-bearing bodies are refused.',
          },
          { status: 421 },
        );
      }
    }
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
