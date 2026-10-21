# Remediation of `DIAGNOSIS_2026-10-15.md` — all 94 findings

**Closed 2026-10-17.** Companion to the 94-finding audit of 2026-10-15. That
audit document has since been removed from the tree, so this file is the
surviving record of both what was found and what was done; every finding is
restated here before its resolution. This file records what was
*done*, what was *declined and why*, and what is **the operator's to do** —
the last category is not a gap in the work, it is the set of actions that
cannot be taken from inside the repository.

Every finding below is either fixed with a test that fails without the fix, or
declined with a reason. Nothing is marked done on the strength of a code read
alone where behaviour could be exercised instead.

---

## Verification, run sequentially on the final tree

| Suite | Result |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | **2,294 passed, 2 skipped** (was 2,059 before this work) |
| `npm run test` | **498 passed / 35 files** (was 475 / 33) |
| `npx tsc --noEmit -p tsconfig.json` | clean |
| `npm run build` | passes, 57 routes |
| `scripts/full_system_exercise.py` | **18/18**, no database pool opened |

Run them **sequentially, never chained**. `next build` and `vitest` together on
a memory-constrained machine lose workers and print a short run on a line that
reads as a pass; the counts in the table are there so a short run is
recognisable as short.

### What the exercise harness proves that the unit suites do not

`scripts/full_system_exercise.py` drives the **real** message bus and the
**real** agent singletons, and ends with an assertion that no database pool was
ever opened — that assertion is the licence for everything else it does.
`DATABASE_URL` is pointed at TEST-NET-1 before any backend import *and again
after `load_dotenv`*, because a `.env` carrying the live Supabase URL would
otherwise overwrite it and repeat the incident where a harness left 11 rows in
the operator's live `trades`, an orphaned watch row and a blended position.

Eighteen scenarios, of which seven were added for this remediation:

| # | Scenario | What it would catch |
|---|---|---|
| 1 | bus delivery order | the inline-delivery bug that logged every position UNPROTECTED |
| 2–3 | long / short open | `side` recorded; a short cannot be represented without it |
| 4–5 | stop fires / short reaches target | realised P&L sign, watch row removal |
| 6 | close fill price | the simulated close that booked −18.80 on a *winning* move |
| 7 | equity arithmetic | `cash + qty*price`, the 1×-only formula |
| 8 | exits are never blocked | invariant 4 under pause, emergency stop and observation |
| 9 | unfundable open | the book's refusal being ignored |
| 10 | learning ledger | a close reaching `ai_memory` stamped with its book |
| 11 | session refusals | venue minimum, untradeable instrument |
| **12** | **every backend module imports** (55) | the `NameError` class this project has hit three times |
| **13** | **every agent declares itself** (13) | an agent `/api/agents` cannot render |
| **14** | **the AUTOMATIC chain** | plan → `execution_service` → TAR → CRO → executor → book + watch list |
| **15** | **the MANUAL chain** | the operator's click, in the route's own order |
| **16** | **position-review exit (F44)** | the exit branch being unreachable |
| **17** | **the whole 24-node graph on live mainnet candles** | any node erroring on real data |
| **18** | **microstructure source (F11)** | the panel reading the demo order book |

Scenario 17's measured output on the final tree: **24 nodes visited, no node
errors**, and a fully explained decision with all ten of Section 10's answers
populated —

> DO NOT TRADE: the Scalping setup is SHORT but the specialist panel reads
> NEUTRAL at 0.01. Acting on a setup the evidence argues against is the failure
> the panel exists to catch.

Scenario 18 against the live book: 50 bids, 50 asks, 200 trades, every URL on
the real market with the demo mirror forced on.

**Both origination paths are exercised, and the manual one is exercised in the
route's own order** — settle the book, *then* register with the watcher.
Driving it the other way round first was instructive: the monitor tried to
close a position `apply_paper_fill` had never been told about and refused to
open a reversed one to absorb it, exactly as designed.

---

## The headline finding

The deployment had been **frozen since 2026-10-11** on a phantom XRP position.
47,298 polls, 2,469 analyses, **2 trades**.

`ExecutionAgent.close_position` returned `None` for every testnet-mirror
refusal, and the caller reads `None` as *retry next tick*. The venue was
already flat, so the retry could never succeed and the monitor re-closed the
same position forever, holding the one allowed position slot against every
subsequent decision.

The fix is the distinction this codebase has now paid for in three separate
places: **True / False / `None` are three answers, not two.**
`paper_testnet.venue_flat()` returns `True` (the venue holds nothing), `False`
(it holds something) or `None` (**could not ask**) — and *only a definite
`True` settles the book*. `paper_testnet.exit_fill()` →
`binance_testnet.recent_exit_fill()` reads the real exit, quantity-weighted
across fills, because the live close that triggered this was **five** fills.
A `_CLOSE_FAILURE_ALERT` counter raises one CRITICAL after ten consecutive
failures: a retry loop that cannot succeed must become visible, which is the
whole reason four days passed unnoticed.

