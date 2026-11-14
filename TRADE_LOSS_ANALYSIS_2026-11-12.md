# Why the live trades lose, and what would fix it — 2026-11-12

Read-only analysis. **No code, no setting, no database row and no order was changed.**
Sources: the live Supabase database (read-only connection), the code at commit `64c41d2`,
125 days of real Binance futures candles, and the project's own measurement scripts run on a
scratch copy of the repo.

Your leverage and allocation settings are treated as fixed, as you asked. Where they matter
to a result, the document says so and does not propose changing them.

---

## 1. Short answer

1. **The system works mechanically.** Entries, stops, targets, exits, the venue mirror, the
   records and the model calls all function. Section 2 lists what was checked.
2. **The trades lose because the entry signals have no edge after fees.** This is not a bug
   in one place. Every one of the eleven strategies is negative over the last 104 days when
   replayed with the live gates and live fees (section 5.1). A system with zero edge, a 2:1
   target and a break-even stop wins about 28% of the time; you got 2 wins in 15. That result
   has a ~15% chance at zero edge, so the live ledger is roughly what the measurements predict.
3. **No exit rule fixes this.** I replayed nearer targets, further targets, the break-even
   stop on and off, and the 8-hour time limit on and off. None of them creates a profit
   (section 5.3). The problem is *which trades are entered*, not how they are closed.
4. **Three changes would stop most of the bleeding** (issues 3, 4 and 6). The measurements
   point to them removing losing trades. They bring the system to about break-even; they do not by
   themselves make it profitable.
5. **Profit needs a new entry method that is measured first.** Two candidates showed a
   positive but *not yet proven* result on 1-hour candles (issue 5). They need a longer test
   before any real money depends on them.
6. **"Every trade profitable" is not achievable** by this or any system. With a 2:1 payoff, a
   good outcome is 38–45% winners. Anything that promises more is fitting past data.

---

## 2. What was verified as working

| Area | Check | Result |
|---|---|---|
| Entry path | 7 of 7 entries since 11-04 have a `decisions` row (`approved-executed`), a CRO approval in `risk_events`, an `execution_submissions` row with outcome `filled`, and a trade row with strategy, run id and entry context | Working |
| Sizing | Position notional ≈ equity × 10 on every entry (e.g. LTC 0.767 × 49.68 = $38.1 on ≈ $3.8) | Matches your 100% / 10x settings |
| Stop and target | Every entry has a stop at 2.5 × ATR(15m) and a target at 5 × ATR(15m); the open LTC position has both resting at the venue (`algo:…150`, `algo:…166`) | Working |
| Exits | Stops fire at the stop price (−1.01R, −1.02R, −1.01R, −1.09R); the 8-hour limit closes on time (480.1 min) | Working |
| Venue mirror | Entries fill on Binance demo; average slippage 0.96 bps, worst 4 bps, latency ≈ 1.4 s | Working |
| Fees and funding | Recorded on every row; fees are 0.43 of the 3.33 lost (13%) | Working |
| Graph refusals | 1,215 `graph-analysis` rows since 11-04 with a readable reason | Working |
| Reflection model | 8 of the last 9 closes got a real model lesson from `nemotron-3-ultra-550b` | Working (see issue 8 for the gaps) |
| Multi-coin rotation | Since 11-12 the session decides on XRP, SOL, DOGE, ADA, LTC, LINK, SUI | Working |
| Test suite | See section 8 | See section 8 |

---

## 3. The live ledger

15 closed trades, 30 Oct – 11 Nov. **All 15 were shorts.** 2 wins, 13 losses, net **−3.33 USDT**.
One LTC short is open now.

