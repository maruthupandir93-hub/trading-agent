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
npm run test                        # vitest; 26 files / 406 tests, must all pass
npm run build                       # catches route/provider issues tsc won't
```

**Run these SEQUENTIALLY, not chained into one parallel invocation.**
Vitest run alongside `tsc` or `next build` on a memory-constrained
machine loses workers and prints `Test Files 20 passed (26)` — six files
that never ran, on a line that reads as a pass. Run alone it is
deterministic (26/26, 406/406, verified over five consecutive runs). The
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
