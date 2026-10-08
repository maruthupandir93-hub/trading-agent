# Binance demo loss investigation and local fixes

Date: 2026-10-06. Changes are local and have not been deployed. No new exchange orders were placed during this fix pass. No historical production records were rewritten.

## Evidence and losses

The five previously reconciled completed trades contained four losses and one profitable Scalping trade. The sixth Binance round trip was a further loss, so the available evidence is five losses and one win, not six losses.

| Trade | Strategy in previous DB audit | Binance net USDT |
| --- | --- | ---: |
| 1 | Breakout | -0.04907884 |
| 2 | Breakout | -0.05936920 |
| 3 | Scalping | +0.04749936 |
| 4 | Breakout | -0.06465696 |
| 5 | Breakout | -0.02862433 |
| 6 | DB unavailable for refreshed attribution | -0.12500647 |
| Total | | -0.27923644 |

Sixth trade: 4.9 XRP bought at 1.5125 (order 3515452108) and sold at 1.4936 (order 3515759650), approximately 16.75 hours later. Read-only exchange queries confirmed gross P&L -0.09261, entry commission 0.00296450, exit commission 0.00292745, and funding cost 0.02650452 during that holding interval. Funding attribution assumes these app trades are the only exposure in the same symbol during that interval. These are small absolute amounts but material relative to the configured 2 USDT paper starting amount.

Three of the four original losing Breakout entries were made during 15-minute candles which subsequently closed back inside the breakout boundary. The signal ignored volume even though its strategy profile required above-baseline volume. This supports fixing confirmation; it does not prove every loss was avoidable. The original fourth remaining breakout loss cannot be explained by candle confirmation alone.

The five original entries were fully filled; measured execution scores were 92-96. Their losses were not explained by rejected entry orders. Losing exits were recorded as thesis-invalidated. Exact specialist/debate reasoning requires the deployed backend run traces, which are not stored in this local checkout.

## Implemented changes

- Analysis uses completed exchange candles for the traded symbol and benchmark, with one recorded run-start cutoff. CCXT close timestamps now respect the actual timeframe.
- Breakout requires valid volume above the prior 14-bar baseline. Invalid or missing volume produces HOLD. Signal revision 2 rejects historical breakout backtest priors from the old rules.
- Independent market and specialist fetches run concurrently. This removes serial network waits; it does not establish a measured improvement in deployed LLM latency.
- Binance demo depth and trade tape now use the demo futures market, with cache isolation from Binance spot.
- Connected demo entry and close failures no longer silently book simulated successes. Missing credentials while demo routing is enabled also refuse execution.
- Partial closes carry actual filled quantity, retain the remainder under monitoring, preserve protection, and retry the remaining quantity. Close receipts retain exchange order IDs.
- Monitored closes respect the requested paper/real book. A failed watched close does not fall through into a second direct close attempt. Successful close responses use actual fill price.
- Close client IDs are unique for successive completed operations and reused for retries until a positive fill is confirmed. This is in-memory retry protection, not durable exactly-once execution across process crashes.
- Demo routing changes through the settings API are refused while paper positions remain open, preventing a normal UI toggle from abandoning their close route.
- Restored watches recover entry fees, initial risk, funding rate and worst price.
- CCXT fee summary and fee breakdown are treated as alternative representations rather than charged twice.
- Full Binance demo round trips can reconcile actual commissions and funding using bounded read-only exchange queries; cash, close P&L and close events use that result when complete. Missing, partial, truncated or unsupported evidence retains modeled accounting. The original partial-scale-out learning aggregation remains a limitation.
- Reflection can join persisted execution quality using the trade authorization ID. Breakout naming now reaches its deterministic loss explanation when model output is absent.
- Session trade counts consume confirmed fill events and deduplicate them, including entries closed between polling cycles.

## Performance check: still not a proven profitable strategy

A read-only fetch of the latest 1,000 XRP/USDT 15-minute demo candles yielded 999 completed candles. The existing deterministic strategy backtester produced:

| Signal on completed candles | Closed trades | Wins | Mean net R per trade |
| --- | ---: | ---: | ---: |
| Previous price-only Breakout | 24 | 5 | -0.4991 |
| Revised volume-confirmed Breakout | 23 | 5 | -0.4616 |

Both results are negative. This is a short historical diagnostic with the backtester's fixed ATR exits and modeled fees, not a replay of the whole specialist/debate/monitoring pipeline. It omits actual funding and live slippage. Both rows use completed candles, so the comparison isolates the volume rule rather than measuring the entire candle-timing fix. No result was written into the scoring priors or used to tune thresholds. There is no evidence here that every trade, or the overall strategy, will now be profitable.

## Validation and limits

- Targeted execution, API and demo regression suite: 143 passed.
- Frontend unit tests: 475 passed (457 in the main run plus 18 after restoring the omitted Tailwind configuration to the isolated test copy).
- Final backend suite: 2,180 passed, 2 skipped, 706 deprecation/dependency warnings; 355.78 seconds. The suite ran in an isolated source copy without production credentials.
- Actual Binance demo public-data verification returned 50 bid levels, 50 ask levels and 200 recent trades from the corrected futures source.
- Actual Binance GET verification exercised the new accounting reader against the known sixth entry/exit IDs. Its DB lookup was replaced with that known entry ID for this read-only check; production persistence was not exercised.
- The production database hostname failed DNS resolution during this session. The latest database state, deployed run traces, and deployed behavior of these local fixes therefore remain unverified.
- No new demo round trip, mainnet trade, deployment, or future profitability verification was performed.
- Partial round trips and holds of seven days or more retain modeled costs. Funding attribution requires isolated same-symbol exposure and may be affected by exchange reporting delay. A later reconciliation job is not implemented.
- The settings guard does not migrate position provenance or protect against manually changing environment variables outside the settings API. In-flight routing changes and restart recovery still need durable per-position venue identity.
- The separate legacy Bybit real-session balance path was outside these Binance loss fixes and still requires migration to the selected venue.
- Existing datetime and dependency deprecation warnings remain.

Binance reference contracts checked: [Account Trade List](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Account-Trade-List) and [Income History](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Get-Income-History). Only GET requests were used for exchange verification.
