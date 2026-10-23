/**
 * Batch 6, the frontend half.
 *
 * Four refusals that were wrong in both directions: a guard that refused a
 * government website because its name starts with "fc", a reachability check
 * that would chase a redirect into the metadata service, a proxy whose comment
 * claimed it could not be walked out of, and a banner that named one of two
 * gates. Plus the close that booked a realised P&L at a price nobody measured.
 */

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { checkOutboundUrl } from './ssrfGuard';
import { writeBlocker } from './writeBlocker';

const read = (path: string) => readFileSync(path, 'utf8');

// ---------------------------------------------------------------------------
// #26 — the address guard refused real hosts
// ---------------------------------------------------------------------------

describe('checkOutboundUrl', () => {
  it('does not mistake a hostname beginning "fc" for a unique-local IPv6 range', () => {
    // `isPrivateIPv6` tested `h.startsWith('fc')` on a bare hostname. An IPv6
    // literal always contains a colon; a DNS name need not. So fcc.gov, and
    // every other host whose first label starts with those two letters, was
    // refused with a message about probing our own network.
    for (const host of ['fcc.gov', 'fdic.gov', 'fc-barcelona.com', 'fdn.example.org']) {
      const out = checkOutboundUrl(`https://${host}/x`);
      expect(out.ok, `${host} was refused`).toBe(true);
    }
  });

  it('still refuses every address the guard exists for', () => {
    const blocked = [
      'http://127.0.0.1:8000/api',
      'http://169.254.169.254/latest/meta-data/',
      'http://10.0.0.5/',
      'http://192.168.1.1/',
      'http://localhost:3000/',
      'http://[::1]/',
      'http://[::ffff:127.0.0.1]/',
      'http://[fd00::1]/',
      'http://[fe80::1]/',
      'file:///etc/passwd',
    ];
    for (const url of blocked) {
      expect(checkOutboundUrl(url).ok, `${url} was allowed`).toBe(false);
    }
  });
});

describe('/api/mcp-status', () => {
  it('does not follow redirects', () => {
    // `checkOutboundUrl` vets the address the CALLER named. `fetch` follows up
    // to twenty redirects and vets none of them, so a public host an attacker
    // controls could answer 302 to the metadata service and this route would
    // report its status and latency — the exact probe the guard refuses,
    // reached by asking politely.
    const src = read('app/api/mcp-status/route.ts');
    expect(src).toContain("redirect: 'manual'");
    expect(src).toContain('redirectedTo');
  });
});

// ---------------------------------------------------------------------------
// #25 — the proxy could walk out of /api
// ---------------------------------------------------------------------------

describe('the backend proxy path', () => {
  const src = read('app/api/backend/[...path]/route.ts');

  it('refuses a traversal segment instead of trying to encode it', () => {
    // `encodeURIComponent` does NOT encode a dot — `.` and `..` are unreserved
    // and come back unchanged — so `/api/backend/%2e%2e/openapi.json` was
    // rebuilt as `<backend>/api/../openapi.json` and normalised by `new URL()`
    // to `<backend>/openapi.json`. The comment above it claimed a caller
    // "cannot walk out of /api with `..`".
    expect(src).toContain("segment === '..'");
    expect(src).toContain('may not traverse directories');
  });

  it('verifies the resolved path is still inside the prefix', () => {
    // The segment check is a blocklist; this is the property that matters, so
    // a future encoding quirk stops here rather than at the backend.
    expect(src).toContain("target.pathname.startsWith('/api/')");
  });
});

// ---------------------------------------------------------------------------
// #23 — one banner for two gates
// ---------------------------------------------------------------------------

describe('writeBlocker with both gates', () => {
  it('names DASHBOARD_PASSWORD when only the frontend is unconfigured', () => {
    // The configuration that actually happens on a first deploy: the backend
    // key set, the dashboard password not. The banner showed NOTHING while
    // every control returned 401 from the proxy.
    const out = writeBlocker(
      { writeAuthEnabled: true, writesPossible: true },
      { proxyWritesPossible: false, dashboardPasswordSet: false },
    );
    expect(out).not.toBeNull();
    expect(out!.title).toContain('DASHBOARD_PASSWORD');
    expect(out!.where).toBe('frontend');
  });

  it('names BOTH when both are unconfigured, because fixing one changes nothing', () => {
    const out = writeBlocker(
      { writeAuthEnabled: false, writesPossible: false },
      { proxyWritesPossible: false },
    );
    expect(out!.where).toBe('both');
    expect(out!.steps.join(' ')).toContain('TRADES_API_KEY');
    expect(out!.steps.join(' ')).toContain('DASHBOARD_PASSWORD');
  });

  it('still names only the backend key when the proxy is fine', () => {
    const out = writeBlocker(
      { writeAuthEnabled: false, writesPossible: false },
      { proxyWritesPossible: true },
    );
    expect(out!.title).toContain('TRADES_API_KEY');
  });

  it('says nothing when the proxy status has not arrived', () => {
    // `undefined` is "not asked yet", not "fine" — the same rule
    // `writesPossible` already follows, and the reason the old call site could
    // keep working unchanged.
    expect(writeBlocker({ writeAuthEnabled: true, writesPossible: true }, null)).toBeNull();
    expect(writeBlocker({ writeAuthEnabled: true, writesPossible: true })).toBeNull();
  });

  it('reports booleans only, never a secret', () => {
    const src = read('app/api/proxy-status/route.ts');
    // The presence of a secret is the fact the UI needs; the secret is not.
    expect(src).toContain('Boolean(process.env.DASHBOARD_PASSWORD)');
    expect(src).not.toMatch(/process\.env\.DASHBOARD_PASSWORD\s*[,}]/);
  });
});

