"""The live round trip: entry -> resting stop -> tighten -> close, on Bybit testnet.

WHY THIS SCRIPT EXISTS RATHER THAN A TEST
=========================================
Everything in `tests/test_venue.py` is offline, and that is the honest limit of a
unit test here: it can pin the parameter matrix, but it cannot tell you whether the
venue ACCEPTS what the matrix produces. The gap is not academic — three separate
faults in the real-money path were invisible to the offline suite and are only
findable by talking to an exchange:

  1. `check_size` looked its symbol up with a plain dict lookup, so "SOL/USDT" hit
     the SPOT market and applied spot filters to a perpetual order (Bybit spot
     minAmount 0.001 vs the swap's 0.1).
  2. ccxt's bybit `market('SOL/USDT')` returns the SPOT market despite
     `defaultType: swap` — so the same call that placed a perpetual order on
     Binance placed a spot order on Bybit.
  3. `place_stop_loss` sent `triggerPrice` without `triggerDirection`, so ccxt
     raised `ArgumentsRequired` before any request left the process. Every Bybit
     stop was logged as "REJECTED by the venue" without the venue ever seeing it.

Bybit's testnet is the only place this chain can be exercised for free. Binance
dropped ccxt futures-testnet support, so its order path cannot be verified without
real funds — which is stated plainly in `venue.py` rather than papered over.

WHY IT IS NOT A pytest FILE
===========================
`tests/conftest.py` blocks the network for every test, deliberately. This script
places real (testnet) orders and must be run on purpose, never as a side effect of
`pytest -q`.

RUN IT
======
    1. Create API keys at https://testnet.bybit.com (Account -> API), with
       "Contract - Orders / Positions" write permission.
    2. Fund the testnet wallet from that site's faucet (Assets -> Deposit).
    3. Put them in `.env` as BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_SECRET —
       NOT over your mainnet BYBIT_API_KEY. The venue layer reads the testnet
       pair when USE_TESTNET=true and never lets a mainnet client read them.
    4. .venv/Scripts/python.exe scripts/bybit_testnet_roundtrip.py

    Optional: --symbol SOL/USDT  --leverage 3  --keep-open (skips the close, for
    inspecting the position in the testnet UI; the cleanup still cancels the stop)

SAFETY
======
It REFUSES to run against mainnet. Not a warning — a hard exit. Every order below
is a market order, and on mainnet this file would spend real money to prove a
point that testnet proves for free.

Cleanup runs in a `finally`, because a script that dies at step 9 must not leave a
position open with a stop resting behind it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The venue layer reads its configuration from the environment, and the operator's
# keys live in `.env`. Loaded before anything imports `venue`.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

# Pinned BEFORE the import so `Venue()` cannot pick up a mainnet configuration
# from a stale `EXCHANGE_ID`. The guard below re-asserts both from the built
# client rather than trusting these two lines.
os.environ["EXCHANGE_ID"] = "bybit"
os.environ["USE_TESTNET"] = "true"

from backend.services.venue import Venue, key_variable  # noqa: E402

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_PASS, _FAIL = 0, 0
_LOG: list[str] = []


def step(name: str) -> None:
    print(f"\n\033[1m--- {name}\033[0m")


def ok(what: str, detail: str = "") -> None:
    global _PASS
    _PASS += 1
    print(f"  \033[32mPASS\033[0m {what}" + (f"  {detail}" if detail else ""))


def fail(what: str, detail: str = "") -> None:
    global _FAIL
    _FAIL += 1
    line = f"  \033[31mFAIL\033[0m {what}" + (f"  {detail}" if detail else "")
    print(line)
    _LOG.append(f"{what} — {detail}")


def info(text: str) -> None:
    print(f"       {text}")


class Abort(RuntimeError):
    """A failure that makes every later step meaningless (no keys, no funds)."""


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


async def run(symbol: str, leverage: int, keep_open: bool) -> None:
    venue = Venue("bybit", testnet=True)

    # State the cleanup needs even if we abort halfway.
    stop_order_id: Optional[str] = None
    filled_qty: Optional[float] = None
    entry_side = "buy"
    exit_side = "sell"

    try:
        # -- 1. the guard ------------------------------------------------
        step("1. Configuration guard")
        if venue.id != "bybit":
            raise Abort(f"venue is {venue.id}, not bybit")
        if not venue.testnet:
            raise Abort(
                "this client is pointed at MAINNET. Every order below is a market "
                "order; refusing to spend real money to test a code path."
            )
        ok("bybit, sandbox mode ON")

        # CREDENTIALS ARE CHECKED LATER, at step 5, deliberately. Steps 2-4 are
        # keyless and they carry the symbol-resolution proof — the fault that
        # placed spot orders on this venue. A run with no keys should still verify
        # that against live metadata rather than abort before reaching it.
        variable = key_variable("bybit", testnet=True)
        if venue.has_credentials():
            ok(f"credentials present via {variable}")
        else:
            info(f"no credentials yet ({variable}) — steps 2-4 still run, 5-14 cannot")

        # -- 2. symbol resolution (KEYLESS) ------------------------------
        step("2. Symbol resolves to the linear perpetual, not spot")
        markets = await venue.markets()
        resolved = await venue.resolve_symbol(symbol)
        if resolved is None:
            raise Abort(f"{symbol} has no linear perpetual market on bybit testnet")
        mkt = markets[resolved]
        if not (mkt.get("swap") and mkt.get("linear")):
            fail(
                f"{symbol} resolved to {resolved}, which is not a linear swap",
                f"type={mkt.get('type')}",
            )
        else:
            ok(f"{symbol} -> {resolved}", f"type={mkt.get('type')} linear=True")

        # THE BUG THIS STEP EXISTS FOR: the spot market under the bare key has
        # different filters, and using them is a rejected order.
        spot = markets.get(symbol) if symbol != resolved else None
        limits = mkt.get("limits") or {}
        min_qty = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        info(f"perp filters: minQty={min_qty} minNotional={min_cost}")
        if spot is not None:
            spot_min = ((spot.get("limits") or {}).get("amount") or {}).get("min")
            info(f"spot  filters: minQty={spot_min}  <- what the old code used")
            if spot_min != min_qty:
                ok("the two markets genuinely differ", "so resolving is load-bearing")

        # -- 3. price (KEYLESS) ------------------------------------------
        step("3. Public price, no key spent")
        ticker = await venue.public.fetch_ticker(resolved)
        price = float(ticker.get("last") or ticker.get("close") or 0)
        if price <= 0:
            raise Abort(f"no usable price for {resolved}: {ticker.get('last')!r}")
        ok(f"{resolved} = {price:,.4f}")

        # -- 4. sizing against the real filters --------------------------
        step("4. Sizing clears the venue's own filters")
        qty = float(min_qty or 0.001)
        # Nudge up to the minimum notional if the minimum quantity does not clear
        # it. Rounded UP here on purpose — this is the SCRIPT choosing a test
        # size, not the sizer moving a size a risk gate approved.
        if min_cost and qty * price < float(min_cost):
            qty = float(min_cost) * 1.05 / price
        check = await venue.check_size(symbol, qty, price)
        if not check.ok:
            raise Abort(f"even the venue minimum will not pass check_size: {check.reason}")
        qty = check.qty
        ok(f"size {qty:g}", f"notional ~{qty * price:,.2f} USDT")

        # The refusal, against real filters rather than a fixture.
        too_small = await venue.check_size(symbol, qty / 1000.0, price)
        if too_small.ok:
            fail(
                "a 1000x-too-small size was ACCEPTED",
                "check_size is not applying this venue's minimums",
            )
        else:
            ok("a below-minimum size is refused, not rounded up", f"{too_small.reason}")

        # -- 5. balance (PRIVATE) ----------------------------------------
        step("5. Balance reads through the private client")
        if not venue.has_credentials():
            raise Abort(
                f"no testnet credentials, so nothing below this line can run. Create a "
                f"key at https://testnet.bybit.com (Account -> API) with Contract "
                f"read+write, fund the wallet from that site's faucet, and set "
                f"{variable} / BYBIT_TESTNET_SECRET in .env. They are separate "
                f"credentials from your mainnet keys — leave BYBIT_API_KEY alone."
            )
        free = await venue.free_usdt()
        if free is None:
            raise Abort(
                "could not read the testnet balance. The usual causes are a key "
                "without Contract permission, or a MAINNET key in the testnet "
                "variable (they are not interchangeable)."
            )
        ok(f"free USDT {free:,.2f}")
        required = qty * price / max(leverage, 1)
        if free < required:
            raise Abort(
                f"{free:,.2f} USDT free, but this position needs ~{required:,.2f} of "
                f"margin at {leverage}x. Use the testnet faucet (Assets -> Deposit)."
            )
        ok(f"enough for ~{required:,.2f} of margin at {leverage}x")

        # -- 6. position mode --------------------------------------------
        step("6. Position mode")
        hedge = await venue.hedge_mode()
        ok(f"{'HEDGE' if hedge else 'ONE-WAY'} mode", "decides positionIdx and reduceOnly")
        info(f"entry params:  {venue._order_params(side='buy', reduce_only=False, hedge=hedge, client_order_id='x')}")
        info(f"close params:  {venue._order_params(side='sell', reduce_only=True, hedge=hedge, client_order_id='x')}")

        # -- 7. leverage --------------------------------------------------
        step("7. Leverage is set ON THE VENUE before the entry")
        if not await venue.ensure_leverage(symbol, leverage):
            raise Abort(
                f"bybit would not accept {leverage}x on {symbol}. The agent ABORTS the "
                f"trade here rather than filling at a leverage it did not size for."
            )
        ok(f"{leverage}x accepted")

        # -- 8. the entry -------------------------------------------------
        step("8. ENTRY — market order")
        tag = f"rt{int(time.time())}"
        entry = await venue.market_order(
            symbol=symbol, side=entry_side, qty=qty,
            reduce_only=False, client_order_id=f"{tag}e", expected_price=price,
        )
        if not entry.ok:
            raise Abort(f"entry rejected: {entry.error}")
        if entry.average_price is None:
            fail(
                "the fill carries no price",
                "the caller refuses to record a trade without one, so this would "
                "open a position that never reaches the book",
            )
        filled_qty = entry.filled_qty or qty
        ok(
            f"filled {filled_qty:g} @ {entry.average_price}",
            f"order {entry.order_id}",
        )

        # -- 9. the venue agrees, in the SAME symbol form -----------------
        step("9. The venue reports the position, and reconciliation can match it")
        await asyncio.sleep(1.5)  # the position endpoint lags the fill slightly
        positions = await venue.open_positions()
        if positions is None:
            fail("could not read positions", "None means 'could not ask', not 'flat'")
        else:
            mine = [p for p in positions if p["symbol"] == symbol]
            if not mine:
                fail(
                    f"the venue reports no {symbol} position",
                    f"it returned {[p['symbol'] for p in positions]} — if the symbol "
                    f"differs only by a ':USDT' suffix, reconciliation would call "
                    f"every real position a phantom",
                )
            else:
                p = mine[0]
                ok(
                    f"{p['symbol']} {p['side']} {p['qty']:g} @ {p['entryPrice']}",
                    f"venueSymbol={p.get('venueSymbol')} lev={p.get('leverage')}",
                )
                if abs(p["qty"] - filled_qty) / max(filled_qty, 1e-9) > 0.02:
                    fail(
                        "size disagrees with the fill",
                        f"venue {p['qty']:g} vs filled {filled_qty:g}",
                    )
                else:
                    ok("size matches the fill within tolerance")

        # -- 10. the resting stop -----------------------------------------
        step("10. RESTING STOP at the venue, on the exit side")
        stop_price = float(venue.public.price_to_precision(resolved, price * 0.90))
        stop = await venue.place_stop_loss(
            symbol=symbol, side=exit_side, qty=filled_qty,
            stop_price=stop_price, client_order_id=f"{tag}s",
        )
        if not stop.ok:
            fail(
                "the stop was refused",
                f"{stop.error}. The position is open and UNPROTECTED at the venue.",
            )
        else:
            stop_order_id = stop.order_id
            ok(f"stop resting at {stop_price:g}", f"order {stop_order_id}")

            resting = await venue.resting_stops(symbol)
            if resting is None:
                fail("could not list resting stops", "cannot verify it is really there")
            elif len(resting) != 1:
                fail(
                    f"{len(resting)} stops are resting, expected exactly 1",
                    f"{resting}",
                )
            else:
                r = resting[0]
                ok(f"the venue confirms 1 resting stop", f"{r['side']} @ {r['triggerPrice']:g}")
                # A stop on the ENTRY side would ADD to the position at the stop
                # rather than close it.
                if r["side"] != exit_side:
                    fail(
                        f"the stop is on the {r['side']} side",
                        f"a {entry_side} position must be stopped by a {exit_side}; "
                        f"on the entry side it doubles the position at the worst price",
                    )
                else:
                    ok(f"it is on the exit side ({exit_side})")

        # -- 11. tightening: cancel THEN place, never both at once ---------
        if stop_order_id:
            step("11. TIGHTENING — cancel then place, never two live stops")
            tighter = float(venue.public.price_to_precision(resolved, price * 0.95))

            cancelled = await venue.cancel_order(stop_order_id, symbol)
            if not cancelled:
                fail("the old stop would not cancel", "refusing to place a second one")
            else:
                ok("old stop cancelled")
                mid = await venue.resting_stops(symbol)
                if mid is None:
                    fail("could not confirm the gap", "")
                elif mid:
                    fail(f"{len(mid)} stops still resting after the cancel", f"{mid}")
                else:
                    ok("zero stops resting", "a brief gap is recoverable; two stops are not")

                stop_order_id = None
                replaced = await venue.place_stop_loss(
                    symbol=symbol, side=exit_side, qty=filled_qty,
                    stop_price=tighter, client_order_id=f"{tag}t",
                )
                if not replaced.ok:
                    fail("the tighter stop was refused", f"{replaced.error}")
                else:
                    stop_order_id = replaced.order_id
                    after = await venue.resting_stops(symbol)
                    if after is None:
                        fail("could not list stops after the replace", "")
                    elif len(after) != 1:
                        fail(
                            f"{len(after)} stops resting after tightening, expected 1",
                            "two reduce-only stops means the second one OPENS a "
                            "reversed position after the first flattens",
                        )
                    else:
                        ok(
                            f"exactly 1 stop, now at {after[0]['triggerPrice']:g}",
                            f"was {stop_price:g}",
                        )

        # -- 12/13/14. the close ------------------------------------------
        if keep_open:
            step("12. Close SKIPPED (--keep-open)")
            info("the cleanup below still cancels the stop; the position stays open")
        else:
            step("12. CLOSE — reduce-only, so a stale size cannot reverse the position")
            close = await venue.market_order(
                symbol=symbol, side=exit_side, qty=filled_qty,
                reduce_only=True, client_order_id=f"{tag}c", expected_price=price,
            )
            if not close.ok:
                fail("the close was REJECTED", f"{close.error}. The position is still open.")
            else:
                ok(f"closed {close.filled_qty} @ {close.average_price}", f"order {close.order_id}")
                filled_qty = None  # nothing left for the cleanup to flatten

                step("13. The venue agrees the book is flat")
                await asyncio.sleep(1.5)
                after_positions = await venue.open_positions()
                if after_positions is None:
                    fail("could not confirm flat", "None is 'could not ask', not 'flat'")
                elif any(p["symbol"] == symbol for p in after_positions):
                    left = [p for p in after_positions if p["symbol"] == symbol][0]
                    fail(
                        f"{symbol} is still open ({left['qty']:g})",
                        "the reduce-only close did not flatten it",
                    )
                else:
                    ok("flat")

            step("14. The stop is cancelled after the close")
            if stop_order_id and await venue.cancel_order(stop_order_id, symbol):
                stop_order_id = None
                ok("stop cancelled")
            elif stop_order_id:
                fail(
                    "the stop would NOT cancel",
                    "a reduce-only stop left on a flat account is an order to OPEN "
                    "a reversed position the next time price touches it",
                )
            left_over = await venue.resting_stops(symbol)
            if left_over is None:
                fail("could not confirm no stops remain", "")
            elif left_over:
                fail(f"{len(left_over)} stops still resting on a flat account", f"{left_over}")
            else:
                ok("nothing resting")

    except Abort as exc:
        fail("ABORTED", str(exc))
    except Exception as exc:  # noqa: BLE001 - a harness reports, it does not raise
        fail(f"unexpected {type(exc).__name__}", str(exc))
    finally:
        # CLEANUP. A run that died at step 10 leaves a real testnet position with a
        # stop behind it; leaving that lying around makes the NEXT run's
        # "1 resting stop" assertion meaningless.
        step("Cleanup")
        try:
            if stop_order_id:
                info(f"cancelling leftover stop {stop_order_id}")
                await venue.cancel_order(stop_order_id, symbol)
            if filled_qty and not keep_open:
                info(f"flattening leftover position {filled_qty:g} {symbol}")
                await venue.market_order(
                    symbol=symbol, side=exit_side, qty=filled_qty, reduce_only=True,
                )
            if not stop_order_id and not filled_qty:
                info("nothing to clean up")
        except Exception as exc:  # noqa: BLE001
            print(f"  \033[31mcleanup failed: {exc}\033[0m")
            print("  Check https://testnet.bybit.com for an orphaned position or order.")
        await venue.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SOL/USDT", help="in the agent's own form, e.g. SOL/USDT")
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--keep-open", action="store_true", help="skip the close")
    args = ap.parse_args()

    print(f"Bybit TESTNET round trip — {args.symbol} at {args.leverage}x")
    asyncio.run(run(args.symbol, args.leverage, args.keep_open))

    print(f"\n{'=' * 68}")
    print(f"  {_PASS} passed, {_FAIL} failed")
    if _FAIL:
        for line in _LOG:
            print(f"    - {line}")
        print("\n  The real-money path is NOT verified.")
    else:
        print("\n  Entry, resting stop, tighten and reduce-only close all verified\n"
              "  against a live venue. Binance's order path remains unverified —\n"
              "  ccxt dropped its futures testnet, and mainnet costs real money.")
    print("=" * 68)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
