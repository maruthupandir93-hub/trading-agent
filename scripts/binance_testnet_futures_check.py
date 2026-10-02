"""BINANCE FUTURES TESTNET — the full exercise, on real orders.

Every futures capability the mirror depends on, measured. LONG and SHORT,
because a short opens on a sell and closes on a buy and this project has been
bitten by long-only assumptions more than once.
"""
import asyncio, os, sys, json, urllib.request
sys.path.insert(0, r"C:\Users\MARUTHUPANDI R\Downloads\Agent\trading-agent")
os.chdir(r"C:\Users\MARUTHUPANDI R\Downloads\Agent\trading-agent")
from dotenv import load_dotenv; load_dotenv(".env", override=True)
import logging; logging.basicConfig(level=logging.CRITICAL)
from backend.services import binance_testnet as bt

R = []
def ok(n, p, d=""):
    R.append((n, p)); print(f"  {'PASS' if p else 'FAIL':<5} {n:<50} {d}")
def rule(t): print(f"\n{'='*80}\n{t}\n{'='*80}")

def px(sym="XRPUSDT"):
    return float(json.load(urllib.request.urlopen(
        bt.BASE_URL + f"/fapi/v1/ticker/price?symbol={sym}", timeout=20))["price"])

async def round_trip(side, label):
    """One full futures position, opened and closed, in the given direction."""
    p = px(); qty = round(130.0 / p, 1)
    exit_side = "sell" if side == "buy" else "buy"
    print(f"\n  {label}: {side.upper()} {qty} XRP @ ~{p}")

    o = await bt.market_order(symbol="XRP/USDT", side=side, qty=qty,
                              client_order_id=f"fut_{label}_open")
    ok(f"{label}: entry accepted", o.ok, (o.error or f"order {o.order_id}")[:70])
    if not o.ok:
        return
    ok(f"{label}: real fill price", bool(o.average_price and o.average_price > 0),
       str(o.average_price))
    ok(f"{label}: real filled qty", bool(o.filled_qty and o.filled_qty > 0),
       f"{o.filled_qty} of {qty}")

    pos = await bt.open_position("XRP/USDT")
    ok(f"{label}: position exists at the venue", bool(pos),
       f"{pos.get('side')} {pos.get('qty')} @ {pos.get('entryPrice')}" if pos else "")
    ok(f"{label}: the venue agrees on DIRECTION", bool(pos) and pos.get("side") == side,
       f"venue says {pos.get('side') if pos else '?'}, we sent {side}")

    c = await bt.market_order(symbol="XRP/USDT", side=exit_side, qty=o.filled_qty,
                              reduce_only=True, client_order_id=f"fut_{label}_close")
    ok(f"{label}: reduce-only close accepted", c.ok, (c.error or f"@ {c.average_price}")[:70])
    flat = await bt.open_position("XRP/USDT")
    ok(f"{label}: flat afterwards", flat == {}, str(flat)[:50])

async def main():
    rule("1  CONNECTIVITY, ACCOUNT, CLOCK")
    v = await bt.verify()
    ok("authenticated against the futures testnet", v["ok"], v["reason"][:88])
    ok("clock offset measured and applied", "clockOffsetMs" in v,
       f"{v.get('clockOffsetMs')} ms")
    bal = await bt.free_usdt()
    ok("balance readable", bal is not None, f"{bal:,.2f} USDT")

    rule("2  INSTRUMENT RULES (what the venue will refuse)")
    import httpx
    async with httpx.AsyncClient(timeout=20.0) as cl:
        rules = await bt._instrument_rules(cl, "XRP/USDT")
    ok("lot step known", bool(rules.get("step")), f"step {rules.get('step')}")
    ok("min qty known", bool(rules.get("minQty")), f"minQty {rules.get('minQty')}")
    ok("price tick known", bool(rules.get("tick")), f"tick {rules.get('tick')}")
    ok("a sub-minimum size is refused, not rounded up",
       bt._quantise(0.0001, rules) is None)
    ok("a size is truncated DOWN to the step", bt._quantise(79.69, rules) == "79.6",
       bt._quantise(79.69, rules))

    rule("3  LEVERAGE")
    ok("leverage settable on the venue", await bt.ensure_leverage("XRP/USDT", 5))

    rule("4  A LONG, END TO END")
    await round_trip("buy", "LONG")

    rule("5  A SHORT, END TO END  (opens on a sell, closes on a buy)")
    await round_trip("sell", "SHORT")

    rule("6  WHAT THIS VENUE CANNOT DO — measured, not assumed")
    p = px()
    sl = await bt.place_stop_loss(symbol="XRP/USDT", side="sell", qty=80.0,
                                  stop_price=p * 0.97)
    ok("conditional orders are REFUSED (known -4120 limitation)",
       (not sl.ok) and "-4120" in (sl.error or ""), (sl.error or "")[:70])
    ok("the facade declares it cannot rest orders",
       bt.BinanceTestnetVenue.supports_resting_orders is False)

    end = await bt.free_usdt()
    rule("SUMMARY")
    print(f"  balance {bal:,.4f} -> {end:,.4f}   ({end-bal:+.4f} in real fees over 2 round trips)")
    print(f"  {sum(1 for _, p in R if p)}/{len(R)} checks passed")
    for n, p in R:
        if not p: print(f"    FAILED: {n}")

asyncio.run(main())
