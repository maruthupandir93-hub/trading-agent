Full Trading Bot Review — Latest Code, Remaining Risks, and Profitability Improvements
I’ll keep this review strictly read-only. I will not edit files, push commits, change your Supabase data, deploy your server, or place exchange orders.

One important distinction before we begin: a code review can identify bugs, execution risks, missing safeguards, and weaknesses in the trading methodology. It cannot establish that a bot will make profitable trades in every market. The realistic goal is a bot that trades only when its strategy has a demonstrated statistical edge, controls losses when conditions change, and can safely stop trading when that edge disappears.

I’ll review the latest repository state rather than assume that the two commits already deployed to your Oracle server. I’ll also distinguish confirmed code findings, risks that need a test, and items that cannot be verified from GitHub alone.
1. What I verified in the latest repository
Latest main commit

53149d004db6c9d81eadad5fce17125c8eaba98b

Open latest commit



Repository inventory

758 tracked files, including 168 backend files, 109 test files, frontend components, API routes, configuration, documentation, and scripts.



Reported test results

The latest commit message reports 2,343 backend tests passed, 2 skipped, and 505 frontend tests across 36 files, with TypeScript and build checks passing. I did not independently rerun that test suite.



I inspected the changes in both commits and reviewed the important code paths involved in risk validation, autonomous sessions, order execution, exchange integration, position monitoring, backtesting, authentication, and the frontend-to-backend proxy.

Scope clarification: I cannot honestly claim to have reviewed every line of all 758 files in this single pass. The findings below are based on the latest source and targeted inspection of the high-risk files. They are a substantial technical review, but not a complete line-by-line certification of every frontend component and backend module.
2. Critical issues I found
These are the issues I would prioritise before considering real-money trading.

Issue 1 — Disabling live mode can simulate a real-position close
P0

File: backend/agents/execution_agent.py

The current routing rule requires both tab == "real" and not self.simulation_mode before sending a close to the exchange. When live trading is disabled, simulation_mode becomes true. A close request for a position on the real book can then take the simulated path instead of closing the actual exchange position.

Consequence: the local book may report a closed trade while the real exchange position remains open and exposed to the market.

Required improvement: make mode changes position-aware. The execution system must always close or reduce a real position on the venue where it actually exists, regardless of whether new entries are disabled. Reconcile against the exchange before declaring it closed.

Review execution-agent source




Issue 2 — The live-enable endpoint can continue after a credential-check error
P0

File: backend/api/admin.py, live-enable handler

The endpoint catches an exception from the venue credential check, logs a warning, and then continues to settings.set_live_trading(True).

This means an unexpected error while checking credentials can still result in live mode being enabled. It is not enough to check that the normal missing-key case is rejected.

Required improvement: the live-enable operation must fail closed if credentials, venue identity, network selection, or other mandatory readiness checks cannot be verified. Its response must accurately reflect the final state.

Review admin endpoints




Issue 3 — A real position can remain unprotected if the exchange stop fails
P0

File: backend/agents/position_monitor.py, _place_resting_stop()

The code attempts to place a resting stop-loss at the exchange. If that request fails, it logs a critical error but does not automatically close or reject the already-open position. The system relies on its in-process monitor to continue watching the price.

The code explicitly acknowledges that a process crash, restart, or outage can leave a real position without exchange-side protection if the resting stop was not successfully placed.

Required improvement: track protection as an explicit state for each real position. Do not label a position protected until the venue confirms the stop order and its quantity. Alert immediately on failure and apply a defined emergency procedure.

Review position monitoring




Issue 4 — Exchange credentials can cross a plain-HTTP backend connection
P0

Files: app/api/exchange/route.ts, app/api/backend/[...path]/route.ts, REAL_TRADING.md

The documented manual exchange flow stores API credentials in browser localStorage. The frontend proxy documentation also describes a Vercel-to-backend connection using plain HTTP.

If that HTTP connection crosses an untrusted network, exchange credentials and other sensitive request data can be exposed in transit. Browser storage also increases exposure if a malicious script runs in the dashboard.