| # | Opened (UTC) | Coin | Strategy | Panel conf. | Best reached (MFE) | Closed by | P&L |
|---|---|---|---|---|---|---|---|
| 1 | 10-30 00:30 | XRP | Momentum | n/r | +0.30R | thesis exit | −0.241 |
| 2 | 10-30 00:56 | XRP | Swing | n/r | +0.76R | thesis exit | −0.253 |
| 3 | 10-30 04:00 | XRP | MeanReversion | n/r | +0.18R | thesis exit | −0.201 |
| 4 | 10-30 05:12 | XRP | MeanReversion | n/r | +0.95R | 8h limit | **+0.303** |
| 5 | 10-30 13:45 | XRP | Breakout | n/r | +0.67R | thesis exit | −0.301 |
| 6 | 10-30 16:38 | XRP | Range | n/r | +0.77R | thesis exit | −0.197 |
| 7 | 11-03 00:08 | DOGE | Range | n/r | +0.06R | thesis exit | −0.247 |
| 8 | 11-03 01:39 | LTC | Range | n/r | +0.59R | thesis exit | −0.286 |
| 9 | 11-04 04:11 | XRP | Grid | 0.21 | +0.76R | stop | −0.370 |
| 10 | 11-04 06:06 | SOL | Range | 0.19 | +0.29R | stop | −0.379 |
| 11 | 11-09 13:25 | XRP | Grid | 0.18 | +0.24R | stop | −0.757 |
| 12 | 11-09 16:38 | XRP | Grid | 0.19 | **+1.29R** | break-even stop | −0.038 |
| 13 | 11-10 19:16 | XRP | MeanReversion | 0.25 | +1.41R | 8h limit | **+0.259** |
| 14 | 11-11 03:40 | XRP | Range | 0.18 | +0.84R | stop | −0.624 |
| 15 | 11-12 05:28 | LTC | Breakout | 0.18 | open | stop now at entry | open |

"n/r" = not recorded (executed trades had no `decisions` row before 11-04).

By strategy: Range 0/5 (−1.73), Grid 0/3 (−1.17), Breakout 0/1, Swing 0/1, Momentum 0/1,
MeanReversion 2/3 (+0.36).

Four facts stand out:

- **The target was never reached.** 0 of 14 closed trades hit the 2R target. The best was +1.41R.
- **Every recorded entry was taken at confidence 0.18–0.25.** The minimum to trade is 0.18.
- **9 of 14 closed trades were Range or Grid, and none of the 9 won.**
- **The market was falling and the shorts still lost.** XRP went from 1.3055 (close of 11-04)
  to 1.036 (11-12), about −20%. Between 11-05 and 11-09, the main −15% leg, the agent took
  **zero** trades.

---

## 4. The issues, with solutions

Ordered by how much each one costs.

### Issue 1 — No strategy has an edge after fees (the root cause)

