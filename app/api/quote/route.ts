// Equity quotes. Equities have no free public WebSocket, so MarketData.tsx polls
// this route.
//
// PROXIES TO THE BACKEND, for two independent reasons:
//
//  1. Vercel's region. Same structural problem as /api/candles — a serverless
//     handler runs wherever Vercel puts it, and providers can and do refuse
//     regions.
//  2. Yahoo killed the endpoint this used to call. `/v7/finance/quote` now
//     answers 401 Unauthorized to unauthenticated clients; it needs a
//     crumb+cookie pair. The backend uses `/v8/finance/chart` instead, which is
//     still keyless and open, and reads the price out of its `meta` block.
//
// That second reason matters when reading old bug reports: this route returned
// 502 for a cause that had NOTHING to do with the Binance geo-block, and both
// looked identical from the browser.
//
// Response shape unchanged: { quotes: [{ symbol, price, prevClose }] }.

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const symbolsParam = searchParams.get('symbols') || '';
  const symbols = [...new Set(symbolsParam.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean))];

  // Answered locally: asking the backend for nothing is a round trip to be told
  // nothing, and the empty case is common (a watchlist with no equities in it).
  if (symbols.length === 0) {
    return Response.json({ quotes: [] });
  }

  return proxyToBackend('/api/marketdata/quote', { symbols: symbols.join(',') });
}