// ---------------------------------------------------------------------------
// #19 / #24 — fabricated prices, and refusals that said nothing
// ---------------------------------------------------------------------------

describe('a close that has no exchange behind it', () => {
  it('is deferred rather than booked at a simulated price', () => {
    // The F25 guard exempted every close under invariant 4, reasoning that
    // "the close books at whatever the executor fills at". With no connected
    // exchange there IS no executor: `sellPaper(symbol, qty, params.price)`
    // books the realised P&L at exactly that number, so a `sim-fallback` price
    // wrote a fabricated exit and P&L into the ledger that feeds the win rate.
    //
    // Invariant 4 protects the ability to EXIT A POSITION. A row in the
    // browser's ledger is a RECORD of an exit, not the exit, and nothing is
    // trapped by declining to write one at a number nobody measured.
    const src = read('components/Supervisor.tsx');
    expect(src).toContain('routesToVenue');
    expect(src).toContain("params.side === 'buy' || !routesToVenue");
  });
});

describe('controls that fail quietly', () => {
  it('pause reads res.ok and has somewhere to put a refusal', () => {
    // `await fetch(...)` with no status check: a 401 or a 503 resolved
    // normally, the reload showed the unchanged state, and the button simply
    // appeared to do nothing. Pause is a safety control; "it did nothing and
    // said nothing" is the worst outcome for one.
    const src = read('components/shell/TopBar.tsx');
    expect(src).toContain('if (!res.ok)');
    expect(src).toContain('pauseError');
  });

  it('the portfolio mirror reports a refused write', () => {
    // `.catch(() => {})` catches a thrown fetch and nothing else, so the one
    // case that happens on a deployment — the write being refused — was
    // discarded in silence while the comment claimed it was logged. The server
    // copy is what a cleared browser store or a second device loads from.
    const src = read('components/Portfolio.tsx');
    // The PUT specifically — the GET a few lines above is a read and has its
    // own (correct) handling of a null body.
    const block = src.slice(src.indexOf('Mirror to the server'));
    expect(block.slice(0, 1400)).toContain('res.ok');
    expect(block.slice(0, 1400)).toContain('NOT backed up');
  });
});

// ---------------------------------------------------------------------------
// Issue 4 — exchange secrets over a plain-HTTP hop
// ---------------------------------------------------------------------------

describe('credentials crossing the backend hop', () => {
  const src = read('app/api/backend/[...path]/route.ts');

  it('refuses a credential-bearing body over plain HTTP', () => {
    // The file's own header says this hop being server-to-server "is what
    // makes plain http OK". That is true of MARKET DATA and false of
    // CREDENTIALS: Vercel to the Oracle box is the public internet, and
    // nothing about a request being made by a server encrypts it. `TradePanel`
    // posts the operator's apiKey and apiSecret in the body of
    // `/api/operator/exchange/order`, and those are trading keys on a real
    // account.
    expect(src).toContain('CREDENTIAL_KEYS');
    expect(src).toContain('apiSecret');
    expect(src).toContain('421');
  });

  it('does not refuse a loopback backend, where no network is crossed', () => {
    // Refusing there would break local development for a risk that does not
    // exist.
    expect(src).toMatch(/127\\\.0\\\.0\\\.1|localhost/);
  });

  it('names the opt-out for what it permits', () => {
    // So switching it on is a decision rather than an accident — the same rule
    // ALLOW_UNAUTHENTICATED_WRITES follows.
    expect(src).toContain('ALLOW_PLAINTEXT_EXCHANGE_SECRETS');
  });

  it('leaves every other route alone', () => {
    // Only credential-bearing bodies are refused; flattening this into "no
    // writes over http" would break the whole dashboard for a risk confined to
    // two routes.
    expect(src).toContain('only credential-bearing bodies are refused');
  });
});
