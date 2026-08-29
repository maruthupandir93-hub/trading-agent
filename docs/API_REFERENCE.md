# Backend API reference

Every endpoint the FastAPI backend serves, for testing with Postman, `curl`, or
anything else.

The endpoint tables are **generated from the live app** — including the Auth
column, which is read from each route's real dependencies rather than from a note
someone remembered to update. Regenerate with
`.venv/Scripts/python.exe scripts/generate_api_reference.py`.

---

## Before you start

### Base URL

| Testing from | Base URL |
|---|---|
| On the Oracle VM | `http://localhost:8000` |
| From your laptop | `http://YOUR_VM_PUBLIC_IP:8000` |
| Through the Vercel site | `https://your-project.vercel.app/api/backend` |

The third one is worth knowing: `/api/backend/<path>` on your Vercel site forwards
to the backend with the `/api` prefix stripped, so `/api/backend/admin/pause`
reaches the backend's `/api/admin/pause`. That proxy is how the browser reaches
the backend at all — see `docs/CONNECTING_VERCEL_TO_ORACLE.md`.

**Test directly against the VM.** Going through Vercel adds a hop that can fail on
its own, which makes a backend problem look like a frontend one.

### FastAPI already serves interactive docs

Every endpoint has a live "Try it out" button, with no setup:

```
http://YOUR_VM_IP:8000/docs          Swagger UI
http://YOUR_VM_IP:8000/redoc         ReDoc
http://YOUR_VM_IP:8000/openapi.json  raw schema
```

For exploring, `/docs` beats Postman. Use Postman when you want to save requests,
script sequences, or share a collection.

### Authentication

Reads are open. Writes need a bearer token **only if** `TRADES_API_KEY` is set in
the backend's `.env`:

```
Authorization: Bearer YOUR_TRADES_API_KEY
```

- **Unset** — everything is open. Fine locally, not for an internet-facing VM.
- **Set** — the operations marked **yes** below return `401` without a matching
  header.

Reads are deliberately open because the browser polls them directly and cannot
hold a server secret without shipping it to every visitor. `backend/core/auth.py`
explains what this model is, and honestly what it is not.

### Import all of it into Postman in one step

Don't build requests by hand:

1. Postman → **Import** → **Link**
2. Paste `http://YOUR_VM_IP:8000/openapi.json`
3. Import

You get every request with its parameters. Then set auth once for the collection:

- Collection → **Variables**: `baseUrl` = `http://YOUR_VM_IP:8000`,
  `apiKey` = your `TRADES_API_KEY`
- Collection → **Authorization**: type **Bearer Token**, token `{{apiKey}}`

Individual requests inherit it, so the authenticated endpoints just work.

---

## Start here: five requests that prove the system works

In order. Each isolates a different failure.

### 1. Is the backend alive?

```
GET {{baseUrl}}/api/monitoring
```

Anything other than JSON means it isn't running, or the port is closed.

### 2. Can the backend reach the exchanges?

```
GET {{baseUrl}}/api/marketdata/upstream-health
```

**The most important diagnostic in the system.** It answers whether *this host's
region* is served by Binance, Yahoo and the rest:

```json
{ "allReachable": true, "geoBlocked": [],
  "note": "This host's region is served by every upstream checked." }
```

If `geoBlocked` lists Binance, your VM is in a blocked region and market data
cannot work until the VM moves. That is the 451 problem this whole architecture
exists to solve.

### 3. Real market data

```
GET {{baseUrl}}/api/marketdata/candles?symbol=BTCUSDT&type=crypto&interval=1h&limit=5
```

Five OHLC candles as `{t, o, h, l, c, v}`.

### 4. Live prices

```
GET {{baseUrl}}/api/marketdata/ticks?binance=btcusdt,ethusdt
```

**The first call returns `null` for every symbol, and that is correct.** Asking
for a symbol subscribes to it, and the first frame takes about a second. Call it
again and you get prices with an `ageSeconds` field. `stream.connected` tells you
whether the backend's Binance socket is up.

