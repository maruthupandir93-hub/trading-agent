import { ping } from '@/lib/db.server';
import { listTrades } from '@/lib/tradeStore.server';
import { proxyToBackend } from '@/lib/api/backendProxy.server';

// ---------------------------------------------------------------------
// Real, server-side active health checks — distinct from
// SystemHealthPanel's client-side rollup of state it already has cached
// (candle presence, MCP reachability last time someone checked). Those
// only reflect what the browser already knows; this route independently
// exercises the two things this app's own server process actually
// depends on, so a genuine outage shows up even if the client's cached
// state still looks fine:
//   - Postgres: a real connect-and-query, so a bad DATABASE_URL or an
//     unreachable host is visible HERE rather than as an empty table on some
//     page. It reports which store the trade read came from too, because a
//     silent fall back to the JSON file means the dashboard and the agent are
//     reading different books.
//   - the trade store (Postgres, JSON fallback — see lib/tradeStore.server.ts):
//     a real read round-trip, not just "the file exists"
//   - Binance's public REST API — the spine of this app's real market
//     data (candles, order flow, funding/OI) — via its dedicated /ping
//     endpoint (near-zero cost, meant exactly for this)
// Nothing here is a synthetic uptime probe against THIS app's own
// process (that would need an external pinger, out of scope for a
// route the app calls on itself) — it's checking the two real external
// dependencies this app can't function without.
// ---------------------------------------------------------------------

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type HealthCheckResult = { label: string; ok: boolean; detail: string; latencyMs: number };

async function checkDatabase(): Promise<HealthCheckResult> {
  const started = Date.now();
  const result = await ping();
  const latencyMs = Date.now() - started;
  if (result.ok) {
    return {
      label: 'Postgres',
      ok: true,
      detail: `server ${result.serverVersion}, ${result.tables} table(s)`,
      latencyMs,
    };
  }
  // NOT ok. A missing DATABASE_URL is a configuration state rather than an
  // outage, but it still means every store is serving the JSON fallback, so it
  // must not read as healthy — that is how two books get read as one.
  return { label: 'Postgres', ok: false, detail: result.reason, latencyMs };
}

async function checkTradeStore(): Promise<HealthCheckResult> {
  const started = Date.now();
  try {
    const trades = await listTrades();
    return {
      label: 'Trade store',
      ok: true,
      detail: `read ${trades.length} trade${trades.length === 1 ? '' : 's'}`,
      latencyMs: Date.now() - started,
    };
  } catch (err) {
    return { label: 'Trade store', ok: false, detail: err instanceof Error ? err.message : 'read failed', latencyMs: Date.now() - started };
  }
}

// Asks the BACKEND what it can reach, instead of probing Binance from here.
//
// THIS CHECK USED TO LIE ON VERCEL, AND IT LIED IN THE MOST EXPENSIVE DIRECTION.
//
// It pinged api.binance.com from this handler. On Vercel that runs in a
// Vercel-chosen region, so what it measured was "can this particular serverless
// invocation reach Binance" — which is no longer how any market data is fetched.
// A green tick here meant nothing about whether the dashboard's data would load,
// and a red one sent the operator to check an exchange that was fine.
//
// The question worth answering now is whether the BACKEND's region is served,
// because that is the machine making the calls. `/api/marketdata/upstream-health`
// answers exactly that and names a geo-block explicitly when it sees one.
async function checkUpstreams(): Promise<HealthCheckResult[]> {
  const started = Date.now();
  try {
    const res = await proxyToBackend('/api/marketdata/upstream-health');
    const latencyMs = Date.now() - started;
    const json = await res.json();

    if (!res.ok) {
      return [{ label: 'Trading backend', ok: false, detail: json?.error ?? `HTTP ${res.status}`, latencyMs }];
    }

    // The backend is reachable — that is itself a check worth reporting, and it
    // is the one that distinguishes "Vercel cannot reach Oracle" from "Oracle
    // cannot reach Binance". Those are different outages with different fixes
    // and they used to be indistinguishable from this page.
    const checks: HealthCheckResult[] = [
      { label: 'Trading backend', ok: true, detail: 'reachable from Vercel', latencyMs },
    ];

    const upstreams: Record<string, { reachable: boolean; status: number | null; geoBlocked: boolean; error: string | null }> =
      json?.upstreams ?? {};

    for (const [name, state] of Object.entries(upstreams)) {
      checks.push({
        label: `${name} (from backend)`,
        ok: state.reachable,
        detail: state.reachable
          ? `reachable${state.status ? ` (HTTP ${state.status})` : ''}`
          : state.geoBlocked
            ? `REGION BLOCKED (HTTP ${state.status}) — the backend host's location is refused by this provider`
            : (state.error ?? 'unreachable'),
        latencyMs,
      });
    }
    return checks;
  } catch (err) {
    return [{
      label: 'Trading backend',
      ok: false,
      detail: err instanceof Error ? err.message : 'unreachable',
      latencyMs: Date.now() - started,
    }];
  }
}

export async function GET() {
  const [database, tradeStore, upstreams] = await Promise.all([
    checkDatabase(),
    checkTradeStore(),
    checkUpstreams(),
  ]);
  const checks = [database, tradeStore, ...upstreams];
  const failing = checks.filter((c) => !c.ok).length;
  const overall = failing === 0 ? 'healthy' : failing === checks.length ? 'unhealthy' : 'degraded';
  return Response.json({ overall, checks, checkedAt: Date.now() });
}
