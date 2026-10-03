"""Verify demo futures data; opt in to small, virtual-fund round trips.

Run: python -B scripts/binance_demo_verify.py --execute-demo-orders
Never enables the application mirror or touches the application's database.
"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import dotenv_values
from backend.services import binance_testnet as bt


async def main(execute: bool) -> None:
    assert bt.BASE_URL == "https://demo-fapi.binance.com"
    config = dotenv_values(ROOT / ".env")
    for name in (bt.KEY_VAR, bt.SECRET_VAR):
        os.environ[name] = os.environ.get(name) or config.get(name) or ""
    logging.disable(logging.CRITICAL)  # Never print signed URLs from HTTP clients.
    verification = await bt.verify()
    assert verification["ok"], verification.get("reason")
    print(json.dumps({"account": verification}), flush=True)
    symbol = "XRP/USDT"
    ticker = await bt.fetch_ticker(symbol)
    candles = await bt.fetch_klines(symbol, "1m", 20)
    assert len(candles) == 20 and ticker["last"] > 0
    print(json.dumps({"ticker": ticker, "candles": len(candles)}), flush=True)
    async with httpx.AsyncClient(timeout=15) as client:
        for path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            ok, orders = await bt._signed(client, "GET", path)
            assert ok and orders == [], "Existing/unknown orders: refusing to disturb this account"
        ok, positions = await bt._signed(client, "GET", "/fapi/v2/positionRisk")
        assert ok and all(float(p["positionAmt"]) == 0 for p in positions), "Account is not flat"
        ok, mode = await bt._signed(client, "GET", "/fapi/v1/positionSide/dual")
        assert ok and mode.get("dualSidePosition") is False, "One-way mode required"
        rules = await bt._instrument_rules(client, symbol)
    if not execute:
        print("Read-only checks passed. Use --execute-demo-orders for virtual-fund orders.")
        return
    prefix = "dv_" + uuid.uuid4().hex[:14]
    for side in ("buy", "sell"):
        ids = []
        try:
            quote = (await bt.fetch_ticker(symbol))["last"]
            qty = float(bt._quantise(12.0 / quote, rules) or 0)
            assert qty > 0 and qty * quote >= rules["minNotional"] and qty * quote <= 15
            order = await bt.market_order(symbol=symbol, side=side, qty=qty,
                                         client_order_id=f"{prefix}_{side}")
            assert order.ok and order.filled_qty and order.average_price, order.error
            position = await bt.open_position(symbol)
            assert position and position["side"] == side
            exit_side = "sell" if side == "buy" else "buy"
            price = order.average_price
            stop = price * (.95 if side == "buy" else 1.05)
            target = price * (1.05 if side == "buy" else .95)
            sl = await bt.place_stop_loss(symbol=symbol, side=exit_side, qty=position["qty"],
                                         stop_price=stop, client_order_id=f"{prefix}_{side}_sl")
            if sl.order_id:
                ids.append(sl.order_id)
            assert sl.ok, sl.error
            tp = await bt.place_take_profit(symbol=symbol, side=exit_side, qty=position["qty"],
                                           take_profit_price=target,
                                           client_order_id=f"{prefix}_{side}_tp")
            if tp.order_id:
                ids.append(tp.order_id)
            assert tp.ok, tp.error
            async with httpx.AsyncClient(timeout=15) as client:
                ok, legs = await bt._signed(client, "GET", "/fapi/v1/openAlgoOrders",
                                           {"symbol": "XRPUSDT"})
            assert ok and all(any(str(r.get("algoId")) == oid[5:] for r in legs) for oid in ids)
            close = await bt.market_order(symbol=symbol, side=exit_side, qty=position["qty"],
                                         reduce_only=True, client_order_id=f"{prefix}_{side}_close")
            assert close.ok and await bt.open_position(symbol) == {}, close.error
            print(json.dumps({"side": side, "entry": order.average_price,
                              "qty": order.filled_qty, "exit": close.average_price,
                              "stop_and_target_verified": True}), flush=True)
        finally:
            # Discover only this run's conditional orders, including a lost acknowledgement.
            async with httpx.AsyncClient(timeout=15) as client:
                ok, legs = await bt._signed(client, "GET", "/fapi/v1/openAlgoOrders")
            if ok:
                ids.extend("algo:" + str(r["algoId"]) for r in legs
                           if str(r.get("clientAlgoId", "")).startswith(prefix))
            cancel_failures = []
            for oid in set(ids):
                if not await bt.cancel_order(oid, symbol):
                    cancel_failures.append(oid)
            for attempt in range(3):
                position = await bt.open_position(symbol)
                if position == {}:
                    break
                assert position is not None, "Cannot verify cleanup; inspect demo account"
                await bt.market_order(symbol=symbol,
                                      side="sell" if position["side"] == "buy" else "buy",
                                      qty=position["qty"], reduce_only=True,
                                      client_order_id=f"{prefix}_cleanup_{side}_{attempt}")
            assert await bt.open_position(symbol) == {}, "Demo position remains open"
            assert not cancel_failures, f"Could not confirm protection cancellation: {cancel_failures}"
    async with httpx.AsyncClient(timeout=15) as client:
        for path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            ok, rows = await bt._signed(client, "GET", path)
            assert ok and rows == [], "Orders remain after cleanup"
    print(json.dumps({"result": "PASS", "flat": True, "open_orders": 0,
                      "balance": await bt.free_usdt()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-demo-orders", action="store_true")
    asyncio.run(main(parser.parse_args().execute_demo_orders))
