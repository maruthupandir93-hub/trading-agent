// One symbol priced across several venues, feeding lib/multiExchange.ts.
// Crypto only — see lib/providerCapabilities.ts for why equities have no
// equivalent.
//
// PROXIES TO THE BACKEND. This route reached six public exchange hosts directly,
// Binance among them, so it carried the same 451 exposure as /api/candles. The
// backend queries the venues instead and reports each one's outcome separately —
// a venue that fails comes back with `price: null` and a reason rather than being
// dropped, because a missing venue reads as "not checked" when the truth is
// "checked, and here is why there is no number".

import { proxyToBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const symbol = searchParams.get('symbol');
  const binanceSymbol = searchParams.get('binance') ?? undefined;

  if (!symbol) {
    return Response.json({ error: 'symbol query param required (e.g. ?symbol=BTC/USDT).' }, { status: 400 });
  }

  // `symbol` arrives as a display pair (BTC/USDT). The venues need the base and
  // quote separately — OKX and Coinbase want a dashed pair, Binance and Bybit
  // want the concatenated slug — so the split happens here, where the app's
  // symbol convention is already known, rather than teaching the backend about it.
  const [base, quote] = symbol.includes('/') ? symbol.split('/') : [symbol, 'USDT'];

  return proxyToBackend('/api/marketdata/multiexchange', {
    symbol: binanceSymbol || `${base}${quote}`.toUpperCase(),
    base,
    quote,
  });
}
