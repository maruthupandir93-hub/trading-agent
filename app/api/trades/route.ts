import { listTrades, addTrade } from '@/lib/tradeStore.server';
import type { NewTrade } from '@/lib/tradeStore.server';
import type { TradeTab, TradeSide } from '@/lib/types';
import { writeAuthFailure } from '@/lib/apiAuth';

// Node runtime, not edge — this route uses the filesystem (see
// lib/tradeStore.server.ts for the persistence caveat).
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// AUTH LIVES IN `lib/apiAuth` NOW, AND THE BESPOKE CHECK HERE WAS HALF OF F35.
//
// It accepted ONLY `Bearer <TRADES_API_KEY>` — a value the browser never has.
// So the moment that key was set (the F6 fix), every browser paper fill,
// including Supervisor-approved AI trades, would have been silently refused:
// `components/Portfolio.tsx` did not check `res.ok`, so the optimistic row
// vanished on refresh and closing P&L never reached `/api/stats`. With the key
// unset, anyone could POST fabricated closes.
//
// `writeAuthFailure` accepts EITHER the dashboard password the browser already
// sends or that Bearer key, so both callers work and neither is open.

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const tabParam = searchParams.get('tab');
  const tab = tabParam === 'paper' || tabParam === 'real' ? (tabParam as TradeTab) : undefined;
  const trades = await listTrades(tab);
  return Response.json({ trades });
}

export async function POST(req: Request) {
  // F34: this route CHANGES STORED STATE, and `middleware.ts` excludes `/api`.
  // Without this the public deployment accepted the write from anyone.
  const unauthorized = writeAuthFailure(req);
  if (unauthorized) return unauthorized;

  let body: {
    tab?: string;
    symbol?: string;
    side?: string;
    qty?: number;
    price?: number;
    note?: string;
    pnl?: number;
    entryContext?: string;
    debateId?: string;
    originTag?: string;
    exchangeOrderId?: string;
  };
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const { tab, symbol, side, qty, price, note, pnl, entryContext, debateId, originTag, exchangeOrderId } = body;

  if (tab !== 'paper' && tab !== 'real') {
    return Response.json({ error: 'tab must be "paper" or "real"' }, { status: 400 });
  }
  if (side !== 'buy' && side !== 'sell') {
    return Response.json({ error: 'side must be "buy" or "sell"' }, { status: 400 });
  }
  if (!symbol || typeof symbol !== 'string') {
    return Response.json({ error: 'symbol is required' }, { status: 400 });
  }
  if (typeof qty !== 'number' || !isFinite(qty) || qty <= 0) {
    return Response.json({ error: 'qty must be a positive number' }, { status: 400 });
  }
  if (typeof price !== 'number' || !isFinite(price) || price <= 0) {
    return Response.json({ error: 'price must be a positive number' }, { status: 400 });
  }

  // Kept in step with `TradeLogEntry['originTag']` in lib/types.ts, which is in
  // turn the set the Python backend writes. A tag missing here is rejected at the
  // door, so an omission does not surface as a validation error on a legitimate
  // trade.
  const VALID_ORIGIN_TAGS = [
    'debate',
    'chat-trade-action',
    'agent-plan',
    'agent-close',
    'user-command',
    'manual-click',
    'manual-panel',
  ];

  const entry = await addTrade({
    tab: tab as TradeTab,
    symbol: symbol.toUpperCase(),
    side: side as TradeSide,
    qty,
    price,
    note,
    pnl: typeof pnl === 'number' && isFinite(pnl) ? pnl : undefined,
    entryContext: typeof entryContext === 'string' ? entryContext : undefined,
    debateId: typeof debateId === 'string' ? debateId : undefined,
    originTag: typeof originTag === 'string' && VALID_ORIGIN_TAGS.includes(originTag) ? (originTag as NewTrade['originTag']) : undefined,
    exchangeOrderId: typeof exchangeOrderId === 'string' ? exchangeOrderId : undefined,
  });

  return Response.json({ trade: entry }, { status: 201 });
}