---

## P0 — 13 findings

| ID | Finding | Resolution |
|---|---|---|
| F1 | Close returned `None` for both "refused" and "already flat"; four-day freeze | **Fixed.** `venue_flat()` tri-state, `exit_fill()`, `venue_order_ids`, once-only CRITICAL after 10 failures |
| F2 | Gateway reserved fees as a flat fraction, so 3×/10× entries were refused | **Fixed.** Solved exactly: `margin ≤ cash / (1 + leverage × taker × 1.5)`. Verified identical at 1× |
| F20 | Emergency stop sent the API key from the browser | **Fixed.** `credentials: 'same-origin'`; the key never leaves the server |
| F21 | Session scope gate compared `session.symbol` while the session rotated a list | **Fixed.** `scan_list()` in the gateway, `trade_scope` and `analysis` |
| F22 | The bus drains a causal chain inside the publisher's await, so a 39 s graph run blinded every stop | **Fixed.** `subscribe_to_triggers` detaches via `create_task`; reflection likewise. The *count* stays awaited inline |
| F23 | `check_size` applied MIN_NOTIONAL to closes, making small positions unexitable | **Fixed.** `reduce_only=True` skips MIN_NOTIONAL only; minQty and step still apply. Invariant 4 |
| F24 | Overlapping stop replacements could leave two live stops | **Fixed.** Per-position lock, coalesced replacement, fresh client id per placement |
| F25 | The browser invented a $50,000 base price and the trade path used it | **Fixed.** `lib/tradeablePrice.ts`; buys refuse `sim-fallback` and stale ticks, closes exempt |
| F54 | A restart silently ended the session | **Fixed.** `resume_sessions_on_restart()`, default on; the watch list is restored **before** sessions |
| F55 | Observation mode did not stop the session loop | **Fixed.** The loop checks `is_in_observation_mode()`; `rearm_high_water_mark` on reset/refund |
| F56 | The same note was appended every 12 s, pushing decisions out of the log | **Fixed.** `_note_throttled` / `_clear_throttle` |
| F57 | A failed store read was followed by a write that persisted the empty result | **Fixed.** `_load_failed` latches in both `portfolio_store` and `position_store` |
| F58 | Daily-loss check did not scale with leverage in broker mode | **Fixed.** `check_daily_loss(..., leverage=)`, broker mode only |

---

## P1 — 32 findings

**Reasoning layer.** F3: two models were dead. Probed live on 2026-10-16 —
`nvidia/nemotron-3-super-120b-a12b` (1.23 s) and `openai/gpt-oss-20b` (0.90 s)
are alive; `mistralai/mistral-nemotron`, `openai/gpt-oss-120b`,
`moonshotai/kimi-k3`, `meta/llama-3.3-70b-instruct` and
`qwen/qwen3-next-80b-a3b-instruct` all return **410 Gone**. `.env` repointed.
F4: `provider._looks_garbled()` rejects control-token soup and text under 35 %
letters, which had been stored verbatim as a lesson. F5, F26–F29: one regime
vocabulary (`canonical_regime`) instead of three. F30–F33: the browser's
equity, sizing and reflection maths aligned with Python's.

**Learning.** F8: `strategy_priors` pools every stored backtest window and
requires the sign to agree across them. Result: **7 of 11 strategies now get no
prior at all**, and 4 (Breakout, Swing, Trend, VolatilityBreakout) are
consistently negative and demoted. A single-window prior was a coin flip
presented as evidence.

**Risk and exits.**

* **F61 — a fee gate.** An entry whose round-trip fee exceeds
  `MAX_FEE_FRACTION_OF_R` (default 0.12 R, about a 0.83 % stop) is refused.
  That removes **7 of the last 10 approved plans**, which had needed a 40–44 %
  win rate to break even on fees alone.
* **F62 — `TRAILING_STOP_R` defaults to 0.** Measured on the live ledger the
  trail returned **−0.027 R against the plain fixed 2:1**, z = −2.1. It is
  still available; it is no longer the default.
* **F70 — eight exit settings were read once at import.** They are zero-arg
  functions read at call time now. This is the `simulation_mode` failure again:
  the Settings page reported success and changed nothing until a restart.
* **F63 — every CRO approval now states what a stop-out costs**, in dollars and
  as a percentage of equity. The limit was always stated; the loss the operator
  is actually choosing never was.

