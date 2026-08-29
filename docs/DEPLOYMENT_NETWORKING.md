# Deployment networking — Vercel frontend, Oracle backend

How the two halves reach each other, why it is built this way, and what to check
when data stops loading.

---

## The rule

```
Browser  ──https──▶  Vercel (Next.js)  ──http──▶  Oracle (FastAPI)  ──▶  Binance / Yahoo / news / LLM
         same-origin                   server-to-server
```

**The browser talks only to Vercel. Vercel talks to everything else.**

Two independent constraints force this, and each one alone would be enough.

### 1. Vercel's region is refused by Binance

A Next.js route handler on Vercel executes in a region Vercel chooses — not
necessarily the one that received the request. From a US region, Binance answers:

```
HTTP 451 — Service unavailable from a restricted location according to 'b. Eligibility'
```

451 is literally *Unavailable For Legal Reasons*. No retry, API key, header or
user-agent changes it: the caller's **location** is what is refused. This is what
produced 502s on `/api/candles`, `/api/orderflow` and `/api/quote` while every
route that only read local JSON kept working.

Pinning `vercel.json` regions would be the fragile fix — Vercel's routing is not
fully under your control and Binance's blocklist can grow. Making the call from a
machine you control, in a region that is served, is the durable one.

### 2. The browser cannot reach the backend directly

The Vercel site is https. The Oracle backend has no TLS certificate, so it speaks
plain http and ws. A browser on an https page **refuses** to issue `http://` or
open `ws://` from it. That is the mixed-content policy — a browser rule, not a
CORS setting. No header on the backend can permit it, and a WebSocket cannot be
proxied through a Vercel serverless function either.

This was breaking far more than the market data:

| What | Was doing | Result on the deployed site |
|---|---|---|
| 18 pages via `useBackend` | `fetch('http://<oracle>:8000/...')` | blocked — pages showed nothing |
| Pause / Resume (`TopBar`) | same | button did nothing |
| **Emergency stop** | same | **silently failed** |
| Live-trading toggle (Settings) | same | did nothing |
| Polymarket panel | same | empty |
| Agent event stream | `new WebSocket('ws://...')` | never connected; terminal always empty |
| Live prices (`MarketData`) | `wss://stream.binance.com` from the browser | *worked*, but only for viewers in an unblocked region |

None of these logged an error a user would find. They looked like an idle system.

---

## How it works now

### Every browser → backend call goes through one proxy

`app/api/backend/[...path]/route.ts` forwards any path to the backend
server-to-server:

```
/api/backend/admin/pause   →   http://<oracle>:8000/api/admin/pause
```

Components use `backendProxyPath()` from `lib/backendConfig.ts`. The old
`backendUrl()` was **renamed to `serverOnlyBackendUrl()`** so that calling it
from a component is a compile error rather than a request that is silently
blocked at runtime.

The shared `TRADES_API_KEY` is attached by the proxy, server-side. The browser
performs privileged actions without ever holding the secret, and the key never
enters the client bundle.

### Market data lives on the backend

`backend/api/marketdata.py` — everything the dashboard needs from a third party:

| Endpoint | Replaces | Upstream |
|---|---|---|
| `/api/marketdata/candles` | `/api/candles` | Binance spot klines / Yahoo chart |
| `/api/marketdata/candles/deep` | `lib/candleSource.server.ts` | Binance, paginated to 10,000 bars |
| `/api/marketdata/orderflow` | `/api/orderflow` | Binance depth + aggTrades |
| `/api/marketdata/quote` | `/api/quote` | Yahoo **v8 chart** (see below) |
| `/api/marketdata/marketintel` | `/api/marketintel` | Binance futures + alternative.me |
| `/api/marketdata/eventdata` | `/api/eventdata` | Binance futures history |
| `/api/marketdata/multiexchange` | `/api/multiexchange` | 6 venues |
| `/api/marketdata/news` | RSS half of `/api/news` | 7 feeds |
| `/api/marketdata/ticks` | the browser's Binance socket | backend-held socket, cached |
| `/api/marketdata/upstream-health` | the Binance probe in `/api/health` | reachability + geo-block report |

