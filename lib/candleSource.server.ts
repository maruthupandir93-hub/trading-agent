// Historical candle fetching for the backtest routes and the candle providers.
//
// SERVER-SIDE ONLY, AND IT NO LONGER TALKS TO BINANCE OR YAHOO.
//
// Every function here now proxies to the FastAPI backend. The upstream calls,
// the 1000-bar pagination and the Yahoo interval mapping all moved to
// `backend/api/marketdata.py`, unchanged in behaviour.
//
// WHY: these ran inside Vercel serverless handlers, which execute in a
// Vercel-chosen region. From a US region Binance answers 451 — "Service
// unavailable from a restricted location" — so /api/backtest,
// /api/backtest/optimize and /api/backtest/montecarlo were all exposed to the
// same failure that broke /api/candles, and would have surfaced as an
// unexplained 502 the first time anyone ran a backtest on the deployment.
// See lib/api/backendProxy.server.ts for the full account.
//
// THE EXPORTED SIGNATURES ARE UNCHANGED so `lib/candleProviders/*` and the
// backtest routes did not have to be touched. What changed is where the bytes
// come from.

import { fetchFromBackend } from './api/backendProxy.server';

export type Candle = { t: number; o: number; h: number; l: number; c: number; v: number };

// Kept here, and still the validation the callers use, even though the backend
// validates too. A bad interval should be an immediate local error rather than a
// network round trip that ends in one, and `lib/candleProviders/yahoo.ts` reads
// YAHOO_INTERVAL_MAP directly to decide what it can offer before fetching.
export const BINANCE_INTERVALS = new Set(['1m', '5m', '15m', '1h', '4h', '1d', '1w']);

// Yahoo's intraday granularity is more restricted than Binance's, and it rejects
// a long range at a fine granularity. This IS the honest ceiling on equity
// backtest depth — there is no deeper history to page through for equities the
// way there is for Binance. Mirrored in backend/api/marketdata.py; both are the
// same table and must stay in step.
export const YAHOO_INTERVAL_MAP: Record<string, { interval: string; range: string }> = {
  '1m': { interval: '1m', range: '5d' },
  '5m': { interval: '5m', range: '1mo' },
  '15m': { interval: '15m', range: '1mo' },
  '1h': { interval: '60m', range: '3mo' },
  '4h': { interval: '60m', range: '3mo' },
  '1d': { interval: '1d', range: '1y' },
  '1w': { interval: '1wk', range: '5y' },
};

type CandleResponse = { candles: Candle[]; sourceNote?: string };

export async function fetchBinanceCandles(
  binanceSymbol: string,
  interval: string,
  limit: number,
): Promise<Candle[]> {
  const json = await fetchFromBackend<CandleResponse>('/api/marketdata/candles', {
    symbol: binanceSymbol.toUpperCase(),
    type: 'crypto',
    interval,
    limit: String(limit),
  });
  return json.candles ?? [];
}

/**
 * More than 1000 bars.
 *
 * Binance caps a single klines call at 1000, so the backend walks backwards
 * through time with `endTime`, stitching pages and de-duplicating the boundary.
 * That pagination used to live here; it lives there now because that is where
 * the request has to originate. The 10-page ceiling (10,000 bars) is unchanged.
 */
export async function fetchBinanceCandlesDeep(
  binanceSymbol: string,
  interval: string,
  totalBars: number,
): Promise<Candle[]> {
  const json = await fetchFromBackend<CandleResponse>('/api/marketdata/candles/deep', {
    symbol: binanceSymbol.toUpperCase(),
    type: 'crypto',
    interval,
    bars: String(totalBars),
  });
  return json.candles ?? [];
}

export async function fetchYahooCandles(equitySymbol: string, interval: string): Promise<Candle[]> {
  const mapped = YAHOO_INTERVAL_MAP[interval];
  if (!mapped) throw new Error(`Unsupported interval for equities: ${interval}`);

  // No `limit`: the caller slices. Yahoo's range is fixed per granularity, so
  // "all of it" is the only thing that can be asked for, and the backend's
  // /candles route with a large limit returns exactly that.
  const json = await fetchFromBackend<CandleResponse>('/api/marketdata/candles', {
    symbol: equitySymbol,
    type: 'equity',
    interval,
    limit: '1000',
  });
  return json.candles ?? [];
}

/**
 * "Give me as much history as you honestly have, up to N bars."
 *
 * `sourceNote` comes from the backend, which is the only layer that knows
 * whether the upstream ran out of history or the request was simply satisfied —
 * the distinction a backtest needs in order to say whether its sample was the
 * one asked for.
 */
export async function fetchDeepHistory(
  symbol: string,
  type: 'crypto' | 'equity',
  interval: string,
  totalBars: number,
): Promise<{ candles: Candle[]; sourceNote: string }> {
  if (type === 'crypto' && !BINANCE_INTERVALS.has(interval)) {
    throw new Error(`Unsupported interval for crypto: ${interval}`);
  }
  if (type !== 'crypto' && type !== 'equity') {
    throw new Error('type must be "crypto" or "equity"');
  }

  const json = await fetchFromBackend<CandleResponse>('/api/marketdata/candles/deep', {
    symbol,
    type,
    interval,
    bars: String(totalBars),
  });

  return {
    candles: json.candles ?? [],
    sourceNote: json.sourceNote ?? `${json.candles?.length ?? 0} bars.`,
  };
}