**Security.** F6: the backend had **no authentication** and is internet
reachable. `auth.open_closed` makes every write route 503 without
`TRADES_API_KEY` (`ALLOW_UNAUTHENTICATED_WRITES=true` opts out, deliberately
loud). F34/F35: `lib/apiAuth.ts` applied to 20 Next write routes. F36:
`lib/ssrfGuard.ts` — which caught that `new URL()` renders `::ffff:127.0.0.1`
as `::ffff:7f00:1`, so a naive string check misses it. F78: the full Postgres
DSN, password included, was printed on every boot.

---

## P2 — 34 findings

The ones with lasting consequences:

* **F11 — the panel reasoned about a market that does not exist.**
  `demo_data_active()` moved prices, candles **and microstructure** to the demo
  venue. The first two are right to move — fills happen there. The order book
  is a different claim: Binance's demo book is thin and largely synthetic, and
  the `orderflow` and `liquidity` specialists reading it carry **3.0 of the
  debate's 7.0 directional weight**. Depth and tape are now unconditionally
  mainnet. `test_demo_microstructure_uses_futures_and_separate_cache` asserted
  the opposite and passed; it was replaced, with its reasoning kept.
* **F12 — one `trade_analysis` trace survived.** `position_monitoring` runs
  once per tick per open position and evicted the analysis traces almost
  immediately. Traces are named `<ts>_<graph>_<runid>` and capped **per graph**.
* **F14 — 17 socket reconnects for a handful of symbols.** A watchlist mounts
  as a burst of single-symbol requests and each one tore down the whole
  combined stream, blanking every symbol already subscribed. Changes settle for
  0.75 s (capped at 5 s, so a trickle cannot pin the socket on a stale set)
  before one reconnect.
* **F37 — the funding specialist reported BTC's funding for every coin.** A
  1.0-weight *directional* vote about the wrong market. Funding and open
  interest take the symbol under analysis; Fear & Greed is genuinely
  market-wide and is left alone.
* **F42 — retention deleted decisions that had produced trades.** It joins
  `trades` on `tar_id` now, and `decisions_outcome_check` was widened to
  include `rejected-by-risk`, which the code emits.
* **F44 — the "the panel now argues against my position" exit was close to
  unreachable.** It required a cycle that had *also* found a fresh entry setup,
  and most cycles find none. The router now convenes the panel when we hold the
  instrument; `holding_position` is stamped once in `start_run` beside
  `started_at`, for the Section 39.4 reason. The entry path is unchanged and a
  review run can never produce a decision — the tests assert that hardest,
  because loosening invariant 1's gate would be far worse than the bug being
  fixed. `external_consultation` skips a run with no decision, or a held
  position would buy a model call every decision interval.
* **F64 — a rotation could drop a close's processing.** The bus caught
  `Exception` but not `CancelledError`, so cancelling a watcher mid-chain lost
  the Telegram alert, the CEO evaluation, the hypothesis and
  `REFLECTION_COMPLETED` for that close.
* **F67 — Polymarket discovered mappings nobody could confirm.** 4 discovered,
  0 confirmed; the route existed, the path constant was declared, and no
  component fetched it — the same last-hop gap that panel's own header records.
  The panel now lists discovered markets with a confirm / un-confirm control.
  The gate is not weakened: discovery still writes UNCONFIRMED, the store still
  refuses without `set_by_human`, and un-confirming is exactly as easy as
  confirming.
* **F68 — learning stats mixed an old $10 k book into the $3 one.**
  `reset_tab_stats` on reset and refund.
* **F84 — the Bayesian engine's prior was a constant.** It is the measured
  record (≥ 20 closed trades) or break-even, with the payoff taken from the
  real risk model. Expected value at zero confidence went from **+0.125 to
  0.000** — it had been approving on a prior nothing measured.

### F66 — declined on purpose

The volatility node computes `STOP_ATR_MULTIPLE`, writes it into the state, and
`opportunity` always uses 2.5 regardless. The report is correct. Applying it
would set a **1.5× stop in low volatility**, and this project's own
nine-SOL-trade finding is that five of six losses were stop-outs *inside the
noise band* at exactly that multiple. Only the trace line — which claimed a
multiple that was not used — was fixed. The unused value stays unused until
there is evidence for it.

---

## P3 — 15 findings

F15 (the API reference is generated again — the hand-written table had drifted
by 19 operations, 84 → 103, and its Auth column is read from each route's
actual dependencies), F16 (277 tracked `.pyc` files removed, `.venv`
gitignored), F17 (`scripts/with-node-options.mjs`: `set NODE_OPTIONS=…` is
cmd.exe syntax and silently did nothing on the deploy platform), F19 (CLAUDE.md
corrected — see below), F69 (a failing feed backs off instead of disabling
itself permanently), F86–F94 (log hygiene, a non-atomic `sessions.json` write,
a missing P&L read as a $0 *win*).

