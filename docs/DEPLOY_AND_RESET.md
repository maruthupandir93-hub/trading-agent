# Deploying to Oracle, and resetting Supabase to a clean start

Written 2026-09-26, against the deployment that actually exists: Docker Compose
on Oracle (`68.233.106.208`, container `trading_os`, port 8000), Supabase
Postgres, Vercel for the frontend.

---

## THE ORDER IS THE WHOLE POINT

**Stop the backend BEFORE touching the database.** This is not caution, it is the
difference between a reset that works and one that silently does nothing:

- `portfolio_store` keeps the book in a module-level dict and `_persist()`
  **replaces** the stored rows from memory after every write.
- `PositionMonitorAgent` keeps the watch list in `self._open`, and
  `save_watch_list` is a `DELETE` + re-`INSERT` of everything it is holding.

So a `TRUNCATE` against a running backend is undone by the next fill. CLAUDE.md
records the exact surprise: *"the operator reset their database, and the
dashboard still read 'Open positions: 1'."*

The correct sequence is therefore:

```
stop  ->  wipe  ->  configure  ->  deploy  ->  start  ->  verify
```

---

## STEP 0 — push the branch (from the dev machine)

The work is committed to `fix/single-trade-originator-and-parity`.

```bash
git push -u origin fix/single-trade-originator-and-parity
```

`.env` is gitignored and is **not** part of this. It is edited on the server in
step 3, and that is deliberate — the file holds live API keys and the database
password.

---

## STEP 1 — stop the backend (on Oracle)

```bash
ssh <you>@68.233.106.208
cd ~/trading-agent

# What is it holding right now? Worth recording before it goes.
curl -s localhost:8000/api/graphs/positions
curl -s localhost:8000/api/session | head -c 400

docker compose down
docker ps          # trading_os should be gone
```

**If `LIVE_TRADING=true` and a REAL position is open, close it at the venue
first.** The reset clears `monitored_positions`, which stops watching without
closing anything — on a real book that abandons an open position at the exchange
with nothing enforcing its stop. `scripts/reset_database.py` refuses while
`LIVE_TRADING=true` for exactly this reason, and that refusal is the most
important line in it.

---

## STEP 2 — wipe Supabase

```bash
python scripts/reset_database.py --all              # DRY RUN — prints every table and row count
python scripts/reset_database.py --all --confirm    # actually erases
```

`--all` truncates **every table in `public`** with `RESTART IDENTITY CASCADE`.
`CASCADE` matters: `decisions.trade_log_entry_id` references `trades`, and
without it the statement fails on the foreign key and *nothing* is truncated —
which reads as the script having silently done nothing.

**It truncates rather than DROPs, and that is deliberate.** `db/schema.sql` is
idempotent (`CREATE ... IF NOT EXISTS`, `INSERT ... ON CONFLICT DO NOTHING`) and
`init_db` re-applies the whole file on every startup, so emptying `migrations` /
`schema_migrations` costs nothing — the next boot re-seeds them. Dropping would
also work, but it leaves the database with no tables at all if the backend then
fails to start for an unrelated reason, and an empty table is a far easier thing
to be wrong about than a missing one.

**Supabase's own schemas are untouched.** The script only looks at `public`, so
`auth`, `realtime`, `storage` and the rest are never in scope. Never truncate
those — it breaks the project, not just the app.

### Is `schema.sql` already migrated?

Yes, and you can check rather than trust it:

```bash
python scripts/verify_schema.py
```

Verified on 2026-09-26: **31 of 31 declared tables present, no column drift, and
`trades_origin_tag_check` permits all 7 origin tags the code writes.**

That last check is not decoration. A `CHECK` that omits a tag the code emits does
not degrade — it **rejects the INSERT**. `agent-close` was missing once, so every
closing trade the agent made was rolled back after the money had already moved:
the positions closed correctly and only the record of them was lost, which
blanked the entire P&L dashboard three pages away.

`init_db` re-applies `schema.sql` on every startup, so after the wipe the tables
and seed rows come back on the next boot with no separate migration step.

### Local state, if you want it fresh too

The Postgres wipe does not touch these:

```bash
rm -f backend/data/ai_memory.json       # win-rate memory   (DOCKER VOLUME — survives rebuilds)
rm -f backend/data/working_memory.json
rm -f .data/graph_checkpoints.sqlite    # LangGraph checkpoints — the file that filled the disk
rm -f .data/*.json                      # the browser's JSON stores
```

Only `backend/data/` is a mounted volume (`./backend/data:/app/backend/data`), so
it is the one that survives a rebuild and genuinely needs clearing by hand.
`.data/` lives inside the container and is replaced on `--build` anyway.

---

## STEP 3 — configure `.env` (on Oracle)

```bash
nano .env
```

**The two lines that matter most.** `openai/gpt-oss-20b` is dead on this NVIDIA
account — still returned by `GET /v1/models`, never answers. It was configured on
the mechanical tier *and* the consultation panel, so every graph run paid up to
60s and up to 300s respectively. A live trace recorded `external_consultation` at
**300,612.9ms**, the reasoning-tier ceiling to the millisecond.

```ini
LLM_MODEL_MECHANICAL=mistralai/mistral-nemotron
LLM_CONSULT_MODEL_NVIDIA=mistralai/mistral-nemotron
```

Measured after the change: whole run **300s+ -> 19.0s**, consultation
**300,612.9ms -> 3.9ms**, and a real second opinion returning in 2.8s.

