# External review — verified, and what was done

**Reviewed and resolved 2026-10-21.** The original review is preserved below
each item as *Claim*; what follows is what the code actually says, checked
against the source and the committed evidence rather than accepted or dismissed.

Three claims were confirmed and fixed. Two were confirmed as **stale comments
where the code is right**, and in one of those the obvious remedy — making the
code match the comment — would have made this account lose money faster. Two
were already fixed before the review and were read from the comments that
record the history. One is true, unfixable by code, and is the most important
sentence in this document.

---

## The headline, because it reframes everything else

> *"in the live server there is all of them all loses 1/5, 4 trades is totally
> loss with more amount losses"*

**Four losses in five trades is an ordinary outcome for this system, and the
loss SIZE is a configuration bug that is now fixed.** Those are two different
findings and only the second was a defect.

This system places a fixed **2:1** payoff (a 2.5× ATR stop against a 5.0× ATR
target). At 2:1, break-even is a **33% win rate**, so **about two trades in
three are designed to lose**. The per-trade loss probability near break-even is
~0.64, which makes four losses in a row a **1-in-6** event — routine variance.
Five trades cannot distinguish a profitable strategy from a worthless one, which
the review itself says in its first finding.

What was genuinely wrong is how much each loss cost:

| | live value | consequence |
|---|---|---|
| `MAX_PORTFOLIO_VAR_FRACTION` | **0.12** | a stop-out costs **~12% of the account** |
| `MAX_DRAWDOWN_FROM_HWM` | **0.30** | the killswitch fires at 30% |
| **losses needed to halt** | **2.5** | the session ends on a routine streak |

