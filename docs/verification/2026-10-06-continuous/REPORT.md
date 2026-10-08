# Continuous trading and profitability verification

Date: 2026-10-06. This is a bounded test, not a claim of guaranteed profits or complete deployed-system certification. Application source and strategy parameters were not changed during this test pass. Earlier local fixes remain uncommitted.

## Actual Binance demo execution

Used the existing `scripts/binance_demo_verify.py --execute-demo-orders` against the fixed `https://demo-fapi.binance.com` host. The account was checked flat, with no regular/conditional orders, and in one-way position mode before orders were sent. No mainnet orders were sent. Each entry was about 12 USDT notional.

| Check | Long round trip | Short round trip |
| --- | ---: | ---: |
| Quantity XRP | 7.9 | 7.9 |
| Entry | 1.5140 | 1.5138 |
| Exit | 1.5138 | 1.5140 |
| Entry order ID | 3515979727 | 3515979789 |
| Exit order ID | 3515979744 | 3515979826 |
| Holding seconds | 6.139 | 6.102 |
| Gross realized USDT | -0.00157999 | -0.00158000 |
| Commissions USDT | 0.00956783 | 0.00956784 |
| Funding during check | 0 | 0 |
| Net USDT | **-0.01114782** | **-0.01114784** |

Total net: **-0.02229566 USDT**. Available balance moved from 4999.03957565 to 4999.01727999 USDT, matching the measured trade costs. Final check: no open XRP position and no regular or conditional orders.

Both entries and reduce-only exits filled. Stops and take-profits were accepted, observed as open conditional orders, and cancelled during cleanup. Their actual price-triggered activation was not tested on the exchange. The long entry had two fills, 1.4 plus 6.5 XRP; accounting sums both commissions. Raw exchange receipts are in [demo_execution_results.json](demo_execution_results.json).

These deliberately brief, manually selected broker checks crossed the spread and paid commission. They were not trades selected by the analysis agents, were not written to the application's production trade log, and must not be counted as evidence of agent profitability. The adapter check does not test the complete deployed decision-to-database path.

## Repeated execution and accounting stress test

**99 tests passed** in the targeted session, learning, lifecycle and regression suite. The new stress harness covers **120 synthetic round trips** in three 40-trade sequences: profitable price moves, losing price moves, and mixed outcomes. Each simulated trade includes:

1. A rejected/unconfirmed close, with the full position retained.
2. A 25% partial close, with 75% retained.
3. A final close for only the remaining 75%.
4. Correct client-ID reuse for an unconfirmed retry and fresh IDs for subsequent operations.
5. Exactly one final POSITION_CLOSED event, no leftover position, and cash reconciled against the sum of partial/full close records after modeled fees.

This represents 360 mocked close attempts and 240 recorded partial/final settlement legs. Synthetic winners test accounting arithmetic; they do not demonstrate profitable market predictions. Exchange responses and journal persistence were mocked in this stress harness. Related existing tests cover learning/event contracts and session symbol rotation. See [test output](continuous_tests.txt) and [harness](continuous_lifecycle_harness.py).

## Real-data analysis and continuous session

Four initial analysis cycles used live Binance demo candles/depth/tape, the configured model provider, and an isolated 2 USDT paper book. Execution subscribers were disconnected and signed exchange mutations were blocked. Three cycles traversed 22 nodes; ETH exited after nine nodes when no strategy scored sufficiently. No node errors were reported. Cycles took 6.463-19.349 seconds and generated zero approved execution plans.

Observed reasons:

- XRP: Range/Grid short candidates opposed by a neutral specialist panel, confidence 0.04-0.08.
- SOL: Grid short candidate opposed by a neutral panel, confidence 0.05.
- ETH: best strategy score about 0.253, below the 0.35 selection floor.

In the first XRP run, data validation took 5.80 seconds, market analysis 2.48 seconds, and the narrative model step 10.25 seconds. This shows where that run spent time; it is not a benchmark of the deployed host. Waiting longer did not make a weak signal valid.

