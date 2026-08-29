// ---------------------------------------------------------------------
// SERVER-SIDE ONLY. One place where Next.js talks to the FastAPI backend.
//
// WHY EVERY MARKET-DATA ROUTE NOW GOES THROUGH HERE
//
// These routes used to call api.binance.com, fapi.binance.com and Yahoo
// directly. On Vercel a route handler executes in whatever region Vercel picks,
// and from a US region Binance answers:
//
//     451 — "Service unavailable from a restricted location according to
//            'b. Eligibility'"
//
// 451 is literally "Unavailable For Legal Reasons". No retry, key or header
// fixes it — the CALLER's location is the problem. The dashboard showed 502s on
// /api/candles, /api/orderflow and /api/quote while every route that only read
// local JSON kept working, which is what made it look like three broken
// endpoints rather than one structural fault.
//
// The fix is that the request must originate from a machine in a served region.
// That machine is the Oracle box running FastAPI, so the upstream call happens
// there and this file is the hop that reaches it.
//
// THIS HOP IS SERVER-TO-SERVER, AND THAT IS WHAT MAKES PLAIN http OK
//
//     Browser --https--> Vercel (this file) --http--> Oracle --> Binance
//
// The browser only ever talks to Vercel over https, so there is no mixed
// content. Mixed-content rules are a BROWSER policy; they do not apply to a
// fetch made by a server. That is why the backend needs no TLS certificate for
// any of this to work — and equally why the browser can never be pointed at the
// backend directly.
//
// BACKEND_INTERNAL_URL, NOT NEXT_PUBLIC_BACKEND_URL
//
// Deliberately a server-only variable. Anything prefixed `NEXT_PUBLIC_` is
// inlined into the client bundle, and a browser that has the backend's address
// will eventually be pointed at it by some future code — which fails on mixed
// content in a way that looks like the backend being down. Keeping the address
// server-only makes that mistake impossible to make by accident.
// ---------------------------------------------------------------------

/**
 * The FastAPI origin, as seen from Vercel's servers.
 *
 * Falls back to localhost so a developer running both halves locally needs no
 * configuration, matching how every other store in this codebase behaves.
 */
export function backendOrigin(): string {
  const configured =
    process.env.BACKEND_INTERNAL_URL ||
    // Accepted as a fallback because an existing deployment already sets it.
    // Read here on the SERVER only; nothing in this file reaches the browser.
    process.env.NEXT_PUBLIC_BACKEND_URL ||
    'http://localhost:8000';
  return configured.replace(/\/$/, '');
}

/**
 * Timeout for the Vercel -> backend hop.
 *
 * Longer than the backend's own 12s upstream timeout, on purpose. If this were
 * shorter, a slow Binance response would time out HERE first and be reported as
 * "the backend is unreachable" — sending the operator to check a server that is
 * fine. Letting the backend's own timeout fire first means the error names the
 * upstream that was actually slow.
 */
const PROXY_TIMEOUT_MS = 20_000;

export type ProxyResult = { response: Response };

/**
 * Forward a GET to the backend and hand its response straight back.
 *
 * THE BODY IS PASSED THROUGH UNTOUCHED, and the status with it. Re-shaping it
 * here would mean the response the browser sees is assembled in two places, and
 * the backend's careful `available: false` / null-not-zero distinctions would
 * have to be re-implemented in a second language to survive the trip.
 *
 * A transport failure becomes a 502 whose body says WHICH hop failed. The
 * distinction matters: "Binance refused our region" and "the backend is not
 * running" are both 502s to a browser, and they have completely different fixes.
 */
export async function proxyToBackend(
  path: string,
  searchParams?: URLSearchParams | Record<string, string | undefined>,
): Promise<Response> {
  const url = new URL(`${backendOrigin()}${path}`);

  if (searchParams instanceof URLSearchParams) {
    searchParams.forEach((value, key) => url.searchParams.set(key, value));
  } else if (searchParams) {
    for (const [key, value] of Object.entries(searchParams)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, value);
      }
    }
  }

  const headers: Record<string, string> = { Accept: 'application/json' };
  // The same shared secret both halves already use (backend/core/auth.py).
  // Reads are open on the backend, so this is only needed if that ever changes
  // — sending it costs nothing and means turning auth on does not break the UI.
  const key = process.env.TRADES_API_KEY;
  if (key) headers.Authorization = `Bearer ${key}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROXY_TIMEOUT_MS);

  try {
    const upstream = await fetch(url.toString(), {
      headers,
      signal: controller.signal,
      // Never cached. These are live market reads, and a cached candle series
      // is worse than a slow one — it looks current and is not.
      cache: 'no-store',
    });

    const body = await upstream.text();

    // FastAPI reports errors as {"detail": "..."}; the frontend's error paths
    // read `error`. Translated here rather than in each route so there is one
    // place that knows both conventions.
    if (!upstream.ok) {
      let message = body.slice(0, 500);
      try {
        const parsed = JSON.parse(body);
        if (typeof parsed?.detail === 'string') message = parsed.detail;
        else if (typeof parsed?.error === 'string') message = parsed.error;
      } catch {
        // Not JSON — an HTML error page from something in front of the
        // backend, most likely. The raw text is more useful than a guess.
      }
      return Response.json({ error: message }, { status: upstream.status });
    }

    return new Response(body, {
      status: upstream.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (err) {
    const aborted = err instanceof Error && err.name === 'AbortError';
    const detail = aborted
      ? `timed out after ${PROXY_TIMEOUT_MS / 1000}s`
      : err instanceof Error
        ? err.message
        : 'unknown error';

    return Response.json(
      {
        error:
          `Could not reach the trading backend at ${backendOrigin()} (${detail}). ` +
          `This is the Vercel-to-backend hop, NOT the market data provider — the backend ` +
          `is either down, or its port is not open to Vercel. Check the host's firewall ` +
          `and security list before looking at the exchange.`,
      },
      { status: 502 },
    );
  }
}

/**
 * Fetch JSON from the backend for code that needs the parsed value rather than
 * a Response to return — `lib/candleSource.server.ts` is the caller.
 *
 * Throws on failure, because its callers are already inside a try/catch that
 * turns an exception into the route's error response.
 */
export async function fetchFromBackend<T>(
  path: string,
  searchParams?: Record<string, string | undefined>,
): Promise<T> {
  const response = await proxyToBackend(path, searchParams);
  const json = await response.json();
  if (!response.ok) {
    throw new Error(typeof json?.error === 'string' ? json.error : `backend returned ${response.status}`);
  }
  return json as T;
}
