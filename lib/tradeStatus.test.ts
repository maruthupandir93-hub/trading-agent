// Per-row trade status. The history table used to render every fill under a
// heading reading "Closed trades", with "—" in the P&L column for most of them —
// so an open position and a finished one were indistinguishable, and the em-dash
// read as missing data rather than as "no result yet, by definition".

import { describe, expect, it } from 'vitest';
import { annotateTrades, openTrades, statusLabel } from './tradeStatus';
import type { TradeLogEntry } from './types';

const MIN = 60_000;
const T0 = Date.UTC(2026, 0, 1, 12, 0, 0);

function trade(over: Partial<TradeLogEntry> & { id: string; side: 'buy' | 'sell' }): TradeLogEntry {
  return { ts: T0, tab: 'paper', symbol: 'BTC/USDT', qty: 1, price: 100, ...over } as TradeLogEntry;
}

const byId = (rows: ReturnType<typeof annotateTrades>) =>
  Object.fromEntries(rows.map((r) => [r.id, r]));

describe('annotateTrades', () => {
  it('marks a lone entry OPEN', () => {
    const [row] = annotateTrades([trade({ id: 'e', side: 'buy' })]);
    expect(row.role).toBe('open');
    expect(row.status).toBe('OPEN');
    expect(row.closedByTradeId).toBeNull();
    expect(statusLabel(row)).toBe('Open');
  });

  it('flips the entry to CLOSED once its exit arrives, and links the two', () => {
    const rows = byId(annotateTrades([
      trade({ id: 'e', side: 'buy', ts: T0 }),
      trade({ id: 'x', side: 'sell', ts: T0 + 30 * MIN, pnl: 12 }),
    ]));

    expect(rows.e.status).toBe('CLOSED');
    expect(rows.e.closedByTradeId).toBe('x');
    expect(rows.e.holdMs).toBe(30 * MIN);

    expect(rows.x.role).toBe('close');
    expect(rows.x.openedByTradeId).toBe('e');
    expect(rows.x.entryPrice).toBe(100);
  });

  it('handles a SHORT — sell opens, buy closes', () => {
    // The direction this agent takes half the time. Treating a sell as an exit
    // by convention would misread both legs.
    const rows = byId(annotateTrades([
      trade({ id: 'e', side: 'sell', ts: T0 }),
      trade({ id: 'x', side: 'buy', ts: T0 + MIN, pnl: -4 }),
    ]));
    expect(rows.e.direction).toBe('short');
    expect(rows.e.status).toBe('CLOSED');
    expect(rows.x.direction).toBe('short');
  });

  it('treats a break-even close at exactly 0 as a close', () => {
    // A truthiness check would read `pnl: 0` as absent and file a finished trade
    // as an open position.
    const rows = byId(annotateTrades([
      trade({ id: 'e', side: 'buy', ts: T0 }),
      trade({ id: 'x', side: 'sell', ts: T0 + MIN, pnl: 0 }),
    ]));
    expect(rows.x.role).toBe('close');
    expect(rows.e.status).toBe('CLOSED');
  });

  it('keeps a partially closed entry OPEN', () => {
    // Half the size is out; the position is still running and must not read as
    // finished.
    const rows = byId(annotateTrades([
      trade({ id: 'e', side: 'buy', ts: T0, qty: 2 }),
      trade({ id: 'x', side: 'sell', ts: T0 + MIN, qty: 1, pnl: 5 }),
    ]));
    expect(rows.e.status).toBe('OPEN');
    expect(rows.x.role).toBe('close');
  });

  it('closes entries FIFO so the oldest finishes first', () => {
    const rows = byId(annotateTrades([
      trade({ id: 'e1', side: 'buy', ts: T0, qty: 1 }),
      trade({ id: 'e2', side: 'buy', ts: T0 + MIN, qty: 1 }),
      trade({ id: 'x', side: 'sell', ts: T0 + 2 * MIN, qty: 1, pnl: 3 }),
    ]));
    expect(rows.e1.status).toBe('CLOSED');
    expect(rows.e2.status).toBe('OPEN');
    expect(rows.x.openedByTradeId).toBe('e1');
  });

  it('does not let one symbol close another symbol`s position', () => {
    const rows = byId(annotateTrades([
      trade({ id: 'btc', side: 'buy', symbol: 'BTC/USDT', ts: T0 }),
      trade({ id: 'eth-x', side: 'sell', symbol: 'ETH/USDT', ts: T0 + MIN, pnl: 5 }),
    ]));
    expect(rows.btc.status).toBe('OPEN');
    expect(rows['eth-x'].openedByTradeId).toBeNull();
  });

  it('keeps the paper and real books separate', () => {
    const rows = byId(annotateTrades([
      trade({ id: 'p', side: 'buy', tab: 'paper', ts: T0 }),
      trade({ id: 'r-x', side: 'sell', tab: 'real', ts: T0 + MIN, pnl: 5 }),
    ]));
    expect(rows.p.status).toBe('OPEN');
    expect(rows['r-x'].openedByTradeId).toBeNull();
  });

  it('reports an unpaired close honestly rather than inventing an entry', () => {
    // Ordinary after a restart or a retention trim. The P&L is real; the hold
    // time and entry price are genuinely unknown.
    const [row] = annotateTrades([trade({ id: 'x', side: 'sell', pnl: -2 })]);
    expect(row.role).toBe('close');
    expect(row.status).toBe('CLOSED');
    expect(row.holdMs).toBeNull();
    expect(row.entryPrice).toBeNull();
    expect(row.direction).toBe('unknown');
  });

  it('returns rows newest first', () => {
    const rows = annotateTrades([
      trade({ id: 'old', side: 'buy', ts: T0 }),
      trade({ id: 'new', side: 'buy', ts: T0 + MIN }),
    ]);
    expect(rows.map((r) => r.id)).toEqual(['new', 'old']);
  });

  it('never puts a pnl on an opening row', () => {
    // An open position's result is unknown until it closes. Deriving one from
    // the entry price would report every open trade as exactly break-even.
    const [row] = annotateTrades([trade({ id: 'e', side: 'buy' })]);
    expect(row.pnl).toBeUndefined();
  });
});

describe('openTrades', () => {
  it('returns only the still-running entries', () => {
    const rows = annotateTrades([
      trade({ id: 'e1', side: 'buy', ts: T0 }),
      trade({ id: 'x1', side: 'sell', ts: T0 + MIN, pnl: 1 }),
      trade({ id: 'e2', side: 'buy', ts: T0 + 2 * MIN }),
    ]);
    expect(openTrades(rows).map((r) => r.id)).toEqual(['e2']);
  });
});