### 5. Is auth wired correctly?

```
POST {{baseUrl}}/api/admin/pause
```

Without the header → `401` (when `TRADES_API_KEY` is set). With it → `200`. Then
`POST /api/admin/resume` to undo. Pausing stops new entries; **exits are never
blocked**, by design.

---

## Danger zone: the four endpoints that move real money

`/api/operator/exchange/*` places **real orders** using credentials you send in
the request body. No risk check, no leverage cap, no supervisor — this is the
operator's manual path and it is unsupervised on purpose (CLAUDE.md invariant 1:
supervising agents means supervising agents, not overriding the operator).

**Always test with `"testnet": true` first.**

```
POST {{baseUrl}}/api/operator/exchange/order
Authorization: Bearer {{apiKey}}
Content-Type: application/json

{
  "exchange": "binance",
  "apiKey": "YOUR_EXCHANGE_KEY",
  "apiSecret": "YOUR_EXCHANGE_SECRET",
  "testnet": true,
  "symbol": "BTC/USDT",
  "side": "buy",
  "qty": 0.001,
  "clientOrderId": "my-unique-id-1"
}
```

Four things to understand about that body:

- **`testnet: true`** routes to the exchange's testnet, and the fill is recorded
  with `tab='paper'` so it never mixes into real history.
- **`clientOrderId` is the idempotency key.** Both venues reject a duplicate, so a
  retry reusing the same id cannot double-fill. It must be *deterministic* for one
  logical order — never random or timestamped, or the retry defeats its own
  purpose.
- **`recorded`** in the response says whether a local trade row was written. It is
  `false` when the venue accepted the order but reported no fill price: the order
  is real, the log entry is not, and `recordNote` says so. Reconcile with
  `/order/status`. No row is written without a price, because
  `lib/tradeStore.server.ts` would render a null as **a trade at price 0**.
- **`qty` is in the base asset** (BTC), not dollars.

The other three take the same auth and credential fields:

```
POST /api/operator/exchange/balance        { exchange, apiKey, apiSecret, testnet }
POST /api/operator/exchange/order/status   { ..., symbol, exchangeOrderId }
POST /api/operator/exchange/order/cancel   { ..., symbol, exchangeOrderId }
```

**`/api/exchange/*` is a different thing and is read-only.** It reports the
*backend's own* connection status using the keys in `.env`. It has no
order-placement route, deliberately — that would be a path to the exchange
bypassing the Supervisor, the CRO and the leverage ceiling.

---

## Two endpoints that behave unusually

### `/api/dashboard/events` — cursor-based, not a list

```
GET {{baseUrl}}/api/dashboard/events
```

The first call returns **no events** and a `cursor`. That is intended: a new
client should not be shown a ten-minute backlog as though it were happening now.
Pass the cursor back for what happened since:

```
GET {{baseUrl}}/api/dashboard/events?cursor=714
```

`missed: true` means you fell behind far enough that events were evicted from the
buffer — reported rather than hidden, so a gap in the timeline is never silent.

### `POST /api/graphs/run/{symbol}` — expensive

Runs the full reasoning graph: market state, opportunity detection, seven
specialists and a debate. Takes seconds and calls the LLM if one is configured.
Auth-gated. It produces an *inert* execution plan — `GRAPH_EXECUTION_ENABLED`
separately gates whether such a plan can ever reach an exchange.

---

## Every endpoint

Auth column: **yes** means `Authorization: Bearer <TRADES_API_KEY>` is required
when that variable is set in the backend's `.env`.

<!-- BEGIN GENERATED ENDPOINTS -->

*84 operations across 80 paths, 24 requiring auth, plus 2 WebSockets. Generated by `scripts/generate_api_reference.py`.*

