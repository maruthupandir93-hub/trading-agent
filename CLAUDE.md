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
npm run test                        # vitest; 29 files / 439 tests, must all pass
npm run build                       # catches route/provider issues tsc won't
```

**Run these SEQUENTIALLY, not chained into one parallel invocation.**
Vitest run alongside `tsc` or `next build` on a memory-constrained
machine loses workers and prints `Test Files 23 passed (29)` — six files
that never ran, on a line that reads as a pass. Run alone it is
deterministic (29/29, 439/439, verified over five consecutive runs). The
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
