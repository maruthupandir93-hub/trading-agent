"""Range strategies: a nearer target, and a range-edge location rule. Measured.

WHY (2026-11-04 review, I-2 and I-3): five consecutive losing trades used
Range/Grid-type logic; every one reached +0.73R to +0.90R and then reversed to
the stop, because the 2:1 target (5 ATR) suits trends, not ranges. And the
entries were mid-range (RSI 45-57), not at the edge a range trade sells from.

Both proposed fixes are measured here, on the range family only
(Range, Grid, MeanReversion), non-overlapping, with the live entry-extension and
relative-strength-chase gates applied, NET OF FEES:

    target 2.0R   the live 5-ATR target  (baseline)
    target 1.0R / 0.75R                  (I-2: take profit nearer)
    + location    shorts only in the upper LOC of the 40-bar high-low range,
                  longs only in the lower LOC                     (I-3)

Fees matter more for a nearer target: a round trip costs the same dollars but a
smaller winner, so every outcome is charged `2 x taker / stop_fraction` in R.
An ambiguous bar (both stop and target inside it) resolves AS THE STOP.

No database, no key, no orders.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@192.0.2.1:5432/none")

import measure_entry_extension as mee  # noqa: E402

FAMILY = ("Range", "Grid", "MeanReversion")
TARGETS = (2.0, 1.0, 0.75)
LOC_BARS = 40
LOC = 0.25
TAKER = 0.0005


def walk(bars, i, d, entry, r, tgt_r):
    sign = 1 if d == "LONG" else -1
    stop, target = entry - sign * r, entry + sign * tgt_r * r
    for j in range(i + 1, min(i + mee.MAX_FORWARD, len(bars))):
        hi, lo = bars[j]["high"], bars[j]["low"]
        if (lo <= stop) if sign > 0 else (hi >= stop):
            return -1.0, j
        if (hi >= target) if sign > 0 else (lo <= target):
            return tgt_r, j
    return None


def stats(rs: List[float]) -> str:
    n = len(rs)
    if n < 2:
        return f"n={n}"
    m = sum(rs) / n
    se = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1)) / math.sqrt(n)
    return f"n={n:5d}  {m:+.4f}R +/-{1.96 * se:.4f}  win {sum(x > 0 for x in rs) / n * 100:4.1f}%"


def paired_z(a, b):
    d = [x - y for x, y in zip(a, b)]
    n = len(d)
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1))
    return m, (m / (sd / math.sqrt(n)) if sd else float("nan"))


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess as ext_assess
    from backend.algorithms.market_context import chases_relative_strength, percent_change
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER

    args = sys.argv[1:]
    mee.CANDLES = int(args[args.index("--candles") + 1]) if "--candles" in args else 6000
    print(f"candles {mee.CANDLES}, range family {FAMILY}, non-overlapping on the "
          f"BASELINE trade, net of {TAKER * 200:.2f}% round-trip fees")

    btc = mee.klines("BTCUSDT")
    btc_at = {b["openTime"]: i for i, b in enumerate(btc)}

    class _Ctx:
        benchmark_symbol = "BTC/USDT"

    # Each signal is evaluated under EVERY variant (paired), so differences are
    # the rule's effect on the same trades, not on different samples.
    rows = []   # (strategy, in_location, {tgt: R_net})
    for symbol in mee.SYMBOLS:
        bars = mee.klines(symbol)
        for name in FAMILY:
            fn = STRATEGY_FUNCTIONS[name]
            i = max(mee.WINDOW, LOC_BARS)
            while i < len(bars) - 1:
                try:
                    sig = (fn(bars[max(0, i - mee.WINDOW):i + 1]) or "").upper()
                except Exception:  # noqa: BLE001
                    i += 1
                    continue
                if sig not in ("BUY", "SELL", "LONG", "SHORT"):
                    i += 1
                    continue
                d = "LONG" if sig in ("BUY", "LONG") else "SHORT"
                a = mee.atr(bars[:i + 1])
                if not a:
                    i += 1
                    continue
                entry = bars[i]["close"]
                r = ATR_STOP_MULTIPLIER * a
                stop = entry - r if d == "LONG" else entry + r
                prefix = bars[:i + 1]
                if ext_assess(d, prefix, entry_price=entry, stop_loss=stop, atr=a).chasing:
                    i += 1
                    continue
                bi = btc_at.get(bars[i]["openTime"])
                if bi is not None:
                    ctx = _Ctx()
                    c1, c2 = percent_change(prefix), percent_change(btc[:bi + 1])
                    ctx.relative_strength_pct = None if c1 is None or c2 is None else c1 - c2
                    if chases_relative_strength(d, ctx) is not None:
                        i += 1
                        continue
                outs = {t: walk(bars, i, d, entry, r, t) for t in TARGETS}
                if any(v is None for v in outs.values()):
                    i += 1
                    continue
                fee_r = 2 * TAKER / (r / entry)
                window = bars[i - LOC_BARS + 1:i + 1]
                hi, lo = max(b["high"] for b in window), min(b["low"] for b in window)
                pos = (entry - lo) / (hi - lo) if hi > lo else 0.5
                in_loc = pos >= 1 - LOC if d == "SHORT" else pos <= LOC
                rows.append((name, in_loc, {t: outs[t][0] - fee_r for t in TARGETS}))
                i = outs[2.0][1] + 1      # non-overlapping on the baseline trade
        print(f"  {symbol:<9} trades so far {len(rows)}")

    def col(sel, t):
        return [row[2][t] for row in rows if sel(row)]

    print("\nALL RANGE-FAMILY TRADES")
    for t in TARGETS:
        print(f"  target {t:4.2f}R          {stats(col(lambda x: True, t))}")
    for t in TARGETS[1:]:
        m, zz = paired_z(col(lambda x: True, t), col(lambda x: True, 2.0))
        print(f"  {t:4.2f}R vs 2.0R paired: {m:+.4f}R per trade, z = {zz:+.2f}")

    print(f"\nLOCATION RULE (shorts in the top {LOC:.0%} / longs in the bottom {LOC:.0%} "
          f"of the {LOC_BARS}-bar range), baseline 2.0R target:")
    print(f"  at the edge   {stats(col(lambda x: x[1], 2.0))}")
    print(f"  mid-range     {stats(col(lambda x: not x[1], 2.0))}")
    for t in TARGETS[1:]:
        print(f"  edge + {t:4.2f}R  {stats(col(lambda x: x[1], t))}")

    print("\nPER STRATEGY (target 2.0 -> 1.0 -> 0.75, net R)")
    for name in FAMILY:
        parts = [f"{sum(v) / len(v):+.4f}" if (v := col(lambda x, n=name: x[0] == n, t)) else "n/a"
                 for t in TARGETS]
        n = len(col(lambda x, n=name: x[0] == n, 2.0))
        print(f"  {name:<14} n={n:5d}  " + " -> ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