The production session loop was then run locally with its normal 12-second poll and 30-second decision interval, rotating XRP, SOL and ETH. Execution stayed disconnected. Final session result: **10 polls, 6 analyses, 0 approved plans, 0 agent trades**, over 187.0 seconds. It stopped at the test's six-analysis limit. XRP/SOL were rejected for neutral specialist evidence; ETH produced NO_DECISION with a blank session rationale. See [session decisions](forward_session_results.json) and [session summary](forward_session_summary.json).

The session's cycle counter counts polls, not trades or full analyses. Thousands of polls therefore do not establish that thousands of trade opportunities were rejected or that the executor failed.

Raw first-cycle results: [live_analysis_results.json](live_analysis_results.json). The isolated book and local memory do not represent the latest production account state.

## Profitability diagnostic across all 11 strategies

Fetched 1,499 completed candles per series for XRP, SOL and ETH, at 15-minute and one-hour timeframes: six series, 8,994 candles, 66 strategy/market/timeframe combinations. Fifteen-minute history spans 2026-09-21 through 2026-10-06; hourly history spans 2026-08-05 through 2026-10-06.

Used each existing signal function and the existing deterministic backtester without changing parameters. Results include modeled taker fees, but not live slippage, funding, venue-size constraints, specialist voting, regime eligibility gates, or the live monitor's dynamic exits. They are individual strategy diagnostics, not a portfolio return or a full-agent replay. Overlapping markets and timeframes are not independent samples.

1R is one planned stop-loss risk unit. The later-half column is a separate fixed-parameter temporal stability check; it is not a properly held-out model-selection result. Resetting positions at the split means the two halves need not sum to the full-window results.

| Strategy | Closed trades | Gross-positive outcomes | Full mean net R/trade | Later-half mean net R/trade |
| --- | ---: | ---: | ---: | ---: |
| Trend | 222 | 65 | -0.1960 | -0.3172 |
| MeanReversion | 160 | 68 | +0.1903 | +0.3447 |
| Momentum | 164 | 48 | -0.2072 | -0.4457 |
| Scalping | 136 | 38 | -0.2479 | -0.0599 |
| Swing | 213 | 59 | -0.2369 | -0.3729 |
| Breakout | 176 | 48 | -0.2843 | -0.3543 |
| Range | 169 | 60 | -0.0130 | +0.3489 |
| Grid | 164 | 62 | +0.0558 | +0.5095 |
| Arbitrage | 110 | 41 | +0.0510 | +0.5135 |
| VWAP | 80 | 26 | -0.0776 | +0.3299 |
| VolatilityBreakout | 65 | 18 | -0.2859 | -0.1608 |

Breakout remains negative in the pooled full sample and the later half. MeanReversion was positive in both halves of this sample, while several other strategies changed sign between halves. That makes them research candidates, not proven profitable replacements. Even the positive aggregate strategies contain many losing trades. No backtest output was promoted into the running scoring priors.

See [aggregate results](history_aggregate.json) and [per-series results and every simulated trade](history_results.json).

## Bugs and verification gaps still present

- **Missing reason in session output:** ETH correctly rejected the weak setup, but returned `decision=null` without `noDecisionReason`; the real session loop recorded `NO_DECISION` with an empty rationale. The reason exists deeper in `unavailable`, making the visible session log less informative than the underlying analysis.
- **Stale explanatory text:** analysis summaries still claim some specialist feeds are absent and describe an obsolete execution path even when the actual run used those feeds. Do not use these static descriptions as evidence of runtime behavior.
- **Production DB unavailable:** the configured database hostname still fails DNS resolution. Production journal writes, deployed memory, persistence across restarts, and the deployed agent's actual continuous execution have not been validated in this pass.
- **Deployment status unknown:** no backend URL/session status was provided during the test. The local configured mirror is off and the saved local session is stopped; this does not establish the remote deployment's state.
- **No profitable autonomous forward sample:** broker routing worked, but the observed analysis produced no approved signals. The two actual trades were execution checks, both net losses. There is no honest basis to claim every trade is profitable or that the full agent has a positive expected return.
- Exchange-triggered stop/target exits, real partial/timeout recovery, long-duration funding, and continuous deployed reflection/database writes still need an integrated demo forward run with the deployed backend and working database.

The next integrated run should retain the existing risk gates, use a bounded demo session, and reconcile every naturally approved trade's exchange fills, fees, funding, final position state and application journal. Forcing entries just to increase the trade count would not validate the strategy.
