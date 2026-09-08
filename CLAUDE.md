# TradingOS AI — Project Instructions

This file is read automatically every session. It is the persistent
instruction layer described in `TradingOS-Engineering-Spec-and-Prompts.md`
Section 21, adapted to the architecture that **actually exists here** —
not the idealized one in the spec. Where the two differ, this file wins,
because it is kept in sync with the code.

Supporting long-term context lives in `docs/`. Read the relevant file
before working in a domain.

---

## Mission

An autonomous AI trading platform that continuously analyzes markets,
makes explainable decisions, preserves capital through rigorous risk
management, learns from validated experience, and operates under
human-defined governance.

## Core principles

1. Capital preservation
2. Explainability
3. Reliability
4. Continuous learning
5. Safety
6. Modularity
7. Scalability
8. Research-driven
9. Risk first
10. Evidence-based

## Primary objective

Seek long-term, risk-adjusted capital growth through disciplined trading.

**Do NOT optimize for a guaranteed return multiple** (e.g. "turn $X into
$Y"). That is a financial outcome, not an engineering requirement, and
encoding it as a hard objective pushes the system toward unsafe
risk-taking. A `capital-target` Mission exists so a user can *state* such
a goal and track progress toward it, and it deliberately has no deadline
and only ever produces advisory caution notes — never a hard rule, never
a sizing override. Keep it that way.

---

## What this codebase actually is

A **single-process Next.js 14 app** (App Router, React 18, TypeScript,
Tailwind). Not microservices. Not an event bus. State lives in React
context providers under `components/`; pure logic lives in `lib/`;
persistence is JSON files under `.data/` via `lib/*.server.ts`, plus
`localStorage` for client state.

The spec describes an event-driven microservice org chart (CEO AI → CIO
AI → CRO AI → …). **That is a description of responsibilities, not a
deployment topology to build.** Those responsibilities are already
implemented as modules. Do not rebuild them as separate services — the
spec's own Master Prompt says "Do NOT rebuild existing systems. Reuse
existing modules."

### Two data stores, both real — do not "consolidate" them

This used to say `db/schema.sql` was a future migration target that nothing
read. **That is no longer true**, and believing it leads directly to
reading the wrong book:

- **Postgres** is live and authoritative for the **backend agent**.
  `backend/core/db.py:init_db()` runs at startup and applies
  `db/schema.sql` (31 tables). The agent's fills, decisions, reflections
  and risk events all land there — thousands of rows. `asyncpg`,
  `DATABASE_URL` in `.env`, and LangGraph's `AsyncPostgresSaver`
  checkpointer.
- **JSON under `.data/`** is authoritative for the **browser**: manual
  paper trades, watchlist, client config, via `lib/*.server.ts` and the
  Next `/api/*` routes.

These are two different actors, not a duplication. `/api/catalog/orders`
returns a `source` field naming which one answered, because a page that
shows one book under a heading implying the other is the failure this
split has already caused once.

**`schema.sql` must stay idempotent** (`CREATE ... IF NOT EXISTS`, seed
`INSERT ... ON CONFLICT DO NOTHING`). `init_db` applies it on *every*
startup. It used to apply it only when the `trades` table was absent,
which meant `execution_quality` — added to the schema later — was never
created, and every write to it failed after the order had already
reached the exchange.

**Positions are persisted — and NOT in the `positions` table.**

This used to read "nothing writes to the `positions` table, put the
backend's book there." **That instruction was wrong and following it
would have corrupted both books.**
`lib/portfolioStore.server.ts::saveBook` writes `positions` — with a
`DELETE FROM positions` that replaces the *browser's* whole book, because
"absent from the payload" is how the browser expresses a close. A second
writer there means the operator's next save deletes every position the
agent holds, and the agent's next write resurrects a position the
operator just closed. Same two-actors-two-books rule as above, one level
down.

So the backend owns three of its own tables (`db/schema.sql` SECTION 3b):

- `monitored_positions` — the **live stop-loss watch list**, written by
  `PositionMonitorAgent` after every change and reloaded by `restore()`
  from `backend/main.py`'s lifespan. This is the safety-critical one.
  Rows are **deleted on close**: the table answers "what must be watched
  right now?", and closed history already lives in `trades` /
  `decisions` / `reflections`.
- `agent_positions` / `agent_paper_account` — the backend paper book,
  written through by `backend/services/portfolio_store.py` and reloaded
  by `load_portfolio()`.

`tests/test_post_trade_chain.py`'s two gap tests were **inverted, not
deleted**, and keep their original reasoning; the round-trip coverage is
`tests/test_position_persistence.py`.

**What this still does not fix:** nothing watches while the process is
**down**. Restore narrows the window from "forever, silently" to "the
length of the restart, and we know what we were holding". Only a resting
stop order at the exchange closes it, and `execution_agent` says plainly
that it does not place one. Do not let a docstring here start implying
otherwise.

Two conventions this cost a real bug to learn:
- `monitored_positions.opened_at` is `timestamptz`, so asyncpg returns an
  **aware** datetime while the whole codebase is naive-UTC
  (`utcnow()`). `position_store._as_naive_utc` converts at the storage
  boundary. Without it `_close`'s `held = utcnow() - opened_at` raised
  *after* the exchange had already filled the close, so the position
  never left the watch list and was re-closed on every tick.
- Persist **before** publishing `POSITION_CLOSED`, not after.

### The browser talks ONLY to Next.js. Never to FastAPI, never to an exchange.

```
Browser --https--> Vercel (Next) --http--> FastAPI --> Binance / Yahoo / news / LLM
        same-origin              server-to-server
```

Two independent constraints force this and **either one alone is
sufficient**, so do not "simplify" it away:

1. **Vercel's region is refused by Binance.** A route handler runs in a
   Vercel-chosen region; from a US one, Binance returns `451 Unavailable
   For Legal Reasons`. No retry, key or header fixes it — the caller's
   *location* is refused. Every third-party market call therefore lives
   in `backend/api/marketdata.py` and the Next route is a thin proxy.
2. **Mixed content.** The site is https and the backend has no TLS
   certificate. A browser on an https page refuses `http://` and `ws://`
   outright. It is a browser policy — no CORS header or fetch option
   permits it, and a WebSocket cannot be proxied through a Vercel
   serverless function.

That second one had broken far more than the market data: 18 pages via
`useBackend`, pause/resume, the live-trading toggle, the Polymarket
panel and **the emergency stop** were all firing requests the browser
discarded, with no error anyone would find.

Consequences to respect:

- `backendUrl()` is now **`serverOnlyBackendUrl()`**, so a browser-side
  call is a compile error rather than a silently blocked request.
  Components use `backendProxyPath()`; `app/api/backend/[...path]/route.ts`
  forwards anything to the backend and attaches `TRADES_API_KEY`
  server-side, so the browser never holds the secret.
- **There are no WebSockets in the frontend.** Live prices and agent
  events are polled (`/api/ticks`, `/api/agent-events`, 2s). Only the
  last hop is polled — the backend still holds a real-time Binance
  socket and a cursor-based event buffer. The backend's WS endpoints are
  kept for direct/local clients and are the transport to return to once
  the backend has TLS. See `docs/DEPLOYMENT_NETWORKING.md`.
- `/api/marketdata/*` is the DASHBOARD's spot data. `/api/market/*` is
  the AGENT's ccxt **futures** view. They look interchangeable and are
  not — pointing one at the other silently swaps the market.
- **The operator's exchange path moved too**, to
  `backend/api/operator_exchange.py` at `/api/operator/exchange`. Read
  its docstring before touching it — it is the only HTTP route in the
  system that places a real order.
  `backend/api/exchange.py` stays read-only and its warning still
  stands; the order route is a **separate module** so nobody finds order
  placement while reading the read-only one. Two planes:
  the AGENT's (Supervisor → CRO → `TAR_APPROVED` → ExecutionAgent,
  `LIVE_TRADING`-gated, risk-checked, unreachable over HTTP) and the
  OPERATOR's (a human's own keys, per request, unsupervised by
  invariant 1). What makes the second acceptable: write auth on every
  route, credentials never stored, no kernel/bus registration, listed in
  `FORBIDDEN_IMPORTS`, and every order persisted as
  `origin_tag='manual-click'`. `tests/test_api_surface.py` asserts the
  separation and the auth — if a route there loses its auth dependency,
  that test fails, and it is the most important assertion in the file.

### Provider tree matters

`app/layout.tsx` nests providers in a specific order, and React context
only flows **downward**. This has real consequences that have already
bitten:

- `components/Supervisor.tsx` sits **above** `AppStateProvider`, so it
  **cannot** call `useAppState()`. Config it needs (risk limits, real
  starting capital, second-opinion model) lives in
  `components/TradingControls.tsx`, which is mounted above it precisely
  for this reason.
- `components/AutonomousTrader.tsx` sits **below** `AgentProvider`
  because it calls `startAgent()`.
- Memory/Reflection data cannot currently reach `Supervisor.tsx` for this
  same structural reason. That is a known, documented gap — see
  `docs/07_MEMORY_SYSTEM.md`. Do not "fix" it by restructuring the tree
  without checking every provider's dependencies first.

Before adding a provider, work out where it must sit and say so in a
comment.

### The ref-in-interval pattern

Any provider running a `setInterval` created once (e.g.
`useEffect(..., [hydrated])`) must read live values through refs
refreshed every render, never by closing over state directly. Otherwise
it permanently reads mount-time values. See `components/Agent.tsx`'s
`ticksRef`/`getCandlesRef` and copy that pattern.

---

### The bus delivers an event to EVERYONE before the events it causes

`MessageBus.publish` queues a publish made while a delivery is in flight, rather
than recursing into it. That ordering is load-bearing, not tidiness.

It used to deliver inline, so a subscriber publishing from inside its own handler
ran that nested delivery to completion before the OUTER event reached the
subscribers behind it. `main.py` builds `ExecutionAgent` before
`PositionMonitorAgent`, so on every single approved trade:

```
publish(TAR_APPROVED)
  -> ExecutionAgent      fills, publishes ORDER_FILLED inline
       -> PositionMonitor sees the FILL with an empty pending map
       -> "UNPROTECTED POSITION ... will NOT be monitored"
  -> PositionMonitor     finally receives TAR_APPROVED. Too late.
```

Every position opened was recorded unprotected, no stop was enforceable on any
of them, and the position never entered the watch list — which is also why the
paper book and `/positions` stayed empty while the event log showed a completed
fill.

Do NOT "fix" a future instance of this by reordering construction in `main.py`.
That works until the next reorder and no test can see it. `tests/test_bus_delivery_order.py`
and the two ordering tests in `tests/test_post_trade_chain.py` pin it; all three
fail against inline delivery, which was verified by reverting.

One consequence to know: node events reach the buffer a beat AFTER the HTTP
response that triggered the run. Nothing is lost — a poll issued in the same
millisecond as the response will just miss them, which cost an hour of chasing a
non-bug.

### The specialists have real feeds now

`orderflow`, `liquidity` and `news` used to be hardcoded `available=False` with
reasons saying no depth/tape/news feed was subscribed. Those reasons were true of
the GRAPH and false of the SYSTEM — `api/marketdata.py` had been serving Binance
`/api/v3/depth`, `/api/v3/aggTrades` and four RSS feeds to the dashboard the whole
time.