### Health & status

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/monitoring` | - | no |

### Market data — third-party upstreams

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/marketdata/candles` | `symbol`, `type`, `interval`, `limit` | no |
| GET | `/api/marketdata/candles/deep` | `symbol`, `type`, `interval`, `bars` | no |
| GET | `/api/marketdata/eventdata` | `binance` | no |
| GET | `/api/marketdata/marketintel` | `binance` | no |
| GET | `/api/marketdata/multiexchange` | `symbol`, `base`, `quote` | no |
| GET | `/api/marketdata/news` | `limit` | no |
| GET | `/api/marketdata/orderflow` | `binance` | no |
| GET | `/api/marketdata/quote` | `symbols` | no |
| GET | `/api/marketdata/ticks` | `binance` | no |
| GET | `/api/marketdata/upstream-health` | - | no |

### Market data — the agent's ccxt view

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/market/analysis/{symbol}` | `symbol` | no |
| GET | `/api/market/klines/{symbol}` | `symbol`, `timeframe`, `limit` | no |
| GET | `/api/market/price/{symbol}` | `symbol` | no |
| GET | `/api/market/prices` | `refresh` | no |
| GET | `/api/market/regime/{symbol}` | `symbol`, `timeframe` | no |

### Exchange status (READ-ONLY)

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/exchange/balance` | - | no |
| GET | `/api/exchange/compare/{symbol}` | `symbol` | no |
| GET | `/api/exchange/status` | - | no |
| GET | `/api/exchange/tickers` | `limit` | no |

### Operator exchange — PLACES REAL ORDERS

| Method | Path | Query / body | Auth |
|---|---|---|---|
| POST | `/api/operator/exchange/balance` | JSON body | **yes** |
| POST | `/api/operator/exchange/order` | JSON body | **yes** |
| POST | `/api/operator/exchange/order/cancel` | JSON body | **yes** |
| POST | `/api/operator/exchange/order/status` | JSON body | **yes** |

### Kill switch & trading mode

| Method | Path | Query / body | Auth |
|---|---|---|---|
| POST | `/api/admin/emergency-stop` | - | **yes** |
| POST | `/api/admin/live-trading/disable` | - | **yes** |
| POST | `/api/admin/live-trading/enable` | JSON body | **yes** |
| POST | `/api/admin/pause` | - | **yes** |
| POST | `/api/admin/resume` | - | **yes** |
| GET | `/api/admin/status` | - | no |
| GET | `/api/admin/trading-mode` | - | no |

### Dashboard & event stream

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/dashboard` | - | no |
| GET | `/api/dashboard/events` | `cursor`, `limit` | no |
| GET | `/api/dashboard/portfolio` | - | no |

### LangGraph reasoning layer

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/graphs` | - | no |
| GET | `/api/graphs/meta-learning` | - | no |
| GET | `/api/graphs/nodes` | - | no |
| GET | `/api/graphs/positions` | - | no |
| POST | `/api/graphs/run/{symbol}` | `symbol` | **yes** |
| GET | `/api/graphs/runs` | `limit` | no |
| GET | `/api/graphs/runs/{run_id}` | `run_id` | no |

### Execution log & audit

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/execution` | `tab`, `limit` | no |
| GET | `/api/execution/audit` | `limit` | no |

### Catalog — orders, strategies, replay

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/catalog` | - | no |
| GET | `/api/catalog/orders` | `limit`, `tab` | no |
| GET | `/api/catalog/replay` | `limit` | no |
| GET | `/api/catalog/strategies` | - | no |

### Memory system

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/memory` | - | no |
| GET | `/api/memory/ledger` | `limit`, `symbol` | no |
| POST | `/api/memory/lesson` | JSON body | **yes** |
| GET | `/api/memory/mistakes` | `limit` | no |
| GET | `/api/memory/report` | - | no |
| GET | `/api/memory/stats` | - | no |

### Research & hypotheses

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/research/benchmark` | - | no |
| GET | `/api/research/dashboard` | - | no |
| GET | `/api/research/hypotheses` | `status` | no |
| POST | `/api/research/hypotheses/{hypothesis_id}/status` | `hypothesis_id` + JSON body | **yes** |
| GET | `/api/research/queue` | - | no |
| POST | `/api/research/run` | JSON body | **yes** |
| GET | `/api/research/tasks` | `open_only` | no |
| POST | `/api/research/tasks/{task_id}/finding` | `task_id` + JSON body | **yes** |

