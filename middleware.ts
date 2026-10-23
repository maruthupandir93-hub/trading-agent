import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(req: NextRequest) {
  // We only want to protect the dashboard if the user has configured a password.
  // If no password is provided in env, we allow access (useful for local dev).
  const requiredPassword = process.env.DASHBOARD_PASSWORD;
  
  if (!requiredPassword) {
    return NextResponse.next();
  }

  const basicAuth = req.headers.get('authorization');

  if (basicAuth) {
    // THREE SMALL FAULTS, ALL IN THESE FOUR LINES.
    //
    // 1. `atob` THROWS on a malformed value, and an uncaught throw in
    //    middleware is an HTTP 500 — so a junk header answered "server error"
    //    instead of "unauthorized".
    // 2. `.split(':')` DESTRUCTURED ONLY THE FIRST SEGMENT, so a password
    //    containing a colon was silently truncated and the comparison was
    //    against a prefix. The operator's password worked for a shorter
    //    string than they set.
    // 3. `===` on a secret returns early at the first differing byte.
    // 4. `atob` ALONE LOCKED OUT EVERY NON-ASCII PASSWORD.
    //
    //    `atob` returns a BYTE string: each UTF-8 byte becomes one character.
    //    A password containing any character outside ASCII therefore decodes
    //    to mojibake and never equals the real one, so the operator is
    //    challenged forever by a password they typed correctly — with no error
    //    anywhere saying why.
    //
    //    `app/api/backend/[...path]/route.ts` already did this correctly with
    //    `Buffer.from(value, 'base64').toString('utf-8')`, so the two halves of
    //    the same gate disagreed about what the password WAS. Buffer does not
    //    exist in the Edge runtime, so the bytes are decoded explicitly.
    const value = basicAuth.split(' ')[1];
    let decoded: string | null = null;
    if (basicAuth.toLowerCase().startsWith('basic ') && value) {
      try {
        const binary = atob(value);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        decoded = new TextDecoder('utf-8').decode(bytes);
      } catch {
        decoded = null;
      }
    }
    if (decoded !== null) {
      // Only the FIRST colon separates user from password; the rest belong to
      // the password.
      const sep = decoded.indexOf(':');
      const pwd = sep === -1 ? '' : decoded.slice(sep + 1);
      // Not constant time in the cryptographic sense — the Edge runtime has no
      // `timingSafeEqual` — but it does not return early on the first
      // mismatching byte, which is the practical leak.
      // Compared as UTF-8 BYTES, so a password whose characters differ in
      // JavaScript's UTF-16 representation but not in what was actually sent
      // cannot slip through, and vice versa. The two sides are now decoded the
      // same way, which is the half that was broken.
      const encoder = new TextEncoder();
      const got = encoder.encode(pwd);
      const want = encoder.encode(requiredPassword);
      if (got.length === want.length) {
        let diff = 0;
        for (let i = 0; i < got.length; i += 1) {
          diff |= got[i] ^ want[i];
        }
        if (diff === 0) {
          return NextResponse.next();
        }
      }
    }
  }

  // Request credentials
  return new NextResponse('Unauthorized.', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="TradingOS Dashboard"',
    },
  });
}

// Apply the middleware to all routes except Next.js static assets and API routes.
//
// `/api` is excluded here because some API routes are polled before a page has
// prompted for Basic auth, and challenging them would break rendering. The
// SENSITIVE one — `/api/backend/[...path]`, which forwards to the backend with
// the server key — enforces the SAME DASHBOARD_PASSWORD check itself (see that
// route), and refuses writes entirely when no password is set. So excluding
// `/api` here does NOT leave the backend proxy unauthenticated; the check lives
// with the route that attaches the key.
export const config = {
  matcher: [
    /*
     * EVERY PATH EXCEPT STATIC ASSETS AND THE HEALTH CHECK.
     *
     * `api` USED TO BE EXCLUDED HERE, and that made the operator's data
     * publicly readable from the Vercel URL with no password at all:
     * /api/trades, /api/stats, /api/decisions, /api/portfolio, /api/memory
     * and the rest answer a plain GET. Verified live by the review.
     *
     * The old comment justified it as "API routes have their own
     * TRADES_API_KEY auth", which was wrong twice over: the backend PROXY
     * supplies that key rather than checking one (fixed separately), and the
     * Next app's OWN read routes never had any auth to begin with. Reads were
     * left open on the backend deliberately — the browser polls them and
     * cannot hold a server secret — but that argument is about the
     * BACKEND's port, not about a public internet URL.
     *
     * The browser replays its Basic credential automatically on same-origin
     * requests once the middleware has challenged the page, so the dashboard
     * keeps working with no code change. With DASHBOARD_PASSWORD unset the
     * middleware returns `next()` immediately, so local development is
     * unaffected.
     *
     * `/api/health` stays open on purpose: an uptime probe must not need a
     * credential, and it returns no account data.
     */
    '/((?!api/health|_next/static|_next/image|favicon.ico).*)',
  ],
};