That was not one missing opinion. `run_debate` scales its verdict by COVERAGE, and
those two directional specialists carry 3.0 of 7.0 weight, so coverage was pinned
at **0.57** and every decision the agent ever made was multiplied by it. Live runs
landed on 0.159, 0.181, 0.196 against a 0.18 minimum — whether the agent traded at
all was decided by rounding. Coverage is now 1.0.

The fetch happens in `validate_market_data` (the single fetch point, Section 39.4)
and lands on `MarketSnapshot`; the specialists are pure readers. Do not move the
fetch into a specialist — a node that fetches its own data is not replay-safe, so
a resumed checkpoint would reason over a different order book than the original run.

Math lives in `algorithms/microstructure.py` and `algorithms/news_sentiment.py`,
both with stated confidence ceilings: a seconds-long tape cannot carry full
conviction about an hours-long position, and a keyword lexicon is not comprehension.

### Reasoning models bill their scratchpad against `max_tokens`

Every model this project is pointed at returns `reasoning_content` alongside
`content`, and the thinking is charged first. A node asking for 400 tokens because
it wants three sentences got `finish_reason="length"`, a full scratchpad and an
EMPTY answer — a 200 OK that reads as "the model had nothing to say".

Call sites pass `request_budget(n)` from `backend/llm/provider.py`, which adds
headroom for the scratchpad on top of the answer length. Do not pass a bare answer
length, and do not make `complete()` add the headroom invisibly — `max_tokens`
must keep meaning `max_tokens` at the call site.

Timeouts are PER TIER for the same class of reason. Measured on this account:
`openai/gpt-oss-120b` 1.4s, `gpt-oss-20b` 2.6s, `moonshotai/kimi-k3` **68-85s**.
The old single 90s ceiling made kimi-k3 lose the race on every real prompt, and
every LLM node reported itself unavailable — which reads from outside as "the
agent ignores the model".

### "Empty result" and "the call failed" are different, and conflating them kills retries

`ExchangeClient.fetch_usdt_perpetual_prices` returned `{}` for BOTH, so
`market_data.fetch_prices`'s retry loop — which retries on exception — was dead
code, because the callee swallowed every exception. A transient ccxt failure
(`'str' object has no attribute 'keys'`, seen live when Binance returns a
non-JSON body) was reported as:

    Price refresh produced no usable symbols ... is now STALE

which describes a symbol-FILTER mismatch and sends you to the wrong file. It now
returns `None` for a failed call and `{}` for "answered, matched nothing"; only
the first is retried. `tests/test_price_cache.py` pins both halves.

### The Research Agent read a key that never existed

`scan_market` did `mtf.get("overall", "Mixed")`, but
`run_multi_timeframe_analysis` sets `features["multi_tf_trend"]`. The default was
therefore always taken, `trend` was permanently `"Mixed"`, and both branches that
set a setup were unreachable — the agent had never discovered a single setup, and
said so in a line that reads like a market observation:
*"Research Agent failed to find any setups."* With the key corrected the same five
symbols immediately produced three "Strong Short Setup" rows.

It now defaults to `"Unknown"` with a warning rather than `"Mixed"`, because
"Mixed" is a real verdict this analysis returns and defaulting to it is what let
the bug hide. The scan also goes through `services/upstream.fetch_json` now: it
previously had `if resp.status_code == 200:` with no `else`, so a 451 or 429 fell
through to `return []` with no log at all, and its one error line printed as
`"Error scanning market: "` because several httpx exceptions stringify to empty.
Both appeared in the live log. `tests/test_research_scan.py` covers all of it.

### The test suite must not read the operator's real LLM config

`tests/conftest.py` clears `LLM_*` / `OPENAI_API_KEY` for every test. Before that,
tests asserting "no provider is configured" passed because nothing WAS configured,
not because they had isolated anything — and the moment a real key went into
`.env` two of them started issuing live HTTP requests. Only the network guard
caught it.

### `get_position_monitor()` and `get_execution_agent()` are singletons NOW

Both used to `return Agent()` — a new, empty object on every call — while
`main.py` built one at startup, subscribed it to the bus and treated it as the
system's book. Every other caller got a different object, and two things broke
silently:

* `GET /api/graphs/positions`, whose docstring calls the monitor "the single
  source of truth on what is open", constructed a fresh empty monitor and
  returned `count: 0` FOREVER. The positions view was not showing an empty book;
  it was showing a different book that could never fill.
* `ExecutionAgent._last_prices` fills from TICK_RECEIVED, so a fresh instance had
  seen no ticks and refused every simulated fill with "no observed price for X
  yet" — which reads as a market-data fault.

`reset_position_monitor()` / `reset_execution_agent()` exist for tests, and
`tests/conftest.py` calls both around every test so one test's open positions
cannot become the next one's starting book.

### `simulation_mode` is a property, read at call time

`ExecutionAgent.simulation_mode` was assigned once in `__init__` from
`settings.LIVE_TRADING`, while `config.set_live_trading`'s docstring claimed the
opposite ("reads it at call time, so the next trade attempt sees the new value").
`main.py` builds the agent once, so the mode was frozen at process start and the
Settings-page toggle changed nothing:

    OFF -> ON   orders keep being simulated; the operator believes they hold
                positions that do not exist.
    ON -> OFF   the operator disables live trading, is told it worked, and REAL
                ORDERS KEEP BEING PLACED until a restart.

A safety control that reports success while doing nothing is worse than none.
An explicit `simulation_mode=` argument still pins the value for the life of the
agent — the backtest engine depends on that and must never follow a mid-run
toggle. `tests/test_live_trading_toggle.py` pins both directions and the override.

### The pipeline view is SEEDED from the last run, not just the live stream

`lib/realtime/usePipeline.ts`. The event stream is a catch-up feed: it opens with
no cursor and the backend deliberately answers that with the head and NO backlog,
so a freshly loaded page knows nothing. Every node rendered IDLE under "No graph
node is running" — true, and useless, because the agent may have finished a
23-node cycle seconds earlier. Between cycles, which is most of the time, the
operator's answer to "what is my agent doing?" was a blank pipeline.

It now seeds from `/api/graphs/runs`, which stores each node's duration, what it
wrote and any error, and the live stream overrides it per node. `source` and
`ageSeconds` are surfaced so a finished run is never rendered as one in progress.

**The run query MUST filter by graph.** The monitoring graph runs once per tick
per open position, so an unfiltered `limit=1` returns a `position_monitoring`
trace essentially always — twelve nodes whose names do not appear in Graph 2, so
nothing matched and the diagram stayed exactly as empty. `/api/graphs/runs` takes
a `graph` parameter for this.

### The operator's manual trade panel

`components/operator/TradePanel.tsx` -> `backend/api/operator_trade.py`. Paper
trades go to `/api/operator/trade/paper`; with `LIVE_TRADING=true` that route
REFUSES (a Buy pressed in live mode means a real order, and booking a simulated
one instead would leave the operator believing they hold something they do not)
and the panel posts to `/api/operator/exchange/order` with the operator's keys.

`GET /context` answers price, balance, leverage ceiling and instrument rules in
ONE call, and the authenticated balance behind it is cached 30s — a panel that
re-read the balance per keystroke would spend the venue's rate limit on a form
nobody had submitted.

Registration with the stop-loss watcher goes through
`PositionMonitorAgent.track_manual_position`, NOT through synthesised
TAR_APPROVED / ORDER_FILLED events. Publishing those makes the AGENT's executor
place a second order, its fill consumes the pending entry, and the hand-published
one is then logged as an UNPROTECTED POSITION — a false alarm on every manual
trade. A TAR also means "the CRO approved this", and minting one for a human's
click would put a fabricated approval in the audit trail.

### `get_price` reads three caches, and the third was missing

`live_market_data` (agent socket), `ticker_stream` (dashboard socket), then the
polled ccxt cache. Source 2 was not consulted, and for the first stretch after
startup 1 and 3 are both empty while 2 is already ticking — so a graph run in
that window aborted at `data_validation` with "no live price for BTC/USDT (feed
returned 0.0)" while `/api/marketdata/ticks` served a price for the same symbol
at the same moment. `price_for` refuses a tick past the stream's staleness
threshold rather than handing back a minutes-old price.

### One `.env`, and the password that was hiding in the second one

There were three. `.env.backup-before-enable` was TRACKED IN GIT and held the
real Postgres password — the only place it existed, so `.env` had the wrong one
and the database had been failing to connect, which is what emptied the
Decisions, History and Learning pages and reset the paper book every boot.
`.env.example` had drifted and documented different variables. Both are gone and
everything is folded into `.env`, which is gitignored.

**That password is still in git history.** Deleting the file does not remove it.

### `/api/chat` runs on NODE, and putting it back on Edge breaks "Ask Agent"

The operator saw one error, on one page:

    Upstream error 403: Direct IP access is not allowed in Vercel's Edge
    environment (hostname: <backend ip>)

That text is written by VERCEL. Its Edge runtime refuses `fetch()` to a bare IP
literal and will only dial a DNS hostname, and `BACKEND_INTERNAL_URL` on this
deployment is `http://<ip>:8000`. The request never reached the backend or the
model provider, so every field an operator would check — API key, model, base
URL, backend health — was fine, and the 403 read as the LLM rejecting the request.

`/api/chat` was the ONLY edge route in the app, which is exactly why chat was the
only broken page while market data, positions and the pipeline all kept working.
A whole-app outage gets found in minutes; one page looked like a chat bug.

The original reason for Edge — that Vercel's Node functions buffer the whole
response — was true of the older Serverless Function model and is not true of
App Router route handlers today; the SSE passthrough is unchanged and still
streams. `lib/chatUpstream.ts` now also DIAGNOSES that 403 by its exact phrase
rather than echoing a hostname, and `lib/chatUpstream.test.ts` fails if the
`runtime` line changes — the bug is invisible in local dev, because `localhost`
is a hostname and edge dials it happily.

### A trade row carrying a `pnl` IS a realized close — the dashboard depended on it

`/api/stats` returned `totalClosedTrades: 0` — every P&L panel, the win rate and
expectancy all blank — on an account whose trade log rendered fine one page over.
`reconstructClosedTrades` had two independent faults with that identical symptom:

  1. **Long-only.** `buy` opened, `sell` closed. This agent trades perpetual
     futures and takes SHORTS, which open on a sell and close on a buy. Every
     short hit the sell branch, found no open state and was skipped by a bare
     `continue`; its closing buy then registered as a brand-new long. Shorts were
     not merely uncounted — they corrupted the running quantity of any long later
     opened on the same symbol.
  2. **It required the opening leg to be in the window.** A close whose open
     predates the log — after any restart, any retention trim, and for every
     position the operator already held — was discarded ENTIRELY, realized P&L
     included. That P&L is authoritative: it was computed at close time against
     the real entry. Discarding it in favour of a pairing walk that failed is
     choosing a reconstruction over a fact.

The rule is now structural, not conventional: the four writers of `trades`
(`execution_agent`, `operator_trade`, `operator_exchange`, `tradeStore.server.ts`)
all insert opening fills with `pnl` ABSENT, and only close paths supply one.
`pnl: 0` is a real break-even close and counts; absent is an open and does not.
The ledger walk survives, demoted from GATE to ENRICHMENT — it supplies hold
time, entry context and direction, and when it fails `paired: false` with
`holdMinutes: null`, never 0. `averageHoldTime` therefore reports a sampleSize
that is NOT `closed.length`.

