// Fear & Greed (market-wide) plus per-symbol derivatives context. Crypto only —
// equities have no equivalent free derivatives source.
//
// PROXIES TO THE BACKEND. fapi.binance.com refuses restricted regions with a 451
// exactly as api.binance.com does, so this shared the latent failure that broke
// /api/candles even though it was not in the original bug report.
//
// The response shape is unchanged for the fields components/MarketIntel.tsx
// reads. The backend adds `fearGreedAvailable` / `derivativesAvailable` on top:
// each half degrades independently, and an all-null `derivatives` block means
// "the derivatives fetch failed", not "funding is zero". A consumer that ignores
// the new flags behaves exactly as before.

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  // Optional: Fear & Greed alone works with no symbol.
  const binanceSymbol = searchParams.get('binance') ?? undefined;

  return proxyToBackend('/api/marketdata/marketintel', { binance: binanceSymbol });
}
