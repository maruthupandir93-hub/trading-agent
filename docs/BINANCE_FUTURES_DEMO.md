# Binance futures demo paper trading

This integration sends orders with virtual funds to Binance USD-M futures demo.
It reads actual demo quotes, candles, order fills and positions. Demo liquidity
and fills are not a guarantee of mainnet execution quality.

## Connect

1. Create Binance Futures **Demo Trading** API credentials.
2. Set `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_SECRET` in the backend
   environment. Do not use the mainnet credential variables.
3. Keep `LIVE_TRADING=false`; select `PAPER_TESTNET_VENUE=binance`.
4. Restart the backend. In Settings, select Binance and click
   **Connect to Binance futures demo**. Verify the account check succeeds.
5. Start a paper session. Risk gates still apply; a connection does not force
   the agents to trade.

`PAPER_TESTNET_MIRROR` defaults off. The button persists the setting, so an enabled
mirror can resume after restart. This change does not enable it automatically.
Use one-way position mode; this adapter does not send hedge-mode position sides.

## Endpoints and data

The adapter hardcodes `https://demo-fapi.binance.com`. It does not fall back to
mainnet, read mainnet credentials, or accept an arbitrary host from configuration.

| Purpose | Endpoint |
|---|---|
| Clock | `GET /fapi/v1/time` |
| Quotes and candles | `GET /fapi/v1/ticker/24hr`, `GET /fapi/v1/klines` |
| Account and positions | `GET /fapi/v2/account`, `GET /fapi/v2/positionRisk` |
| Market entry / reduce-only exit | `POST /fapi/v1/order` |
| Stop-loss / take-profit | `POST /fapi/v1/algoOrder` |
| Cancel protection | `DELETE /fapi/v1/algoOrder` |

Protective orders use `algoType=CONDITIONAL`, `triggerPrice`, `MARK_PRICE`, and
`reduceOnly=true`. Their stored IDs have an `algo:` prefix so cancellation uses
the correct API after restart. Ordinary numeric order IDs retain regular-order
cancellation. The former assertion that demo cannot rest protective orders was
incorrect: `-4120` from the regular order endpoint requests the Algo Order API.

When the Binance paper mirror is active, agent ticks poll demo futures every
two seconds and graph candles use the demo futures endpoint. Price reads refuse
demo quotes older than 15 seconds; they do not substitute a cached spot quote.
Disconnected/non-Binance/live-trading market-data paths retain their existing
behavior. The dashboard's separate general market stream is unchanged.

Quantities use decimal step truncation. Signed requests correct clock skew and
retry a timestamp rejection only once. Unknown market-order acknowledgements
are queried by the original client ID rather than resubmitted. Missing filters,
malformed responses and transport failures return explicit failures. Signed
request URLs and credentials are not included in transport error messages.

## Verification

Read-only account, quote, candle and empty-account checks:

```powershell
python -B scripts/binance_demo_verify.py
```

Explicit virtual-fund orders:

```powershell
python -B scripts/binance_demo_verify.py --execute-demo-orders
```

The script refuses accounts with existing positions or orders. It opens a long
and then a short, each around 12 USDT notional, verifies stop and target orders,
closes reduce-only, cancels its protection and checks the account is flat.
It does not enable the app, write the application's trade database, or change
account leverage. Do not run other trading processes on the demo account during
this check. A failure to verify cleanup requires checking the demo account.

Verified on 2026-10-03: both XRPUSDT round trips filled; stop-loss and take-profit
were visible at Binance; final positions and open orders were empty. These are
adapter lifecycle checks, not a claim that every agent strategy or market
scenario has been validated with live orders. Existing paper fallback, modeled
fee accounting and broader position-reconciliation limitations remain outside
this change; do not treat the local paper balance as the demo wallet balance.

Regression verification: the full backend run passed 2,145 tests with two
intentional skips. After the final input-validation and wiring checks were
added, the focused demo/mirror/audit run passed 90 tests. The frontend passed
475 tests; TypeScript checking and the Next.js production build also passed.

## Official references

- [Demo host, signing, timeouts and response semantics](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)
- [Market and Algo Order APIs](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
