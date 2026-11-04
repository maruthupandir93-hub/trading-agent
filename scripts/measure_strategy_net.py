"""Every strategy, net of fees, with every live entry gate, on several windows.

WHY (2026-11-04): the review's proposed range fixes (a nearer target, a range-
edge location rule) both measured WORSE, and the range family (Range, Grid,
MeanReversion) came out net-negative in all three windows. Before acting on
that, every strategy is put on the same footing: real signal function, the
live entry-extension and relative-strength-chase gates, the 2.5/5.0 ATR model,
non-overlapping trades, `2 x taker / stop_fraction` fees in R.

The higher-timeframe gate is NOT applied here, deliberately: its own effect
flips sign between windows (scripts/measure_htf_consensus.py), so applying it
would mix two unstable questions into one.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@192.0.2.1:5432/none")

import measure_entry_extension as mee  # noqa: E402

TAKER = 0.0005


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess as ext_assess
    from backend.algorithms.market_context import chases_relative_strength, percent_change
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER

    args = sys.argv[1:]
    mee.CANDLES = int(args[args.index("--candles") + 1]) if "--candles" in args else 6000
    btc = mee.klines("BTCUSDT")
    btc_at = {b["openTime"]: i for i, b in enumerate(btc)}

    class _Ctx:
        benchmark_symbol = "BTC/USDT"

    per = {}
    for symbol in mee.SYMBOLS:
        bars = mee.klines(symbol)
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
                d = "LONG" if sig in ("BUY", "LONG") else "SHORT"
                a = mee.atr(bars[:i + 1])
                if not a:
                    i += 1
                    continue
                entry = bars[i]["close"]
                r = ATR_STOP_MULTIPLIER * a
                stop = entry - r if d == "LONG" else entry + r
                target = entry + 2 * r if d == "LONG" else entry - 2 * r
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
                res = mee.resolve(bars, i, d, stop, target)
                if res is None:
                    i += 1
                    continue
                out, j = res
                per.setdefault(name, []).append(out - 2 * TAKER / (r / entry))
                i = j + 1
    print(f"candles {mee.CANDLES}  (net of fees, extension + RS-chase gates)")
    for name in sorted(per, key=lambda k: -sum(per[k]) / len(per[k])):
        v = per[name]
        n = len(v)
        m = sum(v) / n
        se = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) / math.sqrt(n) if n > 1 else float("nan")
        print(f"  {name:<20} n={n:5d}  {m:+.4f}R +/-{1.96 * se:.4f}  total {sum(v):+8.1f}R")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