`TradeLogEntry['originTag']` was also missing `agent-close` and `manual-panel`,
two of the four tags Python actually writes, so real trades grouped under a tag
TypeScript had no name for.

### Volatility history lives in a FRONTEND FILE, capped at 15 — not in Postgres

The obvious home for "volatility history, for learning" is a table, and that is
deliberately not where it is. The volatility node runs on every analysis cycle
AND every monitoring tick per open position — thousands of rows a day, of which
only the few attached to a trade carry a lesson, in a database the operator is
keeping small on purpose.

So: `backend/services/volatility_journal.py` is an in-memory ring (200, cleared
on restart, **write-only with respect to the graph** — nothing reads it back,
which is what keeps `volatility_analysis` replay-safe under Section 39.4), read
through `GET /api/graphs/volatility`. The DURABLE record is
`.data/volatility-history.json` via `lib/volatilityHistoryStore.server.ts`,
capped at `MAX_ENTRIES = 15`, keyed `<runId>:<symbol>`.

Two things that cost real bugs elsewhere and are pinned by tests here:

* The cap is enforced ON WRITE. `pvHistoryStore` does the opposite and says why —
  an equity curve truncated at the front loses the points a drawdown-from-peak
  needs. That reasoning does NOT carry over: a volatility reading is a
  point-in-time observation accumulated into nothing, so trimming on read would
  just let the file grow forever.
* Re-polled readings UPDATE IN PLACE. The ring is polled, so the same reading is
  offered repeatedly; appending would fill all 15 slots with one reading within
  seconds and evict the genuine history.

The backend stamps `time.time()` (seconds) and the frontend renders milliseconds;
`toEntry` converts at the boundary. Without it a reading is stamped ~55 years ago,
sorts last, is evicted immediately, and it reads as the agent having stopped
measuring volatility.

### The pre-execution chain, as actually traced

Graph 2 (`trade_analysis`) is 24 nodes, and a live run visits all of them:

    ... regime_detection -> volatility_analysis -> strategy_scoring
     -> opportunity_detection -> 9 specialists (market, orderflow, liquidity,
        news, funding, portfolio, risk, prediction, event_risk)
     -> debate -> supervisor -> risk_gateway -> external_consultation
     -> trade_thesis_narrative

`external_consultation` was silently OFF: `LLM_CONSULT_PANEL` was unset, so the
node recorded "no panel configured" on every uncertain decision instead of asking
anything. It is now `nvidia`/`openai/gpt-oss-120b` — deliberately NOT kimi-k3,
which is already the reasoning tier, because consulting the same model is one
prior sampled twice and reporting that as a second opinion manufactures
agreement. It stays advisory by CONSTRUCTION: it runs after the Supervisor and
the Risk Gateway, writes only `consultation`, and no gate reads that field.

### The pipeline showed a different coin than the trade being executed

Three causes, all in `lib/realtime/store.ts`, all invisible because the diagram
carried no instrument label at all.

1. **The live node map was shared by every graph.** `route()` discarded `graph`,
   `run_id` and `symbol` — which the backend has always sent — and merged every
   node event into one flat `nodes` map keyed by node NAME. `position_monitoring`
   and `trade_analysis` **share six node names** (`memory_loader`,
   `data_validation`, `feature_generation`, `market_analysis`,
   `regime_detection`, `market_state`), and monitoring runs once per tick per
   open position. So a monitoring tick on BTC constantly overwrote those six
   entries mid-cycle, and a decision run on SOL rendered half from another coin.
2. **Nothing reset the map between runs.** A finished cycle's nodes stayed and
   mixed with the next one's — two cycles displayed as one pipeline, with the
   stale half indistinguishable from the live half.
3. **The seed was not filtered by symbol.** `usePipeline` asked for "the most
   recent `trade_analysis` run", whatever instrument that was.

State is now `graphRuns: Record<graph, GraphRunState>`; a new `runId` within a
graph REPLACES that graph's node map rather than merging; `/api/graphs/runs`
takes a `symbol` filter; `usePipeline(graph, { symbol })` pins the view, and the
four pages that render a pipeline pin it to the running session's coin via
`useActiveSessionSymbol()`. `pipelineSourceLabel` now names the symbol in EVERY
branch, so an unpinned view is readable rather than merely ambiguous, and the
dashboard flags "(not your session)" when they differ.

`mergeNodeStates` takes `NodeDisplayState` (status/detail/duration) rather than
the full `GraphNodeState`: a replayed trace and a selected historical run have no
run identity to offer, and demanding it would have them invent placeholders.

Four tests in `lib/realtime/store.test.ts` pin the reducer.

### A session's start and target are ACCOUNT amounts, not coin prices

"$2 into $5" is a statement about the WALLET. The session ends when the account
reaches the target, whatever the coin is worth then. A price target would say
nothing about how much was staked, so the same move could double the account or
barely touch it.

Where the starting figure comes from differs by book, deliberately:

- **paper** — the operator types it, and `set_paper_starting_amount` WRITES it to
  the paper book's cash. Recording 2.00 on the session while the book held 10,000
  would leave every downstream number about the 10,000: the Risk Gateway sizes a
  percentage of ten thousand, one position exceeds the whole notional stake, and
  the progress bar barely moves. It is REFUSED while paper positions are open —
  rewriting cash underneath a position leaves equity that is part old-basis and
  part new.
- **real** — `real_account_balance()` reads free USDT from the exchange and it is
  NOT typeable. A typed figure would be the denominator of every percentage the
  session reports while the venue held a different number. Cached 30s because
  `/api/session` is polled every 5s and the balance only moves on a fill; `None`
  never `0.0` when unreadable, because "no money" and "could not ask" are
  different facts.

`current_equity('real')` now falls back to that balance. It used to return None
forever — the local store has never held a real cash figure — so a real session
was unstartable and the panel said "equity is not measurable" indefinitely.

`target_equity` still reaches only the stop check. `tests/test_session_amounts.py`
asserts that structurally by scanning the module's own source.

### Truncating the book's tables on a RUNNING backend does nothing

The operator reset their database, and the dashboard still read "Open positions:
1". That is the expected outcome, not a bug in the reset:

* `portfolio_store` keeps the book in a module-level `_portfolio` dict, and
  `_persist()` REPLACES `agent_paper_account` / `agent_positions` from memory
  after every write. The deleted position is back on the next fill.
* `PositionMonitorAgent` keeps the watch list in `self._open`, and
  `save_watch_list` is a `DELETE` + re-`INSERT` of everything it is holding.

So a reset has to clear MEMORY first and let it persist down to empty. That is
`POST /api/admin/reset-paper` (`clear_all` on the monitor, then
`update_portfolio`). Doing it by hand works only if the backend is stopped, or
restarted afterwards so `load_portfolio()` re-reads the empty tables.

**It refuses while `LIVE_TRADING` is on, and that refusal is the most important
line in the route.** `clear_all` stops watching without closing anything — right
when the whole book is being discarded, catastrophic when the position is real and
still open at the venue with nothing enforcing its stop. The check runs before
anything is cleared. Decisions, reflections, traces and volatility history are
deliberately untouched: they are the record of how the book reached the state
being reset.

### The venue layer: Binance and Bybit, and the four things that were missing

`backend/services/venue.py` replaced a hardcoded `ccxt.binance(...)` that sent
market orders carrying nothing but a `clientOrderId`. Enough to open a position;
not enough to trade real money. Each of these is a way to lose money, not a rough
edge:

1. **Leverage was never set on the venue.** The Risk Gateway sized for the
   operator's chosen leverage; the exchange used whatever its UI was last set to.
   `ensure_leverage` now runs before every entry and a refusal ABORTS the trade —
   filling at a leverage we know is wrong is trading on a false number.
2. **Closes had no `reduceOnly`.** A close was just an opposite-side order, and
   any surplus over the live size OPENS a position the other way.
3. **Sizes were not rounded to the venue's filters.** A size below the minimum is
   REFUSED, never rounded up — bumping up would stake more than any gate approved.
4. **Nothing compared the local book to the venue's.** See reconciliation below.

**Two clients, and the split is load-bearing.** `public` carries no credentials
and serves all tickers, candles and market metadata; only balance, positions,
leverage and orders use `private`. The key's rate budget is what places orders,
and spending it on price polls throttles the request that matters.

**The parameter matrix is where the venues genuinely disagree** and is centralised
in `_order_params` so no call site can get half of it right:

    Binance one-way : reduceOnly, no positionSide
    Binance hedge   : positionSide — and NO reduceOnly (Binance REJECTS it)
    Bybit  one-way  : positionIdx 0 + reduceOnly
    Bybit  hedge    : positionIdx 1/2, acting on the leg OPPOSITE the order side
    Bybit idempotency key is `orderLinkId`; `clientOrderId` is ignored

Getting the hedge close backwards does not error — it opens a second position on
the other leg. `EXCHANGE_ID` selects the venue; credentials are per-venue
(`BINANCE_*` / `BYBIT_*`) because these are different accounts holding different
money.

### The stop-loss now RESTS AT THE VENUE

CLAUDE.md used to say plainly that nothing watches while the process is down, and
that only a resting stop order closes that window. It exists now.

`PositionMonitorAgent._place_resting_stop` places a reduce-only stop-market at the
venue on every REAL fill (paper gets none — there is no venue order behind a
simulated fill). Three properties, each pinned by `tests/test_resting_stop.py`:

* **Placed on the EXIT side.** A long's stop sells. On the entry side it would add
  to the position at the stop rather than close it.
* **Tightening CANCELS then places** — never the reverse. Two live reduce-only
  stops means the second, after the first fires and flattens, is an order to OPEN
  the opposite position. A brief gap with no stop is recoverable; that is not.
* **Cancelled when the position closes.** A stop left resting on a flat account is
  an order to open a reversed position the next time price touches it. A failed
  cancel is CRITICAL and KEEPS the id — it is the only handle on that order.

`monitored_positions.stop_order_id` persists it, so a restart can still cancel the
stop this process left behind.

A refused stop does NOT reject the position: it is already open and the money has
moved, so refusing to track it would leave it open AND unwatched. It logs CRITICAL
instead, because the operator is then relying on this process staying up.

**THE TAKE-PROFIT RESTS TOO, as the mirror of the stop.**
`_place_resting_tp` -> `Venue.place_take_profit` places a reduce-only take-profit
beside the stop on every real fill (`monitored_positions.tp_order_id` persists it,
and both legs are cancelled on close). Without it, a favourable move that reaches
the target while the process is restarting is simply MISSED — the position rides
back through it and the monitor, once alive, has a smaller or negative unrealised
to act on. The TP makes the target as durable as the stop.

Two asymmetries with the stop are deliberate:
  * A REFUSED TP is a WARNING, not CRITICAL. An unprotected downside is a loss
    that runs; a missed target is only an upside not captured while down, and the
    in-process monitor still takes it the moment the process is alive. The stop is
    the safety-critical leg; the TP is the profit leg.
  * The two are NOT linked as exchange OCO. Both are reduce-only, so if one fires
    while the process is down the other cannot open or reverse a position — it can
    only close, and closing a flat account does nothing. That reduce-only backstop
    is why they can rest together safely; the in-process close still cancels both
    when alive. ccxt does not expose OCO cleanly for market orders on either venue,
    and it is not needed given reduce-only.
