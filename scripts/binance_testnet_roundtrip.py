"""A REAL round trip on Binance futures testnet: open -> verify -> close -> verify.

Places actual orders with play money. Cleans up in a finally, because a run that
dies mid-way must not leave a position open on the account.
"""
import asyncio, os, sys
sys.path.insert(0, r"C:\Users\MARUTHUPANDI R\Downloads\Agent\trading-agent")
os.chdir(r"C:\Users\MARUTHUPANDI R\Downloads\Agent\trading-agent")
from dotenv import load_dotenv; load_dotenv(".env", override=True)
import logging; logging.basicConfig(level=logging.INFO, format="      %(levelname)s %(message)s")

from backend.services import binance_testnet as bt

SYM = "XRP/USDT"
R = []
def ok(n, p, d=""):
    R.append((n, p)); print(f"  {'PASS' if p else 'FAIL':<5} {n:<46} {d}")

async def main():
    print("BINANCE FUTURES TESTNET — REAL ORDERS, PLAY MONEY")
    print("=" * 76)
    print("  base url (hardcoded):", bt.BASE_URL)
    ok("credentials present", bt.credentials_present())

    v = await bt.verify()
    ok("authenticated /fapi/v2/account", v["ok"], v["reason"][:90])
    if not v["ok"]:
        return
    start_bal = v["balanceUsdt"]

    flat = await bt.open_position(SYM)
    ok("account starts flat in " + SYM, flat == {}, str(flat)[:60])

    # Size from the live price so the notional clears Binance's minimum.
    import urllib.request, json
    px = float(json.load(urllib.request.urlopen(
        bt.BASE_URL + "/fapi/v1/ticker/price?symbol=XRPUSDT", timeout=20))["price"])
    qty = round(120.0 / px, 1)          # ~$120 notional
    print(f"\n  XRP last {px}  -> opening {qty} (~${qty*px:,.2f} notional)\n")

    await bt.ensure_leverage(SYM, 3)

    opened = None
    try:
        o = await bt.market_order(symbol=SYM, side="buy", qty=qty, client_order_id="lifecycle_open")
        ok("market BUY accepted", o.ok, (o.error or "")[:80])
        if not o.ok:
            return
        opened = o
        ok("the venue returned a fill PRICE", bool(o.average_price and o.average_price > 0),
           f"avg {o.average_price}")
        ok("the venue returned a filled QTY", bool(o.filled_qty and o.filled_qty > 0),
           f"filled {o.filled_qty} of {qty} requested")
        ok("order id recorded", bool(o.order_id), str(o.order_id))

        pos = await bt.open_position(SYM)
        ok("the position EXISTS at the venue", bool(pos),
           f"{pos.get('side')} {pos.get('qty')} @ {pos.get('entryPrice')}" if pos else "none")

        c = await bt.market_order(symbol=SYM, side="sell", qty=o.filled_qty,
                                  reduce_only=True, client_order_id="lifecycle_close")
        ok("reduce-only SELL accepted", c.ok, (c.error or "")[:80])
        ok("the close returned a fill price", bool(c.average_price and c.average_price > 0),
           f"avg {c.average_price}")
        opened = None

        after = await bt.open_position(SYM)
        ok("the account is FLAT again", after == {}, str(after)[:60])

        end = await bt.free_usdt()
        ok("balance readable after the round trip", end is not None,
           f"{start_bal:,.2f} -> {end:,.2f}  ({end - start_bal:+.4f} after fees)")

        # The refusal path: a size below the venue minimum must be REFUSED,
        # never rounded up to clear it.
        tiny = await bt.market_order(symbol=SYM, side="buy", qty=0.0001)
        ok("a sub-minimum size is REFUSED, not rounded up", not tiny.ok, (tiny.error or "")[:80])
    finally:
        if opened is not None:
            print("\n  cleaning up an order left open by a failure ...")
            await bt.market_order(symbol=SYM, side="sell", qty=opened.filled_qty or qty,
                                  reduce_only=True, client_order_id="lifecycle_cleanup")

    print("\n" + "=" * 76)
    print(f"  {sum(1 for _, p in R if p)}/{len(R)} checks passed")

asyncio.run(main())
