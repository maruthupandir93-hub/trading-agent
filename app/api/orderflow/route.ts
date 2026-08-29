// Order book depth and the trade tape, feeding lib/orderFlow.ts. Crypto only —
// see lib/providerCapabilities.ts for why equities have no equivalent source.
//
// PROXIES TO THE BACKEND rather than calling Binance directly. From a US Vercel
// region Binance answers 451 ("Unavailable For Legal Reasons"), which this route
// surfaced as a 502. The backend makes the call from a served region instead.
// Full reasoning in lib/api/backendProxy.server.ts.
//
// Response shape unchanged: { bids, asks, trades }.

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const binanceSymbol = searchParams.get('binance');

  if (!binanceSymbol) {
    return Response.json(
      { error: 'Order flow data requires a Binance symbol — this endpoint is crypto-only. Equities have no order book/trade tape data source wired up (see the capability matrix).' },
      { status: 400 },
    );
  }

  return proxyToBackend('/api/marketdata/orderflow', { binance: binanceSymbol });
}