`place_take_profit` uses ccxt's unified `takeProfitPrice` (TAKE_PROFIT_MARKET on
Binance, side-derived trigger on Bybit, mark-price trigger on both) — the exact
mirror of `stopLossPrice`, and it avoids the bare-`triggerPrice` generic-trigger
bug that once stopped the Bybit stop reaching the venue at all.
`tests/test_resting_stop.py` (both legs placed on a fill, both cancelled on close,
tp_order_id survives a watch-row round trip) and `tests/test_venue_live_path.py`
(the unified param, mark price, reduce-only, resolved perpetual) pin it. Binance
order placement of the TP is unverified for the same reason the stop's is — ccxt
dropped Binance futures testnet.

### Reconciliation REPORTS, and must never repair

`backend/services/reconciliation.py` asks the venue what it holds and compares.
It runs once a minute, only while `LIVE_TRADING` is on, and is read through
`GET /api/graphs/reconciliation`.

It never closes, opens or forgets a position, and `tests/test_reconciliation.py`
asserts that against the module's own source. Every automatic "fix" is itself a
trade: forgetting a local position abandons a real one if the venue read was
stale, and closing an unknown venue position fires a market order nobody asked for
— possibly on the operator's own manual trade in the same account.

**`None` from `open_positions()` is not an empty book.** None means "could not
ask"; `[]` means "the venue holds nothing". Collapsing them would flag every real
position as a phantom on one timeout, and anything acting on that report would
flatten the book on a network blip.

### THE AGENT NEVER UPDATED THE PAPER BOOK, and that was four bugs in one

`execution_agent` wrote a row to `trades`, handed the position to
`PositionMonitorAgent`, and never touched `portfolio_store`. Only the OPERATOR's
manual panel ever moved the book. On the paper account that produced four
separate "broken panels" from one absent write:

* cash sat at its starting figure forever, however many trades filled
* `positions` stayed empty, so the dashboard's "Open positions" read 0
* there was no unrealized P&L to move when price moved
* `current_equity()` is cash + marked positions, so a session's progress toward
  its target never moved either

And it could not simply call `buy_paper`, which is LONG-ONLY: this agent shorts,
`sell_paper` rejects a sell with no long open, and its P&L formula is
(exit - entry) — the opposite sign for a short. So
`portfolio_store.apply_paper_fill` is the general, direction-aware, margin-aware
implementation, and `buy_paper`/`sell_paper` stay as the operator's manual API.

**Positions now carry a `side`** (`agent_positions.side`, defaulting to `'buy'`
so pre-existing rows read as the longs they were). Without it the book cannot
represent a short at all.

### Equity is FREE CASH + LOCKED MARGIN + UNREALIZED — `cash + qty*price` is 1x-only

`buy_paper` deducts MARGIN from cash, so cash is free cash. Adding the full
notional back double-counts the leveraged part: at 10x a $7,000 position funded
by $700 reported **$6,300 of equity that did not exist**, and every percentage
derived from it — session progress, drawdown, risk-per-trade — was wrong by the
same factor, in the direction that flatters the account. The old form also
ignored DIRECTION, so a short moving against the operator read as equity going
UP.

Fixed in `portfolio_store.book_equity` and mirrored in `lib/api/portfolio.ts`'s
`equity()`/`unrealised()`. `tests/test_trading_session.py`'s equity test had been
ASSERTING the buggy formula and was rewritten.

### `decisions` grew to 80,525 rows in one day and nothing pruned it

All of them `rejected` — the agent records a decision on every evaluation it
declines. `listDecisions` had **no LIMIT**, so the Decisions page fetched every
row and laid them out. That is a growth problem, not a page problem.

Two fixes, both needed: `/api/decisions` caps at `DECISIONS_PAGE_LIMIT` (300) and
returns `total`/`truncated` so a capped view says so; and
`backend/services/retention.py` prunes hourly.

**The retention bound is a COUNT, not an age.** A 14-day window was written first
and would have deleted NOTHING — every row was from a single day. An age window
is sensitive to how hard the agent happened to be working, which is the one thing
the bound must not depend on. `MIN_KEEP = 2000` is both a floor (a quiet week
cannot blank the page) and a cap. Decisions that EXECUTED a trade are never
pruned, and `trades` is never touched at all. First live pass deleted 78,602 and
left 2,003.

### `/log` was deleted in the UI migration and `TradeLogPanel` still linked at it

`components/TradeLogPanel.tsx` — mounted on `/history` via `HistoryOperator` —
linked to `/log/${id}` and `/log?tab=`. `app/log/` no longer exists; the trade
detail page's own header says it is "the replacement for the old /log/[id]". So
every Detail click from that panel hit Next's 404. Repointed at `/history`.

### A fill row does not say whether its position is still open

`trades` is a fill log, not a position log. The history table rendered every row
under a heading reading "Closed trades" with `—` in the P&L column for most of
them, so an open entry and a completed exit looked identical and the em-dash read
as missing data rather than "no result yet, by definition".

`lib/tradeStatus.ts::annotateTrades` walks the ledger per `(tab, symbol)` and
tags each row `role` (open/close), `status` (OPEN/CLOSED), the id of its
counterpart leg, `holdMs` and `direction`. Closes consume open legs FIFO. It
never recomputes P&L from the pairing — the row's own `pnl` was calculated at
close time against the real entry and is authoritative. Must be annotated BEFORE
any tab filter: a row's status depends on the whole ledger, and filtering first
orphans every close whose entry sits in the other book.

### A CHECK constraint silently discarded every closing trade

This is the one that blanked the entire P&L dashboard, and it was in the SCHEMA,
not in any of the code that reads it.

`trades_origin_tag_check` permitted five `origin_tag` values. The code writes
SEVEN, and the two missing ones were the two that mattered:

    agent-close    `position_monitor._persist_closed_trade` — the ONLY writer of
                   a row carrying a realized `pnl`
    manual-panel   `api/operator_trade` — a manual paper trade

A CHECK that omits a value the code emits does not degrade. It REJECTS the
INSERT. Every close the agent ever made was rolled back with

    new row for relation "trades" violates check constraint
    "trades_origin_tag_check"

logged at ERROR and swallowed — correctly, because by then the position was
already closed and the money had moved, so raising would have made the caller
retry an exit for a flat position. **The positions closed correctly every time;
only the record of them was lost.**

The symptom appeared three pages away and looked like arithmetic: `trades` held
nothing but opening fills, `realised()` found no row carrying a pnl, and the P&L
panel, win rate, expectancy and max drawdown were all blank on an account that
had been trading. Sessions were spent auditing the reconstruction logic that
reads this table.

**`CREATE TABLE IF NOT EXISTS` CANNOT WIDEN A CHECK ON A LIVE TABLE.** That is
the trap: adding the tag to the CREATE block does nothing to an existing
database, exactly as it did nothing for `execution_quality`. The fix is a
`DO $$ ... $$` block that DROPs and re-ADDs the constraint, which is idempotent
under `init_db`'s apply-on-every-startup.

`tests/test_origin_tag_constraint.py` compares the three copies of this list —
the schema CREATE block, the schema constraint block, and
`TradeLogEntry['originTag']` — and scans the writers for tags none of them
permit. Its first run immediately caught a false positive
(`manual-position-tracked`, a `record_decision` kind), which is why the scan is
scoped to `INSERT INTO trades` windows rather than matching hyphenated literals.

Verified end to end after the fix: a monitored long, stop fired, and the log
recorded `manual-panel` entry -> `agent-close` exit carrying a pnl, with
`/api/stats` reporting `totalClosedTrades: 1` and a measured hold time for the
first time.

### The trade waited ~18 seconds for prose about itself

`run_analysis_graph` did `await graph.ainvoke(...)` and published
`EXECUTION_PLAN_READY` afterwards — so the order reached the bus only once all 24
nodes had finished. Measured on a live BTC/USDT run:

    trade_thesis_narrative   7.6s -> 10.2s   LLM
    external_consultation    2.4s ->  8.3s   LLM
    data_validation          2.3s            upstream fetches
    memory_loader            0.8s
    the other 20 nodes       0.04s           combined

Both LLM nodes run AFTER `risk_gateway`, and neither contributes to the trade.
Their contracts say so structurally — `trade_thesis_narrative` writes
`("thesis_narrative",)`, `external_consultation` writes
`("consultation", "llm_calls_made", "llm_tokens_used")`. Not `execution_plan`,
not `risk_assessment`, not `decision`.

The graph is now STREAMED (`astream(..., stream_mode="values")`) and the plan is
published the moment the gateway's output appears. Measured after:

    decision complete, plan on the bus     4.8s
    the two explanation nodes (after)     18.5s
    HTTP response, whole run              23.6s

So the TRADE leaves at ~4.8s instead of ~23.6s. **The HTTP response still takes
the full run** — that endpoint reports the run, and the reporting path is not the
trading path. Do not "fix" that by returning early; the summary needs the final
state.

Three things keep this sound, all in `tests/test_analysis_latency.py`:

* **Publishing stays in the RUNNER, never in a node.** The original reason holds:
  a node that published would make emitting an execution request part of
  reasoning, and a future node could emit one before the gateway ran. The guard
  is still `execution_plan` being present, and only the gateway produces one.
* **Published exactly once.** `stream_mode="values"` re-offers the whole
  accumulated state after every superstep, so the plan appears in every chunk
  from the gateway onward — without the caller's `published` flag, one decision
  becomes a dozen identical submissions. `_publish_plan` returns `bool` for this;
  every refusal path returns False so the post-stream retry still gets a chance.
* **An explanation node may not write a decision key.** Asserted against the
  registered `NodeContract`. If one ever could, the published plan could be stale
  by the time the graph ended — the system would submit one trade and record
  another.

`tests/test_sections_26_to_39.py::test_streaming_is_separate_from_the_invoking_runners`
used to assert `"astream" not in` the runner's source. That was checking the
implementation rather than the property; it now asserts the CONTRACT — that
`stream_run` is an async generator and `run_analysis_graph` is not. The runner
still returns a single dict, so it is still an invoking runner to every caller.

### A hardcoded symbol list meant most instruments had no enforceable stop

`live_market_data` watched a literal `['BTC/USDT', 'ETH/USDT', 'SOL/USDT']`.
`PositionMonitorAgent` enforces every stop by reacting to `TICK_RECEIVED`, and
this module is the ONLY publisher of that event — so a position in any other
instrument received no ticks, `_check_price` never ran for it, and **its stop
could never fire**.

Nothing reported it. The monitor listed the position as watched and the dashboard
showed its stop, so the operator had every reason to believe it was protected.

The subscription set is now derived from what needs watching — open positions,
the running session's symbol, the book, plus `DEFAULT_SYMBOLS` — and reconciled
every 20s, because positions open and close while the process runs. A set
computed once at startup is the same bug with extra steps.

Three properties, pinned by `tests/test_tick_subscription.py`:

* **The set is a UNION and an open position is always in it.** A reconcile that
  dropped a live position's feed would silently disarm its stop, which is worse
  than never subscribing — it looks protected the whole time. A failure reading
  the monitor therefore keeps the existing feeds rather than shrinking the set.
* **A dead watcher is restarted.** A crashed task left in `_watchers` would leave
  its symbol unwatched while still appearing subscribed.
