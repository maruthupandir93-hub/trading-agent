// Parsing the entry-context snapshot behind "How this trade happened".
//
// That view could only ever show market data and execution, with an unknown
// middle. The reason was not a display bug: `trades.entry_context` existed and
// NOTHING WROTE IT, and the graph state holding the indicators is gone by the
// time a fill is booked. The Risk Gateway now records a snapshot at decision
// time; this parses it back.
//
// The important property is that a MISSING field comes back null rather than
// being guessed. A plausible invented RSI would be the most persuasive
// fabrication available here, because it would look exactly like evidence.

import { describe, expect, it } from 'vitest';
import { parseEntryContext } from './viz/entryContext';

const FULL =
  'SOL/USDT @ 15m: RSI(14)=36.4, ATR(14)=0.462, structure trend=Bearish, ' +
  'regime=Range, volatility=LOW (33th pct), strategy=MeanReversion';

describe('parseEntryContext', () => {
  it('pulls every recorded field out of a full snapshot', () => {
    const c = parseEntryContext(FULL);
    expect(c.rsi).toBeCloseTo(36.4);
    expect(c.atr).toBeCloseTo(0.462);
    expect(c.trend).toBe('Bearish');
    expect(c.regime).toBe('Range');
    expect(c.strategy).toBe('MeanReversion');
  });

  it('keeps the percentile with the volatility label', () => {
    // "LOW (33th pct)" says more than "LOW": the whole point of the percentile
    // basis is that it is comparable across instruments where a bare label is not.
    expect(parseEntryContext(FULL).volatility).toBe('LOW (33th pct)');
  });

  it('returns nulls for a trade that has no snapshot', () => {
    // Every trade taken before the gateway recorded one. The page says so in
    // words rather than rendering an empty journey that looks like a failure.
    const c = parseEntryContext(null);
    expect(c).toEqual({
      rsi: null, atr: null, trend: null, regime: null, volatility: null, strategy: null,
    });
  });

  it('returns null for a field the snapshot omits, never a guess', () => {
    const partial = 'BTC/USDT @ 15m: ATR(14)=250.5, strategy=Trend';
    const c = parseEntryContext(partial);
    expect(c.atr).toBeCloseTo(250.5);
    expect(c.strategy).toBe('Trend');
    expect(c.rsi).toBeNull();
    expect(c.regime).toBeNull();
    expect(c.trend).toBeNull();
  });

  it('does not swallow a following field into a multi-word regime', () => {
    // `regime=Trending Bullish, volatility=HIGH` — the regime is two words and
    // the comma is the boundary. A greedy match would take the volatility too.
    const c = parseEntryContext(
      'ETH/USDT @ 15m: regime=Trending Bullish, volatility=HIGH (91th pct), strategy=Trend',
    );
    expect(c.regime).toBe('Trending Bullish');
    expect(c.volatility).toBe('HIGH (91th pct)');
  });

  it('survives a malformed number rather than reporting NaN', () => {
    const c = parseEntryContext('X/USDT @ 15m: RSI(14)=abc, ATR(14)=1.5');
    expect(c.rsi).toBeNull();
    expect(c.atr).toBeCloseTo(1.5);
  });
});
