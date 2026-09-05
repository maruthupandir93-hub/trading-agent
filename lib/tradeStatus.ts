// ---------------------------------------------------------------------
// Per-row trade status — is this trade OPEN, or is it finished?
//
// WHY THE LEDGER DOES NOT ALREADY SAY
//
// `trades` is a fill log, not a position log. Each row is one execution, and
// nothing on a row states whether the position it belongs to is still running.
// The history page rendered every row under a heading reading "Closed trades"
// with a P&L column showing "—" for most of them, so an operator could not tell
// an open position from a finished one, and the em-dash read as missing data
// rather than as "this leg has no result yet, by definition".
//
// THE RULE, AND IT IS STRUCTURAL RATHER THAN CONVENTIONAL
//
// The four writers of this table (`execution_agent`, `operator_trade`,
// `operator_exchange` and `lib/tradeStore.server.ts`) all insert OPENING fills
// with `pnl` ABSENT, and only close paths supply one. So:
//
//     pnl present  ->  this row IS a realized close
//     pnl absent   ->  this row is an opening (or adding) fill
//
// `pnl: 0` is a real break-even close and counts; absent is an open. That is the
// same rule `learningDashboard.reconstructClosedTrades` relies on, and it is
// stated in one place here so the two cannot drift.
//
// WHAT AN OPENING ROW'S STATUS THEN DEPENDS ON is whether a later close consumed
// it. That needs a walk of the ledger per (tab, symbol), which is what this does.
//
// NOTHING HERE INFERS A P&L. An open position's result is unknown until it
// closes, and `unrealizedPnl` is supplied by the caller from live marks or left
// null — never derived from the entry price alone, which would report every open
// position as exactly break-even.
// ---------------------------------------------------------------------

import type { TradeLogEntry } from './types';

export type TradeRole = 'open' | 'close';
export type TradeStatus = 'OPEN' | 'CLOSED';

export type AnnotatedTrade = TradeLogEntry & {
  /** Was this fill an entry or an exit? */
  role: TradeRole;
  /** OPEN means the position this row belongs to is still running. */
  status: TradeStatus;
  /** For a close: the id of the opening fill it was matched to, when found. */
  openedByTradeId: string | null;
  /** For an open that was later closed: the id of the closing fill. */
  closedByTradeId: string | null;
  /** ms the position was held. Null when the counterpart leg is outside the window. */
  holdMs: number | null;
  /** 'long' opens on a buy, 'short' on a sell. 'unknown' when unpaired. */
  direction: 'long' | 'short' | 'unknown';
  /** Entry price of the position this row belongs to, when known. */
  entryPrice: number | null;
};

const DUST = 1e-8;

type OpenLeg = {
  id: string;
  ts: number;
  price: number;
  qty: number;
  direction: 'long' | 'short';
};

/**
 * Tag every row with its role and whether its position is still open.
 *
 * Oldest-first internally so the walk sees entries before exits, then returned
 * NEWEST-FIRST because that is the order every table here renders.
 */
export function annotateTrades(trades: TradeLogEntry[]): AnnotatedTrade[] {
  const chronological = [...trades].sort((a, b) => a.ts - b.ts);
  const open: Record<string, OpenLeg[]> = {};
  const out: AnnotatedTrade[] = [];
  // Index into `out` by trade id, so a close can reach back and mark the entry
  // it consumed as CLOSED.
  const byId = new Map<string, AnnotatedTrade>();

  for (const t of chronological) {
    const key = `${t.tab}:${t.symbol}`;
    const isClose = typeof t.pnl === 'number' && Number.isFinite(t.pnl);
    const legs = (open[key] ??= []);

    if (!isClose) {
      const direction: 'long' | 'short' = t.side === 'buy' ? 'long' : 'short';
      const row: AnnotatedTrade = {
        ...t,
        role: 'open',
        // Provisional. A later close in this window flips it.
        status: 'OPEN',
        openedByTradeId: null,
        closedByTradeId: null,
        holdMs: null,
        direction,
        entryPrice: t.price,
      };
      legs.push({ id: t.id, ts: t.ts, price: t.price, qty: t.qty, direction });
      out.push(row);
      byId.set(t.id, row);
      continue;
    }

    // ---- a CLOSE. Consume open legs FIFO. ----------------------------
    //
    // FIFO rather than average-cost, because the question this answers is "which
    // entry is now finished", and the oldest open leg is the one a reader means
    // by that. The P&L is NOT recomputed from the pairing — the row's own `pnl`
    // was calculated at close time against the real entry and is authoritative.
    let remaining = t.qty;
    let firstMatched: OpenLeg | null = null;

    while (remaining > DUST && legs.length > 0) {
      const leg = legs[0];
      firstMatched ??= leg;
      const consumed = Math.min(remaining, leg.qty);
      leg.qty -= consumed;
      remaining -= consumed;
      if (leg.qty <= DUST) {
        const entry = byId.get(leg.id);
        if (entry) {
          entry.status = 'CLOSED';
          entry.closedByTradeId = t.id;
          entry.holdMs = t.ts - entry.ts;
        }
        legs.shift();
      }
    }

    out.push({
      ...t,
      role: 'close',
      // A close is finished by definition — that is what a realized P&L means.
      status: 'CLOSED',
      openedByTradeId: firstMatched?.id ?? null,
      closedByTradeId: null,
      holdMs: firstMatched ? t.ts - firstMatched.ts : null,
      direction: firstMatched?.direction ?? 'unknown',
      entryPrice: firstMatched?.price ?? null,
    });
  }

  return out.reverse();
}

/** The rows whose position is still running, newest first. */
export function openTrades(annotated: AnnotatedTrade[]): AnnotatedTrade[] {
  return annotated.filter((t) => t.status === 'OPEN');
}

/**
 * A one-line summary for a status chip.
 *
 * Separated from the components so the history table, the orders table and the
 * detail page all phrase it identically — the same state described three
 * different ways on three pages is how an operator stops trusting any of them.
 */
export function statusLabel(t: AnnotatedTrade): string {
  if (t.role === 'close') return 'Closed';
  return t.status === 'OPEN' ? 'Open' : 'Entry (closed)';
}

/** Badge vocabulary, shared for the same reason. */
export function statusBadgeState(t: AnnotatedTrade): string {
  if (t.status === 'OPEN') return 'OPEN';
  return 'FILLED';
}