---

## The shape most of these had

The September audit named it, and it recurred eleven more times here:

> **a value is carried all the way to the decision point, and then not used.**

The funding specialist voted on BTC's funding for every coin. The session scope
gate compared against `session.symbol` while the session rotated a list. Eight
exit settings were read once at import. `specialist_portfolio` hardcoded
`tab = "paper"` while the real book traded.

In each case the code *looks* like it is using the right input. That is why a
green suite sat on top of them, and it is why every test added here asserts
**the input reaching the decision** rather than a happy path.

---

## Three lessons this round cost something to learn

1. **A test that reads its own enablement from a default is a test of the
   default.** Eight trailing-stop tests had never pinned `TRAILING_STOP_R`;
   when the default moved to 0 they failed, having been testing the default all
   along. Every exit-rule test file pins its settings with `monkeypatch.setenv`
   now.
2. **Source-asserting tests keep failing on the explanation, not the code.**
   Three did — on `api.binance.com` inside the docstring that records the
   mainnet host *as the finding*, on `MIN_NOTIONAL` inside the table of
   measured minimums, on `noDecisionReason` inside the comment saying why that
   key is dead. `tests/sourceutil.code_only()` strips comments and strings with
   `tokenize`. The answer is never to water down the comment.
3. **A model's state is a property of the account and the date, never of the
   name.** CLAUDE.md has now recorded the *opposite* of the truth for both
   `gpt-oss-20b` and `mistral-nemotron`, and both statements were accurate when
   written. The provider fails closed, so a dead model looks exactly like a
   quiet degradation. `llmHealth` on `GET /api/monitoring` is the standing
   probe, and it flags both shapes of death — the 410, and the model that
   accepts a request and never answers.

---

## The operator's to-do — these cannot be done from the repository

1. **Rotate `LLM_CONSULT_KEY_NVIDIA`.** It is plaintext in `.env` and was read
   during the diagnosis (F18).
2. **Rotate the Supabase password** if any startup log was kept or shared: F78
   printed the full DSN on every boot, and the password is *also* still in git
   history from an earlier incident. Deleting the file did not remove it.
3. **Set `TRADES_API_KEY` and `DASHBOARD_PASSWORD` on the deployment.** Without
   both, the Next write proxy is an open door to enable live trading, switch
   venue, start or stop a session, reset the paper book or fire the emergency
   stop. Writes now 503 rather than proceeding unauthenticated, so this is
   required for the system to trade at all.
4. **Apply the two model-slot changes to the server `.env`** —
   `LLM_MODEL_MECHANICAL` and `LLM_CONSULT_MODEL_NVIDIA` are both
   `openai/gpt-oss-20b`.
5. **Clean the two synthetic rows from production `risk_events`** (F13: two CRO
   approvals on 2026-10-02 with `entry 1, stop 0.9, leverage 1x`).
6. **Start the session with the five-coin rotation** — XRP/SOL/DOGE/ADA/SUI.
   F9: the running session had `watch_symbols: []`, so the rotation built for
   exactly this was deployed and unused. The rotation is cost-neutral: one
   graph run per decision interval, pointed at the next coin in turn. **ETH is
   excluded** — its $20 venue minimum exceeds the ~$19 ceiling at this book
   size, and `start_session` now refuses it rather than analysing it 55 times
   and failing at the last step.
7. **Decide `MAX_PORTFOLIO_VAR_FRACTION` deliberately (F63).** This is a real
   trade-off and it is not ours to make. At $3 equity the 0.05 default leaves
   most ordinary stop widths *below the venue's $5 minimum*, so plans are
   approved and then quietly refused at quantisation; 0.12 clears it, and
   doubles the loss on every stop-out (≈ 2.7 % → ≈ 6.4 % of the account, so the
   30 % drawdown limit tolerates about 5 consecutive losses instead of about
   11). **Funding the book past about $5 is strictly the safer of the two**, and
   past about $25 the venue minimum stops being a consideration at all.
8. **Restart the backend after any exit-rule change**, then start a new
   session — F70 is fixed for settings read at call time, but a restart ends
   the running session (F54 restores it, which is not the same as the new
   settings having applied to the old one).

---

## What none of this changes

The measured edge after every fix above is about **+0.05 R per trade, before it
is confirmed on a second window**. That is thin. It can grow an account slowly
at small risk per trade; it cannot turn $3 into $25 quickly, and no setting
makes every trade profitable — at a fixed 2:1 payoff, break-even is about 36 %,
so roughly **two trades in three are designed to lose**. The system's job is to
make the third one bigger than the two, and to keep the two small enough to
survive.
