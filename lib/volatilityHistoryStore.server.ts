// ---------------------------------------------------------------------
// Volatility history — ONE JSON FILE, CAPPED AT 15 ENTRIES.
//
// WHY THIS IS NOT IN POSTGRES, UNLIKE EVERY OTHER STORE HERE
//
// The rest of `lib/*Store.server.ts` is Postgres-first with a JSON fallback.
// This one is JSON only, and that is a deliberate exception rather than an
// unfinished migration — so nobody "completes" it later and reintroduces the
// problem it exists to avoid.
//
// The volatility node runs on every analysis cycle AND on every monitoring tick
// per open position. That is thousands of readings a day, of which only the few
// attached to an actual trade carry a lesson. Writing each one to the operator's
// database is unbounded growth for a table nothing queries. The backend holds a
// small in-memory ring (`backend/services/volatility_journal.py`) which is
// cleared on restart; THIS FILE is the durable record, and it is bounded by
// keeping only the most recent MAX_ENTRIES readings.
//
// THE CAP IS ENFORCED ON WRITE, NOT ON READ
//
// `lib/pvHistoryStore.server.ts` does the opposite and says why: an equity curve
// truncated at the front loses exactly the points a drawdown-from-peak needs. The
// reasoning does not carry over. A volatility reading is a point-in-time
// observation that is not accumulated into anything, so the oldest one genuinely
// is the least useful, and the whole reason this store exists is to bound what is
// kept. Trimming on read would leave the file growing forever.
//
// IDENTITY IS `id`, AND RE-RECORDING ONE UPDATES IT IN PLACE
//
// The backend ring is polled, so the same reading is offered repeatedly until it
// falls out of the ring. Appending on every poll would fill all 15 slots with one
// reading within seconds and evict genuine history. `id` is `<runId>:<symbol>`,
// which is unique per reading and stable across polls.
// ---------------------------------------------------------------------

import { readJson, serialize, writeJson } from './jsonFallback.server';

const FILE = 'volatility-history.json';

/**
 * How many readings are kept. The operator asked for 15 explicitly; the oldest
 * is dropped when a 16th arrives.
 */
export const MAX_ENTRIES = 15;

export type VolatilityRegime = 'VERY_LOW' | 'LOW' | 'NORMAL' | 'HIGH' | 'EXTREME';

export type VolatilityHistoryEntry = {
  /** `<runId>:<symbol>` — unique per reading, stable across repeated polls. */
  id: string;
  runId: string | null;
  symbol: string;
  timeframe: string | null;
  /** ms since epoch. */
  ts: number;

  /** null means UNKNOWN — never "calm". See backend/algorithms/volatility.py. */
  regime: VolatilityRegime | null;
  /** 'percentile' (comparable across instruments) or 'absolute' (thin-history fallback, not comparable). */
  basis: 'percentile' | 'absolute' | null;

  atr: number | null;
  atrPercent: number | null;
  realizedVolatility: number | null;
  bollingerWidth: number | null;
  candleRangePercent: number | null;
  /** Where this ATR% sits in the instrument's own recent distribution, 0-100. */
  percentile: number | null;

  volatilityShock: boolean;
  expansionRatio: number | null;

  tradingAllowed: boolean;
  riskMultiplier: number | null;
  maxLeverage: number | null;
  stopAtrMultiple: number | null;

  /** Set when this reading is tied to a trade, so the file doubles as per-trade context. */
  tradeId?: string | null;
};

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const num = (v: unknown): number | null => (isNum(v) ? v : null);

/**
 * Normalise one backend reading into an entry.
 *
 * Every numeric goes through `num`, which yields null rather than 0 for a
 * missing measurement. A `riskMultiplier` of 0 and an unmeasured one look
 * identical once coerced, and they mean opposite things: 0 is "this market is
 * blocked", null is "we do not know".
 */
