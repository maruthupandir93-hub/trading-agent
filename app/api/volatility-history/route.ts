// ---------------------------------------------------------------------
// Volatility history — the frontend's own capped record.
//
// GET  collects whatever the backend's in-memory ring is holding, folds it into
//      `.data/volatility-history.json` (15 entries, oldest evicted), and returns
//      the stored list.
// POST records readings supplied directly, or attaches a trade id to one.
//
// WHY THE COLLECTION HAPPENS ON READ
//
// The alternative was a push: the backend calling into Next whenever the
// volatility node runs. Nothing else in this system points that direction —
// every hop is browser -> Next -> FastAPI — and reversing it for one telemetry
// feed would mean the backend needs the frontend's address, retries, and a
// failure mode where a graph run is slowed by an unreachable web app.
//
// Collecting on read costs nothing when nobody is looking and cannot affect a
// trading decision. The trade-off, stated plainly: readings that fall out of the
// backend's ring before anyone loads the page are lost. With a 200-entry ring and
// a page that polls, that window is large; it is not a guarantee, and this route
// does not pretend otherwise.
// ---------------------------------------------------------------------

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

import { backendOrigin } from '@/lib/api/backendProxy.server';
import {
  MAX_ENTRIES,
  attachTradeId,
  listVolatilityHistory,
  recordVolatilityReadings,
  toEntry,
  type VolatilityHistoryEntry,
} from '@/lib/volatilityHistoryStore.server';

const COLLECT_TIMEOUT_MS = 5_000;

/**
 * Pull the backend ring. Returns null — not [] — when the backend could not be
 * reached, so the caller can say "we could not collect" rather than reporting an
 * empty market.
 */
async function collectFromBackend(): Promise<{ entries: VolatilityHistoryEntry[] } | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), COLLECT_TIMEOUT_MS);
  try {
    const key = process.env.TRADES_API_KEY;
    const res = await fetch(`${backendOrigin()}/api/graphs/volatility?limit=${MAX_ENTRIES}`, {
      headers: { Accept: 'application/json', ...(key ? { Authorization: `Bearer ${key}` } : {}) },
      signal: controller.signal,
      cache: 'no-store',
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { readings?: Record<string, unknown>[] };
    const entries = (body.readings ?? [])
      .map(toEntry)
      .filter((e): e is VolatilityHistoryEntry => e !== null);
    return { entries };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function GET() {
  const collected = await collectFromBackend();

  const history =
    collected === null
      ? await listVolatilityHistory()
      : await recordVolatilityReadings(collected.entries);

  return Response.json({
    history,
    count: history.length,
    maxEntries: MAX_ENTRIES,
    // Distinguishes "the agent has not measured anything" from "we could not ask
    // it". Both render as an empty list otherwise, and they have different fixes.
    collected: collected === null ? null : collected.entries.length,
    source: 'file:.data/volatility-history.json',
    retention: `the ${MAX_ENTRIES} most recent readings; older ones are deleted as new ones arrive`,
  });
}

export async function POST(req: Request) {
  let body: { readings?: Record<string, unknown>[]; readingId?: string; tradeId?: string };
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  // Attaching a trade id to an existing reading.
  if (body.readingId && body.tradeId) {
    const attached = await attachTradeId(body.readingId, body.tradeId);
    return Response.json({
      attached,
      // `false` is not an error: with 15 slots the reading may legitimately have
      // been evicted before the trade closed.
      reason: attached ? null : 'that reading is no longer in the capped history',
    });
  }

  if (!Array.isArray(body.readings)) {
    return Response.json({ error: 'Expected { readings: [...] } or { readingId, tradeId }' }, { status: 400 });
  }

  const entries = body.readings.map(toEntry).filter((e): e is VolatilityHistoryEntry => e !== null);
  const history = await recordVolatilityReadings(entries);
  return Response.json({
    history,
    count: history.length,
    recorded: entries.length,
    skipped: body.readings.length - entries.length,
    maxEntries: MAX_ENTRIES,
  });
}
