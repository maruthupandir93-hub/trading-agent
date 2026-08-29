// Funding-rate and open-interest HISTORY, feeding lib/eventDetection.ts's
// funding-spike and OI-delta detectors. Distinct from /api/marketintel, which
// serves the current snapshot rather than a series. Crypto only — futures
// concepts have no equities equivalent.
//
// PROXIES TO THE BACKEND. fapi.binance.com refuses restricted regions with a
// 451 exactly as api.binance.com does, so this had the same latent failure as
// /api/candles even though it never appeared in the original bug report.
//
// Response shape unchanged.

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const binanceSymbol = searchParams.get('binance');

  if (!binanceSymbol) {
    return Response.json(
      { error: 'Funding-rate/OI history requires a Binance symbol — crypto-only, futures data has no equities equivalent.' },
      { status: 400 },
    );
  }

  return proxyToBackend('/api/marketdata/eventdata', { binance: binanceSymbol });
}
