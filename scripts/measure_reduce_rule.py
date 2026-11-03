"""Does cutting HALF a position at -0.5R beat holding it to the stop?

WHY THIS EXISTS (2026-11-03)
============================
The monitoring graph's REDUCE closes half a position once it is 0.5R against
AND a "condition" dimension reports concern >= 0.5. `portfolio_risk` measured
exposure in NOTIONAL against a 1x limit, so at 10x its concern was pinned at
0.8 and the REDUCE fired on EVERY position reaching -0.5R. That is a bug — the
gate claims independent evidence it was not using — but replaying the trades it
cut found the accidental behaviour was GOOD:

    6 XRP shorts (Oct 30)  thesis exits -2.37R   vs held to stop -2.90R
    DOGE short  (Nov 3)    cut -0.60R            vs held to stop -1.00R
    LTC  short  (Nov 3)    cut -0.79R            vs held to stop -1.00R

Seven trades cannot decide a rule, so this measures it: every strategy's REAL
signal, the live entry-extension gate applied (it is on by default), each
outcome walked forward under the live 2.5/5.0 ATR risk model, NON-OVERLAPPING
(a strategy cannot re-enter while its last trade is open) over a long window.

    HOLD    stop -1R, target +2R                       (the risk model alone)
    CUT     at -0.5R close half (-0.25R banked), the rest to stop/target

AMBIGUOUS BARS ARE RESOLVED AGAINST THE RULE BEING TESTED: a bar that reaches
-0.5R and the target is read as "cut first, then target" (0.75R, not 2R); a bar
reaching -0.5R and the stop is "cut, then stop" (-0.75R). The cut pays one
extra taker fee on half the position; `--fee-r` charges it in R.

No database, no key, no orders. Public Binance klines only.
"""

from __future__ import annotations

import math
import os
import sys
from typing import List

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@192.0.2.1:5432/none")

import measure_entry_extension as mee  # noqa: E402  (klines, atr, describe)

CUT_AT_R = 0.5
CUT_FRACTION = 0.5


def walk(bars, start, direction, entry, r, fee_r):
    """(hold_R, cut_R, resolved_index) or None if unresolved in MAX_FORWARD."""
    sign = 1.0 if direction == "LONG" else -1.0
    stop = entry - sign * r
    target = entry + sign * 2.0 * r
    cut_level = entry - sign * CUT_AT_R * r
    cut = False
    for j in range(start + 1, min(start + mee.MAX_FORWARD, len(bars))):
        hi, lo = bars[j]["high"], bars[j]["low"]
        adverse_hit = (lo <= cut_level) if sign > 0 else (hi >= cut_level)
        stop_hit = (lo <= stop) if sign > 0 else (hi >= stop)
        target_hit = (hi >= target) if sign > 0 else (lo <= target)
        if adverse_hit and not cut:
            cut = True
        if stop_hit:
            hold = -1.0
            res = -1.0
            break
        if target_hit:
            hold = 2.0
            res = 2.0
            break
    else:
        return None
    if cut:
        cut_r = CUT_FRACTION * -CUT_AT_R + (1 - CUT_FRACTION) * res - fee_r
    else:
        cut_r = hold
    return hold, cut_r, j


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER

    args = sys.argv[1:]
    mee.CANDLES = int(args[args.index("--candles") + 1]) if "--candles" in args else 6000
    # One extra taker fee (~0.05%) on half the position, as a fraction of a
    # 2.5-ATR stop (~1-2% on these coins): ~0.01-0.02R. 0.02R is the
    # conservative end, charged against the rule being tested.
    fee_r = float(args[args.index("--fee-r") + 1]) if "--fee-r" in args else 0.02
    print(f"candles per symbol {mee.CANDLES}, non-overlapping, extension gate ON, "
          f"cut fee {fee_r}R")

    hold: List[float] = []
    cutr: List[float] = []
    per = {}
    for symbol in mee.SYMBOLS:
        try:
            bars = mee.klines(symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: {exc}")
            continue
        for name, fn in STRATEGY_FUNCTIONS.items():
            i = mee.WINDOW
            while i < len(bars) - 1:
                try:
                    sig = (fn(bars[max(0, i - mee.WINDOW):i + 1]) or "").upper()
                except Exception:  # noqa: BLE001
                    i += 1
                    continue
                if sig not in ("BUY", "SELL", "LONG", "SHORT"):
                    i += 1
                    continue
                direction = "LONG" if sig in ("BUY", "LONG") else "SHORT"
                a = mee.atr(bars[:i + 1])
                if not a:
                    i += 1
                    continue
                entry = bars[i]["close"]
                r = ATR_STOP_MULTIPLIER * a
                stop = entry - r if direction == "LONG" else entry + r
                if assess(direction, bars[:i + 1], entry_price=entry,
                          stop_loss=stop, atr=a).chasing:
                    i += 1
                    continue
                out = walk(bars, i, direction, entry, r, fee_r)
                if out is None:
                    i += 1
                    continue
                h, c, j = out
                hold.append(h)
                cutr.append(c)
                per.setdefault(name, ([], []))
                per[name][0].append(h)
                per[name][1].append(c)
                i = j + 1
        print(f"  {symbol:<9} trades so far {len(hold)}")

    print()
    print(mee.describe("HOLD to stop/target", hold))
    print(mee.describe("CUT half at -0.5R", cutr))
    diffs = [c - h for h, c in zip(hold, cutr)]
    n = len(diffs)
    if n > 1:
        md = sum(diffs) / n
        sd = math.sqrt(sum((d - md) ** 2 for d in diffs) / (n - 1))
        z = md / (sd / math.sqrt(n)) if sd > 0 else float("nan")
        print(f"\nPAIRED difference (CUT - HOLD) per trade: {md:+.4f}R  z = {z:+.2f}  n = {n}")
        changed = sum(1 for d in diffs if abs(d) > 1e-12)
        print(f"trades the rule actually touched: {changed} ({changed / n * 100:.0f}%)")
    print("\nPER STRATEGY (hold -> cut, expectancy R)")
    for name in sorted(per):
        h, c = per[name]
        print(f"  {name:<20} n={len(h):5d}  {sum(h) / len(h):+.4f} -> {sum(c) / len(c):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
