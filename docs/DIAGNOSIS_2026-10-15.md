# Full-system diagnosis — 2026-10-15

Scope: the deployed system as it stood on 2026-10-15, about 08:00–08:30 UTC.

| What was examined | How |
|---|---|
| Backend | through `https://trading-agent-gray-xi.vercel.app/api/backend/*` |
| Live Supabase Postgres | read-only connection |
| Binance futures DEMO account | signed read-only GETs |
| NVIDIA model endpoint | one tiny probe request per model |
| Local checks | type check, test suite and build |

**No code was changed. No write request was sent to the backend, the database
or any exchange.**

Severity: **P0** stops trading or loses money · **P1** wrong behaviour, silent
failure or security exposure · **P2** wastes time or money, or degrades
decisions · **P3** hygiene.

---

## The short answer: why nothing traded for a week

1. **The session has been frozen since 2026-10-11 01:13 UTC on a phantom
   position (F1).** Binance closed the XRP short at a profit. The backend never
   found out, cannot close what it still believes is open, and the session will
   not open anything while it "holds" a position. It has logged
   `already holding a position; the monitor owns the exit. Waiting.` every 12
   seconds for four days.
2. Even before that, the agent traded rarely, by design: 2 trades in the 30
   hours the session was flat. One coin is configured, the confidence bars are
   strict, and the account is very small (F9, F11b).
3. Behind both sits an honest problem. **No strategy has a demonstrated edge
   (F8 and the profitability section).** Even with every bug fixed, "every trade profitable" is not
   achievable. At the win rate this system measures, a $3 → $25 session reaches
   its target about 5 times in 100 (F11b).

---

## P0 — stops trading or corrupts the books

### F1 · A position the exchange closed stays open locally forever, and blocks all trading

**Evidence**

| Source | Reading |
|---|---|
| `monitored_positions` | `1c1e30c5…` XRP/USDT **SHORT** 20.1 @ 1.403, status `open`, opened 2026-10-10 23:16 UTC |
| `GET /api/graphs/positions` | same position: best price 1.2906, worst 1.4237, MFE **+24.9R** |
| Binance demo `userTrades` | **2026-10-11 01:13 BUY 20.1 @ 1.3985, realizedPnl +0.089** (one order, five fills) |
| Binance demo `positionRisk`, `openOrders`, `openAlgoOrders` | **no position, no orders** |
| `GET /api/session` | 37,897 polls, 2,469 analyses, 2 trades; log is solid "already holding a position" |
| `GET /api/admin/testnet` | mirror `enabled: true`, venue binance |

**What happened**

1. A paper SHORT opened and was mirrored to the Binance demo account, with the
   stop and take-profit resting at the exchange.
2. The trailing stop tightened into profit, and the stop resting at Binance
   filled at 1.3985. Binance triggers on MARK price, while the in-process
   monitor watches last price, so the exchange fired first. The demo account
   went **flat**.
3. Nothing told the backend. `order_stream` (`services/order_stream.py:103`)
   and `reconciliation` both run **only when `LIVE_TRADING` is on**. A mirrored
   paper position has real exchange orders behind it, and no component watches
   them.
4. Price then fell far past the take-profit. On every tick
   `position_monitor._check_price` sees `hit_target` and calls `_close`
   (`agents/position_monitor.py:1670,1741`).
5. `execution_agent.close_position`'s mirror branch (`agents/execution_agent.py`
   about lines 822–845) sends a **reduce-only BUY to an account with no
   position**. Binance rejects it, `paper_testnet.place` returns `None`, and the
   close returns `None` under the comment
   `# Connected mode must never invent a successful demo exit`.
6. `_close` logs "Close … failed … will be retried on the next tick". It
   retries on every tick for four days, and each retry is a signed order
   request to the demo API.
7. `trading_session` sees an open position and never decides again.

**The design mistake.**
- `paper_testnet.place` documents `None` as *"use the simulated fill"*
  (`services/paper_testnet.py:262`). `close_position` treats the same `None` as
  *"do not close, retry"*.
- No code path handles **"the exchange is already flat"**. That is not an
  error: it is the *normal* outcome whenever a resting stop or take-profit does
  its job.
- Two closers exist (the in-process monitor and the resting orders at the
  exchange), and only one of them is ever heard from.

**Fix direction (not applied)**
- On a failed mirrored close, query the venue position.
- If it is flat, close locally at the venue's actual fill. That price can be
  read from `userTrades` using the `stop_order_id` / `tp_order_id` the monitor
  already holds.
- Run `order_stream` and `reconciliation` whenever the mirror is active.
- Cap close retries and alert after N failures. A position that cannot close
  for an hour should page someone, not loop silently.

**To unblock now (operator action, not done):** the demo account is
flat, so forgetting the local row is safe. Either call
`POST /api/admin/reset-paper`, or stop the backend, delete the
`monitored_positions` row and restart. Then book the demo's +0.089 result
by hand if you want the paper record to match.

### F2 · The paper book never held the stuck position (book and watch list disagree)

- `agent_positions` is **empty**, while the watch list holds the XRP short.
- `agent_paper_account.cash = 2.8315`, last written 2026-10-09 21:15, which is
  *before* the 2026-10-10 open. That open never debited margin.
- Margin for the open was 20.1 × 1.403 / 10 = **2.82** plus about 0.014 fee,
  against cash of 2.83. The book almost certainly refused it as unfundable.
  `_execute_tar` then published `ORDER_FILLED` anyway, because a real (demo)
  order stood behind it: the documented "mirrored → publish and log CRITICAL"
  branch.
- Consequences: the session's equity, the dashboard, and progress toward the
  $25 target were computed from a book that did not contain the position the
  agent was managing. Had the close succeeded, settling it against a book with
  no position would itself have failed.

**Root cause:** sizing at `capital_fraction 1.0` and 10x on $3 puts margin
within about one cent of cash. The gateway's `fee_reserve` and the book's
affordability check disagree by roughly the fee.

**Fix direction:** both sides must use the same fee model and rounding, or
the gateway should leave a fixed margin of safety (e.g. 1%) at full
allocation. A mirrored fill that the book refuses should be **closed at the
venue immediately**, not tracked.

---

## P1 — wrong behaviour, silent failure, or exposure

### F3 · The reasoning model died today, and the consultation model with it

Direct probe of the NVIDIA endpoint, 2026-10-15 ~08:20 UTC:

| `.env` slot | Model | Result |
|---|---|---|
| `LLM_MODEL_MECHANICAL` | `openai/gpt-oss-20b` | **alive**, 200 in 1.5s (CLAUDE.md calls it dead; that note is now wrong) |
| `LLM_MODEL` / `LLM_MODEL_NARRATIVE` | `nvidia/nemotron-3-super-120b-a12b` | alive, 200 in 0.9s |
| `LLM_MODEL_REASONING` | `moonshotai/kimi-k3` | **HTTP 410 Gone — end of life 2026-10-15T08:00Z** |
| `LLM_CONSULT_MODEL_NVIDIA` | `moonshotai/kimi-k3` | **same, 410** |
| *(CLAUDE.md's documented replacement)* | `mistralai/mistral-nemotron` | **410, end of life 2026-09-28** |

From today, every trade reflection (REASONING tier) and every second opinion
fails over to the fallback path. CLAUDE.md also still describes
`mistral-nemotron` as the fix for the earlier dead-model incident; that model
is dead too.

**Fix direction:**
- Point `LLM_MODEL_REASONING` and `LLM_CONSULT_MODEL_NVIDIA` at live models.
- Keep the consultation on a **different family** from the reasoning tier
  (CLAUDE.md's own rule).
- `gpt-oss-20b` is alive and fast again, and is a candidate for the
  consultation slot.

### F4 · Even before EOL, kimi-k3 produced garbage, and it was stored as a "lesson"

`reflections` holds exactly **one** row, the lesson for the 2026-10-09 losing
trade:

    lesson_source = model
    lesson_detail = moonshotai/kimi-k3, 2477 tokens, 44304ms
    content       = "<|close|> w.<|close|>think,'. . the , .,0. A.S +, not available., X9. The in. A. 、 ```!.<|close|>!1. ..."

Nothing validates model output before it is persisted as a lesson. It looks
like control tokens leaking from a reasoning model. The `llmHealth` panel calls
this "healthy" because the HTTP call succeeded; an output that is not
language is not classed as a failure. `hypotheses` has **0 rows**, so the
learning loop has never produced a thing.

**Fix direction:** reject a completion that contains special tokens
(`<|…|>`) or fails a basic sanity check (minimum share of letters, sentence
structure), and record it as a fallback with that reason.

### F5 · Every analysis wastes about 38 seconds waiting on models that answer nothing

The one stored `trade_analysis` trace (2026-10-15 02:28, a `price_move`
trigger):

    decision path, memory_loader -> risk_gateway   ~1.0 s total
    external_consultation                          30,005 ms   llm_calls = 0   (hit CONSULT_DEADLINE_S)
    trade_thesis_narrative                          7,944 ms   llm_calls = 0
    whole run                                      39.2 s

The trade itself is not delayed: the plan publishes at the gateway. But
`trading_session` awaits the full run between decisions, so each decision
costs about 40s of session time for zero LLM output. `/api/monitoring`
reports 20 of the last 500 calls as `empty_completion` and still says
"healthy". Fixing F3 should remove most of this.

### F6 · The backend has NO authentication

`GET /api/admin/status`:

    "writeAuthEnabled": false, "note": "TRADES_API_KEY is not set, so ALL endpoints are open."

`require_write_auth` (`core/auth.py:76`) is a **no-op when the key is unset**.
The Vercel proxy has its own password check, but anyone who can reach the VM's
port 8000 directly can:
- turn on live trading,
- switch venue,
- start or stop sessions,
- reset the paper book,
- place real orders through `/api/operator/exchange/order`.

`/api/ai/chat` has no auth at all, even when the key is set, so anyone can
spend your NVIDIA quota. The local `.env` also defines `TRADES_API_KEY`
**twice**.

**Fix direction:**
- Set `TRADES_API_KEY` on the VM and in Vercel.
- Restrict port 8000 to Vercel's egress, or bind it to localhost behind a
  tunnel.
- Make the auth dependency fail CLOSED when the variable is absent.

### F7 · Mirrored paper positions have no venue-state observation at all

This is the general case behind F1. `order_stream.stream_enabled()` and the
reconciliation loop both check `settings.LIVE_TRADING`. When the testnet
mirror is on, paper positions hold **real resting orders** at Binance demo.
Nothing compares or observes them, so every disagreement between the venue
and the book is invisible until it blocks something.

### F8 · Strategy preference flips with whichever backtest was run last

`services/strategy_priors` reads only the newest `backtests/*/summary.json`
(currently 2026-10-08). The two committed runs disagree almost perfectly:

| Strategy | 2026-09-06 | 2026-10-08 |
|---|---|---|
| Scalping | **+0.160R (best)** | **−0.309R (worst)** |
| VWAP | **−0.328R (worst)** | **+0.241R (best)** |
| Grid | −0.206R | +0.113R |
| Momentum | +0.125R | −0.282R |

Each window is about 10 days of 15m/1h candles. Rankings that invert month to
month are measuring the **market regime of that window**, not a strategy's
edge. Feeding the newest one into live selection means "re-run backtests" is
also "silently change which strategies the live agent prefers".

**Fix direction:**
- Pool all runs, or require a strategy's sign to agree across N windows
  before giving it a prior.
- Otherwise treat it as neutral.

---

## P2 — wastes time or money, or degrades decisions

### F9 · The session watches ONE coin, and the rotation is unused again

The active session (`bf5f77cc4c16`, started 2026-10-09 17:00 UTC) has
`watch_symbols: []`. CLAUDE.md records that one coin is "flat most of the
time" and that the five-coin rotation was the fix. That rotation was lost when
this session was started. Starting it with XRP/SOL/DOGE/ADA/SUI costs the
same LLM budget (the rotation is cost-neutral) and multiplies the chances of
finding a setup.

**Correction from the module test:** with `SESSION_ONLY_TRADING=true` (your
`.env`), adding coins does **not** work today. The session-scope gate refuses
every coin except the primary one; see **F21**. F21 must be fixed first, or
the rotated coins are analysed and then always refused.

### F10 · The decisions table is flooded by a path that is switched off

`decisions` holds 2,366 rows, **all from the last 86 minutes**. Every one is:

    agent-plan | rejected | "entries are originated by the 24-node analysis graph while
    GRAPH_EXECUTION_ENABLED=true, so this event-driven path does not submit its own…"

That is about 27 rows a minute, from `agents/supervisor_agent` re-evaluating
ETH and XRP on every tick. Each evaluation also runs the debate, confidence
calibration and a 2000×200 Monte-Carlo stress test. Retention keeps about
2,000 rows, so **this noise evicts every real decision record within about 1.5
hours**, and "why didn't it trade yesterday?" can no longer be answered from
the database.

**Fix direction:** when `GRAPH_EXECUTION_ENABLED=true`:
- unsubscribe the event-driven chain (debate → confidence → portfolio →
  simulation → supervisor) entirely, or
- record its refusal once per symbol per hour, not on every tick.

### F11 · Agent analysis runs on DEMO market data while the mirror is on

`binance_testnet.demo_data_active()` switches the price feed, candles and
microstructure (order book, tape) to `demo-fapi.binance.com` whenever the
mirror is active. Tick events carry `"exchange": "binance_testnet"`. Demo
prices track mainnet closely; demo **order books and trade tapes do not**.
They are thin and synthetic. So the `orderflow` and `liquidity` specialists,
3.0 of 7.0 debate weight, are reading a market that does not exist. The fills
are consistent with the venue that executes them, but the *signals* are not
representative of the real market you plan to trade.

### F12 · Only one `trade_analysis` trace is retained

`GET /api/graphs/runs?graph=trade_analysis&limit=200` returns **one** run. The
trace store is shared with `position_monitoring`, which runs on every tick per
open position, so analysis traces are evicted almost immediately. Combined
with F10, there is effectively no history of why the agent did or did not
trade.

### F13 · Test data in the production `risk_events` table

`risk_events` holds two CRO approvals on 2026-10-02 with
`entry 1, stop 0.9, leverage 1x`: clearly synthetic fixtures. A harness or
test was pointed at the live database. CLAUDE.md records this happening once
before with `trades`.

### F14 · Dashboard ticker returns `null` on first request for a symbol

`/api/marketdata/ticks` subscribes on demand and reconnects the socket. The
first poll for a symbol not already subscribed returns `null` (observed for
BTC, SOL and ETH; filled on the next poll). This is cosmetic, but
`connectAttempts` climbs with every new symbol (17 so far), because each new
symbol reconnects the whole combined stream.

---

## P3 — hygiene

- **F15** `docs/API_REFERENCE.md` is generated by
  `scripts/generate_api_reference.py` from the live app, including the Auth
  column. It has been hand-rewritten in the working tree, and an identical
  untracked copy sits at the repo root. Regenerate it or keep it generated;
  a hand-written auth column is how F6 goes unnoticed.
- **F16** 277 `__pycache__/*.pyc` files are tracked in git (CPython 3.13).
  Every commit, including "v7.3", carries binary churn. `.venv` is also not in
  `.gitignore`.
- **F17** `package.json` scripts use `set NODE_OPTIONS=…`, which is Windows
  `cmd` syntax. On macOS/Linux it silently does nothing, so `npm run build` runs
  without the 4 GB heap flag. (The build still passed here.)
- **F18** `.env` contains `LLM_CONSULT_KEY_NVIDIA` in plain text. It was printed
  once during this diagnosis by mistake, so **rotate that key**.
- **F19** CLAUDE.md is stale in places that would mislead the next reader:
  - the margin buffer is now 1.0 (`MARGIN_BUFFER_MULTIPLIER`), not "1.2x, 100%
    deploys ~83%";
  - `gpt-oss-20b` is alive;
  - `mistral-nemotron` is dead.

---

## Round 2 — module-by-module test (2026-10-15, later the same day)

Six review tracks covered:
- the execution plane,
- the graph decision path,
- sessions/services/LLM/bus,
- the backend API,
- the frontend lib and API routes,
- the frontend UI.

Alongside them ran a live sweep of every backend GET endpoint (70), every
Next.js API route (25) and every dashboard page (26), plus the full backend and
frontend test suites. A finding is listed only if it was **confirmed**: either
an offline reproduction (scripts under the session scratchpad, against fake
venues and a dummy database) or an exact code read naming the line.

The backend-API review track was cancelled before it reported. Its ground is
covered by the live sweep and by findings F34–F36 and F50–F51.

### P0 — stops trading, loses money, or disables a safety control

#### F20 · The Emergency Stop and Pause buttons cannot reach the backend

`components/shell/EmergencyStopModal.tsx:73` and
`components/shell/TopBar.tsx:123` both set
`Authorization: Bearer ${process.env.NEXT_PUBLIC_TRADES_API_KEY || ''}`.

Setting an explicit `Authorization` header stops the browser from sending its
cached dashboard (Basic) credentials. The proxy
`app/api/backend/[...path]/route.ts` accepts only Basic when
`DASHBOARD_PASSWORD` is set, and refuses every write when it is unset. So
**on every configuration both buttons get HTTP 401**:
- The stop modal shows "NOT stopped".
- Pause swallows the error (`catch {}`), so nothing visible happens.

*Verified:* a vitest repro against the real route handler. `Bearer ` returned
401; the correct Basic header returned 200.

*Do not "fix" it by setting `NEXT_PUBLIC_TRADES_API_KEY`.* That would compile
the backend's write key into the public JavaScript bundle. The fix is to remove
the header and let the browser's Basic credentials through.

#### F21 · The multi-coin rotation can never open a position on any coin but the first

`graphs/nodes/risk_gateway.py:421` and `services/trade_scope.py:147` compare
`session.symbol` with the symbol under analysis. They should test membership
of `session.scan_list()`.

With `SESSION_ONLY_TRADING=true` (set in `.env`):
- every rotated coin (SOL, DOGE, ADA, SUI) runs all 24 nodes and is then
  refused with "the running session is on XRP/USDT, not SOL/USDT";
- 4 of every 5 decisions, and their LLM budget, are wasted.

`tests/test_session_symbol_rotation.py` never drives `gate()`, so the suite
passes.

*Verified:* offline repro on a session for XRP watching SOL. XRP was approved;
SOL was refused with that reason.

#### F22 · While an analysis or reflection runs, that coin's price feed stops, so its stop cannot fire

The causal-chain delivery in `core/message_bus.py:publish` queues a nested
publish into the *publisher's own* `await bus.publish(...)`. So the
tick watcher in `services/live_market_data.py` stays blocked until every event
its tick caused has been delivered. There are two slow paths.

**Path A:**
1. A tick reaches `TriggerWorker._on_tick`, which publishes `TRIGGER_FIRED`.
2. `analysis.on_trigger` awaits the whole graph run.
3. In production that run took **39 seconds** (the 2026-10-15 02:28 trace).
4. This also delays `EXECUTION_PLAN_READY` until the run ends, which undoes
   the "plan on the bus at 4.8 s" streaming fix on the trigger path.

**Path B:**
1. A tick triggers `position_monitor._close`, which publishes
   `POSITION_CLOSED`.
2. `ReflectionAgent` awaits `run_reflection_graph` on the REASONING tier.
3. That tier has a 300 s timeout plus rate-limiter and 429 waits. A stop can
   be blind for minutes.

For that whole window no `TICK_RECEIVED` is published for the symbol, and
`_check_price` never runs. Paper positions have no resting stop at all. A
session re-enters immediately after a close, often on the same coin whose
feed is still stalled.

*Verified:* offline repro with the real `MessageBus`. Ticks were seen at
0, 0.25, 0.5 and 0.75 s, then **nothing until 5.01 s**. A plan published at
1.76 s was delivered at 4.76 s. The real code path matches: `_on_tick`
awaits `_publish`, and `on_trigger` awaits `run_analysis_graph`.

#### F23 · A small REAL position cannot be closed, and its resting stop is refused, by our own pre-check

`services/venue.py:787` (`market_order`) and `:851` (`place_stop_loss`) call
`check_size`. That applies the venue's **minimum notional** to reduce-only
closes and stops. Binance exempts reduce-only orders from that minimum.

Scenario:
1. Open 4.0 XRP at 1.28, a notional of $5.12.
2. The stop is 2.5 % away. At the stop price the notional is $4.99, below the
   $5 minimum.
3. The resting stop is refused at entry.
4. When the stop fires, `close_position` is refused on every tick. **The
   position cannot exit**, which breaks invariant 4.

This is exactly the $2–$5-account regime this project runs in.

*Verified:* offline repro. The fake exchange received zero orders.

#### F24 · Trailing the resting stop can leave NO stop, or an orphaned one, at the venue

Every replacement places the new stop with the same client id `sl_{tar_id}`
(`agents/position_monitor.py:1049-1055`). Replacements are also fired as
unawaited tasks with no serialisation (`_replace_resting_stop_soon`), and the
trailing stop calls `tighten_stop` on every new peak.

- **Binance-like (client id unique among open orders):** two ticks 20 ms
  apart ended with a stop **resting at the venue that the monitor no longer
  knows about** (`stop_order_id=None`). A close cannot cancel it and a
  restart cannot find it. In hedge mode it can open a reversed position.
- **Bybit (`orderLinkId` unique for all orders):** even one tighten cancels
  the old stop and has the new one refused. **No stop rests at all.**

*Verified:* offline repro, with three CRITICAL "NO RESTING STOP" logs. The
Bybit round-trip script uses a fresh id for its tighten, so it never tested
the real path.

#### F25 · The browser invents prices, and AI paper trades use them (invariant 6)

`components/MarketData.tsx:205-228` runs every 4 s. For any **crypto** price
older than 12 s it writes a random walk into the shared `ticks` map, starting
from **$50,000** when there is no prior price. The comment claims "only for
symbols with NO real tick", but the code applies it to stale crypto too.

Nothing downstream filters on `source:'sim-fallback'`:
1. `Agent.tsx:236` feeds those prices to `agentTick`.
2. `Supervisor.reviewAndExecute` then calls `buyPaper`/`sellPaper`/
   `removeRealPosition` at that price.
3. A backend feed stall of 12 s therefore produces paper trades and P&L at
   fabricated prices.

*Verified:* code read of producer and consumers.

### P1 — wrong behaviour, silent failure, or exposure

#### F26 · Close idempotency keys leak and are reused by a later position

`execution_agent.close_position` keys its client id on
`(tab, symbol, side, qty, reason)` and pops the key only on success. If a
take-profit close fails and the position then exits by stop-loss, the
take-profit key is left behind.

A later position with the same quantity (likely on minimum-size accounts) then
reuses the **old** client id:
- Bybit rejects it as a duplicate.
- On the Binance demo mirror an "unknown" error can look up the old order and
  return **the previous position's fill** as this one's.

*Verified:* offline repro. The same `close_6b78…` id was sent for a different
position.

#### F27 · An EXIT plan for a held SHORT gets side "sell"

`risk_gateway._exit_plan` (`:1001`, `:1032`) derives the side from the sign of
`qty`, but the book stores a positive qty plus `side`. That is the same bug
CLAUDE.md records as fixed in the Supervisor.

On the fallback close path (`execution_service._close`, when the monitor does
not track the position), the close is sent as if the position were long. That
**adds to the short** instead of closing it.

*Verified:* offline repro printed `EXIT of a held SHORT -> plan side: sell`.

#### F28 · The volatility layer's leverage cap is computed but never applied

`algorithms/volatility.py:126-132` sets `MAX_LEVERAGE_BY_REGIME`: HIGH 2x,
NORMAL 4x, EXTREME 1x. The value lands on `VolatilityState.max_leverage`, and
nothing reads it, although its docstrings and evidence text say the Risk
Gateway applies it.

A 10x session in a HIGH-volatility regime trades at **10x**.

*Verified:* offline repro with `VolatilityState(regime="HIGH", max_leverage=2)`
produced a plan with `leverage=10`.

#### F29 · The debate's Volatility leg never fires in a trend (string mismatch)

`algorithms/debate.py:290-293` checks for `"Trending Bullish"` and
`"Trending Bearish"`. The regime classifier returns `"Bull Trend"` and
`"Bear Trend"`. As a result:
- in trends the leg always scores 0, while its 1.0 weight stays in the
  denominator and dilutes conviction;
- Panic, Euphoria and Liquidity Crisis are never treated as high volatility.

*Verified:* offline repro on a synthetic uptrend. Confidence was LONG 0.179 as
written, and 0.248 with the strings matched. That matters against a 0.18 bar.

#### F30 · Turning on live trading mid-session leaves the session watching the paper book

`trading_session.py:511` reads `tab` once, and the live-trading enable route
(`api/admin.py:594`) does not check for a running session. After the switch:
- the graph trades the **real** book;
- target and floor are compared with **paper** equity, so the floor never
  fires on real losses;
- `count_fill` counts only paper fills, so `MAX_TRADES_PER_SESSION` never
  advances.

*Verified:* offline repro.

#### F31 · The manual real-money ticket's leverage does nothing, and the order goes to Binance SPOT

`components/operator/TradePanel.tsx:211-223` omits `leverage` and hardcodes
`exchange:'binance'`. `api/operator_exchange.py:183` deliberately builds a
`defaultType: "spot"` client.

The panel's slider goes to 10x and sizes `qty = balance × fraction ×
leverage / price`. At 5x / 100 % the operator is shown 1× balance of margin but
submits a **5×-balance spot order**, which is rejected or spends 5× the cash
shown. On a Bybit deployment it still goes to Binance. Also, a real Sell has no
balance check.

*Verified:* code read of both sides.

#### F32 · The browser paper path still uses the 1x-only equity formula

`components/Supervisor.tsx:145-148`, `AutonomousTrader.tsx:242` and
`PaperTradePanel.tsx:23` compute `cash + qty × price`. But `buyPaper` deducts
only the margin, so at 10x a $7,000 position funded by $700 adds
**$6,300 of equity that does not exist**. CLAUDE.md records this formula as
removed. It survives in the browser Supervisor's sizing inputs.

*Verified:* code read.

#### F33 · The live-trading toggle in Settings reports nothing when refused

`app/(terminal)/settings/page.tsx:74-86` never checks `res.ok`. A 401/403 from
the proxy or backend (`{error}` / `{detail}`) shows no message, and the badge
keeps its old state. The confirm text still says "3× leverage" (the ceiling is
10x), and the no-keys hint names only Binance.

*Verified:* code read.

#### F34 · Public Next.js `/api` routes: anyone can read the books and rewrite them

`middleware.ts` excludes `/api`. Of 21 write handlers, only `/api/trades` and
`/api/trades/[id]` check anything.

**Live check, no password:** `/api/trades`, `/api/portfolio`, `/api/decisions`
and `/api/stats` all answered **200** on the public Vercel URL.

Open to anyone:
- `PUT`/`DELETE /api/portfolio`: replace or wipe the browser book;
- `PUT /api/watchlist`;
- `POST /api/decisions`;
- `POST`/`PATCH /api/hypotheses` (mark a hypothesis *applied*);
- `/api/missions`, `/api/memory`, `/api/reflections`, `/api/debate`,
  `/api/strategy-versions`, `/api/pv-history`, `/api/autonomous-cycles`,
  `/api/collaboration`, and the three backtest routes (CPU).

*Verified:* live GETs, plus a grep of every handler for any caller check. No
write was sent.

#### F35 · Browser trade-log writes break as soon as `TRADES_API_KEY` is set

`app/api/trades/route.ts:15-33` requires `Bearer <TRADES_API_KEY>`, which the
browser never has. `components/Portfolio.tsx` (`logTrade`, lines
127/155/251) never checks `res.ok`.

The moment you set the key (the F6 fix):
1. Every browser paper fill, including Supervisor-approved AI trades, is
   silently refused.
2. The optimistic row vanishes on refresh.
3. Closing P&L never reaches `/api/stats`.

With the key unset, anyone can POST fabricated closes. **F6 and F35 must be
fixed together.**

*Verified:* code read.

#### F36 · Two server-side request relays (SSRF)

- **`POST /api/mcp-status`** (no auth) fetches any http(s) URL from Vercel and
  returns its status and latency. That is a blind probe.
- **`POST /api/chat`** (no auth) forwards a caller-chosen `baseUrl` to the
  backend's `/api/ai/chat` (no auth). The backend then **POSTs to that URL
  from the VM and streams the reply back**. That turns the VM into a POST relay
  into its own network.

Backend admin routes are not reachable this way (the outgoing token is the
caller's own).

*Verified:* code read; not exploited.

#### F37 · The funding specialist reports BTC's funding for every coin

`agents/sentiment_agent.py:53` hardcodes `premiumIndex?symbol=BTCUSDT`, and
`specialists.py:599` presents it as the funding of SOL, XRP and the rest. That
is a 1.0-weight directional vote about the wrong market.

*Verified:* code read.

### P2 — degrades decisions, data or reliability

- **F38 · Two Next routes return 500 in production.**
  - `GET /api/collaboration` and `GET /api/volatility-history` both returned
    HTTP 500 live.
  - Their stores (`lib/collaborationStore.server.ts`,
    `lib/volatilityHistoryStore.server.ts`) write only to `.data/*.json`, and
    Vercel's filesystem is read-only. Even a GET fails, because the file is
    created on first read.
  - A `collaboration_requests` table exists in Postgres but nothing writes it,
    so every browser second-opinion record is lost.
- **F39 · A corrupted `ai_memory.json` silently disables the daily-loss check.**
  - `_save_memory` truncates in place (not atomic), and `_load_memory` swallows
    a parse error and returns `{}`.
  - `ledger_for_tab` then returns `[]`, so `check_daily_loss` passes after a
    losing day, and `record_closed_trade` stops recording forever. *Offline
    repro.*
- **F40 · Every hour the whole backend freezes on a SQLite VACUUM.**
  - `retention.py:232` calls the synchronous `prune_checkpoints()` (deletes +
    `VACUUM`) inside async code.
  - Ticks, stops, HTTP and emergency-stop all freeze. *Repro:* 9.2 s for a
    246 MB file.
- **F41 · A refused `start_session` still rewrites paper cash.**
  - `set_paper_starting_amount` runs before the "already running" and other
    refusals.
  - A second Start click with $2 sets cash 10,000 → 2, and the running session
    then ends "floored". *Offline repro.*
- **F42 · Retention prunes the decisions that produced trades, and CRO vetoes
  are never recorded.**
  - Retention keeps only `approved-executed`/`manually-approved`, which no
    backend code writes. Backend TARs stay `pending-approval` and get pruned.
  - `supervisor_agent.py:268` writes `rejected-by-risk`, which the
    `decisions.outcome` CHECK (`db/schema.sql:117`) forbids, so every veto
    update fails and is swallowed. *Code + schema read.*
- **F43 · A dead price feed keeps serving its last price as live.**
  - `market_data` caches (`_live_prices`, `_prices`) carry no timestamp, so the
    last price is served indefinitely.
  - That price feeds equity, gateway entry and operator panel figures.
    *Offline repro.*
- **F44 · The graph's "panel now opposes my position" EXIT is rarely reached.**
  - It only runs when a fresh entry thesis exists
    (`analysis.py:135`, `supervisor.py:179`).
  - Stops and targets still protect the position. *Code read.*
- **F45 · TS backtest risk is 1.67× the configured risk.**
  - Applies under `volatility-based` sizing.
  - `lib/positionSizing.ts:24` assumes a 1.5× ATR stop; the real stop is 2.5×
    ATR. This was missed by the TS↔Python alignment.
- **F46 · Browser hypotheses are never generated.**
  - `components/Hypothesis.tsx:68-71,130-147`: the 5 s interval calls the
    mount-time `generate`, which holds a stale `getReflection` and API key.
  - New reflections are retried forever and never become hypotheses. This
    breaks the ref-in-interval rule.
- **F47 · The browser reflection gets the wrong entry price for shorts.**
  - `components/Reflection.tsx:69-72` uses `price − pnl/qty`; for a short the
    entry is `price + pnl/qty`.
  - The opening-leg lookup searches only `side==='buy'`.
- **F48 · A real ledger close with no exit price is logged as a $0
  break-even.**
  - `components/Portfolio.tsx:224-231` books the close at the entry price, so
    the win rate counts a trade that has no measured result (invariant 6).
- **F49 · The session panel cannot build a working rotation.**
  - It defaults to `BTC/USDT`, which `start_session` refuses.
  - Its coin list lacks DOGE/ADA/SUI (`AutonomousSessionPanel.tsx:118,134`).
- **F50 · `/api/research/benchmark` reports fabricated alpha.**
  - `tools/benchmark.py:30`: `baseline_pnl = total_pnl * 0.8` ("Assume AI beats
    naive hold by 20% in this mock"). It always reports +20 % alpha.
  - No page shows it, but it is a public API answer made of invented numbers
    (invariant 6).
- **F51 · `/api/exchange/tickers` always returns 0 rows.**
  - `api/exchange.py:137` filters `symbol.endswith("/USDT")` on futures keys
    (`BTC/USDT:USDT`). CLAUDE.md records fixing exactly this in
    `market_data.py`; this copy was missed. *Live:* `count: 0`.
- **F52 · The backend test suite writes into a tracked file.**
  - Running `pytest` modified `db/knowledge_graph.db`, which is committed to
    git. (Restored after this run.)
- **F53 · The suite misses the failure that actually stopped trading.**
  - **2,243 passed / 2 skipped** with F1, F21, F22, F23 and F24 all present.
  - No test covers "the venue is already flat", "the gate with a rotated
    symbol", "tick delivery while a subscriber is slow", "reduce-only below the
    minimum notional", or "two overlapping stop replacements".

### P3 — minor

- **Model health:** `llm/health.py` keeps a model flagged dead after it
  recovers, until the 500-call window rolls over.
- **Session log and file:** `trading_session._note` appends "already holding a
  position" every 12 s, pushing every decision out of the 50-line log. Each
  note also rewrites `sessions.json` non-atomically.
- **Missing P&L read as a win:** `graphs/nodes/reflection.py:170` turns a
  missing `pnl` into 0.0, classifies it "Success", and derives a calibration
  delta from it.
- **Proxy path:** a `..` segment in `/api/backend/..` resolves to backend
  `/openapi.json` and `/docs`, with the server key attached. Low impact.
- **Middleware:** `atob` throws (HTTP 500) on a malformed header. Passwords
  containing `:` are truncated. Comparisons are not constant-time.
- **Backtest route:** `/api/backtest` accepts `"constructor"`/`"__proto__"` as a
  strategy name and crashes.
- **Optimistic gap fills:** the TS backtest fills a gapped stop at the stop
  price, not at the open.
- **Stale data on switch:** `useBackend` keeps the previous path's data after a
  path change, so `/replay` and `TradePanel` briefly show the previous
  run/symbol.

### Suspicious, not confirmed (worth a test, not yet a finding)

- `position_store.save_watch_list` does DELETE+INSERT with fire-and-forget
  writes. A stale snapshot committing after a close could resurrect a closed
  position on restart.
- Leverage is not persisted on the watch row, so after a restart the
  account-basis profit target is computed at 1x. It only matters if
  `PROFIT_TARGET_PCT > 0`.
- `_venue_backed(pos)` asks "is the mirror on *now*", not "was this position
  mirrored". A position opened before the mirror was enabled would be closed
  through the mirror and refused forever (F1's loop, from a different trigger).
- `rate_limit.acquire` has no overall cap under sustained overload.
- `telegram_notifier` creates tasks without keeping references, so a send could
  be garbage-collected.
- `/api/trades` has no LIMIT, the same growth pattern that hit `decisions`.

---

## Round 3 — what stops it running 24/7, and what blocks profit

Two more review tracks ran: continuous unattended operation, and profit logic.
The profit track used real mainnet klines for 8 coins × 4,499 15m bars
(2026-08-30 → 10-15), run through the system's own strategy functions, the
live ATR and HTF gate, 1m bars for exits, and 0.05 % taker per side. A live
re-check confirmed nothing has changed: the session is still stuck on F1
(39,142 polls, 2 trades), observation mode is off, and the high-water mark is
$2.83.

### P0 — stops the agent running unattended

#### F54 · Any restart ends trading until a human starts a new session

`services/trading_session.py:272` (`restore`) marks every `running` session
`stopped` ("NOT resumed automatically"). With `SESSION_ONLY_TRADING=true`,
`trade_scope.py:139` then refuses every entry: "no trading session is
running". Every deploy, OOM kill or VM reboot, including the systemd
`Restart=always` recovery, ends trading silently. Telegram sends only fills
and closes, so you are not told.

*Verified:* code read and offline repro (`interrupted: 1 | active: None`, then
the refusal).

#### F55 · Observation mode latches on forever, and the session keeps burning budget under it

- `agents/ceo_agent.py:294` enters observation mode.
- Only `POST /api/admin/observation/resume` (`api/admin.py:570`) clears it;
  the monthly high-water-mark reset does not.
- The session loop (`trading_session.py:566-572`) checks only pause and
  emergency stop.

With `MAX_DRAWDOWN_FROM_HWM=30 %` and VaR 0.12 (about 8 % per stop-out), about
four losses in a row trips it, roughly one run in six at a 36 % win rate.
After that the session runs a full, always-refused graph every 30 s,
indefinitely, while showing "running".

*Verified:* code read.

#### F56 · The high-water mark is not reset when the book is reset or re-funded

`set_paper_starting_amount` (`trading_session.py:830`) and `reset-paper`
never call `rearm_high_water_mark`; only the admin route does. If the CEO
agent last saw a $10,000 book and the operator then starts a $3 session, the
first close computes **99.97 % drawdown** and F55 latches.

*Live today:* the mark is correctly $2.83 (a restart re-anchored it), so this
has not fired, but nothing prevents it. *Offline repro:* `halted True 99.97`.

#### F57 · One failed database read at boot wipes the watch list and the paper book

`portfolio_store.py:166-180` and `position_store.py:230-238` treat a read
error as "empty". The next persist then overwrites the stored rows:
- `DELETE FROM monitored_positions` (real positions with resting stops are
  forgotten);
- `DELETE FROM agent_positions`;
- cash is reset to 25,000.

`core/db.py:init_db` is never retried, so if Supabase is down at boot, the
process runs its whole life without a database.

*Offline repro:* a fake pool failing the first read produced all three writes.

#### F58 · A REAL session ends "floored" right after its first fill

`current_equity('real')` (`trading_session.py:466`) is free USDT only. Agent
fills never write the local real book, so locked margin and unrealised P&L are
missing. $100 at 3x with $60 locked reads equity 40, below the 50 floor, so the
session finishes. `real_account_balance` also uses the Binance-hardcoded
`exchange_client`, so on Bybit it reads the wrong account.

*Offline repro.*

### P1

#### F59 · One ordinary stop-out halts entries for the rest of the UTC day

`core/risk_manager.py:63,681`: the daily-loss limit is a flat 5 % of
*current* equity and does not scale in broker mode. One stop-out at VaR 0.12
is about 8 %, so entries are hard-rejected until 00:00 UTC while the session
keeps running refused graphs. Earlier losses on a larger book also count
against a new small one.

*Offline repro.*

#### F60 · The CEO drawdown kill switch never evaluates the REAL book

`supervisor_agent.py:746` `_equity_for` returns 0 without a local cash figure,
which `_read_equity` maps to "unevaluated". The paper path still uses the 1x
`cash + qty × mark` equity, ignoring direction.

*Verified:* code read.

#### F61 · No gate weighs fees against stop distance; tight-stop trades cannot win

There is no stop floor (`opportunity.py:557`) and no fee-to-R check in
`risk_gateway`/`supervisor`. With a 0.10 % round trip, the last 10 CRO-approved
live plans:

| Stop % | Fee as R | Break-even win rate at 2:1 |
|---|---|---|
| 0.32 (last live trade) | 0.31 R | **43.8 %** |
| 0.39 – 0.48 (6 plans) | 0.21 – 0.26 R | 40.3 – 41.9 % |
| 0.82 – 1.17 (3 plans) | 0.09 – 0.12 R | 36.2 – 37.4 % |

Measured win rate is 34–36 %, so **7 of the last 10 approved trades needed a
win rate the system has never shown just to break even**. A 0.32 % stop is
also below anything mainnet XRP 15m produced in 47 days (minimum 0.336 %). That
ATR came from an extreme quiet tail or from demo candles (F11).

#### F62 · The live trailing stop lowers expectancy, and every backtest models a different exit

The trail (`position_monitor.py:255-256, 1861-1920`) arms at +1R, follows 1R
behind the peak, and keeps the 2R target. A paired test on 7,591 identical
entries, net of fees:

| Exit policy | All signals | After HTF gate (n = 3,724) |
|---|---|---|
| Fixed 2:1, no trail | −0.063 ± 0.016 R | **+0.003 ± 0.023 R** |
| **LIVE trail (1R / 1R)** | −0.078 R (Δ −0.015, z −1.7) | **−0.025 R (Δ −0.027, z −2.1)** |
| No target, arm 2R / trail 1.5R | **Δ +0.052 (z +4.9)** | Δ +0.020 (z +1.4) |
| Fixed 3R, no trail | Δ +0.033 (z +3.2) | Δ +0.026 (z +1.7) |

Arming at +1R turns the 1–2R excursions that would have paid 2R into small
scratches: 417 exits at about 0R and 770 at about +0.25R. The stored
backtests, the strategy priors and the HTF-gate measurement all assume fixed
2:1, a different exit from the one running live. (Correction to an earlier
assumption: a trade peaking at +1.9R exits at about +0.9R, not 0R. The scratches
come from peaks just above +1R.)

#### F63 · At $3 and 10x, every trade risks about 3–8 % of equity plus about 1 % in fees, with no measured edge

The broker-style notional is $30, and the VaR cap binds only above a 0.8 % stop.

| Stop | Notional | Loss at stop | Round-trip fee |
|---|---|---|---|
| 0.32 % | $30 | 3.2 % of equity | 1.0 % of equity |
| 0.8 % | $29.9 | 8.0 % | 1.0 % |
| 1.2 % | $19.9 | 7.96 % | 0.66 % |

The measured net edge after the HTF gate is zero (+0.003 ± 0.023R), or
negative with the live trail. Kelly sizing for a zero edge is zero, so any
positive size is over-betting, and 8 % per loss is what makes F55/F59 fire.

### P2

- **F64 · Rotating away from a coin can drop its close processing.**
  `live_market_data._reconcile` (`:215`) cancels a watcher while its tick
  chain is still being delivered. The bus catches `Exception` but not
  `CancelledError` (`message_bus.py:250`). The Telegram close alert, the CEO
  evaluation, the hypothesis and `REFLECTION_COMPLETED` for that close are
  lost. *Offline repro:* only `reflection-start` was delivered.
- **F65 · Approved-but-unfilled entries leak forever.** `_pending` in
  `position_monitor` is cleared only by a fill or by `clear_all`. TARs refused
  after approval (venue minimum, unfundable, restart) are re-persisted and
  re-restored indefinitely.
- **F66 · The volatility stop multiple is never applied.**
  `STOP_ATR_MULTIPLE` (`volatility.py:139-145`) is copied into the state and
  the trace says "stop Nx ATR", but `opportunity.py:557` always uses 2.5.
  Same class as F28. (Harmless while 2.5 stays; applying 1.5 would bring back
  noise-band stop-outs.)
- **F67 · Polymarket is enabled and does nothing for this book.**
  - Live: 4 mappings discovered, **0 confirmed**, only BTC/ETH watched.
  - The prediction and event-risk specialists are "not applicable" for XRP on
    every run, while the workers keep polling.
  - The UI has no prompt to confirm a mapping, so it stays inert until someone
    calls `confirm_mapping`.
- **F68 · Learning stats mix an old $10k book into the $3 one.**
  - `/api/memory`: 12 trades, 25 % win rate, **+$769.19** total P&L, on an
    account holding $2.83.
  - The probability calibrator counts those 12 toward its 20-trade minimum.
  - Nothing resets `global_stats` when the book is reset or re-funded.

### P3

- **F69 · AgentOS can permanently disable an agent.** An agent that errors
  5 times in a row never runs again (`core/agent_os.py:134`). That includes
  `trading_agent`, whose `fetch_prices` feeds the polled price cache.

### Suspicious, not confirmed (round 3)

- **No overall deadline on `run_analysis_graph` in the session loop.** With
  uncapped rate-limiter waits and 300 s reasoning timeouts, target and floor
  checks can stall for minutes.
- **`asyncpg pool.acquire()` has no timeout.** Half-open Supabase connections
  could stall close-path writes inside the tick chain (compounding F22).
- **`binance_testnet` has no 429/418 handling.** It opens a new HTTP client per
  call (every 2 s per symbol) with a fixed 5 s backoff. An IP ban would blind
  demo ticks, and therefore stops.
- **Entry timing.** Entering after the signal bar, rather than at its open,
  improved expectancy by +0.031 ± 0.008R (z 3.7). Pullback or limit entries
  (also maker fees) are worth testing.
- **The latest window ranks strategies differently again.** After the HTF gate:
  VWAP +0.51 ± 0.15R (95 trades), Grid −0.07, Range −0.085. This is the third
  ordering in three windows (F8). VWAP does not survive a correction for the 11
  comparisons made.

### Coverage audit: earlier observations now recorded

Everything reported during this session is now in this document:

| Observation | Where it is recorded |
|---|---|
| Polymarket inert | F67 |
| Mixed learning stats | F68 |
| Trailed stop sitting past the target while closes failed | consequence of F1 |
| Signed demo order per tick in the F1 retry loop | F1 step 6 |
| Duplicate `TRADES_API_KEY` | F6 |
| Session-panel BTC default | F49 |
| Positions page matches the monitor row by symbol only; `LivePriceBar` never shows staleness; two ATR implementations; 15m in one trend vote but not the HTF gate; opposite-side open closing an existing position | below |

Those last ones remain unconfirmed and are listed here so they are not lost:
- `positions/page.tsx:61-64` matches the monitor row by symbol only (paper
  and real in one coin could show the wrong stop).
- `LivePriceBar` never shows staleness.
- Live Wilder ATR vs simple ATR in `validate_trade` and the backtest.
- `_multi_timeframe_trend` includes 15m, but the HTF gate does not.
- `apply_paper_fill` closes an existing opposite-side position on a new open.

---

## Round 4 — final full retest

The last round re-ran every automated suite and every live sweep, and three
reviewers covered the code nobody had reviewed yet:
- the backend API routers (in-process fuzzing, 347 requests);
- the remaining agents, tools, workers and Polymarket;
- consistency between the database schema, the data contracts, environment
  variables and constants.

**Re-run results (unchanged from earlier unless noted):**

| Check | Result |
|---|---|
| Type check | clean |
| Frontend tests | 33 files / 475 passed |
| `next build` | passed |
| Backend tests | **2,242 passed, 1 failed, 2 skipped**. The failure is `test_venue.py::test_a_failed_order_carries_no_fill_price`, which passes or fails depending on what is in `.env` (F93). It is not a product failure. |
| Backend GET sweep | 70 endpoints: 66 OK; 4 non-200 are expected or were my test inputs (re-checked) |
| Next.js API routes | 24 routes: `collaboration` and `volatility-history` still return 500 (F38) |
| Dashboard pages | 25/25 return 200 |
| Database | watch list 1 vs book 0 (F1/F2 unchanged); 1,643 decision rows in the last hour (F10); size 18 MB |

**Fixed since earlier, worth knowing:** every row in `trades` now carries
`strategy`, `run_id` and `entry_context`, so the attribution chain works.

### P1 — high

#### F70 · Changing exit rules in Settings does nothing until the backend restarts

`agents/position_monitor.py:124-256` reads `TRAILING_STOP_R`,
`TRAILING_ACTIVATE_R`, `PROFIT_TARGET_PCT`, `PROFIT_TARGET_BASIS`,
`PARTIAL_TP_FRACTION` and `RESTING_STOP_*` **once, at import**.
`POST /api/admin/exit-rules` writes `os.environ` and `.env` and says "takes effect
on the NEXT TICK". `GET` shows the new value, while the running monitor keeps
the old one: the same failure CLAUDE.md records for `simulation_mode`.

**This changes the advice above:** after changing an exit rule you must
restart the backend, and a restart ends the session (F54), so you must then
start a new session.

*Verified:* code read, and an offline import test (the value stayed `1.0`
after `os.environ` was changed).

#### F71 · Each backend backtest run leaves an extra executor on the LIVE bus, so trades then fill several times

`core/backtest_engine.py:38-72`:
1. `ExecutionAgent(simulation_mode=True)` subscribes to the live bus when it
   is built.
2. `rebind_bus` moves it to the private bus.
3. `restore_agent_buses()` moves it **back to the live bus**.

After 2 runs, one `TAR_APPROVED` executed **3 times**: duplicate trade rows,
paper fills and `ORDER_FILLED` events until restart. It is triggered by
`POST /api/research/run`. No page calls it, but with no auth key (F6) anyone
can.

*Offline repro.*

#### F72 · The backend "historical" backtest analyses the live market and cannot trade

`MarketIntelligenceAgent.handle_event` ignores the simulated tick and fetches
**live** klines and macro data: about 4,000 live requests per 1,000 candles,
which risks a Binance ban. There is no debate or CRO on the private bus, so the
result is always `trades_executed: 0`. For the whole run, the live MI agent and
Supervisor are unsubscribed from the live bus.

*Offline repro.*

#### F73 · A manual REAL buy is reported as stop-protected, but its stop books a fake close

`api/operator_exchange.py:386,413` hands the spot position to the agent's
monitor. With `LIVE_TRADING=false`, the stop "closes" it by simulation: a `real`
agent-close row (P&L −10.04 in the repro) is written and the watch row deleted,
while **the spot position stays open at the exchange with no stop**. With
`testnet:true` it is booked as `paper`.

*Offline repro.*

#### F74 · Closing a manual paper long from the panel leaves a ghost in the watch list

`api/operator_trade.py:327`: a sell calls `sell_paper` but never removes the
monitor row its buy created. The session then treats that coin as still held,
and `trade_scope` counts it against `MAX_CONCURRENT_POSITIONS=1`, so it
**can block agent entries, the F1 symptom by a different route**. When the
stop later fires, a fake −10.04 loss is persisted into the win rate and
daily-loss check.

*Offline repro.*

#### F75 · A NaN equity turns off the drawdown kill switch

`POST /api/admin/observation/resume` with `{"equity": NaN}` (or Infinity)
returns 200 and sets the high-water mark to NaN. After that, a 99 % drawdown
reports 0 % and never halts, until the month rolls over. `"abc"` returns a 500,
but only after observation mode has already been cleared.

*Offline repro.*

#### F76 · The 60-second monitor reads shorts backwards and re-runs the full graph every minute

`workers/monitor_worker.py:105` uses `(live − cost)/cost`, which is long-only:
- a short **winning** 6 % is reported "down −6 %";
- a **losing** short raises nothing.

Each concern publishes `TRIGGER_FIRED acted=True` directly, bypassing the
trigger cooldown and the 6-per-minute cap, so a 24-node graph runs every 60 s.

*Offline repro.*

#### F77 · Event-path risk inverts: a portfolio veto passes and a confident trade fails

`portfolio_agent.py:157-162` sets `risk_score = confidence × size_multiplier`,
and `simulation_agent.py:41` fails at ≥ 0.8. So a VETO (×0) becomes risk 0 and
passes, while confidence 0.9 fails. The side is also hardcoded LONG. This is
dormant while `GRAPH_EXECUTION_ENABLED=true`.

*Offline repro.*

#### F78 · The database password is written to the log on every startup

`core/db.py:22`: `logger.info(f"Connecting to database at {settings.DATABASE_URL}")`
prints the full DSN, password included, to stdout/journald. The error branch
already redacts it; this line does not. **Rotate the Supabase password** if
those logs have been shared or kept anywhere.

*Verified:* code read.

### P2

- **F79 · Unusable manual stops are accepted.** A NaN, negative or wrong-side
  stop/target is accepted and reported "enforced on every tick"; a NaN stop
  never fires (`operator_trade.py:262,458`, also `operator_exchange`).
  Invariant 3 is weakened.
- **F80 · Infinite amounts are accepted.** `startingCash`, `targetEquity` and
  `startAmount` take `Infinity`/`1e400` (`admin.py:189`, `session.py:55,59`).
- **F81 · The graph-progress WebSocket runs the graph twice.** `graphs.py:448-452`
  streams the graph, then `ainvoke`s it again. That doubles LLM and data cost,
  and the final summary comes from a different run.
- **F82 · The strategy-selection regime bonus never applies.**
  `strategy_selection_graph.py` compares "Bull Trend" with "Trending Bullish",
  so the +15 bonus never applies in 3 of 4 regimes. `_volatility_fit`
  substring-matches "low" inside "s*low* EMA" and "be*low*". These scores are
  the ensemble's vote weights.
- **F83 · The CIO's correlation cap assumes every held position is long.**
  A same-direction short is not counted; a hedging long is. Correlations are
  cached forever (event path).
- **F84 · The Bayesian EV gate can never reject.** With a 0.45 prior and an
  assumed 1.5:1 payoff, EV ≥ +0.125 at **zero** confidence. It also presents an
  assumed probability as computed (invariant 6; event path).
- **F85 · Event timestamps carry no timezone.** `models/events.py:74`
  (`utcnow`) is serialised without `Z`, and the browser parses it as local
  time. In India the stream age starts at about 19,800 s and event times are
  shifted by 5.5 h; west of UTC a dead stream never looks stale.

### P3

- **F86 · Duplicate research questions every hour.** The curiosity worker's
  per-cycle `trade_id` defeats de-duplication.
- **F87 · API edge cases.**
  - A non-ASCII bearer token gives 500, not 401 (`core/auth.py:100`).
  - `live-trading/enable` returns 200 for refusals.
  - `POST /testnet` saves the venue before the credential check.
  - `/market/klines` returns `success` with 0 rows on failure.
  - `/market/regime` accepts any timeframe.
  - `/api/execution` limits are unbounded (`LIMIT -1` = all).
  - `operator_exchange /order` treats a 0/missing fill as a full fill.
  - `/trading-mode` always says "binance futures".
- **F88 · Reflections on manual trades always fail.** They are rejected by the
  foreign key (`manual-<hex>` id is not in `trades`).
- **F89 · `execution_quality` rows mislabel venue and book.** The venue is
  hardcoded `binance_futures`, and `tab` comes from settings, not the trade.
- **F90 · The Markets page "last decision" panel is almost always blank.**
  Field and event names do not match the backend.
- **F91 · Dead config.** `ENABLE_MESSAGE_BUS` and `NEWS_API_KEY` in `.env` are
  read by nothing (the news code reads `APITUBE_`/`GNEWS_`/`NEWSX_`/
  `NEWSDATA_API_KEY`).
- **F92 · The knowledge graph uses its own regime names.** "Trending Bullish",
  "Ranging": `/api/knowledge/implications/Bull Trend` returns 404. No decision
  code reads it.
- **F93 · `test_venue.py::test_a_failed_order_carries_no_fill_price` is not
  hermetic.** It passes or fails depending on `.env` (with `USE_TESTNET` blank
  it attempts a real connection).
- **F94 · The `paper_*`/`real_*` views read the browser's tables.** They are
  built on the browser's `positions`, not the agent's book, so they are
  misleading for anyone querying the agent's positions.

### Suspicious, not confirmed (round 4)

- `admin.py:639`: if `get_venue()` raises, live trading is enabled **without**
  a credential check (no input found that triggers it).
- ccxt network exceptions can include signed request URLs, and `_describe(e)`
  returns them to the caller.
- Session symbols are stored as typed (`SOLUSDT`, lowercase) and compared by
  exact string.
- Trigger suppressions (about 170 per cooldown) may crowd real events out of
  the 2,000-event buffer.
- `position_worker.run_cycle` aborts the whole sweep if one position's
  monitoring graph raises.
- Naive datetimes written to `timestamptz` may be shifted on a non-UTC host.

---

## Final status — all 94 findings

| Severity | Count | Findings |
|---|---|---|
| P0 | 13 | F1, F2, F20–F25, F54–F58 (F61–F63, counted under P1, are the profit blockers) |
| P1 | 32 | F3–F8, F26–F37, F59–F63, F70–F78 |
| P2 | 34 | F9–F14, F38–F53, F64–F68, F79–F85 |
| P3 | 15 | F15–F19, F69, F86–F94 |

Every issue observed in this session is in this document. Unconfirmed
suspicions are listed separately under each round, and are not counted.

---

## Profitability — what the evidence actually supports

"Every trade positive" is not achievable by any trading system. Attempting it
(tiny targets, wide stops) produces a high win rate and a losing account;
CLAUDE.md already notes this. What the project's own measurements say:

| Evidence | Result |
|---|---|
| Strategy backtests | Rankings invert month to month (F8). No stable edge. |
| Risk-model sweep, out of sample | 19 of 20 stop/target pairs negative. The best in-sample pair is −0.146R out of sample. |
| HTF gate, 3,539 trades | Removes a losing half. What is left is **+0.008R ± 0.035R, which is statistically zero.** |
| Debate conviction vs outcome | Flat (z ≈ 0.03). Higher confidence does not mean better trades. |
| Live record | 12 resolved trades total; probability calibration needs 20. |

### F11b · The session's target is out of reach at the measured edge

Simulation, wins +2R, losses −1R, about 0.09R fees, from $3 to $25 with a
$1.50 floor (this session's exact settings):

| Win rate | Risk/trade 5% | 8% | 12% |
|---|---|---|---|
| 33% | 0.1% | 0.7% | 1.4% |
| **36% (measured)** | **4.6%** | **5.0%** | **5.3%** |
| 40% | 57.8% | 34.3% | 22.7% |
| 45% | 94.0% | 78.6% | 57.9% |

Values are the chance of reaching $25 before the floor. At the measured edge,
**about 95 sessions in 100 hit the floor first**, whatever the leverage.
`MAX_PORTFOLIO_VAR_FRACTION=0.12` and 10x make each loss bigger without
changing the odds. The only lever that moves this table is a **real win rate
above about 40%**, and nothing in the evidence shows that yet. Note also that
at 40%+ a *lower* risk per trade reaches the target more often, because
surviving the losing streaks matters more than speed.

**What would actually improve results, in order (updated after round 3):**

1. **Make it able to trade and keep trading:**
   - F1/F2/F7 (phantom position),
   - F54 (restart ends trading),
   - F55/F56 (observation latch),
   - F57 (boot wipe),
   - F22 (stops blind during analysis),
   - F20 (emergency stop).
2. **Fix the exit policy (F62).** Either set `TRAILING_STOP_R=0` (plain 2:1,
   +0.015–0.027R measured), or test "no fixed target, arm the trail at 2R,
   trail 1.5R" (+0.052R pooled, z 4.9) on a second window before adopting it.
   Then make `strategy_backtest` model whatever exit is live. It is a
   **settings change** (Settings → Exit rules), not code, **but because of F70
   it applies only after a backend restart, and a restart ends the session
   (F54): restart, then start a new session.**
3. **Refuse fee-dominated trades (F61).** Skip entries where the round-trip
   fee is above about 0.12R, i.e. stops under about 0.85 %. That removes 7 of
   the last 10 approved plans, which needed a 40–44 % win rate.
4. **Cut risk per trade to 1–2 % of equity (F63):**
   `MAX_PORTFOLIO_VAR_FRACTION` 0.02–0.03, or 3x. At a measured edge of zero,
   8 % per loss only reaches the floor faster, and trips F55/F59.
5. Fix F21, then run the five-coin rotation (F9). More opportunities at the
   same selectivity.
6. Fix F3/F4 so it can learn. Give selection evidence that is net of fees,
   pooled across windows and run on the live exit (F8).
7. Test pullback/limit entries: +0.031R from timing alone, plus maker fees.
8. Accumulate **real** closed trades: 20 per strategy is the system's own
   promotion floor and the only path to a measured edge.
9. Fund past the venue floor. Below about $5 the exchange minimums dominate
   sizing.

**Honest bottom line:** even with all of the above, the best measured
configuration is about **+0.05R per trade before it is confirmed on a second
window**. That is a thin edge. It can grow an account slowly with small risk
per trade; it cannot turn $3 into $25 quickly, and no setting makes every
trade profitable.

---

## Verification performed

| Check | Result |
|---|---|
| `npx tsc --noEmit -p tsconfig.json` | **clean** |
| `npx vitest run` | **33 files / 475 tests passed** |
| `next build` | **passed** |
| Backend `pytest` (round 2, isolated: `DATABASE_URL` pointed at an unroutable test address, all exchange/LLM/Telegram keys blank, Python 3.12) | **2,243 passed, 2 skipped** (3 min). Passes with F1, F21–F24 present (see F53). |
| Live backend GET sweep | **all 70 GET endpoints**; all answered, mostly in < 1 s. Two errors were inputs on my side. |
| Live Next.js API sweep | **all 25 GET routes**; two return 500 (F38); four expose data without a password (F34). |
| Live dashboard pages | **all 26 pages** return 200 with the password and 401 without. |
| Offline repros | about 15 scripts against fake venues / dummy DB (F21–F30, F39–F41, F43). |
| Round 3 offline repros | restart restore, high-water-mark carry-over, DB-blip wipe, real-floor, daily-loss, watcher-cancel (F54–F59, F64) |
| Round 3 market simulation | 8 coins × 4,499 15m bars of real mainnet klines, real strategy functions and HTF gate, 1m exits, fees; paired exit-policy test on 7,591 entries (F61–F63) |
| Round 3 live re-check | session still stuck (39,142 polls / 2 trades), observation off, high-water mark $2.83, Polymarket 0 confirmed mappings |
| Live backend endpoints (round 1) | session, monitoring, positions, runs, events, testnet, status, ticks, price |
| Live Postgres (read-only) | trades, decisions, monitored_positions, agent_positions, agent_paper_account, risk_events, reflections, hypotheses |
| Binance demo (read-only) | positionRisk, openOrders, openAlgoOrders, userTrades, balance |
| NVIDIA models | one probe each for 4 models |