Required improvement: use TLS for the entire network path, keep exchange secrets out of browser storage, and prefer server-side secret storage with access restrictions. Exchange keys should have no withdrawal permission and should be IP-restricted where practical.

Review manual exchange route · Review backend proxy



These are not hypothetical requests for extra features. They concern the correctness of position closure, live-mode control, protective stops, and credential handling.
3. Risk configuration and trade sizing
The new risk-coherence check is a useful improvement, but its behaviour and deployment configuration deserve special attention.
Issue 5 — Unmeasurable risk configuration can pass the new guard
File: risk_coherence.py

The function assess() returns ok=True when the VaR fraction or drawdown limit is missing or non-positive, with a reason saying the configuration could not be measured.

I understand the design distinction: “unknown” is not the same as “measured and unsafe.” That is logically correct for reporting, but it is not sufficient for authorising live trading.

For a live entry, an unknown risk budget should not automatically count as a safe configuration.

Improvement: distinguish three outcomes:
- SAFE: all mandatory risk values are valid and coherent.
- UNSAFE: the measured configuration violates a limit.
- UNKNOWN: a required value cannot be established.
Allow the first to proceed; reject live entries for the second and third. Paper research can have a more permissive policy, provided the state is clearly labelled.
Issue 6 — The new guard does not make the chosen risk amount small
The repository's diagnosis records a prior live configuration with MAX_PORTFOLIO_VAR_FRACTION=0.12 and MAX_DRAWDOWN_FROM_HWM=0.30. It estimates that a stop-out could cost around 12% of equity under that configuration.

The new guard refuses certain self-defeating combinations. It does not automatically reduce risk or choose a suitable risk amount for you.

The diagnosis suggests a VaR fraction no greater than approximately 0.037 for a 30% drawdown limit and an eight-loss survival requirement. That arithmetic is configuration-specific; it is not a universally safe setting. A smaller drawdown limit requires a smaller per-trade risk budget if you want the same streak tolerance.

Improvement: choose per-trade risk, daily loss, and maximum drawdown as a coordinated policy. Validate the actual deployed values, not just the defaults in the repository.
Issue 7 — A successful risk check does not prove the whole portfolio is safe
File: risk_gateway.py

The gateway performs sizing and risk validation, which is a good separation of responsibilities. However, the operator guide still lists several limitations that should be verified against the current implementation:
- Cross-asset correlation is not checked synchronously in every relevant path.
- The live price is not always cross-checked against the candle close.
- Some callers use a more permissive risk-gateway mode.
- The non-graph/agent order path has historically had different exchange-size validation behaviour.
These are review items, not proof that every current trade bypasses the controls. The concern is that a control enforced on one entry path may not be enforced identically on another.

Improvement: one authoritative pre-trade validator should govern every automated and manual entry. It must check the actual position, account equity, correlated exposure, venue constraints, stale data, and final stop-based risk immediately before submission.
4. Execution and reliability
Issue 8 — Duplicate protection is in memory, not durable
File: execution_service.py

The execution service keeps its _submitted idempotency map in process memory. The source explicitly acknowledges that this state is lost on restart.

If an exchange order is accepted and the process crashes before the outcome is durably recorded, the system may not have enough information to distinguish a completed submission from a new one after restart.

Improvement: persist idempotency keys and order intent states. On recovery, query the venue using the exchange order/client ID and reconcile before any retry. Never assume a timeout means the exchange rejected the order.
Issue 9 — The project has multiple exchange-order interfaces
Files: venue.py, execution_agent.py, REAL_TRADING.md.

The backend venue adapter is designed for Binance and Bybit linear perpetual markets. The manual frontend exchange clients are described as spot clients, and REAL_TRADING.md explicitly states that the manual real-trading feature is spot-only.

That means the project has different trading capabilities depending on which interface you use. The UI's “real” mode must not be assumed to mean the same thing as the autonomous futures execution path.