* **Unsubscribing PURGES the cached price.** `get_live_price` carries no
  timestamp, so a value left behind is served forever as a live websocket price
  — `/api/market/price` even labels its source `websocket`. Dropping it makes
  `get_price` fall through to the polled cache, which knows how old it is.

Verified live: opening an XRP/USDT position flipped its price source from
`polled-http-cache` to `websocket` within 10 seconds.

### The stress simulation OOM was REJECTING TRADES, not just failing

    Stress simulation failed for SOL/USDT: Unable to allocate 3.05 MiB for an
    array with shape (2000, 200) and data type float64

`SimulationAgent` FAILS CLOSED by design, so an allocation error is a REJECTED
TRADE. The OOM was manufacturing rejections — and `decisions` held 80,525 rows
that day, every one of them `rejected`.

`monte_carlo_trade_sequence` is SEEDED and otherwise pure, and the agent calls it
with the same arguments every time (`settings.RISK_PER_TRADE` plus two module
constants). It re-derived one number that cannot change, thousands of times a
day, allocating six full 2000x200 float64 arrays (~16 MB) on each call.

Two changes, and the results are bit-identical — verified against a captured
baseline before/after:

* **`lru_cache`.** Exact rather than approximate, because the function is seeded;
  `tests/test_algorithm_library.py` already asserts that determinism. The
  returned dict is now SHARED — callers must not mutate it.
* **Row-blocked simulation** (`_SIM_BLOCK_ROWS = 250`), so peak memory does not
  scale with `num_simulations`. `rng.random` fills row-major, so sequential row
  blocks from one generator yield the same numbers as one big draw.

Measured: peak 16 MB -> 2.78 MB, cold call 37.5ms, cached call 0.24us.

### The trade detail page shows a LIFECYCLE, not one row

`/history/[id]` rendered the clicked fill's price, quantity and timestamp and
nothing else — so an entry leg showed a P&L of `—` with no way to tell that
meant "still running" rather than "missing".

It now annotates the WHOLE ledger (`annotateTrades`) and renders the round trip:
opened/closed timestamps, hold time, direction, entry and exit price, origin,
which leg this fill is, and a link to the counterpart. Absences are dimmed and
worded — "still open", "not in this log" — rather than dashed, because a dash and
a missing measurement look identical.

`usePortfolio().tradeLog` already reads `/api/trades`, the same source the
history table uses, so the page resolves any trade in the database. The 404 was
never a lookup failure — it was the dead `/log/` link in `TradeLogPanel`.

ONE CONSEQUENCE OF `reset-paper` WORTH KNOWING: it forgets positions without
closing them, so it writes no closing row. Entries that were open at reset time
stay OPEN in the trade log forever. That is accurate rather than a bug — they
genuinely never closed — but it means the history can show open legs that the
book no longer has. Writing a synthetic close would mean inventing an exit price
and a P&L, which invariant 6 forbids.

### The three changes that came out of reading the live ledger

Nine closed trades, 33% win rate, +$24.67 net. Five of six losses were stop-outs
of 0.45-0.79% while SOL's 15m ATR% was ~0.43 — the stop sat INSIDE the noise
band. The three wins all landed in one 30-minute trending window.

**1. THE SIZING CAP WAS OVERRIDING RISK SIZING ON EVERY TRADE.**
`max_qty_by_cash = equity * 0.5 / price` — half the NOTIONAL, ignoring leverage.
Measured: the risk sizer wanted 310 SOL, the cap allowed 50, on every trade. Two
consequences, the second dangerous:

  * `RISK_PER_TRADE` did nothing. Actual risk was ~0.3% while it said 2%.
  * Widening the stop would have INCREASED risk, not held it: same quantity over
    twice the distance is twice the loss. 1.5 -> 3.0 ATR doubled risk from $32 to
    $64 while looking like a safety improvement.

Now `MAX_MARGIN_FRACTION_PER_TRADE = 0.20` on MARGIN, which scales with leverage
(the old notional cap got STRICTER as leverage rose). `position_size_detail`
reports whether the cap bound, so a size that ignores the risk setting is visible
instead of silent.

**2. STOP AND TARGET WIDENED TOGETHER.** 1.5/3.0 -> 2.5/5.0. The stop moves
outside the noise band; the target had to move with it because widening only the
stop makes the payoff 1.2:1, which at a 33% win rate is reliably losing. 2:1 is
preserved. `RISK_PER_TRADE` dropped 2% -> 0.5%, which is the level at which risk
sizing GOVERNS rather than the cap — that is what makes a wider stop
automatically reduce size and hold dollar risk constant.

**3. VERY_LOW JOINED `BLOCKED_REGIMES`.** A market in the bottom fifth of its own
recent range has no momentum to carry a position to a 5-ATR target. Fewer trades
is the intended effect.

All three are HYPOTHESES. A wider stop takes fewer noise stop-outs and a wider
target is hit less often; which dominates is empirical and nine trades cannot
say. That is why the learning loop below exists.

### The learning loop — `historical_success_rate` is real now

All nine profiles carried `historical_success_rate=None` and the scorer reported
it on every run. The agent picked strategies purely on current-conditions fit and
nothing it learned from an outcome ever reached that choice.

Two things blocked it, both fixed: `trades` did not record WHICH strategy
produced a fill (`trades.strategy` now exists, carried plan -> TAR -> CRO ->
approval -> row, alongside `trades.run_id`), and nothing aggregated
(`backend/services/strategy_performance.py`).

Scoring gained a fourth weight: signal 0.4, trend 0.25, volatility 0.15,
**track record 0.2**. Conditions still carry 0.8.

**WHY THIS IS NOT AN INVARIANT-5 VIOLATION.** That invariant forbids an
LLM-authored HYPOTHESIS rewriting a strategy (`Loss -> AI rewrites strategy ->
Live`). This is deterministic arithmetic over closed trades: no model is
consulted, the same ledger always gives the same numbers, and it cannot invent,
edit or disable a strategy — it supplies the measured win rate the profile
already declares and Section 11.3 already lists as required for a score. A test
asserts the module contains no model call.

**THE SAMPLE FLOOR IS THE SAFETY ARGUMENT.** `MIN_SAMPLE = 20`. Below it the rate
is reported but must not steer selection — one lucky run would otherwise entrench
a bad strategy, which is how naive adaptive systems destroy themselves. And no
record scores NEUTRAL (0.5), never zero: zero would permanently freeze out every
strategy that has not traded yet, including the one that would have worked.

Read through `GET /api/graphs/strategy-performance`.

### Cash does NOT move while a position is open, and that looked like a bug

The operator reported paper cash "static at 10000". It was not stuck — the panel
showed one figure and that figure genuinely does not move:

    cash $9790.86 | locked $209.14 | unreal $+0.000 | equity $10000.000
    cash $9790.86 | locked $209.14 | unreal $+0.400 | equity $10000.400
    cash $9790.86 | locked $209.14 | unreal $+0.700 | equity $10000.700

Margin is LOCKED, not spent. Cash moves exactly twice per position — out on open,
back plus P&L on close — and everything that moves in between lives in
`unrealized`. A single "Account now" number therefore looks identical whether the
book is FLAT or the number is STUCK, and those have opposite responses.

`equity_breakdown()` returns the three parts and the panel renders them. On an
idle book it reads free cash 10,000 / locked 0 / unrealised 0 / 0 positions,
which is plainly idle rather than broken.

`session_progress()` is computed SERVER-SIDE for the same reason the plan is
published from the runner: the number the operator reads and the check that ends
the session must come from one place. `gained` is signed and unclamped so a
session that is down says so; only `fraction` is clamped, because a bar cannot
render backwards.

### "How this trade happened" — the middle was never recorded, not lost

The journey view showed market data and execution with an unknown middle. Not a
display bug: `trades.entry_context` existed and NOTHING WROTE IT.

A join could not have recovered it either. `trades` now carries `run_id`, but the
run trace records which node ran and which state KEYS it wrote — not the values —
and the graph state holding the indicators is gone by the time a fill is booked.

So `risk_gateway.build_entry_context` writes a snapshot at decision time, because
the gateway is the last node holding `technical_analysis`, `market_regime` and
`volatility` together. It travels plan -> TAR -> CRO -> approval -> trade row
beside `run_id` and `strategy`.

The format is the one `learningDashboard.classifyEntryContext` already parsed;
`lib/viz/entryContext.ts` is the second reader. `tests/test_entry_context.py`
duplicates the TypeScript regexes deliberately — a snapshot the frontend cannot
parse is the same as no snapshot at all.

**A MISSING INPUT IS OMITTED, NEVER DEFAULTED.** An invented RSI would be the most
persuasive fabrication available here, because it would look exactly like
evidence. The page says "this trade predates the snapshot" rather than rendering
an empty journey that reads as a failure.

TESTED AS A PURE FUNCTION AFTER THE FIRST ATTEMPT FAILED SILENTLY. Driving
`gate()` with synthetic state made every test SKIP — the gateway reads the live
book and ledger and refused the fabricated inputs. A test that always skips
proves nothing, so the formatter was extracted and the WIRING is asserted
separately against the node's source.

### Switching venue is an ACCOUNT change, and it refuses with a position open

`POST /api/admin/venue` (Settings -> Exchange) moves the agent between Binance and
Bybit and persists `EXCHANGE_ID` to `.env`.

**It refuses while a REAL position is open, and that refusal is the route's
reason for existing.** The two venues are different accounts holding different
money — a position opened on one does not exist on the other. Switching
underneath one would leave it at the old venue while:

* `PositionMonitorAgent` goes on enforcing its stop by placing orders on the NEW
  venue, where the position is not;
* the resting stop this process left behind stays live and cannot be cancelled
  through the new client;
* reconciliation compares the local book against the wrong exchange and reports
  every real position as a phantom.

None of those announce themselves — the switch would look like it worked. Paper
positions do NOT block it: they have no venue counterpart at all.

`reset_venue()` drops the singleton so the next call rebuilds the client. The old
instance holds the other venue's markets, credentials and cached position mode,
and reusing it would place orders with one venue's parameters against the other's
API.

The panel shows credentials PER VENUE and renders the blocker BEFORE the control,
because discovering it from a 409 is worse than never being offered the action. A
venue with no keys can still be selected — market data needs none — and the
response says plainly that every private call will be refused until they are set.

### The Bybit testnet round trip, and the three faults it found

`scripts/bybit_testnet_roundtrip.py` — entry -> resting stop -> tighten -> close
against Bybit's sandbox. It is a SCRIPT, not a test: `tests/conftest.py` blocks
the network for every test on purpose, and this places real (testnet) orders.
It refuses to run against mainnet — a hard exit, not a warning — and cleans up in
a `finally`, because a run that dies at step 10 must not leave a position open
with a stop resting behind it.

**Every offline test passed while the real-money path was broken in three
places.** That is the value of this file and the reason it stays. All three were
invisible to a hand-built fixture because each depends on what the VENUE holds or
what ccxt does with a parameter.

**1. THE AGENT'S OWN SYMBOL RESOLVED TO THE SPOT MARKET.** Every caller says
`SOL/USDT`, and that exact key is a SPOT market in ccxt's dict; the perpetual is
`SOL/USDT:USDT`. `check_size` used a plain `dict.get`, so a perpetual order was
filtered against spot limits. Measured live on Bybit testnet:

    SOL/USDT       spot  minQty 0.001      <- what the code read
    SOL/USDT:USDT  perp  minQty 0.1        <- what the venue enforces