**What.** The eleven signal functions in `backend/agents/strategy_ensemble.py` are very simple
rules (the mean-reversion one's own docstring says "Very basic RSI approximation"). Replayed
over real candles with the live stop, target, fee gate and entry gates, none of them makes money.

**Evidence.** `scripts/measure_strategy_net.py --candles 10000`, run today on a scratch copy
(real signal functions, extension and relative-strength gates, fees, non-overlapping trades):

| Strategy | Trades | Net per trade |
|---|---|---|
| Arbitrage | 637 | −0.022R |
| Breakout | 237 | −0.029R |
| VWAP | 319 | −0.029R |
| Grid | 1,352 | −0.052R |
| Trend | 1,037 | −0.079R |
| Range | 1,359 | −0.079R |
| MeanReversion | 1,289 | −0.095R |
| VolatilityBreakout | 115 | −0.108R |
| Swing | 1,083 | −0.119R |
| Momentum | 992 | −0.129R |
| Scalping | 1,004 | −0.138R |

All eleven are negative. `CLAUDE.md` recorded the same finding on 11-04; it still holds.

**Why it matters.** Every other issue below only decides *how fast* a zero-edge system loses.

**Solution.** Do not tune thresholds to get more of these trades. Do two things:
1. Remove the trades measured to be the worst (issues 3 and 4).
2. Develop and measure a better entry method before trusting it (issue 5).

---

### Issue 2 — The panel's main vote looks only at 15-minute candles, so trades happen at the bare minimum confidence

**What.** `specialist_market` (`backend/graphs/nodes/specialists.py:251`) calls
`score_debate(snapshot.candles.get("15m"))`. It carries 3.0 of the 7.0 directional weight.
The 1h/4h trend is not part of the vote. It is only a veto in the Risk Gateway.

The arithmetic: with the other three specialists neutral, a market reading of 0.40 gives
3.0 × 0.40 / 7.0 = **0.17**, below the 0.18 minimum. So a trade needs the order-flow
specialist (a seconds-long tape, weight 1.5) or the news keyword score (weight 1.5) to
happen to agree at that moment.

**Evidence.**
- All 7 recorded entries were approved at 0.18, 0.18, 0.18, 0.19, 0.19, 0.21 and 0.25.
- 209 short setups were refused as WAIT at a median of 0.156, just under the bar.
- 678 setups were refused because the panel read NEUTRAL, during a −20% move with a
  Bearish 1h/4h trend.

So the entries that pass are not the strongest setups. They are marginal setups that a
few seconds of order flow pushed over the line. That selects for noise.

**Solution (needs measurement before shipping).**
- Add the higher-timeframe trend as a directional input to the market vote, not only as a
  veto. `TechnicalAnalysis.multi_timeframe_trend` is already computed from candles fetched
  at the single fetch point, so this stays replay-safe.
- Keep it deterministic. Do not let a model decide direction.
- Measure it the way `scripts/measure_htf_gate.py` does: real functions, non-overlapping
  trades, several windows. Ship only if the kept set's interval excludes zero.
- Do **not** simply lower `MIN_CONFIDENCE_TO_TRADE`. `scripts/measure_conviction.py` found
  that confidence does not predict outcome (z = +0.03), so a lower bar only adds more
  zero-edge trades.

---

### Issue 3 — Grid and Range are not real grid or range strategies, and they are most of the losses

**What.** In `backend/agents/strategy_ensemble.py`:
- `grid_strategy_agent` (line 169) says SELL whenever price is 0.1% above the median of the
  last 20 closes, and BUY whenever it is 0.1% below. That is true on most bars. It is not a grid.
- `range_trading_agent` (line 146) says SELL in the top 20% of the last 20 closes (5 hours).
  It does not check that the market is actually ranging on a larger scale.

Both are then traded as one directional position with a 2.5-ATR stop and a 5-ATR target.
For a range trade the 5-ATR target usually sits beyond the far edge of the range it is fading.

**Evidence.**
- Live: 9 of 14 closed trades, 0 wins, −2.90 of the −3.33 lost.
- Replay over 115 days, 7 coins (section 5.2): Grid −0.130R per trade (z = −4.9) and Range
  −0.119R (z = −4.4) before the trend filter.
- The scorer keeps choosing them because in a Range regime they are the best
  conditions-fit, as `CLAUDE.md` already notes.

**Solution.**
- Stop Grid and Range from originating entries until one of them is measured positive.
  The clean way is a configurable deny-list read at call time in
  `backend/graphs/nodes/opportunity.py`, so the choice is visible and reversible.
- Do not rewrite their targets. `scripts/measure_range_rules.py` already measured nearer
  targets and range-edge entries, and both were worse.
- Expected effect: fewer trades, and the removed ones are the measured losers.

---

### Issue 4 — The trend gate's classifier does not separate winners from losers, and it lets "Mixed" through

**What.** `market_context.assess` blocks an entry against a clear 1h/4h trend. It allows
entries when the 1h and 4h disagree (Mixed) or are unknown.

**Evidence (my replay, section 5.2).** Same signals, split by higher-timeframe state, where
the trend is a simple EMA rule (close vs EMA20 vs EMA50 on 1h and 4h):

| Higher-timeframe state | Grid | Range | MeanReversion |
|---|---|---|---|
| Against the trend | −0.139R (z −4.1) | −0.132R (z −3.8) | −0.116R (z −2.9) |
| Mixed | −0.067R (z −2.0) | −0.055R (z −1.6) | −0.038R (z −1.0) |
| Aligned with the trend | +0.010R | +0.012R | +0.020R |

With this rule, counter-trend entries clearly lose. Mixed entries are also negative, though
less strongly. Aligned entries are about zero.

**The project's own script disagrees, and that is the more important finding.**
`scripts/measure_htf_consensus.py --candles 10000`, run today, uses the repo's real trend
function on SOL, XRP, DOGE, ADA, ETH and BNB:

| The repo's 1h/4h reading | Trades | Result per trade |
|---|---|---|
| Both timeframes with the trade | 1,209 | +0.000R |
| Both timeframes against the trade (blocked today) | 3,742 | +0.002R |
| One against (blocked today) | 2,197 | +0.073R |
| One with | 1,299 | −0.042R |

With the repo's own trend classifier, trades **with** the trend and trades **against** it
perform the same (z = −0.11). With a plain EMA20/EMA50 rule on the same kind of data, trades
against the trend lose about 0.13R (z around −4) on every signal and on both timeframes.

So the idea of the gate is right, but **the classifier behind it does not separate good
entries from bad ones in this window**. This matches what the ledger shows: all 15 live
trades were labelled "1h/4h Bearish" and aligned, and 13 lost.

**Limits, stated plainly.**
- The two replays use different coins and slightly different rules, so the numbers are not
  directly comparable. The direction of the difference is what matters.
- `CLAUDE.md` records an earlier 3,539-trade test where the gate did separate (z = +2.76).
  The effect of the repo's classifier changes between windows. The EMA rule's effect was
  stable across all three time-thirds of my window.

**Solution.**
1. Add the EMA20/EMA50 trend rule to `scripts/measure_htf_gate.py` beside the existing
   classifier and compare both on the same trades, over at least three windows.
2. If the EMA rule separates and the current one does not, replace the classifier used by
   `market_context.build` (or require both to agree).
3. In the same test, measure refusing Mixed. Add a strict mode (aligned only) if it holds.

---

### Issue 5 — The stop is sized on 15-minute noise while positions are held for hours

**What.** The stop is 2.5 × ATR(15m), on average **0.95%** from entry. Positions are held
2–12 hours. Over 8 hours a coin normally moves several 15-minute ATRs in both directions,
so the stop is reached by ordinary movement. The reflection model reached the same
conclusion on trades 9, 10 and 14 ("the stop sat inside the normal noise band").

It also makes fees expensive. A round trip costs 0.10% of notional. Against a 0.95% stop
that is **0.106R per trade**. Against a 1-hour ATR stop (average 2.03%) it is **0.049R**.

**Evidence (section 5.4).** The same signals on 1-hour candles, with a 2.5 × ATR(1h) stop
and a 4h trend filter:

| 1-hour signal, trend-aligned | Trades | Net per trade | z |
|---|---|---|---|
| Breakout | 284 | +0.133R | +1.5 |
| MeanReversion (pullback into the trend) | 152 | +0.170R | +1.4 |
| Range | 224 | +0.020R | +0.2 |
| Grid | 254 | −0.022R | −0.3 |

Breakout and the trend pullback are positive, but **z ≈ 1.5 is not proof.** By the
project's own rule (`core/evidence.py`) this is a lead, not a finding. The result by
time-third is uneven, which is another reason not to trust it yet.

**The cost you must know about.** With your fixed 10x leverage and 100% allocation, a 2%
stop loses about 20–25% of the account when it is hit, against about 10–17% now. I am not
proposing a settings change. I am stating that a wider stop and unchanged settings mean a
bigger loss per stop, and the two cannot be separated.

**Solution.**
1. Write a measurement script for 1-hour Breakout and 1-hour trend-pullback entries using
   the real signal functions, non-overlapping trades, fees and funding, over at least 12
   months and 12+ coins, split into in-sample and out-of-sample halves.
2. Ship one only if its out-of-sample interval excludes zero.
3. Until then, keep trading on paper/demo only.

---

### Issue 6 — The break-even stop is hardcoded and cannot be turned off

**What.** `backend/graphs/nodes/monitoring.py:77` sets `TRAIL_TO_BREAKEVEN_R = 1.0`. When a
position reaches +1R the graph review moves the stop to the entry price. It is a constant,
not a setting. `TRAILING_STOP_R=0` does not disable it, because that setting belongs to a
different mechanism in `position_monitor`.

**Evidence.**
- Trade 12 reached +1.29R, the stop moved to entry, and it closed at −0.038 (fees).
- The open LTC trade now has its stop at its entry price (49.68).
- Replay (section 5.3): with the 2R target, break-even ON was lower than OFF for every
  aligned signal — Grid +0.010 vs +0.029R, Range +0.012 vs +0.036R, MeanReversion +0.020
  vs +0.058R. Each difference is inside the noise on its own, but the direction is the same
  every time, and it matches the repo's own trail measurement (−0.027R, z = −2.1).
- With break-even ON the win rate falls from about 38% to 28%, because winners that would
  have reached the target are scratched first.

**Solution.** Make the threshold an environment setting read at call time, default **off**
(0), matching `TRAILING_STOP_R`. Keep the code path so it can be turned back on if a
later measurement supports it.

---

### Issue 7 — The agent cannot join a trend that has already started

**What.** During 11-05 to 11-09, XRP fell about 15% with a Bearish 1h/4h trend. The agent
wanted to short and was refused every time:

| Day | Panel neutral | Confidence too low | Chasing (extension) | Chasing (relative strength) |
|---|---|---|---|---|
| 11-05 | 72 | 57 | 27 | 8 |
| 11-06 | 54 | 16 | 8 | 0 |
| 11-07 | 68 | 28 | 0 | 0 |
| 11-08 | 46 | 24 | 0 | 9 |
| 11-09 | 34 | 34 | 2 | 24 |

When the 15-minute chart fell hard, the extension gate refused it as chasing (for example
"7.10 ATR in the SHORT direction from 1.3222 to 1.2532"). When the 15-minute chart paused,
the panel read NEUTRAL. There is no state in between where an entry is allowed.

**Do not remove the chasing gates.** Both were measured on long non-overlapping samples and
both remove a losing set (`backtests/2026-11-02/`, `backtests/2026-11-03/`).

**Solution.** Add an entry designed for this case: a **pullback into the trend**. In a
Bearish 1h/4h trend, wait for a 15-minute or 1-hour bounce (for example RSI above 70) and
short the bounce. That entry is by construction not extended, so the chasing gates pass it.

Both live wins were exactly this pattern (trade 4: RSI 86.8; trade 13: RSI 82.5; both with
a Bearish 1h/4h). The replay is encouraging but not proven:

| 15-minute pullback, trend-aligned | Trades | Net per trade | z |
|---|---|---|---|
| RSI > 70 / < 30 | 270 | +0.058R | +0.7 |
| RSI > 75 / < 25 | 156 | +0.098R | +0.9 |
| RSI > 80 / < 20 | 78 | −0.099R | −0.6 |

Two wins and a z under 1 are not evidence. This goes into the same measurement as issue 5.

---

### Issue 8 — Learning produces correct lessons that nothing uses, and one false lesson

**What is true today.**
- `strategy_performance` needs 20 closed trades **per strategy** before a win rate affects
  selection. There are 15 closed trades across 6 strategies. At this rate the loop will not
  influence anything for months.
- Reflection lessons do not steer entries. That is by design (invariant 5) and should stay.
- 6 of 14 reflections are the canned fallback: five from before the 11-02 model fix
  (HTTP 404), and one on 11-09 where the model returned an empty answer.

**The false lesson.** A break-even exit is recorded as `stop-loss`. For trade 12 the model
was told "stop-loss at 1.1051 from entry 1.1052" and concluded the stop had been *placed*
0.0001 from entry. It was not. It was placed at 1.1217 and moved after +1.29R. The lesson
is wrong because the input was misleading.

**Solutions.**
1. Record a stop that was moved to entry as `breakeven-stop`, not `stop-loss`, in
   `position_monitor._check_price`. `strategy_performance` should then count it as a
   scratch, not as a stopped-out loss.
2. Give the reflection prompt the original stop, the final stop and the MFE/MAE (all
   already stored on the trade row).
3. Add a pooled view by strategy *family* (range family, trend family) for reporting only,
   so a pattern like "0 of 9 range-family trades won" is visible long before any single
   strategy reaches 20 trades. Keep it out of selection, as `by_regime()` is.
4. Retry once when the model returns an empty completion, before falling back.
5. Treat each repeated model lesson as a hypothesis to measure with a script. The lesson
   "the stop sits inside the noise band" appeared three times; issue 5 is that measurement.

---

### Issue 9 — With these settings one stop costs 10–20% of the account

Not a defect and not something I am asking you to change. It is the arithmetic of 10x and
100% allocation:

- Equity is about **$3.82** (cash 0.01 + locked margin 3.81).
- The open LTC position risks 0.65, which is **17%** of the account.
- The last three full stops cost 0.37, 0.76 and 0.62.
- The 15 trades lost 3.33 in total. If nothing was deposited or reset in between, the
  account started this run near $7.2 and has lost about 47%.

At zero edge, four stops in a row is ordinary, and four stops is roughly half the account.
`ACCOUNT_HARD_STOP_LOSS_FRACTION` (50% below the session start) will end the session when
that happens. Below about $0.50 of equity the $5 venue minimum can no longer be met at 10x.

**What follows for the plan.** Because size is fixed, the only protection is entry quality.
That is why issues 3, 4 and 6 should come before anything that adds trades.

---

### Issue 10 — Five of nine closes since 11-04 have no venue order id

Trades 10, 11, 12 (both legs) and 14 closed with `exchange_order_id` NULL, although their
entries were filled on Binance demo. The likely explanation is that the resting stop at the
venue fired first and the monitor then settled the book locally. In that case the recorded
exit price is the price the monitor observed, not the venue's actual fill.

I could not confirm this from the database alone.

**How to check.** Compare those five closes with the Binance demo order history for the
same minutes. If the venue shows a stop fill each time, the book is correct and the only
gap is that the algo order's id and fill price are not written to the closing row.
**Solution if confirmed:** when a close is settled because the venue is already flat, read
the fired stop order and store its id and average price on the closing row.

---

### Issue 11 — Smaller findings

| # | Finding | Evidence | Solution |
|---|---|---|---|
| 11a | The current LTC short was entered at "2.92 ATR into the move" against a 3.0 limit, RSI 34.5, regime Accumulation, confidence 0.18 | `monitored_positions.entry_context` | None needed now. It is an example of issue 2: a marginal setup just inside every limit |
| 11b | The liquidity constraint cut 217 WAIT decisions further, usually by under 10% | `decisions.rejection_reasons` | Low priority. Without the cut the median would be about 0.17, still under 0.18 |
| 11c | `decisions` structured columns (`debate_confidence_pct`, `ensemble_*`) are NULL on graph rows; the confidence is only inside the rationale text | Read from the table | Write the panel direction and confidence to the columns so the abstention report can use them without parsing text |
| 11d | 11 `execution_submissions` rows before 11-04 have no outcome | Read from the table | Leave as is. `CLAUDE.md` says inventing them would be a fabricated history |
| 11e | The 8-hour time limit lowers the replay result by about 0.03–0.05R, but both live wins were closed by it | Section 5.3 | No change. The two views disagree and neither is significant |
| 11g | `scripts/full_system_exercise.py` passes 18/18 with the local `.env` and fails scenario 14 ("the CRO must approve a plan inside every limit") without it. Setting only `MAX_PORTFOLIO_VAR_FRACTION=0.12` does not fix it | Run today, both ways | The harness depends on some operator setting. Pin the configuration inside the harness, as `tests/conftest.py` does, so its result does not depend on whose `.env` is present |
| 11f | The local `.env` on this Mac still names dead models | Earlier in this session | Only matters if the backend is run from this machine. The server's models work (issue 8 evidence) |

---

## 5. The measurements

### 5.1 The project's own script, run today

`scripts/measure_strategy_net.py --candles 10000` on a scratch copy of the repo. Table in
issue 1. All eleven strategies are negative net of fees.

### 5.2 Entry signals by higher-timeframe state

My replay. 7 coins (XRP, SOL, DOGE, ADA, LTC, LINK, SUI), 15-minute Binance futures candles,
2026-07-19 to 2026-11-12. Entry at the close of the signal bar. Stop 2.5 × ATR(15m). Fees
2 × 0.05%. The live fee gate is applied. A strategy cannot re-enter while its last trade is
open. A bar containing both stop and target counts as the stop. Live exit model (2R target,
break-even at +1R).

| Signal | All | Live gate (not counter) | Aligned only | Counter | Mixed |
|---|---|---|---|---|---|
| Grid | −0.130R (1,911) | −0.034R (1,628) | +0.010R (746) | −0.139R (1,220) | −0.067R (1,314) |
| Range | −0.119R (1,856) | −0.035R (1,540) | +0.012R (634) | −0.132R (1,128) | −0.055R (1,291) |
| MeanReversion | −0.100R (1,514) | −0.030R (1,117) | +0.020R (289) | −0.116R (867) | −0.038R (987) |
| Breakout | −0.086R (1,587) | −0.082R (1,518) | −0.069R (727) | −0.122R (396) | −0.087R (949) |

Trade counts in brackets. Standard errors are 0.03–0.07R, so the "aligned only" column is
not distinguishable from zero.

### 5.3 Exit rules (trend-aligned entries only)

| Exit rule | Grid | Range | MeanReversion |
|---|---|---|---|
| Target 1R | −0.013R | −0.034R | +0.001R |
| Target 1.5R | +0.021R | +0.009R | +0.003R |
| Target 2R, break-even off | +0.029R | +0.036R | +0.058R |
| **Target 2R, break-even on (live)** | +0.010R | +0.012R | +0.020R |
| Target 2R, break-even off, 8h limit | −0.018R | −0.001R | +0.031R |
| Target 3R, break-even off | +0.056R | +0.053R | +0.084R |

No row is distinguishable from zero. No exit rule turns these entries into a profit. A
nearer target is not better, which agrees with `scripts/measure_range_rules.py`.

### 5.4 One-hour candles

Table in issue 5. Counter-trend entries are clearly negative there too (−0.12R to −0.24R,
z up to −2.5), which supports the existing trend gate on a second timeframe.

### 5.5 Limits of these replays

- They replay the deterministic signal functions. The nine-specialist panel needs an order
  book, a tape and news and cannot be replayed.
- My trend filter is a simple EMA rule, not the repo's function.
- 115 days, mostly a falling market. A rising market may rank things differently.
- Many comparisons were made. A single z near 2 among dozens of rows is expected by chance.
  Only the counter-trend result (z around −4, repeated on every signal and both timeframes)
  is strong enough to stand on its own.

The replay scripts and their full output are in
`/private/tmp/claude-501/-Users-maruthu-Documents-Personal-trading-agent/1f0b8eab-c6a2-4303-b3ed-d9797e92cc39/scratchpad/`
(`measure.py`, `measure2.py`, `measure_out.txt`, `measure2_out.txt`). That folder is
temporary. Copy them into `scripts/` if you want to keep them.

---

## 6. Recommended order of work

| Step | Change | Type | Expected effect | Risk |
|---|---|---|---|---|
| 1 | Stop Grid and Range from originating entries (issue 3) | Small code change, reversible setting | Removes the 0-for-9 group | Fewer trades |
| 2 | Make the break-even stop a setting, default off (issue 6) | Small code change | Winners are no longer scratched at +1R | A +1R trade can return to a full stop |
| 3 | Label break-even exits correctly and fix the reflection input (issue 8) | Small code change | Lessons and statistics stop being distorted | None |
| 4 | Compare the repo's trend classifier with an EMA20/EMA50 rule; replace it and add a strict mode if the test agrees (issue 4) | Measurement, then small change | Makes the trend gate actually separate losers | Fewer trades |
| 5 | Measure 1-hour Breakout and trend-pullback entries over 12+ months (issues 5, 7) | New script, no live change | Finds out whether a real edge exists | None until shipped |
| 6 | If step 5 is positive out of sample, add it as a strategy and put the higher-timeframe trend into the market vote (issue 2) | Larger change with tests | The first entry method with measured edge | Larger loss per stop at fixed settings |
| 7 | Confirm the five closes without a venue id (issue 10) | Manual check | Confirms the book matches the venue | None |

Steps 1–4 reduce losses. Only steps 5–6 can produce profit, and only if the measurement
supports them.

**What not to do:**
- Do not lower `MIN_CONFIDENCE_TO_TRADE` or the neutral bands to get more trades.
- Do not remove the extension or relative-strength gates.
- Do not add a fixed profit target (`PROFIT_TARGET_PCT`) or partial take-profit at 10x.
  `CLAUDE.md` shows the arithmetic: a 2% target at 10x needs a 93% win rate.
- Do not move to real money until at least 30 closed demo trades show a net profit.

---

## 7. Honest expectations

- After steps 1–4, the measurements point to about break-even before luck, with fewer trades.
- A profitable result depends on step 5 finding an entry that survives an out-of-sample
  test. The two candidates are promising at z ≈ 1.5. That is not proof, and they may fail.
- At a 2:1 payoff, even a good system loses more trades than it wins. Runs of 4–6 losses
  are normal. With 10x and 100% allocation each of those costs 10–20% of the account.

---

## 8. Verification status

All run today against commit `64c41d2`. Backend checks ran on a scratch copy of the repo
with a fresh Python 3.12 environment, so nothing in the project was modified.

| Check | Result |
|---|---|
| `npx tsc --noEmit` | Clean |
| `npx vitest run` | 37 files, 522 of 522 tests passed |
| `pytest -q` (backend) | 2,639 passed, 2 skipped |
| `scripts/full_system_exercise.py` with the local `.env` | 18 of 18 scenarios passed; no database pool opened |
| `scripts/full_system_exercise.py` without `.env` | 17 of 18; scenario 14 fails (issue 11g) |
| `scripts/measure_strategy_net.py --candles 10000` | All eleven strategies negative (issue 1) |
| `scripts/measure_htf_consensus.py --candles 10000` | The trend classifier shows no separation (issue 4) |
| Live database | Read-only connection; 30 trade rows, 2,011 decisions, 14 reflections, 18 submissions read |

Not verified:
- `npm run build` was not run.
- The server's `.env` and logs cannot be read from this machine, so server settings
  (`RUN_UNTIL_TARGET`, model ids, the current session's coin list) are inferred from the
  database, not read directly.
- Binance demo order history was not checked (issue 10).
- The nine-specialist panel cannot be replayed offline, so issue 2 rests on the code and
  the recorded confidences, not on a backtest.










Act as a senior quantitative trading engineer and autonomous AI agent architect. Perform a complete end-to-end audit, testing, debugging, and optimization of my existing cryptocurrency trading agent.

Inspect my entire GitHub repository, including every agent, LangGraph node, workflow, strategy, indicator, market-data integration, Binance/Bybit execution module, Supabase integration, volatility analysis, Polymarket, news, sentiment, funding rates, Open Interest, and learning mechanisms.

**Most important: Preserve my existing core trading logic and architecture.** Do not rewrite, replace, remove, or redesign my existing strategy, core modules, entry conditions, exit conditions, or agent architecture. Improve the existing implementation without changing its fundamental behavior. If a core logic change is necessary, explain it and request my approval first.

Your main objectives:

1. Verify that every agent and LangGraph node executes correctly, communicates properly, receives valid data, and contributes to the intended trading decisions.
2. Test the complete trading lifecycle: market analysis, signal generation, LONG/SHORT entry, confidence evaluation, position sizing, order execution, TP, SL, trailing stop, partial exits, and final position closure.
3. Verify that volatility, Polymarket, news, sentiment, funding, Open Interest, and all existing intelligence integrations actually influence their intended decisions.
4. Thoroughly test the learning system to ensure it learns from actual winning and losing trades, including fees, funding, slippage, and exit outcomes.
5. Investigate why trades are rejected or not executed, identify bottlenecks, and prevent incorrect entry and exit decisions.
6. Run historical backtests, paper trading simulations, integration tests, and end-to-end tests. Measure net profitability, win rate, profit factor, expectancy, and maximum drawdown.
7. Fix confirmed bugs, broken integrations, incorrect calculations, synchronization problems, and performance issues without changing core strategy logic.

**Profitability objective:** Improve trade selection, entry accuracy, exit management, and risk-adjusted returns. Optimize for positive net expectancy after fees, funding, and slippage, not simply more trades or higher win rate. Never guarantee profits.

First understand the complete repository before modifying anything. Test every module individually and together. Never use real-money trades or modify production Supabase data.

Finally, provide a detailed report of every agent, node, bug, fix, test result, profitability result, and unresolved issue. List every modified file and explicitly confirm whether my original core trading logic was preserved.

Do not claim something works unless you have actually tested it. Prioritize making my existing trading agent reliable, intelligent, fully integrated, and consistently profitable through evidence-based improvements.