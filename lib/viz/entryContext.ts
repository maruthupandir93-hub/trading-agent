// ---------------------------------------------------------------------
// Parse the entry-context snapshot a trade carries into the journey's shape.
//
// WHY A SNAPSHOT STRING RATHER THAN A JOIN
//
// "How this trade happened" could only ever show the entry and the outcome. The
// middle — indicators, regime, strategy — was not lost, it was NEVER RECORDED:
// `trades.entry_context` existed and nothing wrote it, and the graph state that
// held those values is gone by the time a fill is booked.
//
// The Risk Gateway now writes a compact snapshot at decision time, because it is
// the last node holding `technical_analysis`, `market_regime` and `volatility`
// together. A join against the run trace would not have worked: the trace records
// which node ran and what state KEYS it wrote, not the values.
//
// The format is the one `learningDashboard.classifyEntryContext` already parses,
// so there is one shape of snapshot in the system rather than two.
//
// PARSES LENIENTLY, REPORTS ABSENCE. A field that is not in the string comes back
// null, and the journey renders its own "not recorded" step. Guessing a plausible
// RSI would be the most persuasive fabrication available here, because it would
// look exactly like evidence.
// ---------------------------------------------------------------------

export type ParsedEntryContext = {
  rsi: number | null;
  atr: number | null;
  trend: string | null;
  regime: string | null;
  volatility: string | null;
  strategy: string | null;
};

const EMPTY: ParsedEntryContext = {
  rsi: null,
  atr: null,
  trend: null,
  regime: null,
  volatility: null,
  strategy: null,
};

function num(match: RegExpMatchArray | null): number | null {
  if (!match) return null;
  const v = Number.parseFloat(match[1]);
  return Number.isFinite(v) ? v : null;
}

/**
 * Pull the recorded values out of a snapshot string.
 *
 * Example input:
 *   "SOL/USDT @ 15m: RSI(14)=36.4, ATR(14)=0.462, structure trend=Bearish,
 *    regime=Range, volatility=LOW (33th pct), strategy=MeanReversion"
 */
export function parseEntryContext(context: string | null | undefined): ParsedEntryContext {
  if (!context) return EMPTY;

  return {
    rsi: num(context.match(/RSI\(\d+\)=([\d.]+)/)),
    atr: num(context.match(/ATR\(\d+\)=([\d.]+)/)),
    trend: context.match(/structure trend=([A-Za-z]+)/)?.[1] ?? null,
    regime: context.match(/regime=([A-Za-z ]+?)(?:,|$)/)?.[1]?.trim() ?? null,
    // The percentile in brackets is deliberately kept with the label — "LOW
    // (33th pct)" says more than "LOW", and the whole point of the percentile
    // basis is that it is comparable where a bare label is not.
    volatility: context.match(/volatility=([A-Z_]+(?: \(\d+th pct\))?)/)?.[1] ?? null,
    strategy: context.match(/strategy=([A-Za-z]+)/)?.[1] ?? null,
  };
}