### Knowledge graph

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/knowledge` | - | no |
| GET | `/api/knowledge/implications/{state}` | `state` | no |
| GET | `/api/knowledge/path` | `source`, `target` | no |
| POST | `/api/knowledge/relationship` | JSON body | **yes** |

### Missions

| Method | Path | Query / body | Auth |
|---|---|---|---|
| DELETE | `/api/missions` | `id` | **yes** |
| GET | `/api/missions` | - | **yes** |
| PATCH | `/api/missions` | JSON body | **yes** |
| POST | `/api/missions` | JSON body | **yes** |

### Agent tasks

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/agents/tasks` | - | **yes** |
| POST | `/api/agents/tasks` | JSON body | **yes** |
| DELETE | `/api/agents/tasks/{task_id}` | `task_id` | **yes** |

### AI routing & chat proxy

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/ai/agents` | - | no |
| GET | `/api/ai/agents/{agent_id}` | `agent_id` | no |
| POST | `/api/ai/chat` | JSON body | no |
| POST | `/api/ai/reason` | JSON body | no |
| POST | `/api/ai/route` | JSON body | no |

### Polymarket feed

| Method | Path | Query / body | Auth |
|---|---|---|---|
| GET | `/api/polymarket` | - | no |
| POST | `/api/polymarket/discover/{symbol}` | `symbol` | **yes** |
| GET | `/api/polymarket/mappings` | `symbol`, `confirmedOnly` | no |
| POST | `/api/polymarket/mappings/confirm` | JSON body | **yes** |
| GET | `/api/polymarket/series` | `outcome`, `limit` | no |
| GET | `/api/polymarket/signals` | - | no |
| GET | `/api/polymarket/snapshots` | - | no |

### WebSockets

| Path |
|---|
| `/api/dashboard/agent-events` |
| `/api/graphs/stream` |

<!-- END GENERATED ENDPOINTS -->

### Testing the WebSockets

The two WebSocket endpoints are **not reachable from the deployed browser**, and
that is not a bug: the Vercel site is `https`, the backend has no certificate, and
a browser refuses a `ws://` connection from an `https` page. The frontend polls
`/api/dashboard/events` instead.

They work fine from anything that can reach the host directly. In Postman:
**New** → **WebSocket Request** → `ws://YOUR_VM_IP:8000/api/dashboard/agent-events`
→ **Connect**. Every event the agent publishes appears live.

They become the better transport again once the backend has TLS — see
`docs/CONNECTING_VERCEL_TO_ORACLE.md` Part 4.

---

## Response conventions

Worth knowing before reading any output. These are deliberate and consistent
across the whole API.

**`null` means "not measured", never "zero".** A missing funding rate, an
unmeasurable confidence and an unknown probability of ruin all come back as
`null`. `0.0` means genuinely measured as zero. This distinction is enforced
throughout: `prob_of_ruin: 0.0` on a strategy with no data once passed every
threshold check, which is exactly what the rule prevents.

**`available: false` and `*Available` flags** separate "evaluated and found
nothing" from "could not evaluate". An empty array with
`fundingHistoryAvailable: false` means the fetch failed — not that the market is
quiet.

**Partial results are labelled, not hidden.** `/api/marketdata/multiexchange`
returns every venue including the ones that failed, each with its reason. A
silently dropped venue would read as "not checked".

**Errors name which hop failed.** "Could not reach the trading backend" is
Vercel→backend. "returned 451 … location is refused" is backend→provider. Both are
a 502 in a browser and they have completely different fixes.

---

## Related documents

- `docs/CONNECTING_VERCEL_TO_ORACLE.md` — deploying and connecting the two halves
- `docs/DEPLOYMENT_NETWORKING.md` — why the architecture is shaped this way
- `db/schema.sql` and `db/README.md` — the database
- `backend/core/auth.py` — the auth model, and what it honestly is not
