"""Does trading AGAINST a coin's relative strength vs BTC lose money?

WHY (2026-11-03 review): a DOGE short was opened while DOGE was OUTPERFORMING
BTC by +0.69%. `market_context.build` already computes that number on every
run and the Risk Gateway records it in the entry snapshot — and nothing gated
on it. The same shape this codebase keeps finding: computed, recorded, unused.

Before gating on it, measure it. Every strategy's REAL signal, the live
entry-extension gate applied (on by default), each trade walked forward under
the live 2.5/5.0 ATR model, NON-OVERLAPPING, over a long window. Relative
strength is the REAL `market_context.percent_change` of the coin minus BTC's
over the same bars, aligned by timestamp, using only bars up to the entry.

    AGAINST   a SHORT while the coin outperforms BTC by more than --threshold,
              or a LONG while it underperforms by more than it
    WITH      the opposite
    NEUTRAL   |rel. strength| within the threshold

No database, no key, no orders. Public Binance klines only.
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


def stats(rs: List[float]) -> str:
    n = len(rs)
    if n < 2:
        return f"n={n}"
    m = sum(rs) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
    se = sd / math.sqrt(n)
    win = sum(1 for r in rs if r > 0) / n * 100
    return f"n={n:5d}  {m:+.4f}R +/-{1.96 * se:.4f}  win {win:4.1f}%"


def z(a: List[float], b: List[float]) -> float:
    def ms(x):
        m = sum(x) / len(x)
        return m, (sum((r - m) ** 2 for r in x) / (len(x) - 1)) / len(x)
    ma, va = ms(a)
    mb, vb = ms(b)
    return (ma - mb) / math.sqrt(va + vb)


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess
    from backend.algorithms.market_context import percent_change
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER

    args = sys.argv[1:]
    mee.CANDLES = int(args[args.index("--candles") + 1]) if "--candles" in args else 6000
    thr = float(args[args.index("--threshold") + 1]) if "--threshold" in args else 0.5
    mee.NON_OVERLAPPING = True
    print(f"candles {mee.CANDLES}, non-overlapping, extension gate ON, threshold {thr}%")

    btc = mee.klines("BTCUSDT")
    btc_at = {b["openTime"]: i for i, b in enumerate(btc)}
    groups: Dict[str, List[float]] = {"AGAINST": [], "WITH": [], "NEUTRAL": []}
    by_dir: Dict[str, List[float]] = {}

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
                bi = btc_at.get(bars[i]["openTime"])
                if not a or bi is None:
                    i += 1
                    continue
                entry = bars[i]["close"]
                r = ATR_STOP_MULTIPLIER * a
                stop = entry - r if d == "LONG" else entry + r
                target = entry + 2 * r if d == "LONG" else entry - 2 * r
                if assess(d, bars[:i + 1], entry_price=entry, stop_loss=stop, atr=a).chasing:
                    i += 1
                    continue
                res = mee.resolve(bars, i, d, stop, target)
                if res is None:
                    i += 1
                    continue
                out, j = res
                coin = percent_change(bars[:i + 1])
                bench = percent_change(btc[:bi + 1])
                if coin is None or bench is None:
                    i = j + 1
                    continue
                rel = coin - bench
                signed = rel if d == "LONG" else -rel   # >0 = trading WITH strength
                g = "NEUTRAL" if abs(rel) <= thr else ("WITH" if signed > 0 else "AGAINST")
                groups[g].append(out)
                by_dir.setdefault(f"{d} {g}", []).append(out)
                i = j + 1
        print(f"  {symbol:<9} done")

    print()
    for g in ("WITH", "NEUTRAL", "AGAINST"):
        print(f"  {g:<8} {stats(groups[g])}")
    if len(groups["AGAINST"]) > 1 and len(groups["WITH"]) > 1:
        rest = groups["WITH"] + groups["NEUTRAL"]
        print(f"\n  AGAINST vs everything else: z = {z(groups['AGAINST'], rest):+.2f}"
              f"   (negative = trading against strength is WORSE)")
    print("\n  by direction:")
    for k in sorted(by_dir):
        print(f"    {k:<14} {stats(by_dir[k])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
