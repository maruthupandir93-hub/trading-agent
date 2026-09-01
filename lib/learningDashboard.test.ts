// The round-trip reconstruction behind the P&L dashboard, win rate and trade log.
//
// WHY THIS FILE EXISTS
// --------------------
// The dashboard reported `totalClosedTrades: 0` — every panel blank, expectancy
// null, win rate null — on an account whose trade log rendered four rows on the
// next page over. Two independent faults produced that identical symptom, and
// neither was visible from the outside: the reconstruction was long-only, and it
// discarded any close whose opening leg was not in the window.
//
// Both are asserted below against the shape of ledger that triggered them, so a
// future simplification of `reconstructClosedTrades` cannot quietly reintroduce
// either. The tests deliberately use tiny hand-written ledgers: the expected
// answer should be something a reader can work out without running the code.

import { describe, expect, it } from 'vitest';
import {
  averageHoldTime,
  buildDashboardStats,
  computeExpectancy,
  performanceByHourOfDay,
  reconstructClosedTrades,
} from './learningDashboard';
import type { TradeLogEntry } from './types';

const MIN = 60_000;
const T0 = Date.UTC(2026, 0, 1, 12, 0, 0);

type TradeOverride = Partial<TradeLogEntry> & { id: string; side: 'buy' | 'sell' };

function trade(over: TradeOverride): TradeLogEntry {
  return {
    ts: T0,
    tab: 'paper',
    symbol: 'BTC/USDT',
    qty: 1,
    price: 100,
    ...over,
  } as TradeLogEntry;
}

describe('reconstructClosedTrades', () => {
  it('counts a long round trip and measures its hold time', () => {
    const closed = reconstructClosedTrades([
      trade({ id: 'open', side: 'buy', ts: T0 }),
      trade({ id: 'close', side: 'sell', ts: T0 + 30 * MIN, pnl: 12 }),
    ]);

    expect(closed).toHaveLength(1);
    expect(closed[0].pnl).toBe(12);
    expect(closed[0].direction).toBe('long');
    expect(closed[0].holdMinutes).toBe(30);
    expect(closed[0].paired).toBe(true);
  });

  it('counts a SHORT round trip — sell opens, buy closes', () => {
    // The original bug. This agent trades perpetual futures and shorts are half
    // its trades; the old walk treated the opening sell as an unmatched close,
    // skipped it, and then read the closing buy as a brand-new long. Both legs of
    // every short were therefore invisible to every panel on the dashboard.
    const closed = reconstructClosedTrades([
      trade({ id: 'open', side: 'sell', ts: T0 }),
      trade({ id: 'close', side: 'buy', ts: T0 + 10 * MIN, pnl: -4 }),
    ]);

    expect(closed).toHaveLength(1);
    expect(closed[0].direction).toBe('short');
    expect(closed[0].pnl).toBe(-4);
    expect(closed[0].holdMinutes).toBe(10);
  });

  it('does not let a short corrupt a later long on the same symbol', () => {
    // The second-order damage from the same bug: a mis-read short left stale
    // running quantity behind, so the next genuine long never closed out.
    const closed = reconstructClosedTrades([
      trade({ id: 's-open', side: 'sell', ts: T0 }),
      trade({ id: 's-close', side: 'buy', ts: T0 + 5 * MIN, pnl: 3 }),
      trade({ id: 'l-open', side: 'buy', ts: T0 + 10 * MIN }),
      trade({ id: 'l-close', side: 'sell', ts: T0 + 20 * MIN, pnl: 7 }),
    ]);

    expect(closed.map((c) => c.exitTradeId)).toEqual(['s-close', 'l-close']);
    expect(closed[1].holdMinutes).toBe(10);
    expect(closed[1].direction).toBe('long');
  });

  it('counts a close whose opening leg predates the log, without inventing a hold time', () => {
    // The ordinary case after a restart, a retention trim, or for a position the
    // operator already held. The realized P&L on the row is authoritative — it
    // was computed at close time against the real entry — so discarding the whole
    // trade because a pairing walk failed threw away a fact in favour of a failed
    // reconstruction.
    const closed = reconstructClosedTrades([trade({ id: 'orphan', side: 'sell', pnl: -0.05 })]);

    expect(closed).toHaveLength(1);
    expect(closed[0].pnl).toBe(-0.05);
    expect(closed[0].paired).toBe(false);
    // null, NOT 0. A zero would enter the hold-time average as an instant trade.
    expect(closed[0].holdMinutes).toBeNull();
    expect(closed[0].entryTs).toBeNull();
    expect(closed[0].direction).toBe('unknown');
  });

  it('treats a break-even close at exactly 0 as a close, not an open', () => {
    // A truthiness check here would read `pnl: 0` as absent and file a completed
    // trade as an open position.
    const closed = reconstructClosedTrades([
      trade({ id: 'open', side: 'buy', ts: T0 }),
      trade({ id: 'close', side: 'sell', ts: T0 + MIN, pnl: 0 }),
    ]);
    expect(closed).toHaveLength(1);
    expect(closed[0].pnl).toBe(0);
  });

  it('does not count an opening fill as a trade', () => {
    expect(reconstructClosedTrades([trade({ id: 'open', side: 'buy' })])).toHaveLength(0);
  });

  it('attributes a round trip to the origin that OPENED it', () => {
    // A stop-loss exit is tagged `agent-close` whoever opened the position.
    // Crediting the result to the exit would attribute every outcome in the
    // system to the stop mechanism and none to the strategy that chose the trade.
    const closed = reconstructClosedTrades([
      trade({ id: 'open', side: 'buy', ts: T0, originTag: 'agent-plan' }),
      trade({ id: 'close', side: 'sell', ts: T0 + MIN, pnl: 5, originTag: 'agent-close' }),
    ]);
    expect(closed[0].originTag).toBe('agent-plan');
  });

  it('keeps one continuous hold when a position is averaged into', () => {
    const closed = reconstructClosedTrades([
      trade({ id: 'a', side: 'buy', ts: T0, qty: 1 }),
      trade({ id: 'b', side: 'buy', ts: T0 + 10 * MIN, qty: 1 }),
      trade({ id: 'c', side: 'sell', ts: T0 + 40 * MIN, qty: 2, pnl: 9 }),
    ]);
    expect(closed).toHaveLength(1);
    expect(closed[0].holdMinutes).toBe(40); // from the FIRST entry, not the average-in
  });
});