Worse, **the two venues disagree about where the order goes.** ccxt's binance
honours `defaultType: future` inside `market()` and resolves the bare key to the
swap; bybit's does NOT and returns spot. So the same call placed a perpetual on
one venue and a SPOT order on the other, while `positionIdx`, `reduceOnly`, the
leverage call, the stop and reconciliation all described a perpetual.

`Venue.resolve_symbol` now maps the caller's form to the venue's perpetual and
every order path goes through it. **It never falls back to spot** — a spot fill
is a different instrument: no leverage, no reduce-only close, and no position to
rest a stop against. Resolution lives in the venue layer rather than at the call
sites because a symbol form is a venue detail, and thirty callers remembering to
append `:USDT` is thirty chances to place a spot order with real money.

**2. THE BYBIT STOP NEVER REACHED THE VENUE.** `place_stop_loss` sent
`triggerPrice`, which makes ccxt treat the order as a GENERIC trigger order — and
a generic trigger order requires an explicit direction:

    ArgumentsRequired: bybit stop/trigger orders require a triggerDirection
    parameter, either "ascending" or "descending"

raised before any request left the process. The handler caught it and logged
"stop-loss order REJECTED", which reads as the venue refusing a stop rather than
as this process never having asked for one. It also passed `stopLoss` as a bare
string where ccxt expects an object, which would have attached a SECOND
position-level stop on top.

Both venues now take the unified `stopLossPrice`, read out of ccxt 4.5's own
source: binance turns it into `STOP_MARKET` + `stopPrice`, and bybit DERIVES
`triggerDirection` from the order side (a sell stop triggers on a fall, a buy
stop on a rise) and forces `reduceOnly`. The trigger is the MARK price on both
now; it was set on Bybit only, leaving every Binance stop on last price, which a
thin book can move on a single print.

**3. RECONCILIATION COMPARED TWO SPELLINGS OF ONE POSITION.** The local book
holds `SOL/USDT`; ccxt reports the same perpetual as `SOL/USDT:USDT`. `_compare`
keyed on the raw strings, so every real position was reported CRITICAL
`missing_at_venue` AND the venue's own position as unknown. That is a false alarm
on precisely the alert that is supposed to mean a liquidation, an ADL or a close
by hand — and an alert that fires on every position teaches an operator to ignore
it, which is worse than not having it. `open_positions` returns the display form
(keeping `venueSymbol` alongside) and `_compare` normalises both sides.

`resting_stops()` exists so the script can assert **exactly one** stop rests
after a tighten. Listing them is itself venue-specific: Binance returns
STOP_MARKET orders from a plain `fetch_open_orders`, while Bybit keeps
conditional orders in a separate book and returns nothing unless the request asks
(`trigger=True` -> `orderFilter=StopOrder`). Asking Bybit the Binance way answers
"no stops are resting" while a stop rests.

**TESTNET KEYS ARE SEPARATE VARIABLES** (`BYBIT_TESTNET_API_KEY` / `_SECRET`).
`_credentials(venue, testnet=)` reads them only in sandbox mode, and **mainnet
never reads them** — the asymmetry is the point: verifying on testnet must not
require pasting keys over the mainnet pair and putting them back, because that
put-them-back step is where a real key ends up in play by accident. Testnet still
falls back to the mainnet variable, which is what existed before and fails closed
(a mainnet key is refused by a sandbox endpoint).

`tests/test_venue_live_path.py` pins all of it offline, since the round trip
needs testnet keys and a funded wallet and cannot run in CI. One thing that file
learned the hard way: its first sizing fixture used `f"{amount:.1f}"`, which
ROUNDS where ccxt truncates — so 0.05 became 0.1, cleared the perpetual's
minimum, and the test asserting a refusal passed for the wrong reason. **A
fixture kinder than the venue proves nothing.**

**WHAT IS STILL NOT VERIFIED: Binance order placement.** ccxt dropped Binance
futures testnet support, so the only way to exercise it is with real funds. The
parameter matrix is shared and unit-tested, and the two faults above were fixed
on both venues — but "tested on Bybit" is not "tested on Binance", and this note
exists so nobody upgrades it to that.

### One missing field declaration disabled the entire learning loop

The learning-loop section above says `trades.strategy` is "carried plan -> TAR ->
CRO -> approval -> row". It was not. Read from the live database:

    30 trade rows. strategy, run_id AND entry_context NULL on EVERY one —
    openings and closings alike. `strategy_performance.aggregate()` returned {}
    on an account that had traded for three days.

`strategy_performance` selects `WHERE pnl IS NOT NULL AND strategy IS NOT NULL`.
Only a CLOSE carries a `pnl`; only an OPEN was ever going to carry a `strategy`.
Even with the plumbing working those two sets never intersect — so the query
could not return a row, every profile's `historical_success_rate` stayed None,
and the 0.2 track-record weight was permanently neutral (0.5). The loop was wired
end to end and structurally incapable of producing a number.

The root cause was one hop earlier than the symptom and is a Pydantic footgun:
**`TarApprovedEvent` did not DECLARE `run_id`, `strategy` or `entry_context`.**
`TarSubmittedEvent` declared all three and `cro_agent` passed all three to the
`TarApprovedEvent(...)` constructor — but Pydantic v2 IGNORES unknown keyword
arguments by default, so nothing raised. The CRO believed it forwarded them; the
event that came out simply did not have them.

It stayed invisible because every reader is defensive: `getattr(tar, "strategy",
None)` in `execution_agent`, and the same in the position monitor. **A defensive
read of a field that does not exist is indistinguishable from a legitimate
absence** — the whole chain reported None and looked like it was working. The
frontend panel said "No strategy has closed a trade yet", which is exactly what an
honest, empty, WORKING loop also says.

Two more drops fixed on the way, both found by following the same thread:

* THE CLOSING ROW NEVER RECORDED ATTRIBUTION AT ALL. `position_monitor`'s closing
  INSERT named only ten columns; `strategy`/`run_id`/`entry_context` were absent
  from the SQL. Now carried on `_Tracked` from the approval and written on close.
  A MANUAL position closes with strategy NULL rather than a fabricated one — a
  human's click was not chosen by an algorithm, and crediting one would poison the
  measurement it feeds.

* `stop_order_id` WAS NEVER PERSISTED. `save_watch_list` binds `row.get(f) for f
  in _FIELDS`, and `_watch_rows` never emitted `stop_order_id` — so the column was
  written NULL on every row despite the schema comment explaining precisely why it
  had to survive a restart (to CANCEL the orphaned venue stop). Same class of bug:
  a field named where it is consumed and silently never produced.
  `tests/test_close_attribution.py::test_every_persisted_field_is_actually_emitted_by_the_watch_rows`
  now asserts `_FIELDS` is a subset of `_watch_rows()` keys so a future addition
  to one cannot silently NULL out the other.

`tests/test_close_attribution.py` pins the whole chain, including that
`OrderFilledEvent` is the LAST place attribution could be dropped (it does not
carry these fields, so the TAR handler keeping them in `_pending` is load-bearing,
not incidental).

### BTC is a SIGNAL, not a tradeable instrument — and those are different sets

The operator asked not to trade BTC/USDT. On the live ledger it earned that:

    BTC/USDT    2 closed trades, 0 wins, -43.59
    SOL/USDT   10 closed trades, 3 wins, -13.42

Two of twelve trades produced 76% of the total loss. Two trades cannot prove BTC
is a bad instrument and `tradeable_universe` does not claim they do — it makes the
operator's preference a configured fact.

**The important design point: this could NOT be done by removing BTC from a watch
list.** BTC is the market's beta. `triggers.py` attributes regime triggers to
`BTC_SYMBOL` because "the underlying condition is market-wide", `REGIME_WATCH`
polls it, and `market_context` now reads it as the benchmark for every OTHER
symbol's relative strength. Deleting it as an OBSERVED symbol would blind every
alt decision. So two questions that were answered by one list are now separate:

    OBSERVED   what needs prices and context?   (BTC stays in — live_market_data,
                                                  REGIME_WATCH, the benchmark)
    TRADEABLE  what may we open a position in?   (BTC excluded — the new module)

`backend/services/tradeable_universe.py`. `UNTRADEABLE_SYMBOLS` env var, default
`BTC/USDT`, read at CALL time (a frozen-at-import list is the `simulation_mode`
bug again — the operator excludes an instrument, is told it worked, and the agent
keeps trading it until a restart). The blocklist normalises the `:USDT` perpetual
suffix, so a list matching one spelling is not bypassed by whichever hop resolves
the symbol first.

The gate is in `risk_gateway.gate`, placed AFTER the EXIT branch (invariant 4 — a
position in a now-excluded symbol must still be closable) and FIRST among the
entry checks (a refusal that is a property of the instrument must not be reachable
by making the trade smaller). `analysis.subscribe_to_triggers` also skips the full
24-node run early for an untradeable symbol WITH NOTHING OPEN in it — pure cost
saving; the gate is what makes it correct, and a held position still runs so an
EXIT can be reached.

### Higher-timeframe alignment — the fetched-but-unused signal

`validate_market_data` has fetched 15m, 1h and 4h since the beginning, and
`_multi_timeframe_trend` computes their consensus into
`TechnicalAnalysis.multi_timeframe_trend` — where `build_entry_context` RECORDED
it and nothing GATED on it. The comment on `TIMEFRAMES` even said the higher ones
exist "to cut conviction on a counter-trend read". Nothing cut anything.

The ledger is the argument. 12 closed trades, 3 wins (25%), payoff 2.27:1 —
break-even needs 30.6%. The 9 losses average -26.04 and are tightly clustered:
stop-outs at a consistent risk, not disasters. All 3 wins landed in one 30-minute
window on one day. That is a trend-follower run in conditions that are not
trending, and the lever is SELECTIVITY.

`backend/algorithms/market_context.py` (pure, deterministic, no I/O — reads
candles `validate_market_data` already fetched; a node fetching its own data is
not replay-safe, Section 39.4). `assess(direction, context)` blocks a CLEAR
counter-trend entry — a 15m long into a 1h/4h downtrend — in `risk_gateway.gate`.

Two deliberate non-blocks, and the reasoning differs from the volatility gate
right beside it: UNKNOWN (fewer than two timeframes measurable) and MIXED (1h and
4h disagree) both pass. Volatility feeds sizing and stop distance, so unmeasured
volatility means the loss cannot be bounded at all — a hard block. An unmeasured
higher-timeframe trend costs conviction, not bounding: the stop is still computed,
enforced and sized against measured ATR. Blocking on it would halt trading
whenever a 4h fetch was slow, and refusing every MIXED state refuses the turns
this strategy exists to trade.

`market_context.build` also reads BTC as the benchmark (fetched onto
`MarketSnapshot.benchmark_candles` at the single fetch point) and computes the
coin's relative strength. The context is appended to the entry-context snapshot —
AFTER the existing fields, so every frontend parser regex still matches — so the
learning loop can later answer whether the gate was worth having.

`REQUIRE_HTF_ALIGNMENT` (env, default on) gates it; the context is BUILT
unconditionally so turning the gate off does not also stop recording the data that
would judge it. Gate, universe and benchmark are pinned by
`tests/test_market_context.py`.

