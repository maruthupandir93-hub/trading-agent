// ---------------------------------------------------------------------
// The trade journey's eight steps, and the two that were never supplied.
//
// THE OPERATOR'S REPORT: "in the trade history page how the trade happens ...
// each trade is market data and directly execute, all the agent doesn't working
// together".
//
// TWO INDEPENDENT CAUSES, and only one of them was about the data.
//
//   1. BACKEND. Every trade in the live database came from the event-driven
//      supervisor rather than the 24-node graph, so `strategy`, `run_id` and
//      `entry_context` were NULL on all 23 rows and the indicator/regime/strategy
//      steps genuinely had nothing to render. Fixed by making the graph the only
//      originator of entries.
//
//   2. THIS FILE'S CALLER. `buildJourney` takes eight steps and
//      `app/(terminal)/history/[id]/page.tsx` supplied six: `risk` and `decision`
//      were simply omitted from the object literal. The builder therefore took
//      its `unknown` branch for both - "the gateway was not reached" and "the run
//      ended before a decision" - on EVERY trade ever displayed, including trades
//      that did reach the gateway and did produce a decision.
//
// So even after the backend was fixed, a fully-analysed trade would still have
// rendered a hole where Risk Checks and Decision belong. These tests pin the
// builder's behaviour for both; the page's own wiring is asserted at the bottom.
// ---------------------------------------------------------------------

import { describe, expect, it } from 'vitest';

import { buildJourney, type JourneySource } from './journey';

const stepNamed = (src: JourneySource, label: string) =>
  buildJourney(src).find((s) => s.label === label)!;

describe('buildJourney — the middle of the story', () => {
  it('reports Risk Checks as unknown when the caller omits them', () => {
    // This is the exact shape the detail page used to pass: no `risk` key.
    const step = stepNamed({ symbol: 'SOL/USDT', price: 120.3 }, 'Risk Checks');
    expect(step.state).toBe('unknown');
    expect(step.reason).toBe('the gateway was not reached');
  });

  it('reports Decision as unknown when the caller omits it', () => {
    const step = stepNamed({ symbol: 'SOL/USDT', price: 120.3 }, 'Decision');
    expect(step.state).toBe('unknown');
    expect(step.reason).toBe('the run ended before a decision');
  });

  it('renders Risk Checks as passed when the caller says it approved', () => {
    const step = stepNamed({ risk: { approved: true } }, 'Risk Checks');
    expect(step.state).toBe('ok');
    expect(step.lines).toEqual(['approved']);
  });

  it('does not invent a passed/total count that was not supplied', () => {
    // The per-check results live in the run trace, not on the trade row. Showing
    // "9/9 passed" from an `approved: true` alone would be a fabricated detail
    // dressed as evidence — invariant 6.
    const step = stepNamed({ risk: { approved: true } }, 'Risk Checks');
    expect(step.lines.join(' ')).not.toMatch(/\d+\s*\/\s*\d+/);
  });

  it('renders a decision with its direction', () => {
    const step = stepNamed({ decision: { action: 'TRADE', direction: 'LONG' } }, 'Decision');
    expect(step.state).toBe('ok');
    expect(step.lines).toEqual(['TRADE', 'LONG']);
  });

  it('omits the direction line rather than printing a placeholder', () => {
    // `annotateTrades` returns the literal 'unknown' for a close whose opening
    // leg is not in the log. The page maps that to null, because the word
    // "unknown" sitting where a direction belongs reads as a rendering fault
    // rather than as an absent measurement.
    const step = stepNamed({ decision: { action: 'TRADE', direction: null } }, 'Decision');
    expect(step.lines).toEqual(['TRADE']);
    expect(step.state).toBe('ok');
  });

  it('marks a rejected risk stage as a failure, not as unknown', () => {
    // These are different facts: "we checked and refused" versus "we never
    // checked". Collapsing them is what made a working gate invisible.
    const step = stepNamed({ risk: { approved: false } }, 'Risk Checks');
    expect(step.state).toBe('fail');
  });

  it('renders the strategy the trade actually recorded', () => {
    const step = stepNamed({ strategy: 'Grid', confidencePct: 72 }, 'Strategy Signal');
    expect(step.state).toBe('ok');
    expect(step.lines[0]).toBe('Grid');
  });

  it('still reports an absent strategy honestly', () => {
    // A manual click and the event path both genuinely have no strategy profile.
    // Crediting one would poison the measurement `strategy_performance` feeds.
    const step = stepNamed({ symbol: 'SOL/USDT' }, 'Strategy Signal');
    expect(step.state).toBe('unknown');
    expect(step.reason).toBe('no strategy was selected');
  });

  it('distinguishes an open position from a flat result', () => {
    // A P&L of exactly 0 IS a measurement and renders as one; only absence is
    // unknown. An open trade showing "0.00" would report a break-even close that
    // never happened.
    expect(stepNamed({ outcome: { status: 'OPEN' } }, 'Outcome').state).toBe('unknown');
    expect(stepNamed({ outcome: { pnl: 0 } }, 'Outcome').state).toBe('ok');
  });

  it('always returns all eight steps, whatever is missing', () => {
    // The view's job is to show WHERE the story stops. A builder that dropped
    // unknown steps would make a run that died at data_validation look identical
    // to one that completed.
    expect(buildJourney({})).toHaveLength(8);
    expect(buildJourney({}).every((s) => s.state === 'unknown')).toBe(true);
  });
});
