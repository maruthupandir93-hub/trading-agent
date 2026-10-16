import type { TickSource } from './types';

/**
 * May this price be used to SIZE OR PLACE a trade?
 *
 * WHY THIS EXISTS (F25). `components/MarketData.tsx` writes a random walk into
 * the shared `ticks` map for any symbol whose price is older than 12 seconds,
 * tagged `source: 'sim-fallback'`. Nothing downstream looked at that tag:
 *
 *   1. `Agent.tsx` fed those prices to `agentTick`;
 *   2. `Supervisor.reviewAndExecute` then called `buyPaper`/`sellPaper`/
 *      `removeRealPosition` at them.
 *
 * So a backend feed stall of twelve seconds produced paper trades, fills and
 * realised P&L at fabricated prices — which is invariant 6 ("never fabricate
 * market data: no invented prices, fills or indicator values") being violated
 * by the display layer and consumed by the trading layer.
 *
 * THE FALLBACK ITSELF IS NOT THE BUG and is kept: a labelled placeholder on a
 * chart is honest, and blanking the whole UI when a poll is late is worse. The
 * bug was that nothing separated "fine to render" from "fine to trade on".
 * That is what this function is.
 */

export type PriceTick = {
  price?: number;
  ts?: number;
  source?: TickSource;
};

/** How stale a real price may be and still be used for a trade. */
export const MAX_TRADE_PRICE_AGE_MS = 15_000;

export type TradeablePrice =
  | { ok: true; price: number }
  | { ok: false; reason: string };

export function tradeablePrice(
  tick: PriceTick | undefined | null,
  now: number = Date.now(),
): TradeablePrice {
  if (!tick || typeof tick.price !== 'number' || !Number.isFinite(tick.price) || tick.price <= 0) {
    return { ok: false, reason: 'no price is available for this symbol' };
  }

  if (tick.source === 'sim-fallback') {
    return {
      ok: false,
      reason:
        'the only price available is a simulated fallback, not a real tick. ' +
        'Trading on it would book a fill at a fabricated price.',
    };
  }

  if (typeof tick.ts === 'number' && now - tick.ts > MAX_TRADE_PRICE_AGE_MS) {
    const age = Math.round((now - tick.ts) / 1000);
    return {
      ok: false,
      reason: `the last real price is ${age}s old, past the ${
        MAX_TRADE_PRICE_AGE_MS / 1000
      }s limit for sizing a trade`,
    };
  }

  return { ok: true, price: tick.price };
}

/** Convenience for call sites that only need the number or undefined. */
export function priceForTrade(
  tick: PriceTick | undefined | null,
  now: number = Date.now(),
): number | undefined {
  const out = tradeablePrice(tick, now);
  return out.ok ? out.price : undefined;
}