So four losses ≈ **half the account**, and the agent halted — exactly what was
observed. `backend/agents/ceo_agent.py` already carried this arithmetic in a
comment (*"losses needed to trip 3.8 → FOUR consecutive stop-outs … a limit
certain to fire within the first handful of trades stops being a disaster brake
and becomes a scheduled outage"*). **Nothing acted on it.** Now something does.

`backend/services/risk_coherence.py` refuses to start a session whose own
limits cannot survive `MIN_LOSSES_BEFORE_HALT` (8) consecutive stop-outs, at the
moment the setting is chosen rather than four losses later. It never resizes
anything — silently trading a smaller size than the operator configured is the
`simulation_mode` failure in a new place — it refuses and names all three
levers, which are **not** equivalent:

1. **Lower `MAX_PORTFOLIO_VAR_FRACTION` to ≤ 0.037.** The only one that reduces risk.
2. **Raise `MAX_DRAWDOWN_FROM_HWM` to ≥ 0.96.** Accepts a deeper drawdown instead of avoiding one.
3. **Fund the account further.** 0.12 was set to clear the venue's $5 minimum
   order on a ~$2 book; past ~$25 that constraint disappears and a safe VaR
   fraction clears it anyway.

**AND THE SHIPPED DEFAULTS ARE SELF-DEFEATING TOO**, which is why this is a
check rather than a one-line correction to one operator's `.env`. The defaults
are `MAX_PORTFOLIO_VAR_FRACTION=0.05` against `MAX_DRAWDOWN_FROM_HWM=0.10`:

```
losses to halt = 0.10 / 0.05 = 2.0
P(2 consecutive losses at a 33% break-even) = 0.64^2 = 41%
```

A brake that fires two fifths of the time is not a disaster brake. Any
combination the operator picks is now measured against the same bar, including
the defaults, so the number has to be chosen deliberately rather than inherited:

| VaR fraction | a stop-out costs | halts after | at 30% drawdown |
|---|---|---|---|
| 0.12 *(live)* | 12.0% | 2.5 losses | refused |
| 0.08 | 8.0% | 3.8 | refused |
| 0.05 *(default)* | 5.0% | 6.0 | refused |
| **0.0375** | 3.8% | **8.0** | **accepted** |
| 0.03 | 3.0% | 10.0 | accepted |

**Recommended: (1) plus (3).** `docs/DIAGNOSIS_2026-10-15_REMEDIATION.md`
already records that funding past ~$5 is strictly safer than raising the VaR
fraction, and this is the same trade-off with the cost now measured.

---

## P1 — "Strategy selection is not proof of profitability"

> *Claim: the four closed trades were all losers; Breakout accounted for most
> of the loss; the sample is too small to rank strategies.*

**CONFIRMED, and it cannot be fixed by code.** It is the honest constraint on
everything else here. Four trades is not evidence about Breakout; at a 33%
break-even, four losses from one strategy is unremarkable.

This is already enforced structurally and stays that way:
`strategy_performance.MIN_SAMPLE = 20` **real closed trades** before a measured
win rate may steer selection, and no record scores NEUTRAL (0.5) rather than
zero — zero would permanently freeze out every strategy that has not traded yet,
including the one that would have worked.

What exists to answer the question properly: `scripts/run_backtests.py` (all
eleven strategies, real klines, fees charged, results committed under
`backtests/<date>/`), `scripts/sweep_risk_model.py` (60/40 in/out-of-sample
split), and `backend/core/evidence.py`, which attaches a standard error to every
expectancy and applies a Bonferroni correction — it exists because three
readings on an earlier sample looked decisive and were all noise (z = +1.01,
−0.78, +0.03).

**The measured edge after every fix to date is about +0.05R per trade, and it is
not yet distinguishable from zero on a second window.** No change in this
document alters that.

---

## P1 — "Position duration does not match the strategy label"

> *Claim: a Scalping trade stayed open over an hour; others ~9 hours; the
> monitored position over 14 hours.*

**CONFIRMED — and the cause was worse than mislabelling. FIXED.**

`position_monitor` had exactly three exits — stop, target, partial scale-out —
and **no concept of time anywhere in the codebase**. Meanwhile every profile has
carried `expected_holding_time` since Section 11.3 as **prose nothing read**.
The same shape this project has recorded a dozen times: a value declared where
it is consumed and never produced.

It is a loss source, not untidiness. Past its horizon a position is no longer
the trade the signal justified — the setup has expired, it is paying funding
nobody budgeted for, and it is exposed to a market nothing analysed. A
`Scalping` profile whose own risk note says it *"needs a high win rate to
survive costs"*, held 14 hours, is pure cost.

Every profile now carries an enforceable `max_holding_minutes`, derived from the
same sentence so the two cannot drift:

| | bound | | bound |
|---|---|---|---|
| Arbitrage | 15 min | Range | 12 h |
| Scalping | 30 min | VolatilityBreakout | 12 h |
| Momentum | 6 h | Breakout | 24 h |
| MeanReversion / VWAP | 8 h | Trend / Grid | 2 d |
| | | Swing | 7 d |

Four properties, each pinned by `tests/test_max_holding_time.py`:

- **The bound is near the TOP of each stated range, not its median.** This is a
  backstop against a stuck position, not a second profit target — cutting
  winners early is exactly how a 2:1 payoff is destroyed.
- **A tick that hits the stop still closes as `stop-loss`.** Re-labelling it
  would corrupt the attribution the learning loop reads.
- **A manual position gets no horizon imposed on it.** It closes with `strategy`
  NULL by design; inventing one would close the operator's own trade on a rule
  they never chose.
- **It only ever closes**, through the same ungated `_close` the stop uses, so
  invariant 4 is untouched.

---

## P1 — "The persisted learning history is incomplete"

> *Claim: zero rows in `reflections`, `debate_records`, `hypotheses`,
> `strategy_versions`.*

**CONFIRMED, and the cause is sharper than "the agent has not learned yet."**
Checked by searching for every writer:

| table | writers in the backend |
|---|---|
| `reflections` | 1 — `reflection_agent.py:390` |
| `debate_records` | **none** |
| `hypotheses` | **none** (they persist to `.data/hypotheses.json` via `research_store`) |
| `strategy_versions` | **none** (`signal_version()` exists; nothing records it) |

Three of the four tables **cannot** have rows: no `INSERT` exists anywhere. They
are declared in `db/schema.sql` with full columns, read by the API, and never
produced — the schema-level instance of the same shape.

`hypotheses` is **not** a bug to fix by adding an INSERT: it already persists to
JSON, and a second writer is precisely the two-books hazard CLAUDE.md forbids.
The remaining two are real attribution gaps and are tracked as such.

What already works and answers most of the review's required outcome:
`trades.strategy`, `trades.run_id` and `trades.entry_context` are carried
plan → TAR → CRO → approval → row, so *which strategy made each trade* and *what
the market looked like when it was chosen* are both recorded.

---

## Issue A — "Execution and risk controls may not be applied consistently"

> *Claim: the risk gateway's comments identify a second submission path through
> the supervisor agent that historically traded without the same scope checks.*

**ALREADY FIXED — the review read the comment that records the history.**

`supervisor_agent._consider_trade` refuses outright while
`GRAPH_EXECUTION_ENABLED=true` (the live setting), with the reason recorded in
`decisions` so *"why did this not trade?"* stays answerable. Verified at
`backend/agents/supervisor_agent.py:449`. Exits are untouched, because
`_consider_trade` only ever opens and closes belong to the position monitor
(invariant 4).

The review is right that a comment describing a fixed bug reads as a live one.
That is a real cost of this codebase's commenting style and the reason each such
note now says plainly that it is history.

The review's broader checklist is largely satisfied already: `execution_service`
re-validates at the execution boundary, claims its idempotency basis **before**
the first `await` (two tasks once both passed the duplicate test and opened one
position twice), and refuses an open with no stop (invariant 3).

---

## Issue B — "Position-monitor defaults contradict their own documentation"

> *Claim: the comments describe a 2% profit target and partial profit-taking as
> enabled, while the code defaults both to 0.*

**CONFIRMED — the mismatch is real, the COMMENTS were stale, and the CODE is
right. FIXED by correcting the comments.**

This one matters because the obvious remedy is backwards. **Do not turn these
on.** The setting was reversed twice and each step corrected the one before:

| | result |
|---|---|
| shipped OFF | wrong — 53.4% of trades closed at ~0.00 (the scale-out left a runner at break-even) |
| defaulted 2.0 | **also wrong** — the target is a % of *margin*, so the price move it needs is `PROFIT_TARGET_PCT / leverage`, while the stop stays at 2.5× ATR and does not shrink |
| back to 0 | current — the exit returns to the ATR pair, 2:1 by construction at every leverage |

Measured on a live XRP/USDT session (ATR 0.504%, stop 1.26%):

```
2.0% at 10x  ->  0.20% target vs 1.26% stop  ->  93.2% break-even
2.0% at  3x  ->  0.67% target vs 1.26% stop  ->  70.6% break-even
0 (ATR pair) ->  2.52% target vs 1.26% stop  ->  36.0% break-even
```

**At the 10× this account runs, turning the target back on sets a 93%
break-even win rate — losing by construction.** Measured live win rate at the
time: 54.5%.

`0` does not mean "no target": every position always carries an ATR stop (2.5×)
and an ATR target (5.0×), checked on every tick. And `PARTIAL_TP_FRACTION` had
to move with it — the scale-out is gated on `PROFIT_TARGET_PCT <= 0`, so it was
dormant only *because* the target was on.

---

## Issue C — "Strategy profiles and executable risk settings disagree"

> *Claim: profiles describe a 1.5-ATR stop and 3-ATR target; the risk manager
> uses 2.5 and 5.0.*

**CONFIRMED. FIXED — and the review undercounted it.**

The prose is not executable, so this was never a live divergence in placed
orders. It is still exactly the failure the review names: *a strategy can appear
successful in a backtest while behaving differently in production*, and it is
the third time these two numbers have drifted apart in this codebase.

The fix is structural rather than a correction. `exit_logic` now **interpolates**
`ATR_STOP_MULTIPLIER` / `ATR_TARGET_MULTIPLIER` from `core/risk_manager` instead
of restating them, and `tests/test_spec_matches_execution.py` fails if anyone
restates them again. **That test immediately found two more instances the review
missed** — `VWAP` and `VolatilityBreakout` both quoted 1.5× — which is the
argument for a test over a correction.

---

## Issue D — "Some strategies are simple signal generators"

> *Claim: three bullish candles, an MA comparison, RSI thresholds and wick
> rejection are features, not complete trading systems.*

**TRUE AS WRITTEN, and partly answered by the surrounding system rather than by
the signal functions.** A signal function here is deliberately one input, not a
trading system: eligibility is `active_regimes` + `BLOCKED_REGIMES`, sizing and
the stop are the Risk Gateway's, costs are charged by `services/fees`, and the
nine-specialist panel can and does refuse a setup the scorer proposed.

Two of the review's seven requirements were genuinely missing and are now
present: **(5) when to exit early** is the time exit above, and **(7) which data
proves an advantage** is `backtests/` plus the `MIN_SAMPLE` floor.

The rest is a research programme, not a defect list. The honest status is that
this system's own backtest already says four of eleven strategies are
net-negative in the window measured, and `services/strategy_priors` carries that
into selection — shrunk halfway to neutral, because the backtest is in-sample
and gross of slippage.

---

## Issue E — "The backtest and the live system need to agree"

> *Claim: verify closed candles, realistic entry, conservative same-candle
> resolution, costs, no look-ahead, identical selection and sizing.*

**MOSTLY ALREADY SATISFIED; ONE REAL GAP, now asserted by a test.**

Confirmed already correct: the backtest **imports** the live
`ATR_STOP_MULTIPLIER`/`ATR_TARGET_MULTIPLIER` rather than restating them; it
**charges the real taker fee** and subtracts it from reported expectancy; an
**ambiguous bar assumes the stop**; and it feeds each strategy's **real**
`STRATEGY_FUNCTIONS[name]` over a trailing window, so selection logic is shared
rather than reimplemented.

**The genuine gap: the backtest models a pure ATR stop/target pair and does not
model the live exit policy** — `PROFIT_TARGET_PCT`, `PARTIAL_TP_FRACTION`,
`TRAILING_STOP_R`, or the new `max_holding_minutes`. With all of them at their
defaults the two agree today (the ATR pair *is* the live policy), which is why
the stored backtests remain valid evidence. They stop agreeing the moment an
operator sets any of them.
`test_spec_matches_execution.py::test_the_backtest_does_not_yet_model_the_live_exit_policy`
fails if one is turned on, so divergence becomes a deliberate act with a visible
consequence instead of a silent one.

Still not modelled, and stated rather than hidden: **slippage and funding**. The
Bybit/Binance demo mirror exists precisely because a simulated fill reports both
as zero.

---

## Sections 3–7 — strategy design, regimes, risk, metrics, learning

These are a **research programme**, and the review says so itself (*"these are
strategy-design recommendations, not claims that any strategy will necessarily
be profitable"*). They are not defects and are not treated as fixed.

Two points worth recording, because acting on them wrongly would cost money:

- **Section 5A's "evaluate 0.25–0.5% per trade"** is the right direction and is
  what the risk-coherence refusal now pushes toward from the opposite end. Note
  the interaction the review could not have known: on a ~$2 book, a VaR fraction
  that small produces orders **below the venue's $5 minimum**, so they are
  approved and then silently refused at quantisation. Lowering risk and funding
  the account are the same decision here.
- **Section 7's identifier chain** is largely built: `run_id`, `strategy`,
  `entry_context` and the TAR id already travel with every graph-path trade. The
  missing links are `debate_records` and `strategy_versions` above.

---

## What changed, in files

| | |
|---|---|
| `backend/services/risk_coherence.py` | **new** — refuses a configuration that halts itself on routine variance |
| `backend/services/trading_session.py` | checks it first in `start_session`, before any per-symbol work |
| `backend/algorithms/strategy_profiles.py` | `max_holding_minutes` on all 11 profiles; ATR multipliers interpolated, not restated |
| `backend/agents/position_monitor.py` | the `max-holding-time` exit; Issue B's comments corrected |
| `tests/test_risk_coherence.py` | 16 tests |
| `tests/test_max_holding_time.py` | 8 tests |
| `tests/test_spec_matches_execution.py` | 6 tests |

## What is still open, stated plainly

1. **`debate_records` and `strategy_versions` are never written.** Confirmed; not fixed here.
2. **The backtest does not model the live exit policy.** Guarded by a test, not closed.
3. **Slippage and funding are not modelled** in the backtest.
4. **The edge is ~+0.05R and not yet distinguishable from zero.** Only more real closed trades settle this, and `MIN_SAMPLE` is 20 per strategy.

## What no change in this document can deliver

A system that wins every trade. At a fixed 2:1 payoff, **roughly two trades in
three are designed to lose** — the third is meant to be larger than the two
together. The work here makes each loss survivable and each trade attributable,
so that twenty trades from now the question *"is this strategy profitable?"* has
an answer instead of a guess. It does not make the next five trades win.