The Next routes keep their paths and response shapes exactly, so no component
changed. They are now thin proxies.

### Real-time became polling, and only on the last hop

| Stream | Before | Now |
|---|---|---|
| Crypto prices | browser ↔ Binance WebSocket | backend holds **one** Binance socket for all viewers; browser polls `/api/ticks` every 2s |
| Agent events | browser ↔ backend WebSocket (blocked) | backend buffers events with a cursor; browser polls `/api/agent-events` every 2s |

The exchange socket is still real-time. Only the browser's final hop is polled,
so a tick read from the cache is about a second old — not a fresh REST call per
symbol.

The backend's WebSocket endpoints are **not removed**. They still work for any
client that can reach the host directly, and they are the transport to return to
once the backend has TLS.

---

## Getting real-time back (optional)

Everything above works with no domain and no certificate. If you want true
push instead of a 2s poll, the backend needs an https/wss hostname. Two routes
that do not require buying a domain:

1. **A free DNS name that maps to your IP**, e.g. `<your-ip>.nip.io` or
   `<your-ip>.sslip.io`. These are real DNS names, so Let's Encrypt will issue a
   certificate for them. Put Caddy in front of the backend and it handles the
   certificate automatically.
2. **Cloudflare Tunnel** — `cloudflared` runs on the Oracle box and gives you an
   https hostname without opening an inbound port at all. Useful if the Oracle
   security list is the obstacle.

Once the backend answers on https, point the frontend back at the WebSocket in
`lib/agentEventStream.ts` and re-add a socket to `components/MarketData.tsx`.
Nothing else needs to change — the polling and the socket read the same
server-side caches.

---

## Configuration

On **Vercel** (server-side environment variables):

```
BACKEND_INTERNAL_URL=http://<your-oracle-ip>:8000
TRADES_API_KEY=<the same value as on the backend>
DASHBOARD_PASSWORD=<basic-auth password for the dashboard>
```

`BACKEND_INTERNAL_URL` is deliberately **not** `NEXT_PUBLIC_`-prefixed. Anything
with that prefix is inlined into the browser bundle, and a browser that knows the
backend's address will eventually be pointed at it by some future code — which
fails on mixed content in a way that looks like the backend being down.

On **Oracle**, in `.env`: exchange keys, `DATABASE_URL`, `TRADES_API_KEY`, and
whichever feature flags you want. See `.env.example`.

Oracle Cloud also needs the port open in **both** places — this catches people
out because they look at only one:

1. the VCN **security list** (or network security group) — ingress on 8000;
2. the instance's own firewall — `iptables`/`firewalld` on Oracle Linux blocks it
   by default even when the security list allows it.

---

## Triage: data is not loading

Work outward. Each step distinguishes a different failure.

**1. Can the backend reach the providers?**

```bash
curl -s http://localhost:8000/api/marketdata/upstream-health | python -m json.tool
```

`geoBlocked` names any provider refusing this host's region. If Binance appears
there, the backend is in a blocked region too and moving the calls off Vercel did
not help — the host has to move.

**2. Can Vercel reach the backend?**

Open `/api/health` on the deployed site. It reports `Trading backend: reachable
from Vercel` as its own check, separately from each upstream. If that line is red
and step 1 was green, it is the port — security list or host firewall.

**3. Is the browser reaching Vercel?**

Browser devtools → Network. Every request should be same-origin and https. **Any
request to `http://` or `ws://` is a bug** — it means a call site is bypassing
the proxy. Search for `serverOnlyBackendUrl` outside `.server.ts` files.

### Reading the errors

Error messages name the hop deliberately, because these are all 502s to a browser
and have completely different fixes:

