// The capped volatility history — the eviction rule and the boundary conversion.
//
// Only the pure exports are tested. `recordVolatilityReadings` touches the
// filesystem and its interesting behaviour IS `mergeCapped`, which it calls; a
// test that wrote real files would exercise `serialize` and `writeJson`, both
// already covered where they live.
//
// The eviction rule is the entire point of this store, and a bug in it is
// invisible from the outside: history is simply gone, and nothing reports that it
// was ever there.

import { describe, expect, it } from 'vitest';
import { MAX_ENTRIES, mergeCapped, toEntry, type VolatilityHistoryEntry } from './volatilityHistoryStore.server';

function entry(id: string, ts: number, over: Partial<VolatilityHistoryEntry> = {}): VolatilityHistoryEntry {
  return {
    id,
    runId: id.split(':')[0] ?? null,
    symbol: 'BTC/USDT',
    timeframe: '15m',
    ts,
    regime: 'NORMAL',
    basis: 'percentile',
    atr: 1,
    atrPercent: 1,
    realizedVolatility: 0.1,
    bollingerWidth: 1,
    candleRangePercent: 1,
    percentile: 50,
    volatilityShock: false,
    expansionRatio: null,
    tradingAllowed: true,
    riskMultiplier: 1,
    maxLeverage: 4,
    stopAtrMultiple: 1.5,
    ...over,
  };
}

describe('mergeCapped', () => {
  it('keeps at most MAX_ENTRIES and drops the OLDEST', () => {
    const incoming = Array.from({ length: MAX_ENTRIES + 5 }, (_, i) => entry(`r${i}:BTC/USDT`, i));
    const merged = mergeCapped([], incoming);

    expect(merged).toHaveLength(MAX_ENTRIES);
    // Newest first, and the five lowest timestamps are the ones gone.
    expect(merged[0].ts).toBe(MAX_ENTRIES + 4);
    expect(merged.map((e) => e.ts).sort((a, b) => a - b)[0]).toBe(5);
  });

  it('evicts one old entry when one new one arrives at a full store', () => {
    // The operator's requirement in its plainest form: 15 in, a 16th arrives,
    // the oldest goes.
    const full = Array.from({ length: MAX_ENTRIES }, (_, i) => entry(`r${i}:BTC/USDT`, i + 1));
    const merged = mergeCapped(full, [entry('new:BTC/USDT', 999)]);

    expect(merged).toHaveLength(MAX_ENTRIES);
    expect(merged[0].id).toBe('new:BTC/USDT');
    expect(merged.some((e) => e.id === 'r0:BTC/USDT')).toBe(false);
    expect(merged.some((e) => e.id === 'r1:BTC/USDT')).toBe(true);
  });

  it('updates a re-polled reading in place instead of duplicating it', () => {
    // The backend ring is polled repeatedly, so the same reading is offered again
    // and again. Appending each time would fill all 15 slots with one reading
    // within seconds and evict the genuine history this store exists to keep.
    const first = mergeCapped([], [entry('run-1:BTC/USDT', 100)]);
    const second = mergeCapped(first, [entry('run-1:BTC/USDT', 100, { regime: 'HIGH' })]);

    expect(second).toHaveLength(1);
    expect(second[0].regime).toBe('HIGH');
  });

  it('lets a later poll add a tradeId without losing the reading', () => {
    const stored = mergeCapped([], [entry('run-1:BTC/USDT', 100)]);
    const withTrade = mergeCapped(stored, [entry('run-1:BTC/USDT', 100, { tradeId: 't-9' })]);
    expect(withTrade[0].tradeId).toBe('t-9');
    expect(withTrade[0].atrPercent).toBe(1);
  });

  it('keeps readings for different symbols from the same run apart', () => {
    // One run analysing two symbols produces two genuinely different readings;
    // keying on runId alone would silently discard one of them.
    const merged = mergeCapped([], [entry('run-1:BTC/USDT', 1), entry('run-1:ETH/USDT', 2, { symbol: 'ETH/USDT' })]);
    expect(merged).toHaveLength(2);
  });
});

describe('toEntry', () => {
  const raw = {
    id: 'run-1:BTC/USDT',
    runId: 'run-1',
    symbol: 'BTC/USDT',
    timeframe: '15m',
    ts: 1_788_000_000, // seconds, as the backend stamps them
    regime: 'HIGH',
    basis: 'percentile',
    atr_percent: 0.405,
    realized_volatility: 0.274,
    percentile: 89,
    volatility_shock: true,
    expansion_ratio: 2.4,
    trading_allowed: true,
    risk_multiplier: 0.6,
    max_leverage: 2,
    stop_atr_multiple: 1.5,
  };

  it('converts the backend`s seconds to milliseconds', () => {
    // Without this the entry is stamped ~55 years ago, sorts to the bottom of a
    // 15-entry list, and is evicted immediately — which reads as the agent having
    // stopped measuring volatility.
    expect(toEntry(raw)!.ts).toBe(1_788_000_000_000);
  });

  it('does not re-multiply a timestamp already in milliseconds', () => {
    expect(toEntry({ ...raw, ts: 1_788_000_000_000 })!.ts).toBe(1_788_000_000_000);
  });

  it('maps the snake_case measurements the backend emits', () => {
    const e = toEntry(raw)!;
    expect(e.atrPercent).toBeCloseTo(0.405);
    expect(e.riskMultiplier).toBe(0.6);
    expect(e.volatilityShock).toBe(true);
    expect(e.expansionRatio).toBeCloseTo(2.4);
  });

  it('defaults tradingAllowed to FALSE when the field is missing', () => {
    // The engine's own rule is that unknown volatility blocks rather than
    // defaulting to calm. That has to survive the trip through this boundary — a
    // truthy default here would turn a parse failure into permission to trade.
    const { trading_allowed, ...without } = raw;
    expect(toEntry(without)!.tradingAllowed).toBe(false);
  });

  it('yields null, not 0, for an unmeasured value', () => {
    // riskMultiplier 0 means "blocked"; null means "not measured". Coercing the
    // second into the first would report a hard block that was never computed.
    const { risk_multiplier, ...without } = raw;
    expect(toEntry(without)!.riskMultiplier).toBeNull();
    expect(toEntry({ ...raw, risk_multiplier: 0 })!.riskMultiplier).toBe(0);
  });

  it('rejects a reading with no symbol rather than storing an anonymous one', () => {
    expect(toEntry({ ...raw, symbol: undefined })).toBeNull();
  });
});
