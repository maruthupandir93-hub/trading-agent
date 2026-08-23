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

// Apply the middleware to all routes except Next.js static assets and API routes 
// (which have their own TRADES_API_KEY auth).
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
