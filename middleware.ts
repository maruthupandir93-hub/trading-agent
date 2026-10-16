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
    const value = basicAuth.split(' ')[1];
    let decoded: string | null = null;
    if (basicAuth.toLowerCase().startsWith('basic ') && value) {
      try {
        decoded = atob(value);
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
      if (pwd.length === requiredPassword.length) {
        let diff = 0;
        for (let i = 0; i < pwd.length; i += 1) {
          diff |= pwd.charCodeAt(i) ^ requiredPassword.charCodeAt(i);
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
     * Match all request paths except for the ones starting with:
     * - api (API routes)
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     */
    '/((?!api|_next/static|_next/image|favicon.ico).*)',
  ],
};