Improvement: clearly separate and label:
- Paper simulation.
- Testnet exchange execution.
- Live spot execution.
- Live perpetual-futures execution.
Each should expose its actual venue, market type, account, network, position mode, and protective-order status. Do not let a generic “Real” label hide these distinctions.
Issue 10 — The TWAP code does not yet execute a true TWAP schedule
File: execution_agent.py

The code calculates order slices and logs the intended execution window, but the source states that it still submits a single market order for the full size. There is no scheduler submitting the slices over time.

Improvement: either implement and test a real scheduled execution plan, including cancellation and partial-fill handling, or ensure the UI and logs call it a sizing plan rather than an active TWAP risk control.
Issue 11 — Exchange-side protection and local monitoring must agree
Files: position_monitor.py, position_worker.py.

POSITION_MONITORING_ENABLED defaults to false. That setting disables the graph-based position-review actions; it does not mean every mechanical stop is necessarily disabled. The mechanical price monitor and resting-stop mechanism are separate.

However, if you expect AI-driven stop adjustments, reductions, or exit decisions, you must verify that the graph worker is enabled and functioning. If the process is alive but the monitoring cycle is repeatedly failing, the logs must make that visible.

Improvement: monitor three independent states: mechanical stop active, exchange-side stop confirmed, and graph-based position review healthy. One status should not stand in for the others.
5. Strategy selection and profitability
The two commits improve holding-time consistency and backtest accounting. They do not demonstrate that the trading strategies have a reliable edge.

The latest diagnosis itself states that the estimated edge is around +0.05R and is not yet distinguishable from zero. It also notes that optimizer_agent.optimize_strategy currently has no callers, so it is not an active optimisation loop.
Issue 12 — Scalping is still mismatched to the analysis timeframe
The system primarily analyses 15-minute candles. The strategy profiles include short-horizon strategies such as Scalping and Arbitrage, whose intended horizons are seconds to minutes.

The new minimum holding-time floor is eight bars, which equals two hours on a 15-minute chart. The diagnosis reports that, even after the floor, approximately 76% of Scalping trades and 73% of Arbitrage trades are closed by the clock.

That is an important finding: a strategy whose holding period is dominated by the time exit is not necessarily being tested as the strategy it claims to be.

Improvement: match each strategy to an appropriate execution and analysis timeframe. Do not rank a scalping strategy as the best one until it has been evaluated using data and fill assumptions appropriate for scalping.
Issue 13 — Backtest costs are improved, but not complete
File: strategy_backtest.py

The new backtest includes estimated slippage and the live holding-time rule. Funding is explicitly not modelled. The default slippage rate is an assumption, not a measured value for every instrument and market condition.

Other details still deserve testing against realistic fills:
- A signal based on a candle close cannot generally be assumed to fill at that exact close.
- If a candle crosses both a stop and target, the simulator must apply a conservative and documented ordering rule.
- Trailing-stop updates within a candle can produce optimistic results if the model does not correctly account for the order of intrabar price movements.
- Perpetual funding, exchange fees, spread, partial fills, and adverse stop execution can change net expectancy.
Improvement: compare the simulator against historical trades and actual testnet fills. Report gross and net expectancy separately, and use conservative fill assumptions for ambiguous candles.
Issue 14 — Strategy selection is not proof of profitability
File: opportunity.py

A strategy score combines signal strength, multi-timeframe alignment, volatility suitability, and historical performance. The performance component can be neutral when there are too few trades.

That is reasonable for ranking candidates, but it means a strategy can rank well without having demonstrated positive net expectancy.

Improvement: keep “best available candidate” separate from “approved for real money.” Require out-of-sample validation, minimum evidence, cost-adjusted expectancy, and regime-specific results before promoting a strategy.
6. What your trading bot should learn to do better
Do not focus on making the agent trade more often. Focus on making it distinguish good opportunities, poor opportunities, and situations where it should do nothing.









A. Strong trend

Prefer trend continuation, pullbacks, or confirmed breakouts in the trend direction. Avoid automatically fading a strong trend just because RSI is overbought or oversold.
![alt text](image.png)


