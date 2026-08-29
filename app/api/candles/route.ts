// OHLC history for lib/indicators.ts and the charts.
//
// THIS ROUTE NO LONGER CALLS BINANCE OR YAHOO. It proxies to the FastAPI
// backend, which makes the upstream call from a region those providers serve.
//
// WHY: on Vercel this handler executes in a Vercel-chosen region, and from a US
// region Binance answers 451 — "Service unavailable from a restricted location".
// That surfaced here as a bare 502 and looked like a broken candles endpoint.
// It was not: the request was simply coming from a location Binance refuses.
// See lib/api/backendProxy.server.ts for the full account.
//
// The response shape is UNCHANGED — same fields, same types — because
// components/Candles.tsx, the chart components and every indicator parse it.
// The backend reproduces it exactly; this file adds no transformation.

import { resolveLimit } from '@/lib/candleLimit';
import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const symbol = searchParams.get('symbol');
  const type = searchParams.get('type'); // 'crypto' | 'equity'
  const interval = searchParams.get('interval') || '1h';

  if (!symbol || !type) {
    return Response.json({ error: 'symbol and type are required' }, { status: 400 });
  }

  // Still validated HERE, before the hop, and deliberately so.
  //
  // The inline `Math.min(500, Math.max(20, parseInt(...)))` this replaced let
  // `limit=abc` through as NaN, which reached Binance as `limit=NaN` and came
  // back as a 502 blaming the exchange for a parameter this route never checked.
  // Validating locally keeps a bad request a 400 from Vercel instead of a
  // round-trip that ends in a confusing upstream error. See lib/candleLimit.ts.
  const parsed = resolveLimit(searchParams.get('limit'));
  if (!parsed.ok) {
    return Response.json({ error: parsed.error }, { status: 400 });
  }

  const response = await proxyToBackend('/api/marketdata/candles', {
    symbol,
    type,
    interval,
    limit: String(parsed.limit),
  });

  // The clamp note is added here rather than by the backend because the clamp
  // itself happened here — the backend was told the resolved limit and has no
  // way to know the caller asked for something else.
  if (response.ok && parsed.note) {
    const body = await response.json();
    return Response.json({ ...body, requestedLimit: parsed.requested, limitNote: parsed.note });
  }

  return response;
}
