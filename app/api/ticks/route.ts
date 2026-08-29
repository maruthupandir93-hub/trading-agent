// Live crypto prices, polled by components/MarketData.tsx.
//
// WHAT THIS REPLACES
//
// MarketData.tsx used to open `wss://stream.binance.com:9443` FROM THE BROWSER,
// one socket per visitor. That made the dashboard's correctness depend on the
// VIEWER's location: an operator in a region Binance blocks saw a price grid that
// silently never ticked, from the same build that worked fine elsewhere. It is
// also the reason live prices kept updating while /api/candles returned 502 —
// the two took completely different routes to the same exchange.
//
// The backend now holds ONE socket to Binance for all viewers and caches the
// ticks; this route hands that cache to the browser.
//
// WHY POLLING AND NOT A WEBSOCKET
//
// The frontend is served over https and the backend has no TLS certificate. A
// browser on an https page refuses to open `ws://` and refuses plain `http://`
// fetches — mixed content, a browser policy that cannot be worked around from the
// page. A WebSocket cannot be proxied through a Vercel serverless function either.
// So the browser's last hop must be an ordinary same-origin https request, which
// means polling. Only that last hop is polled: the exchange socket itself is
// still real-time, so the cache a poll reads is a second or so old at most.
//
// Asking for a symbol also SUBSCRIBES to it — see the backend's /ticks docstring.

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const binance = searchParams.get('binance') ?? '';

  // Answered locally: an empty watchlist is common and a round trip to be told
  // "nothing" on every poll is pure cost.
  if (!binance.trim()) {
    return Response.json({ ticks: {}, requested: [] });
  }

  return proxyToBackend('/api/marketdata/ticks', { binance });
}