B. Sideways market

Test mean reversion and range strategies only when the market is genuinely range-bound. Do not use a grid strategy without strict inventory limits and a defined exit if the range breaks.

![alt text](image-1.png)


C. Breakout or volatility expansion

Require a confirmed move, sufficient liquidity, and a reasonable entry distance. Avoid chasing an extended candle where the stop is too far away or the reward-to-risk ratio has deteriorated.

![alt text](image-2.png)


D. Panic, thin liquidity, or conflicting signals

Reduce exposure or stand aside. Do not assume that a sharp decline must rebound, or that every breakout must continue. A no-trade decision is a successful risk decision when the edge is weak.

![alt text](image-3.png)

Improvements to strategy intelligence
Area	What the agent should measure
Market regime	Trend strength, realised volatility, range conditions, liquidity, and regime changes
Signal quality	Entry location, multi-timeframe agreement, distance from invalidation, and confirmation
Execution quality	Spread, actual slippage, partial fills, rejection rate, and latency
Risk	Stop-based loss, correlated exposure, leverage, liquidation distance, and remaining account risk
Strategy performance	Net expectancy, profit factor, drawdown, win/loss size, and performance by regime
Abstention	How often the agent correctly rejects weak or conflicting opportunities

The LLM or debate panel can help explain a decision, but it should not override deterministic risk limits or autonomously increase live risk to recover losses.
7. A proper validation sequence
I recommend this order rather than adding more indicators or strategies.
Phase 1 — Correctness and safety

Resolve the mode-switch close path, fail-closed live-enable checks, protective-stop failure handling, and credential transport. Verify all automated and manual entry routes.



Phase 2 — Execution and recovery

Verify that the exchange and local book reconcile after partial fills, restarts, timeouts, manual closes, stop triggers, and duplicate events. Confirm that protective orders really exist on the venue.



Phase 3 — Strategy evidence

Backtest each strategy independently, with realistic costs, no lookahead, conservative stop/target ordering, out-of-sample data, and results split by market regime. Retire or restrict strategies whose edge is unproven.



Phase 4 — Learning and measurement

Make every trade traceable from market snapshot through decision, strategy version, risk approval, exchange order, fill, exit, fees, and final realised P&L. Use these records for research rather than allowing the learning loop to rewrite live risk automatically.



Phase 5 — Controlled deployment

Deploy to paper first, verify the deployed SHA and configuration, then use testnet where the selected venue supports the required instrument. Only consider live trading after recovery, protection, reconciliation, and net profitability evidence have been independently verified.



8. What I would not change yet
- Do not add more strategies before the existing ones have measurable net expectancy.
- Do not increase the risk budget merely to satisfy the new risk-coherence check.
- Do not trust a high backtest ranking when most trades are ending through the holding-time rule.
- Do not treat a successful API response or a dashboard status as proof of an actual exchange fill.
- Do not treat a local stop-loss value as proof that the exchange has a protective order.
- Do not switch to real trading based only on passing unit tests.
Final assessment
The latest commits improve the project in meaningful ways: they align live and backtest holding-time rules, account for estimated slippage, improve exit reporting, and refuse some risk configurations that would halt after ordinary losing streaks.

But the project still has significant risks in live-mode transitions, credential-check failure handling, exchange-side protection, durable idempotency, and the distinction between spot and perpetual-futures execution. More importantly, the repository's own diagnosis says the measured trading edge is not yet statistically distinguishable from zero.


## more issue