**ALL THREE ARE HYPOTHESES.** They will reduce the number of trades and should
raise the win rate. Whether they raise EXPECTANCY — the number that actually
matters — depends on how many removed trades would have won, and 12 trades cannot
say. The learning loop, now that it records a strategy, is what will. A win rate
targeted directly (a tiny target, a huge stop) is trivially reachable and reliably
loses money; these raise win rate as a CONSEQUENCE of selectivity, which is the
only version worth having.

### The learning kept repeating one canned lesson — three causes, all fixed

The operator saw the same line on every loss:

    "Check if losses cluster in this regime before changing weighting."

That is `reflection.rule_based_lesson`'s FALLBACK — reached only when the model
call did not produce an answer. So the learning was not shallow; the model was
not being reached, or was reasoning over almost nothing. Three causes:

**1. A MODEL DIED AND NOTHING SAID SO.** `openai/gpt-oss-120b` reached end of life
on 2026-09-03 and the NVIDIA endpoint returns **HTTP 410 Gone**. It was the
NARRATIVE tier AND the consultation model. So from that date the thesis narrative
and every second opinion had been silently failing to their fallbacks. A live
probe of the catalog found it gone; `nvidia/nemotron-3-super-120b-a12b` replaced
it — a 120b-class NVIDIA reasoning model, verified live at ~2.9s, producing real
causal analysis. **Verify a model is alive before trusting a config that names
it** — the provider fails closed, so a dead model looks exactly like a quiet
degradation. There are no hardcoded model ids in code; `.env` is the only place.

**2. NOTHING RATE-LIMITED THE KEY.** `budget.py` caps calls PER RUN (loop
protection) and says so: "Rate limiting across runs is the trigger layer's job."
But the trigger layer limits trade ANALYSIS, not model CALLS, and the NVIDIA free
tier is 40 requests/minute PER KEY, shared by the analysis narrative, the
consultation panel and the reflection. A busy minute returned HTTP 429, and 429
fell straight through to the canned fallback. `backend/llm/rate_limit.py` is a
process-wide sliding-window limiter keyed by the API key (the main provider and
the panel share one NVIDIA key, so they must share one bucket — a limiter per
provider-id would let them spend 80/min between them). It WAITS for a slot rather
than failing; every LLM call here is off the trading critical path (the trade is
on the bus at ~4.8s, narration and reflection run after), so a wait costs latency
on prose, never on a fill. `provider.complete` acquires a slot before the request
and, on a 429 from usage this process cannot see (another client on the same key),
honours `Retry-After` for ONE retry. `LLM_MAX_RPM` configures it. A live probe of
kimi-k3 on 2026-09-05 returned 429 immediately — the limit is real and was being
hit. `tests/test_rate_limit.py` pins it.

**3. THE REFLECTION PROMPT WAS THIN.** Even when the model was reached it saw only
symbol, side, pnl and exit reason — NOT the RSI/ATR/structure/regime/HTF-trend/BTC
snapshot the Risk Gateway records at entry. A model given only the outcome cannot
tell "stopped inside the noise band in a range" from "counter-trend against the
4h", so it retreats to a category. Three fixes: `PositionClosedEvent` now carries
`entry_context`/`strategy`/`run_id` (the receipt read them off an event that never
had them — `strategies` was even published empty, so the deterministic attribution
never fired); the lesson prompt includes the entry context and demands a specific
CAUSE, naming the old canned line as what NOT to produce; and the lesson runs on
the REASONING tier, not NARRATIVE — connecting an outcome to its context is the
judgment Section 39.6 reserves the strongest model for, and it is off the critical
path so the slower tier costs nothing a fill waits on. The contract is unchanged:
the node still writes only `reflection_lesson` and cannot reach the calibration
delta that feeds sizing.

Verified end to end on 2026-09-05 against the fixed config — a losing SOL long
with full entry context produced, from the model:

    "The trade was stopped out because a long was entered while the 1h and 4h
    trends and the 15m structure were bearish, putting the position against the
    dominant direction; in a low-volatility range regime the price drifted down
    ~0.6% (~1 ATR) and hit the stop-loss. This hypothesis can be checked by
    comparing the stop-out rate of TrendFollowing longs taken when both
    higher-timeframe trend and 15m structure are bearish versus when they are not."

`tests/test_reflection_analysis.py` pins that the context reaches the prompt, that
a missing context is stated rather than faked (invariant 6), and that the tier is
REASONING.

One model the operator asked for was NOT usable: `writer/palmyra-fin-70b-32k` (a
finance model) is in the catalog but returns 404 "Not found for account" — it is
not enabled on this key. If it is ever enabled, it is a natural fit for the
consultation slot (a genuinely different, finance-specialised prior).

### Risk parameters are now aligned across TS and Python

An external audit (Sept 2026) flagged that `lib/riskManager.ts` used 1.2x/1.8x ATR
while `backend/core/risk_manager.py` used 2.5x — so the SAME setup sized a tighter
stop on the browser/manual path than on the autonomous backend path. Python is
authoritative (it carries the only live-trade evidence — the nine-SOL-trade
finding that drove 1.5x -> 2.5x because five of six losses were noise-band
stop-outs), so the TS side was aligned TO it: `ATR_FLOOR_MULTIPLIER` and
`ATR_FALLBACK_MULTIPLIER` are both 2.5 now, reward ratio still 2 (== 5.0/2.5). The
structural swing stop is kept but floored at 2.5x ATR for the same noise-band
reason. If these change again, change the Python file first and mirror it; two
numbers that must agree live in two files, kept in sync deliberately.

### LLM health is a metric now, not something a human notices in prose

`backend/llm/health.py` records the outcome of every `complete()` call — the
provider wires `_record_health` into all six return paths — and
`GET /api/monitoring` exposes `llmHealth`: the FALLBACK RATE over a rolling 500
calls, a breakdown by failure class (rate_limited / timeout / model_eol / auth /
empty), and any configured model that looks DEAD.

This exists because the reasoning layer degraded silently once (the gpt-oss-120b
EOL) and the only reason it was caught is a human noticing the lessons all read
the same. The provider fails closed, so a dead model is invisible from the
outside. Dead-model detection is DERIVED FROM REAL CALLS, not a scheduled probe:
two 410/404 responses for a model in the window flag it (one is a fluke), and the
note names it and says to update `.env`. No probe means no extra spend against the
40/min key budget, and it reflects the requests that actually matter. It is
measurement only — it never retries, degrades or gates. `tests/test_llm_health.py`
pins the classification, the fallback-rate maths, the two-strike dead-model rule,
and that a recovered model ages out of the window.

### The strategies are backtested now, and the results are stored

The audit's §2.6: `core/backtest_engine.py` existed but produced no per-strategy
P&L ("we just track trade count"), so every profile carried
`historical_success_rate=None` with nothing measured.

`backend/core/strategy_backtest.py` is a pure, deterministic simulation: for each
strategy it walks historical candles, feeds the trailing 120-bar window to that
strategy's REAL `STRATEGY_FUNCTIONS[name]` signal function, and simulates every
signal under the SAME risk model the live agent places —
`ATR_STOP_MULTIPLIER`/`ATR_TARGET_MULTIPLIER` imported from `core/risk_manager`,
so the backtest and the live system (and now the aligned TS path) cannot disagree
about the stop and target. Outcomes are in R (1R = the risk); expectancy in R is
the edge figure, comparable across symbols and ATRs. `tests/test_strategy_backtest.py`
pins the arithmetic offline (a target is +2R, a stop -1R, an ambiguous bar assumes
the stop, and — a fixture lesson — a resolving bar's large range inflates the
14-bar ATR, so trades must be spaced for clean numbers; the engine was right and
the first fixture was naive).

`scripts/run_backtests.py` fetches real klines (public Binance, no key), runs all
eleven strategies over several symbols/timeframes, prints a ranked table and
STORES `backtests/<date>/summary.json` (reproducible — the candle window is
stamped in). First run, 2026-09-06, 1000 candles each of SOL/ETH/XRP at 15m/1h,
pooled expectancy in R:

    Scalping   +0.160    Breakout  +0.142    Momentum  +0.125     (net positive)
    Swing      -0.007    Trend     -0.007                          (break-even)
    MeanReversion -0.103  Range -0.184  Grid -0.206  Arbitrage -0.212  VWAP -0.328

READ THIS HONESTLY, and the script says so in its output:
  * Gross of fees and slippage. Scalping tops the list but its own profile says a
    gross backtest overstates it — fees eat the edge — so treat its +0.16R as
    break-even net.
  * IN-SAMPLE and mostly a trending window (ETH 1913->2500, XRP 1.10->1.42). The
    range/mean-reversion/grid strategies losing here is partly that they were
    tested in a trend, which their regime gate would have muted live. A ranging
    window would rank them differently. This is EVIDENCE for the ensemble weights,
    not a verdict to delete a strategy.
  * The payoff is a fixed 2:1 (the risk model), so win rate alone decides sign —
    break-even is 33.3%, which is exactly where the table splits.
  * `strategy_performance.MIN_SAMPLE` (20 REAL closed trades) still governs live
    promotion. A backtest informs; it does not deploy (invariant 5).

### `exchange_agent.py` no longer swallows exceptions silently

The four multi-exchange price fetchers (dashboard widget only — NOT the trade
path) did `except Exception: pass; return None`, so a rate-limit, a schema change,
a timeout and a real outage were indistinguishable and the widget blanked with no
line anywhere. Each now logs the reason at WARNING before returning None, per
invariant 6. None is still the return; the widget still shows the venues that
answered; the reason is now diagnosable.

### Telegram alerts — entry and close, paper and real on separate channels

`backend/services/telegram_notifier.py`. For an operator running the agent 24/7
unattended, the two messages that matter: an ENTRY on every fill, and a CLOSE
carrying realised P&L, the current WIN RATE and the current TOTAL BALANCE.

WHY THESE TWO EVENTS. `ORDER_FILLED` is published only on the execution agent's
OPEN path; `close_position` publishes nothing and the CLOSE is announced by
`POSITION_CLOSED` (which carries the realised P&L). So entry->ORDER_FILLED and
close->POSITION_CLOSED with no open/close ambiguity. Manual operator-panel trades
do NOT flow through ORDER_FILLED and are deliberately not notified — a human
clicking Buy already knows; this is for the agent nobody is watching.

TWO CHANNELS, ROUTED BY TAB. `TELEGRAM_CHAT_ID_PAPER` and `TELEGRAM_CHAT_ID_REAL`;
the message goes to the channel for `event.tab`. A tab with no chat id is SKIPPED,
never cross-posted — a real fill must never land in the paper channel because the
real one is not set up yet, so paper-only is a valid config.

IT NEVER BLOCKS OR BREAKS THE BUS. The bus delivers an event to every subscriber
in turn, so an awaited ~200ms Telegram POST would delay POSITION_CLOSED reaching
the reflection and monitor agents. The send is FIRE-AND-FORGET: `handle_event`
schedules the HTTP call and returns; the call has its own 10s timeout and swallows
every error. A missed alert is a missed alert; it may never slow a trade.
`current_equity(tab)` supplies the balance (exchange balance for real, marked book
for paper); the win rate is a direct `count(pnl>0)/count(pnl)` over the tab's
closed trades. Both are OMITTED if unreadable (invariant 6), and their absence
never suppresses the P&L line. Off unless `TELEGRAM_BOT_TOKEN` + a chat id are set;
status (never the token) is on `GET /api/monitoring` as `telegram`.
`tests/test_telegram_notifier.py` pins routing, content, the skip-not-crosspost
rule, and that a send neither blocks nor raises.

