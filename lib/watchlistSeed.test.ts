// ---------------------------------------------------------------------
// The watchlist has TWO seed sources, and they must agree.
//
// `db/schema.sql` seeds the table, and `MarketData.tsx` mirrors the browser's
// copy back to Postgres on every change — so whenever the table is EMPTY the
// browser refills it from `DEFAULT_WATCHLIST`. That is not hypothetical: after a
// full database reset on 2026-09-26 the schema seeded five crypto pairs and an
// open browser tab wrote NVDA and SPY back on top within seconds, leaving seven
// rows and no indication of where the extra two came from.
//
// Same arrangement, and the same hazard, as the ATR multipliers in
// `lib/riskManager.ts` and `backend/core/risk_manager.py`: two copies of one
// fact, kept in step deliberately, with a test that fails when they drift.
//
// THE EQUITY CHECK IS NOT STYLISTIC. This agent trades crypto perpetual futures
// through ccxt. It cannot open a position in NVDA or SPY, so listing an equity
// offers the operator a session symbol that every entry path then refuses —
// which reads as the agent being broken rather than as the instrument being
// impossible.
// ---------------------------------------------------------------------

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { DEFAULT_WATCHLIST } from './constants';

const schema = readFileSync(join(process.cwd(), 'db', 'schema.sql'), 'utf8');

/** The symbols `schema.sql` seeds into `watchlist`. */
function seededInSchema(): string[] {
  const block = schema.match(
    /INSERT INTO watchlist \(symbol, type\) VALUES([\s\S]*?)ON CONFLICT/i,
  );
  if (!block) return [];
  return [...block[1].matchAll(/'([^']+)'\s*,\s*'(?:crypto|equity)'/g)].map((m) => m[1]);
}

describe('the watchlist seeds', () => {
  it('schema.sql seeds the watchlist at all', () => {
    // It did not, once — and `watchlist` was then the ONE table where a reset was
    // a permanent loss, because every other config table (`memory_prefs`,
    // `paper_account`, `trading_controls`) carries a seed row and comes straight
    // back on the next `init_db`.
    expect(seededInSchema().length).toBeGreaterThan(0);
  });

  it('both sources list exactly the same symbols', () => {
    const fromCode = DEFAULT_WATCHLIST.map((w) => w.symbol).sort();
    expect(seededInSchema().sort()).toEqual(fromCode);
  });

  it('lists no equities — this agent cannot open one', () => {
    // `w.type` is widened to string deliberately. With every entry crypto, tsc
    // narrows the union to `'crypto'` and rejects the comparison outright —
    // which is a STRONGER guarantee than this assertion, not a reason to drop
    // it: the type check fails at build time if an equity is ever added back,
    // and this keeps the runtime check meaningful for the schema half below.
    const types = DEFAULT_WATCHLIST.map((w) => w.type as string);
    expect(types.filter((t) => t === 'equity')).toEqual([]);
    expect(schema).not.toMatch(/INSERT INTO watchlist[\s\S]*?'equity'[\s\S]*?ON CONFLICT/i);
  });

  it('keeps BTC, which is observed but NOT tradeable', () => {
    // `tradeable_universe` blocks BTC by default and `start_session` refuses a
    // session on it. It stays in the watchlist because it is the market's beta:
    // regime triggers are attributed to it, REGIME_WATCH polls it, and
    // `market_context` reads it as the benchmark for every other symbol's
    // relative strength. Dropping it as an OBSERVED symbol would blind every alt
    // decision — "what needs prices?" and "what may we open?" are different
    // questions.
    expect(DEFAULT_WATCHLIST.map((w) => w.symbol)).toContain('BTC/USDT');
  });

  it('gives every crypto pair the lowercase Binance stream symbol', () => {
    // `MarketData.tsx` subscribes with this; a missing one silently drops the
    // symbol from the live price feed while it still renders in the list.
    for (const w of DEFAULT_WATCHLIST) {
      expect(w.binance, `${w.symbol} has no binance stream symbol`).toBeTruthy();
      expect(w.binance).toBe(w.symbol.replace('/', '').toLowerCase());
    }
  });

  it('uses the display form, not the ccxt perpetual form', () => {
    // Every caller in this system says `SOL/USDT`; `Venue.resolve_symbol` maps it
    // to `SOL/USDT:USDT` at the order boundary. Storing the settle suffix here
    // would make the blocklist and the watchlist disagree about one instrument.
    for (const w of DEFAULT_WATCHLIST) expect(w.symbol).not.toContain(':');
  });
});
