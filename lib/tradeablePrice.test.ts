/**
 * F25: a fabricated price must never reach a trade.
 *
 * `MarketData` writes a random walk into the shared `ticks` map for any symbol
 * whose price is older than 12 seconds, tagged `sim-fallback` — and before
 * this, nothing downstream read that tag. `Agent.tsx` fed those prices to
 * `agentTick` and `Supervisor.reviewAndExecute` then called
 * `buyPaper`/`sellPaper` at them, so a twelve-second backend stall produced
 * paper fills and realised P&L at invented prices.
 *
 * The fallback itself is kept: a labelled placeholder on a chart is honest and
 * blanking the UI on a late poll is worse. What was missing is the line
 * between "fine to render" and "fine to trade on".
 */

import { describe, expect, it } from 'vitest';
import { MAX_TRADE_PRICE_AGE_MS, priceForTrade, tradeablePrice } from './tradeablePrice';

const NOW = 1_700_000_000_000;

describe('tradeablePrice', () => {
  it('accepts a fresh real tick', () => {
    const out = tradeablePrice({ price: 121.5, ts: NOW - 1_000, source: 'ws-live' }, NOW);
    expect(out).toEqual({ ok: true, price: 121.5 });
  });

  it('accepts a fresh polled price too', () => {
    expect(tradeablePrice({ price: 2.3, ts: NOW, source: 'poll-live' }, NOW).ok).toBe(true);
  });

  it('REFUSES a simulated fallback however fresh it is', () => {
    // The whole point: the fabricated tick is written with `ts: Date.now()`,
    // so it is always the freshest thing in the map. Age cannot catch it.
    const out = tradeablePrice({ price: 50_000, ts: NOW, source: 'sim-fallback' }, NOW);
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.reason).toContain('simulated fallback');
  });

  it('refuses a real price that has gone stale', () => {
    const out = tradeablePrice(
      { price: 121.5, ts: NOW - MAX_TRADE_PRICE_AGE_MS - 1, source: 'ws-live' },
      NOW,
    );
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.reason).toContain('old');
  });

  it('refuses an absent, zero, negative or non-finite price', () => {
    for (const tick of [
      undefined,
      null,
      {},
      { price: 0, ts: NOW, source: 'ws-live' as const },
      { price: -5, ts: NOW, source: 'ws-live' as const },
      { price: Number.NaN, ts: NOW, source: 'ws-live' as const },
      { price: Number.POSITIVE_INFINITY, ts: NOW, source: 'ws-live' as const },
    ]) {
      expect(tradeablePrice(tick, NOW).ok).toBe(false);
    }
  });

  it('says WHY, because the refusal is shown to the operator', () => {
    const out = tradeablePrice(undefined, NOW);
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.reason.length).toBeGreaterThan(10);
  });

  it('priceForTrade is undefined rather than a fabricated number', () => {
    expect(priceForTrade({ price: 50_000, ts: NOW, source: 'sim-fallback' }, NOW)).toBeUndefined();
    expect(priceForTrade({ price: 121.5, ts: NOW, source: 'ws-live' }, NOW)).toBe(121.5);
  });
});