**These are already the code defaults** — set them explicitly so the running
configuration is readable rather than inferred:

```ini
GRAPH_EXECUTION_ENABLED=true    # the 24-node graph is the only originator of entries
SESSION_ONLY_TRADING=true       # no entry without an operator session
MAX_CONCURRENT_POSITIONS=1      # one trade at a time
UNTRADEABLE_SYMBOLS=BTC/USDT    # BTC stays a SIGNAL and a benchmark, never a position
PROFIT_TARGET_PCT=2.0           # per trade, of margin — also editable on the Settings page
PROFIT_TARGET_BASIS=account
```

Read the per-trade target against the payoff table in the notes below before
leaving it at 2.0.

---

## STEP 4 — deploy the code (on Oracle)

```bash
git fetch origin
git checkout fix/single-trade-originator-and-parity
git pull
git log --oneline -1     # expect: Make the 24-node graph the only originator...
```

If `git checkout` complains about local changes, they are almost certainly build
artifacts (`__pycache__`, `db/knowledge_graph.db`). `git stash` them; do **not**
stash `.env` — it is gitignored and will not appear.

---

## STEP 5 — start

```bash
docker compose up -d --build
docker compose logs -f trading-os
```

Look for, in order:

```
Connecting to database at postgresql://...
Database schema applied; all N table(s) already present.
Restored agent portfolio: 25000.00 paper cash, 0 paper position(s), 0 real position(s)
Execution service subscribed to EXECUTION_PLAN_READY (GRAPH_EXECUTION_ENABLED=True)
Consultation panel: nvidia (1 distinct provider(s)).
```

Paper cash comes back at **25,000** — `PAPER_STARTING_CASH` in
`portfolio_store`, matching the `paper_account` seed row in `schema.sql`.

---

## STEP 6 — verify

```bash
# the book is genuinely empty
curl -s localhost:8000/api/graphs/positions        # expect count: 0

# the reasoning layer is healthy and the dead model is gone
curl -s localhost:8000/api/monitoring | python3 -m json.tool | head -40
#   llmHealth.deadModels  -> []
#   llmHealth.healthy     -> true
#   telegram.enabled      -> true

# the schema survived the wipe
python scripts/verify_schema.py

# the two books agree
python scripts/repair_paper_book.py                # expect: IN STEP. Nothing to repair.
```

Then start a session from the dashboard and watch one full cycle. A run should
finish in **~19s**, not five minutes.

---

## The frontend is a SEPARATE deploy

The change set includes frontend work — the Exit Rules panel on Settings, the
trade-journey fix, and `strategy` / `runId` reaching `TradeLogEntry`. None of it
ships with the backend.

```bash
npx tsc --noEmit -p tsconfig.json
npm run test
npm run build
```

Push the branch and let Vercel build it, or merge to `main` if that is what your
Vercel project tracks.

**`DASHBOARD_PASSWORD` must be set on the Vercel deployment.** The backend proxy
attaches `TRADES_API_KEY` to every forwarded request; with no dashboard password
it authenticates nothing and refuses writes, and without that refusal any caller
reaching the public URL could invoke backend write routes with the server's
credential.

---

## Rolling back

```bash
cd ~/trading-agent
git checkout main
docker compose up -d --build
```

The database is not rolled back by this, and does not need to be: the schema is
unchanged by this release — only behaviour is.

One behavioural change to expect either way: with `GRAPH_EXECUTION_ENABLED=true`
the event-driven path no longer originates entries, so **the agent will trade
less**. Across six live runs on SOL/ETH/XRP/DOGE/BNB on 2026-09-25, every
instrument was in a Range regime and the Supervisor returned `DO_NOT_TRADE` on
all six. It was trading before because a four-leg debate was bypassing the
nine-specialist panel, not because the panel saw opportunities.
`GRAPH_EXECUTION_ENABLED=false` hands that path its old role back, and it keeps
every gate it has.

---

## Before you leave `PROFIT_TARGET_PCT` at 2.0

The target scales **down** with leverage (2% ÷ lev) while the ATR stop does not,
so the payoff inverts as leverage rises. Computed from the live SOL/USDT ATR and
this system's own 2.5×/5.0× multipliers:

```
 lev    target     stop  randomwalk   net b/e   edge req
  1x    2.000%   1.796%       47.3%     49.9%     2.6 pts
  3x    0.667%   1.796%       72.9%     77.0%     4.1 pts
 10x    0.200%   1.796%       90.0%     95.0%     5.0 pts
 ATR    3.592%   1.796%       33.3%     35.2%     1.9 pts   <- PROFIT_TARGET_PCT=0
```

"randomwalk" is `stop / (target + stop)` — for a driftless walk every one of
these is **exactly fair gross**, which is what a barrier pair does. Fees are what
make it negative, and "edge req" is the directional edge, in win-rate points,
needed just to reach zero.

At 3× you win **+1.70%** of margin and lose **−5.69%**, so you need a **77% win
rate** to break even. The measured live win rate is **54.5%** (6 of 11).

`PROFIT_TARGET_PCT=0` with `PARTIAL_TP_FRACTION=0` restores the ATR target's 2:1
geometry — the most forgiving column in that table — with no scale-out and so no
0.00 scratch exits. To keep a fixed target instead, it has to be about
`3.6 × leverage` percent (≈10.8% at 3×) to preserve 2:1.