describe('derived statistics', () => {
  it('reports hold-time sample size separately from trade count', () => {
    // An average over 1 of 3 trades has to be readable as such. Counting the
    // unpaired two as zero-minute holds would report a scalping system that does
    // not exist.
    const closed = reconstructClosedTrades([
      // Two closes with no opening leg in the window, then one fully paired.
      trade({ id: 'o1', side: 'sell', ts: T0, pnl: 1 }),
      trade({ id: 'o2', side: 'sell', ts: T0 + MIN, pnl: 1 }),
      trade({ id: 'open', side: 'buy', ts: T0 + 2 * MIN }),
      trade({ id: 'close', side: 'sell', ts: T0 + 22 * MIN, pnl: 2 }),
    ]);
    const hold = averageHoldTime(closed);
    expect(closed.length).toBeGreaterThan(hold.sampleSize);
    expect(hold.avgMinutes).toBe(20);
  });

  it('groups an unpaired close under "unknown" rather than the hour it exited', () => {
    const closed = reconstructClosedTrades([trade({ id: 'orphan', side: 'sell', pnl: 1 })]);
    expect(performanceByHourOfDay(closed).map((g) => g.group)).toEqual(['unknown']);
  });

  it('produces a non-empty dashboard from a ledger of unpaired closes', () => {
    // The exact failure the operator reported: real trades, all panels blank.
    const log: TradeLogEntry[] = [
      trade({ id: 'a', side: 'sell', ts: T0, pnl: 5 }),
      trade({ id: 'b', side: 'sell', ts: T0 + MIN, pnl: -2 }),
      trade({ id: 'c', side: 'buy', ts: T0 + 2 * MIN, pnl: 3 }),
    ];
    const stats = buildDashboardStats(log);

    expect(stats.totalClosedTrades).toBe(3);
    expect(stats.expectancy.winRatePct).toBeCloseTo((2 / 3) * 100);
    expect(computeExpectancy(reconstructClosedTrades(log)).sampleSize).toBe(3);
    // Hold time is still honestly unknown — the fix restores the P&L, it does not
    // invent the entry timestamps.
    expect(stats.holdTime.avgMinutes).toBeNull();
  });
});