1. Trade reflections can never be saved. backend/agents/reflection_agent.py never imports asyncio, but line 273 uses it, so every closed trade raises an error there. That is why 6 closes produced 0 reflections and 0 hypotheses. The fix is one line: import asyncio.
2. Every configured AI model fails on your key: kimi-k3 410, nemotron 404, gpt-oss-20b no answer. Even after fix 1, every lesson would fall back to a canned rule-based text.
3. The hourly cleanup of decision records is broken. Its query uses a tar_id column that neither trades nor decisions has, as I checked in the live database. It errors every hour, so the table now grows forever.
4. Your server is unprotected through /api/ai/chat (backend/api/ai.py:201). It has no auth, so anyone can make your server send requests to any address on its own machine or network and read the replies. The Vercel /api/chat route has no auth either, and its address guard can be bypassed with addresses like localhost..
5. Simulated closes can be booked at a wrong blended price. When the mirror finds the exchange already flat, binance_testnet.py:268 averages a week of older exits instead of using this trade's actual exit. In the reviewer's test, a −$5 loss was booked as −$37.7.
6. Stop-order IDs repeat after a restart (position_monitor.py:815, counter n % 100 kept only in memory). On Bybit the replacement stop would be refused, leaving the position with no exchange stop.
7. Real-account equity still ignores locked margin. The code reads margin fields that the exchange layer never returns, so a healthy real session ends as "floored" right after its first trade. The earlier fix (F58) doesn't work.
8. Your settings block new sessions: VaR 0.12 with a 0.30 drawdown limit survives only 2.5 losses in a row, and the rule requires 8. A session that resumes after a restart skips this check entirely.
9. The server is very likely still using the 1R/1R trailing stop. Two trades moved their stop to exactly the entry price, and one closed for −$42 in fees only.

Medium

10. Turning live trading on mid-session ends the session for a false reason. It compares the paper target against the real balance and says "floored".
11. The daily loss limit can't fire at high leverage. It scales to 50% at 10×, above the 10% drawdown halt.
12. A refused session start still changes your money. It rewrites paper cash and resets the high-water mark before refusing.
13. A restored pending approval never expires: the time it was approved isn't saved.
14. Close alerts and reflections lost their link to the original trade. Telegram close alerts no longer show margin %, and reflections can't find the trade's execution-quality data.
15. The bus fix for cancelled tasks backfired. It swallows the cancel, so a cancelled price-feed watcher keeps running and ticks can arrive twice.
16. The "already flat" fix (F1) doesn't cover mirrored paper positions. The comparison check only looks at real positions, and the order watcher connects to the wrong testnet.
17. The WebSocket /api/graphs/stream is open when no key is set. Anyone can start full analyses and burn your AI budget.
18. A manual Sell of 1 coin closes the agent's entire position in that coin, and then logs an extra open row.
19. The browser can still act on made-up prices. Fake random-walk prices can still trigger AI paper closes that book P&L.
20. One write error makes every browser data store read as empty for the rest of the process.
21. Your trades, portfolio, decisions and P&L are publicly readable from the Vercel URL without the password (checked live).

Smaller

22. The check that a manual stop is on the correct side never runs, because it reads fields that do
23. The write-refused banner names the wrong cause, or shows nothing.
24. Pause and the portfolio save still ignore failed requests; the portfolio failure can lose paper trades.
25. The backend proxy can still reach /openapi.json via ...
26. The address guard refuses legitimate hosts such as fcc.gov, and /api/mcp-status follows redirects to blocked addresses.
27. ALLOW_UNAUTHENTICATED_WRITES is parsed differently by the frontend and the backend.
28. A dashboard password with non-ASCII characters locks you out.
29. The backtest uses a different holding-time limit on 1h data than live does.
30. Live progress summaries lose specialist results when several finish together.
31. A second corruption of the AI memory file freezes the win-rate and daily-loss stats.
32. Resetting stats stores win rate as a fraction where everything else uses a percentage.
33. A busy graph can push all trade_analysis runs out of the run history.
34. Telegram alert tasks can be garbage-collected before they send.
35. The event-path risk fix has no effect: it reads a direction field the event doesn't carry. Dormant while that path is off.
36. A tighten that arrives during a stop replacement is dropped, so the exchange stop can stay looser than the in-process one.

Biggest wins: fixes 1–3 and 5–7 are small code changes, and items 2, 8 and 9 are only settings chan

Do you want me to add all 36 to a new section in docs/new.md beside your 14, so everything is in on