import type { WatchItem } from './types';

// ---------------------------------------------------------------------
// Multi-Exchange Price Aggregation (Level 14) — crypto only.
//
// Binance is already this app's primary crypto data source (candles,
// order flow). This adds five more public, no-key REST endpoints purely
// for a cross-venue PRICE comparison: Bybit, OKX, Kraken, Coinbase, Crypto.com.
// Nothing here executes anything on any exchange — it's read-only price
// aggregation, feeding the Arbitrage detector (informational — spread
// detection only, not execution, see lib/strategies/arbitrage.ts) and
// giving a sanity check against a single venue's price being stale or
// an outlier.
//
// Equities have no equivalent here at all — there's no second free
// equities data source wired into this app (see providerCapabilities.ts)
// — so this module simply doesn't run for equity WatchItems.
//
// Every exchange call fails independently and is tolerated: a 4/5 or
// even 1/5 result is still useful, so Promise.allSettled is used
// throughout rather than Promise.all, and the aggregate result reports
// exactly which venues answered and which didn't, honestly, rather than
// silently dropping failed ones.
// ---------------------------------------------------------------------

export type ExchangeId = 'binance' | 'bybit' | 'okx' | 'kraken' | 'coinbase' | 'cryptocom';

export const EXCHANGE_LABELS: Record<ExchangeId, string> = {
  binance: 'Binance',
  bybit: 'Bybit',
  okx: 'OKX',
  kraken: 'Kraken',
  coinbase: 'Coinbase',
  cryptocom: 'Crypto.com',
};

export type ExchangeQuote =
  | { exchange: ExchangeId; ok: true; price: number; quoteCurrency: string }
  | { exchange: ExchangeId; ok: false; error: string };

export type MultiExchangeSnapshot = {
  symbol: string;
  quotes: ExchangeQuote[];
  fetchedAt: number;
};

// ---------------------------------------------------------------------
// THE VENUE FETCHERS USED TO LIVE HERE AND HAVE MOVED TO THE BACKEND.
//
// Six `fetchBinance`/`fetchBybit`/... functions and `aggregateMultiExchangePrices`
// called api.binance.com, api.bybit.com, okx.com, kraken.com,
// api.exchange.coinbase.com and api.crypto.com directly from a Vercel route
// handler. Binance refuses a restricted region with a 451, so this shared the
// exact failure that broke /api/candles — and in its quieter form, because
// `Promise.allSettled` meant a refused venue came back as one more failed quote
// rather than as an error. The panel would have shown five venues instead of six
// and looked like Binance was merely slow.
//
// `backend/api/marketdata.py::get_multi_exchange` now queries them from a served
// region and returns THIS FILE'S `MultiExchangeSnapshot` shape verbatim, so
// everything below is unchanged and `app/api/multiexchange/route.ts` is a plain
// proxy.
//
// What remains here is the pure logic, which is where CLAUDE.md says it belongs:
// the types, `computeSpread`, and the chat-context builder. None of it does I/O.
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// Spread analysis — pure, given a snapshot. Only compares quotes that
// actually succeeded; a venue that failed to answer is excluded from
// the spread math, not treated as agreeing or as an outlier.
// ---------------------------------------------------------------------
export type SpreadResult = {
  maxPrice: { exchange: ExchangeId; price: number };
  minPrice: { exchange: ExchangeId; price: number };
  spreadPct: number; // (max - min) / min * 100
  successCount: number;
  failedExchanges: { exchange: ExchangeId; error: string }[];
};

export function computeSpread(snapshot: MultiExchangeSnapshot): SpreadResult | null {
  const successes = snapshot.quotes.filter((q): q is Extract<ExchangeQuote, { ok: true }> => q.ok);
  const failures = snapshot.quotes.filter((q): q is Extract<ExchangeQuote, { ok: false }> => !q.ok);
  if (successes.length < 2) return null; // need at least 2 venues to talk about a spread at all

  let max = successes[0];
  let min = successes[0];
  for (const q of successes) {
    if (q.price > max.price) max = q;
    if (q.price < min.price) min = q;
  }
  const spreadPct = min.price > 0 ? ((max.price - min.price) / min.price) * 100 : 0;

  return {
    maxPrice: { exchange: max.exchange, price: max.price },
    minPrice: { exchange: min.exchange, price: min.price },
    spreadPct,
    successCount: successes.length,
    failedExchanges: failures.map((f) => ({ exchange: f.exchange, error: f.error })),
  };
}

// ---------------------------------------------------------------------
// Chat context injection.
// ---------------------------------------------------------------------
export function buildMultiExchangeContext(snapshots: Record<string, MultiExchangeSnapshot | undefined>, watchlist: WatchItem[]): string {
  const cryptoItems = watchlist.filter((w) => w.type === 'crypto');
  if (cryptoItems.length === 0) return 'MULTI-EXCHANGE: no crypto watchlist symbols (equities have no second free data source — crypto-only feature).';

  const lines = cryptoItems.map((item) => {
    const snap = snapshots[item.symbol];
    if (!snap) return `  ${item.symbol}: not fetched yet`;
    const spread = computeSpread(snap);
    const quoteLine = snap.quotes
      .map((q) => (q.ok ? `${EXCHANGE_LABELS[q.exchange]} $${q.price.toLocaleString()}` : `${EXCHANGE_LABELS[q.exchange]} unavailable`))
      .join(', ');
    if (!spread) return `  ${item.symbol}: ${quoteLine} (fewer than 2 venues answered — no spread to report)`;
    return `  ${item.symbol}: ${quoteLine} — spread ${spread.spreadPct.toFixed(3)}% (${EXCHANGE_LABELS[spread.maxPrice.exchange]} high / ${EXCHANGE_LABELS[spread.minPrice.exchange]} low)`;
  });

  return `MULTI-EXCHANGE PRICES (Binance, Bybit, OKX, Kraken, Coinbase, Crypto.com — public REST, no keys; Coinbase is USD-quoted, the rest USDT-quoted, a small basis difference is expected and not itself an arbitrage signal):\n${lines.join('\n')}`;
}
