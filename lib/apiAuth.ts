/**
 * Caller authentication for the Next.js `/api` write routes.
 *
 * WHY THIS EXISTS (F34/F35). `middleware.ts` excludes `/api`, and of the 21
 * write handlers under `app/api/`, only `/api/trades` checked anything. On the
 * live Vercel URL, with no password, `/api/trades`, `/api/portfolio`,
 * `/api/decisions` and `/api/stats` all answered 200 — and `PUT`/`DELETE
 * /api/portfolio` would replace or wipe the browser's book for anyone who
 * asked.
 *
 * THE TWO HALVES HAD TO BE FIXED TOGETHER, which is F35. `/api/trades` already
 * required `Bearer <TRADES_API_KEY>` — a value the browser never has — so the
 * moment that key was set to close F6, every browser paper fill would have been
 * silently refused: `components/Portfolio.tsx` never checks `res.ok`, so the
 * optimistic row simply vanished on refresh and closing P&L never reached
 * `/api/stats`. Fixing one without the other breaks the dashboard or leaves it
 * open; so both now accept the SAME two credentials.
 *
 * WHAT COUNTS AS AUTHENTICATED:
 *
 *   1. The browser's dashboard Basic credentials, which it attaches
 *      automatically once `middleware.ts` has challenged for them. This is the
 *      path a logged-in operator uses, and it needs no client code.
 *   2. `Authorization: Bearer <TRADES_API_KEY>`, for a server-to-server caller
 *      (a script, the backend itself).
 *
 * WITH NEITHER CONFIGURED, writes are REFUSED rather than allowed — the same
 * fail-closed direction as `backend/core/auth.py`, and for the same reason: an
 * open write surface on a system that holds a trading book is the exposure, and
 * leaving it open "so nothing breaks" is not a fix. Reads are untouched.
 */

const UNSET = 'unset';

export type ApiAuthResult =
  | { ok: true; via: 'dashboard-password' | 'api-key' | 'open-dev' }
  | { ok: false; status: 401 | 503; message: string };

function timingSafeEqual(a: string, b: string): boolean {
  // Not constant time in the cryptographic sense — the Edge/Node runtimes here
  // have no `crypto.timingSafeEqual` in every target — but it does not return
  // early on the first differing byte, which is the practical leak.
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function basicPasswordFrom(header: string): string | null {
  // `atob` THROWS on a malformed header, which turned a bad request into an
  // HTTP 500 in `middleware.ts`. Guarded here so a prober gets a 401.
  const [scheme, value] = header.split(' ');
  if (!scheme || scheme.toLowerCase() !== 'basic' || !value) return null;
  let decoded: string;
  try {
    decoded = atob(value);
  } catch {
    return null;
  }
  // A password may CONTAIN a colon; only the first separates user from pass.
  // Splitting naively truncated such a password and silently compared a prefix.
  const idx = decoded.indexOf(':');
  return idx === -1 ? null : decoded.slice(idx + 1);
}

/**
 * Authenticate a caller for a STATE-CHANGING `/api` route.
 *
 * Returns `{ ok: true }` to proceed, or a status and message to return.
 */
export function authenticateWrite(req: Request): ApiAuthResult {
  const password = process.env.DASHBOARD_PASSWORD || '';
  const apiKey = process.env.TRADES_API_KEY || '';
  const header = req.headers.get('authorization') || '';

  if (!password && !apiKey) {
    if ((process.env.ALLOW_UNAUTHENTICATED_WRITES || '').toLowerCase() === 'true') {
      return { ok: true, via: 'open-dev' };
    }
    return {
      ok: false,
      status: 503,
      message:
        'This route changes stored state and neither DASHBOARD_PASSWORD nor ' +
        'TRADES_API_KEY is configured, so writes are refused. Set DASHBOARD_PASSWORD ' +
        'on the deployment (the browser then authenticates automatically), or set ' +
        'ALLOW_UNAUTHENTICATED_WRITES=true if this instance is genuinely private.',
    };
  }

  if (password) {
    const supplied = basicPasswordFrom(header);
    if (supplied !== null && timingSafeEqual(supplied, password)) {
      return { ok: true, via: 'dashboard-password' };
    }
  }

  if (apiKey) {
    const [scheme, token] = header.split(' ');
    if (scheme && scheme.toLowerCase() === 'bearer' && token && timingSafeEqual(token, apiKey)) {
      return { ok: true, via: 'api-key' };
    }
  }

  return {
    ok: false,
    status: 401,
    message:
      'Unauthorized. Sign in to the dashboard (the browser then sends its ' +
      'credentials automatically), or send Authorization: Bearer <TRADES_API_KEY>.',
  };
}

/** The `Response` for a refused write, or null when the caller may proceed. */
export function writeAuthFailure(req: Request): Response | null {
  const result = authenticateWrite(req);
  if (result.ok) return null;
  return new Response(JSON.stringify({ error: result.message }), {
    status: result.status,
    headers: {
      'Content-Type': 'application/json',
      // Only prompt for Basic when a password is what is missing; prompting
      // when the deployment uses an API key would pop a browser dialog no
      // password can satisfy.
      ...(result.status === 401 && process.env.DASHBOARD_PASSWORD
        ? { 'WWW-Authenticate': 'Basic realm="TradingOS Dashboard"' }
        : {}),
    },
  });
}

/** For diagnostics: which credentials this deployment accepts. */
export function writeAuthMode(): string {
  const password = process.env.DASHBOARD_PASSWORD ? 'dashboard-password' : UNSET;
  const key = process.env.TRADES_API_KEY ? 'api-key' : UNSET;
  return `${password}+${key}`;
}