### Session capital allocation — trade with 25 / 50 / 75 / 100% of the balance

The operator picks, per session on the home page, how much of the account the
agent may deploy. `TradingSession.capital_fraction` (default 1.0), set through
`POST /api/session/start` (`capitalFraction`), read by the Risk Gateway via
`trading_session.active_capital_fraction()`.

It does TWO things, both in `risk_gateway.gate()`:
  1. SIZES each trade against that fraction of the account (`equity *= fraction`
     before sizing), so 25% makes every trade a quarter of what it would be.
  2. CAPS the total margin committed at once at `fraction × account_capital` —
     once the pool is deployed, `CapitalPool` rejects new trades until one closes.
     `account_capital` is `cash + deployed_margin` (free cash + locked margin),
     NOT the leverage-inflated `cash + notional`; `_deployed_margin` reads
     `marginLocked` and falls back to notional/leverage (unknown leverage → 1x,
     which over-counts and so caps SOONER, never later).

IT IS NOT A LEVERAGE SOURCE. The leverage ceiling and mandatory stop are
untouched, so 100% means "use the whole account as margin", never "use more
leverage". 1.0 is byte-for-byte the pre-feature behaviour (no `CapitalPool` check
appears), so the feature is fully opt-in and applies to paper and real alike.
`tests/test_capital_allocation.py` pins the linear sizing, the pool rejection, and
that full allocation changes nothing.

### Why the agent barely trades in sideways markets (it is not a bug)

Confidence-to-trade is set PER REGIME in `dynamic_thresholding.get_required_confidence`:
Bull Trend 0.60, Bear Trend 0.65, **Range 0.75, Low Volatility 0.70**, High Vol
0.85. In a ranging/quiet market the debate reaches only ~0.20, so the Supervisor
returns WAIT — measured live, 2,904 evaluations in one day, every rejection either
"debate concluded NEUTRAL" or "Confidence 0.20 does not meet the 0.75 threshold
for regime 'Range'". This is the trend-follower correctly staying out of chop: the
backtest showed the range strategies (MeanReversion, Range, Grid) are
net-negative, and the ledger's noise-band stop-outs were exactly what forcing
range trades produces. Longs-only recently is the same cause — the debate found no
confident SHORT, not a hardcoded bias (`score_debate` returns SHORT and the
Supervisor sizes it). The operator was asked whether to lower the range threshold
to trade sideways and chose to keep the current selectivity.

### An external source review found two NameError crashes and an auth gap

A source-level review (Sept 2026) surfaced real bugs the test suite had not — a
reminder that a green suite proves the paths it exercises, not the ones it skips.

**1. THE BACKEND PROXY HANDED THE SERVER KEY TO ANYONE.**
`app/api/backend/[...path]/route.ts` attaches `TRADES_API_KEY` to every forwarded
request, and `middleware.ts` excludes `/api` from its `DASHBOARD_PASSWORD` gate —
its comment even claimed these routes "have their own TRADES_API_KEY auth", which
was backwards: the proxy SUPPLIES that key, it does not check one. So any caller
reaching the public Vercel URL could invoke backend write routes (enable live
trading, switch venue, start/stop a session, reset-paper, emergency-stop) with the
server's credential. The proxy now authenticates the INCOMING caller before
attaching the key: with `DASHBOARD_PASSWORD` set it requires the matching Basic
auth (the browser sends it automatically after the middleware challenge); with it
UNSET it allows reads but REFUSES writes, because an open write proxy on a
real-money system is the exposure and leaving it open "so nothing breaks" is not a
fix. **`DASHBOARD_PASSWORD` must be set on the Vercel deployment.**

**2. `risk_gateway.gate()` CRASHED ON A COUNTER-TREND REJECTION.** The HTF gate
built the context as a local `context` but referenced `market_context` in the
rejection branch — a NameError on the exact path the gate exists for, which no
test drove into. Fixed by building `market_context` once (which also restored the
market context to `build_entry_context`, dropped by a partial revert).
`tests/test_market_context.py` now drives `gate()` into the block branch.

**3. THE EVENT-DRIVEN SUPERVISOR CRASHED THE INSTANT IT APPROVED A TRADE.**
`supervisor_agent.py`'s approval rationale referenced `sizing['rule']` /
`sizing['detail']` — a dict that method never defines (`size` is a bare number
from `calculate_position_size`). A NameError fired right before TAR submission. Now
uses the risk fraction it actually computed. Guarded by a comment-stripped source
scan.

Also corrected: stale comments the review flagged. `position_monitor`'s docstring
said "this system does not place a resting stop" (it does now, stop AND take-profit
for real fills); `provider.py` opened with "There is no LLM client anywhere in
backend/" (this file IS it); `research_graph.BACKTEST_UNAVAILABLE` cited the
bus-clearing backtester defect that is already fixed. The review's remaining
finding — "task execution passes 0 as time and hardcodes sell on closes" — does
NOT apply to the live code: every close path computes `exit_side` by direction
(`sell if buy else buy`), and no such task scheduler exists in the tree.

## Safety invariants — never break these

These are enforced in code, and there are tests that exist specifically
to keep them enforced. Breaking one is a serious regression, not a
refactor.

1. **No AI-initiated trade may bypass the Supervisor gate.**
   `components/Supervisor.tsx`'s `reviewAndExecute()` is the single
   execution path for every AI-originated trade (chat trade-action,
   agent-plan ticks, Debate "Act on this", the autonomous loop). Manual
   human clicks are deliberately out of scope — supervising agents means
   supervising agents, not overriding the operator.

2. **The leverage ceiling is not overridable.**
   `ABSOLUTE_MAX_LEVERAGE` (3x real / 10x paper) in `lib/riskManager.ts`
   is deliberately **not** part of `RiskConfig`, so no setting, agent, or
   confidence level can raise it. It is checked before any stop-distance
   math so a tight stop cannot compute past it. Do not move it into
   `RiskConfig`.

3. **Every position requires a computed stop-loss.** If no stop can be
   computed (no ATR), `validateTrade()` hard-rejects. Do not soften this
   back to a non-blocking `'unavailable'`.

4. **Closes/exits are never blocked.** Not by pause, not by risk checks,
   not by a Debate veto. Refusing to let someone exit a position they are
   already in is actively harmful. This holds for real money more, not
   less.

5. **Learning never auto-deploys.** Reflection → Hypothesis produces
   *understanding*. A hypothesis reaching production requires an explicit
   human click. `Loss → AI rewrites strategy → Live` must remain
   impossible. Nothing in `lib/hypothesis*` or `lib/curiosityEngine.ts`
   may write to production risk config or strategy selection.

6. **Never fabricate market data.** No invented prices, fills, or
   indicator values. If something isn't computable, say so honestly and
   return `null`/`'unavailable'` rather than a plausible number. This
   codebase's comments are full of this discipline — match it.

---

## Engineering conventions actually used here

- **Comments explain *why*, especially non-obvious tradeoffs and past
  bugs.** This codebase documents root causes inline so regressions
  don't recur (see `components/Agent.tsx`'s React Strict Mode
  double-invocation comment). Match that density; don't strip it.
- **Pure logic in `lib/`, side effects in `components/`.** Decision
  functions (`agentTick`, `scoreOpportunity`, `moderate`,
  `validateTrade`) are pure and unit-tested. Keep them that way — pass
  computed context in rather than reaching for I/O.
- **Deterministic over LLM where the math is real.** The Debate
  moderator, opportunity scanner, and curiosity engine are deliberately
  pure computation, not model calls — asking a model to "reason over"
  numbers already on hand adds hallucination risk to a financial
  decision for no benefit and isn't reproducible. Reserve LLM calls for
  genuine judgment (chat, reflection, hypothesis, second opinion).
- **Stores follow one pattern:** `lib/<name>Store.server.ts`, JSON under
  `.data/`, lazy file creation, a serialize() promise queue against write
  races. Copy an existing one.
- **New LLM call?** Reuse the `/api/chat` + `readSSEStream` buffering
  pattern from `components/Reflection.tsx`. There is no separate
  non-streaming endpoint.

## Verification — run these

```bash
npx tsc --noEmit -p tsconfig.json   # must be clean
npm run test                        # vitest; 31 files / 457 tests, must all pass
npm run build                       # catches route/provider issues tsc won't
```

**Run these SEQUENTIALLY, not chained into one parallel invocation.**
Vitest run alongside `tsc` or `next build` on a memory-constrained
machine loses workers and prints `Test Files 23 passed (29)` — six files
that never ran, on a line that reads as a pass. Run alone it is
deterministic (31/31, 457/457, verified over five consecutive runs). The
count in the header is there so a short run is recognisable as short.

**`next.config.js` caps the build worker count, and that is load-bearing
— do not delete it as noise.** The build used to compile and type-check
clean and then die in *Collecting page data* with `worker exited with
code: 3221226505`. That is `0xC0000409`, the Windows `__fastfail` code,
which reads as a stack buffer overrun and sends you hunting for infinite
recursion in a route module. It was not that: V8 raises the same
fast-fail on a failed allocation, so on Windows an OOM worker and a
corrupted stack look identical from the exit code.

Next spawns one static worker per CPU and each loads the whole app (57
routes, 22 providers, lightweight-charts). Sixteen did not fit in
available memory. Bisected: 16 fails, 8 fails, 4 passes, 1 passes — so
it is fan-out, not any one page. `experimental.cpus` is derived from
free memory there, with the measurement written down. Two dead ends
already checked, so nobody re-checks them: `workerThreads: false` is
unnecessary, and removing `--max-old-space-size=4096` from the build
script does NOT help even though every worker inherits it.

`npx next lint` will try to run a first-time ESLint setup wizard (no
config exists) — it is not part of the verification loop.

```bash
.venv/Scripts/python.exe -m pytest -q   # backend; must all pass
```

**Network: `api.binance.com` IS reachable from this machine.** This note
previously said it was not, and that assumption hid a real bug for
months: `market_data.fetch_prices()` was filtering ccxt's futures ticker
keys with `symbol.endswith("/USDT")` — but futures keys are
`BTC/USDT:USDT`, so it matched nothing, the price cache stayed empty, and
the code logged *"Failed to fetch prices via CCXT after maximum
retries"*. Everyone read that as the documented network limitation. It
was a string-matching bug, and `fetch_tickers` had been succeeding all
along.

So: public Binance endpoints (tickers, klines) work and are worth
verifying against. **Private** calls still fail while `USE_TESTNET=true`,
because Binance dropped futures testnet support in ccxt — that one is
real, and `fetch_balance` reports it once rather than on every poll.
Polymarket reachability is untested. Verify before claiming either way
rather than inheriting a note.

---

## Output expectations

When implementing a feature: analyze the current architecture first,
explain the design, name affected modules, then write production-ready
code **with tests, honest comments, and failure handling**. Describe
risks and how to roll back. Ask rather than guess when a decision is the
user's to make. Never invent functionality that conflicts with what's
already here.