- *"Could not reach the trading backend at …"* — the Vercel→Oracle hop. Firewall
  or the backend is down.
- *"… returned 451 … this location is refused by the provider"* — the
  Oracle→provider hop. Region problem.
- *"Yahoo quote: … 401 Unauthorized"* — not a region problem at all; see below.

---

## Two bugs found during this work that were not the geo-block

**Yahoo's `/v7/finance/quote` is dead.** It now returns `401 Unauthorized` to
unauthenticated callers — it requires a crumb+cookie pair. `/api/quote` was
failing for this reason, *not* the Binance geo-block, and both surfaced as an
identical 502. Fixing only the region would have left quotes broken and looking
like the same fault. The backend uses `/v8/finance/chart` instead, which is
keyless and open, and reads the price from its `meta` block. The cost is one
request per symbol instead of one batched call.

**`LiveAgentInspectorModal` asked the backend for `/api/news`.** That is a route
of the *Next.js* app; FastAPI has no such path, so it 404'd and rendered as "no
news". This is exactly the confusion `lib/backendConfig.ts` was written to
prevent, and one instance had survived. It now uses `useSameOrigin`.

---

## The operator's exchange path

`app/api/exchange/route.ts` used to sign and send Binance/Bybit orders from
Vercel. It is now a proxy to `backend/api/operator_exchange.py`, mounted at
`/api/operator/exchange`.

This one needed care, because `backend/api/exchange.py` says:

> there is deliberately **no** order-placement route here … An HTTP endpoint that
> placed an order would be a path to the exchange that bypasses the Supervisor,
> the CRO, and the leverage ceiling — reachable by anything that can reach the
> port.

That warning still stands and `/api/exchange` is still read-only. The order route
lives in a **separate, differently-named module** so nobody discovers order
placement while reading the read-only one.

### Two planes, kept distinct

| | Agent | Operator |
|---|---|---|
| Path | Supervisor → CRO → `TAR_APPROVED` → ExecutionAgent | a human clicking a button |
| Credentials | the backend's, from `.env` | the operator's, sent per request |
| Gated by | `LIVE_TRADING`, risk checks, leverage ceiling, mandatory stop | the operator's own judgment |
| Reachable over HTTP | **no** | yes, authenticated |

CLAUDE.md invariant 1 puts manual clicks outside the Supervisor's scope on
purpose: *"supervising agents means supervising agents, not overriding the
operator."* The capability is not new — it existed in Next.js and was equally
unsupervised there. Only its location changed.

### What makes the exception safe

1. **Write auth on every route.** The Next.js route had no credential of its own,
   so this *closes* the "reachable by anything that can reach the port" exposure
   rather than relocating it. The proxy attaches `TRADES_API_KEY` server-side, so
   the browser performs a real-money action without ever holding it.
2. **Credentials are per request and never stored.** `Credentials.__repr__` is
   overridden so a stray log line or traceback cannot print the secret.
3. **Not registered with the AgentOS kernel or the message bus.** No agent can
   invoke it; no event reaches it.
4. **Listed in `graphs/contracts.FORBIDDEN_IMPORTS`** — the AST test fails if
   anything under `graphs/` imports it.
5. **Every order is persisted** with `origin_tag='manual-click'` and logged at
   WARNING, so an operator order is distinguishable from an agent order forever
   after. Testnet fills record as `tab='paper'` so they never mix into real
   history.
6. **Spot only**, matching `lib/exchangeClients/types.ts`. Futures/margin
   mechanics are their own deliberate piece of work.

`tests/test_api_surface.py` asserts the plane separation and that every route
here is authenticated; `tests/test_operator_exchange.py` covers the module.

### One thing to fix before mainnet

The browser → Vercel leg is https, but the **Vercel → backend leg is plain
http** on this deployment, and it now carries the operator's exchange secret.
Before using this with mainnet keys, put that hop on a private network or a
tunnel — the same TLS work described under *Getting real-time back* above solves
both at once.
