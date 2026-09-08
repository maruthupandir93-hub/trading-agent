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
    const authValue = basicAuth.split(' ')[1];
    const [user, pwd] = atob(authValue).split(':');

    // We allow any username as long as the password matches.
    if (pwd === requiredPassword) {
      return NextResponse.next();
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
