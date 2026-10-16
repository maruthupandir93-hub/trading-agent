/**
 * Refuse a caller-supplied URL that points back into our own network.
 *
 * WHY (F36). Two routes fetch a URL the CALLER chooses, from the server:
 *
 *   * `POST /api/mcp-status` returns the status and latency of any http(s)
 *     URL — a blind probe of anything the Vercel function can reach.
 *   * `POST /api/chat` forwards a caller-chosen `baseUrl` to the backend's
 *     `/api/ai/chat`, which then POSTs to that URL **from the VM** and streams
 *     the reply back. That turns the VM into a POST relay into its own
 *     network, which is where the database, the admin API and the cloud
 *     metadata service live.
 *
 * Authentication (F34) is the first half and this is the second: an
 * authenticated operator still should not be able to turn the server into a
 * scanner, and a leaked dashboard password should not come with one.
 *
 * WHAT IS BLOCKED, and why each:
 *
 *   loopback      127.0.0.0/8, ::1          the server's own admin surfaces
 *   link-local    169.254.0.0/16, fe80::/10 cloud metadata (169.254.169.254)
 *   private       10/8, 172.16/12, 192.168/16, fc00::/7, fd00::/8
 *   unspecified   0.0.0.0, ::
 *   non-http(s)   file:, gopher:, ftp: ...  not ours to fetch at all
 *
 * WHAT THIS DOES NOT DO, stated plainly because the gap matters: it checks the
 * HOSTNAME as written. A DNS name that resolves to a private address, or that
 * resolves differently on the second lookup (a rebind), still passes. Closing
 * that needs resolution-then-pinned-connect, which neither the Edge nor the
 * Node fetch here exposes. This removes the trivial `http://127.0.0.1:8000`
 * and metadata-service cases, not every SSRF.
 */

const BLOCKED_HOSTNAMES = new Set([
  'localhost',
  'localhost.localdomain',
  'metadata.google.internal',
  'metadata',
]);

function isPrivateIPv4(host: string): boolean {
  const parts = host.split('.');
  if (parts.length !== 4) return false;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return false;
  const [a, b] = nums;
  if (a === 0) return true;                      // 0.0.0.0/8
  if (a === 10) return true;                     // 10/8
  if (a === 127) return true;                    // loopback
  if (a === 169 && b === 254) return true;       // link-local / cloud metadata
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;  // CGNAT
  return false;
}

function isPrivateIPv6(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (h === '::1' || h === '::') return true;
  if (h.startsWith('fe80:')) return true;        // link-local
  if (h.startsWith('fc') || h.startsWith('fd')) return true;  // unique local
  // IPv4-MAPPED, IN EITHER SPELLING. `new URL()` normalises
  // `::ffff:127.0.0.1` to `::ffff:7f00:1` — the IPv4 part re-expressed as two
  // hex groups — so matching only the dotted form let loopback through under
  // an IPv6 mapping. Caught by a test; the literal a caller types and the
  // literal the parser hands back are not the same string.
  const dotted = h.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (dotted) return isPrivateIPv4(dotted[1]);

  const hex = h.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (hex) {
    const hi = parseInt(hex[1], 16);
    const lo = parseInt(hex[2], 16);
    const quad = [hi >> 8, hi & 0xff, lo >> 8, lo & 0xff].join('.');
    return isPrivateIPv4(quad);
  }
  return false;
}

export type UrlCheck = { ok: true; url: URL } | { ok: false; reason: string };

/** Is this URL safe for the SERVER to fetch on a caller's behalf? */
export function checkOutboundUrl(raw: string | undefined | null): UrlCheck {
  const value = (raw || '').trim();
  if (!value) return { ok: false, reason: 'Missing url' };

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return { ok: false, reason: 'Not a valid URL' };
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { ok: false, reason: 'Only http(s) URLs are supported' };
  }

  const host = parsed.hostname.toLowerCase();
  if (BLOCKED_HOSTNAMES.has(host) || host.endsWith('.localhost')) {
    return { ok: false, reason: 'Refusing to fetch a loopback address from the server' };
  }
  if (isPrivateIPv4(host) || isPrivateIPv6(host)) {
    return {
      ok: false,
      reason:
        'Refusing to fetch a private, loopback or link-local address from the ' +
        'server. This endpoint would otherwise probe our own network, including ' +
        'the cloud metadata service.',
    };
  }

  return { ok: true, url: parsed };
}

/**
 * Allow a caller-supplied base URL ONLY when it matches an allow-list.
 *
 * For `/api/chat`, where the caller names an LLM endpoint. An allow-list is
 * the right shape there because the set of real providers is small and known,
 * while the set of things a relay could be pointed at is everything.
 *
 * An empty or unset `LLM_BASE_URL_ALLOWLIST` falls back to the SSRF check
 * alone, so an operator running their own gateway is not locked out — the
 * private-address refusal still applies.
 */
export function checkLlmBaseUrl(raw: string | undefined | null): UrlCheck {
  const basic = checkOutboundUrl(raw);
  if (!basic.ok) return basic;

  const allow = (process.env.LLM_BASE_URL_ALLOWLIST || '')
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  if (allow.length === 0) return basic;

  const host = basic.url.hostname.toLowerCase();
  const permitted = allow.some((entry) => host === entry || host.endsWith(`.${entry}`));
  if (!permitted) {
    return {
      ok: false,
      reason:
        `${host} is not in LLM_BASE_URL_ALLOWLIST. Add it there if you intend ` +
        `this server to forward requests to it.`,
    };
  }
  return basic;
}
