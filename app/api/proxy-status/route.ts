/**
 * Can the DASHBOARD's own half of the write path work?
 *
 * WHY THIS EXISTS (#23). There are two independent gates between a button and
 * the backend, and `writeBlocker` could only ever see one of them:
 *
 *   TRADES_API_KEY      on the BACKEND. `GET /api/admin/status` reports it as
 *                       `auth.writesPossible`, which is what the banner read.
 *   DASHBOARD_PASSWORD  on the FRONTEND. `app/api/backend/[...path]/route.ts`
 *                       refuses every write with 401 when it is unset, and
 *                       NOTHING reported that.
 *
 * So the configuration that actually happens on a first deploy — the backend
 * key set, the dashboard password not — rendered NO banner at all while every
 * control returned 401. And when both were missing the banner named only
 * TRADES_API_KEY, so an operator who fixed it saw no change whatsoever. That
 * is the "two gates, and fixing one looks identical to fixing neither" failure
 * CLAUDE.md already records, reached through the UI that was built to prevent
 * it.
 *
 * IT REPORTS BOOLEANS AND NOTHING ELSE. No value, no length, no prefix — the
 * presence of a secret is the fact the UI needs, and the secret itself is not.
 * That is also why it needs no auth: there is nothing here an unauthenticated
 * caller learns that the 401 they already receive does not tell them.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

function openWritesAllowed(): boolean {
  // The same four spellings the backend and the proxy both parse. A security
  // flag meaning "on" to one half and "off" to another is the hole nobody can
  // see from either side.
  const raw = (process.env.ALLOW_UNAUTHENTICATED_WRITES ?? '').trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on';
}

export async function GET() {
  const dashboardPasswordSet = Boolean(process.env.DASHBOARD_PASSWORD);
  const backendKeySet = Boolean(process.env.TRADES_API_KEY);
  const openWrites = openWritesAllowed();

  return Response.json({
    dashboardPasswordSet,
    // The proxy ATTACHES this; the backend CHECKS it. Set on one side only and
    // the 503 does not change, which is why both halves are reported.
    proxyHasBackendKey: backendKeySet,
    openWritesAllowed: openWrites,
    // What the proxy will actually do with a write right now.
    proxyWritesPossible: dashboardPasswordSet || openWrites,
  });
}