export function toEntry(raw: Record<string, unknown>): VolatilityHistoryEntry | null {
  const symbol = typeof raw.symbol === 'string' ? raw.symbol : null;
  if (!symbol) return null;

  const runId = typeof raw.runId === 'string' ? raw.runId : null;
  const id = typeof raw.id === 'string' && raw.id.length > 0 ? raw.id : `${runId ?? 'unknown'}:${symbol}`;

  // The backend stamps seconds (time.time()); everything the frontend renders is
  // milliseconds. Converted here, at the boundary, rather than at each render —
  // a reading a thousand times too old sorts to the bottom and silently never
  // appears, which reads as "the agent stopped measuring volatility".
  const rawTs = num(raw.ts);
  const ts = rawTs === null ? Date.now() : rawTs < 1e11 ? rawTs * 1000 : rawTs;

  const regime = typeof raw.regime === 'string' ? (raw.regime as VolatilityRegime) : null;
  const basis = raw.basis === 'percentile' || raw.basis === 'absolute' ? raw.basis : null;

  return {
    id,
    runId,
    symbol,
    timeframe: typeof raw.timeframe === 'string' ? raw.timeframe : null,
    ts,
    regime,
    basis,
    atr: num(raw.atr),
    atrPercent: num(raw.atr_percent ?? raw.atrPercent),
    realizedVolatility: num(raw.realized_volatility ?? raw.realizedVolatility),
    bollingerWidth: num(raw.bollinger_width ?? raw.bollingerWidth),
    candleRangePercent: num(raw.candle_range_percent ?? raw.candleRangePercent),
    percentile: num(raw.percentile),
    volatilityShock: raw.volatility_shock === true || raw.volatilityShock === true,
    expansionRatio: num(raw.expansion_ratio ?? raw.expansionRatio),
    // Defaults to FALSE when the field is absent. An unparseable reading must not
    // be read as permission to trade — the engine's own rule is that unknown
    // volatility blocks, and that has to survive the trip through this boundary.
    tradingAllowed: raw.trading_allowed === true || raw.tradingAllowed === true,
    riskMultiplier: num(raw.risk_multiplier ?? raw.riskMultiplier),
    maxLeverage: num(raw.max_leverage ?? raw.maxLeverage),
    stopAtrMultiple: num(raw.stop_atr_multiple ?? raw.stopAtrMultiple),
    tradeId: typeof raw.tradeId === 'string' ? raw.tradeId : null,
  };
}

/**
 * Fold new readings into an existing list, newest first, capped.
 *
 * Pure and exported so the cap and the de-duplication are unit-testable without
 * touching the filesystem — the eviction rule is the whole point of this store
 * and a bug in it is invisible until history has silently been lost.
 */
export function mergeCapped(
  existing: VolatilityHistoryEntry[],
  incoming: VolatilityHistoryEntry[],
  max: number = MAX_ENTRIES,
): VolatilityHistoryEntry[] {
  const byId = new Map<string, VolatilityHistoryEntry>();
  // Existing first, then incoming, so a re-polled reading OVERWRITES rather than
  // duplicating. A later poll of the same run carries the same numbers, but if it
  // ever carried more (a tradeId attached after the fact) the newer wins.
  for (const e of existing) byId.set(e.id, e);
  for (const e of incoming) {
    const prior = byId.get(e.id);
    byId.set(e.id, prior ? { ...prior, ...e } : e);
  }

  return Array.from(byId.values())
    .sort((a, b) => b.ts - a.ts)
    .slice(0, max);
}

export async function listVolatilityHistory(): Promise<VolatilityHistoryEntry[]> {
  return readJson<VolatilityHistoryEntry[]>(FILE, []);
}

/**
 * Record readings, evicting the oldest beyond MAX_ENTRIES. Returns what is now
 * stored.
 *
 * Read-modify-write, so it goes through `serialize` on the same key every other
 * store uses for this: two concurrent polls would otherwise both read 15 entries,
 * each append their own, and the second write would discard the first's.
 */
export async function recordVolatilityReadings(
  entries: VolatilityHistoryEntry[],
): Promise<VolatilityHistoryEntry[]> {
  if (entries.length === 0) return listVolatilityHistory();

  return serialize(FILE, async () => {
    const existing = await readJson<VolatilityHistoryEntry[]>(FILE, []);
    const merged = mergeCapped(existing, entries);
    await writeJson(FILE, merged);
    return merged;
  });
}

/**
 * Attach a trade id to the reading a trade was taken on, so the file answers
 * "what was volatility doing when we opened this?".
 *
 * A no-op when the reading has already been evicted. That is honest and expected
 * — with 15 slots a trade opened long enough ago genuinely has no reading left,
 * and inventing one would be fabricating market data.
 */
export async function attachTradeId(readingId: string, tradeId: string): Promise<boolean> {
  return serialize(FILE, async () => {
    const existing = await readJson<VolatilityHistoryEntry[]>(FILE, []);
    const found = existing.find((e) => e.id === readingId);
    if (!found) return false;
    found.tradeId = tradeId;
    await writeJson(FILE, existing);
    return true;
  });
}
