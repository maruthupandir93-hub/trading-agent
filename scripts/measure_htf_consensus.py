"""Is "one timeframe trending + the other ranging" a trend? Measured.

WHY (2026-11-04 review, I-4): six consecutive trades were SHORTS on four coins
while the coins kept bouncing, and the 1h/4h label read "Bearish" for days.

`market_context._consensus` counts only Bullish/Bearish votes, and the structure
analyser's third answer is "Ranging". So 1h Bearish + 4h Ranging reports a
"Bearish" CONSENSUS, the HTF gate refuses every LONG as counter-trend, and in a
ranging market — where one timeframe is always leaning somewhere — the agent is
pushed onto one side. That is a mechanism for a short bias. Whether it COSTS
money is what this measures.

Each non-overlapping trade (every strategy's real signal, the live entry-
extension and relative-strength-chase gates applied, the 2.5/5.0 ATR model) is
classified by the two timeframes' ACTUAL readings, computed by the real
`market_context._trend_of` on 15m bars aggregated to 1h/4h up to the entry:

    BOTH_WITH      both timeframes agree with the trade
    WEAK_WITH      one agrees, the other Ranging
    MIXED          one with, one against
    WEAK_AGAINST   one AGAINST, the other Ranging   <- blocked TODAY, the question
    BOTH_AGAINST   both against                      <- blocked today, stays blocked
    BOTH_RANGING / UNKNOWN

If WEAK_AGAINST performs like the kept groups, today's consensus is refusing
good trades and manufacturing the one-sided bias. If it performs like
BOTH_AGAINST, the current rule is earning its refusals.
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

AGG = {"1h": 4, "4h": 16}


def aggregate(bars, factor):
    out = []
    n = len(bars)
    for i in range(n % factor, n, factor):
        c = bars[i:i + factor]
        if len(c) < factor:
            continue
        out.append({"open": c[0]["open"], "high": max(b["high"] for b in c),
                    "low": min(b["low"] for b in c), "close": c[-1]["close"],
                    "volume": sum(b["volume"] for b in c)})
    return out


def stats(rs: List[float]) -> str:
    n = len(rs)
    if n < 2:
        return f"n={n}"
    m = sum(rs) / n
    se = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1)) / math.sqrt(n)
    return f"n={n:5d}  {m:+.4f}R +/-{1.96 * se:.4f}  win {sum(r > 0 for r in rs) / n * 100:4.1f}%"


def z(a, b):
    def ms(x):
        m = sum(x) / len(x)
        return m, (sum((r - m) ** 2 for r in x) / (len(x) - 1)) / len(x)
    (ma, va), (mb, vb) = ms(a), ms(b)
    return (ma - mb) / math.sqrt(va + vb)


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess as ext_assess
    from backend.algorithms.market_context import _trend_of, chases_relative_strength, percent_change
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER

    args = sys.argv[1:]
    mee.CANDLES = int(args[args.index("--candles") + 1]) if "--candles" in args else 6000
    print(f"candles {mee.CANDLES}, non-overlapping, extension + RS-chase gates ON")

    btc = mee.klines("BTCUSDT")
    btc_at = {b["openTime"]: i for i, b in enumerate(btc)}
    groups: Dict[str, List[float]] = {}
    by_side: Dict[str, List[float]] = {}
    warm = 20 * 16

    class _Ctx:
        benchmark_symbol = "BTC/USDT"

    for symbol in mee.SYMBOLS:
        bars = mee.klines(symbol)
        for name, fn in STRATEGY_FUNCTIONS.items():
            i = max(mee.WINDOW, warm)
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
                want = "Bullish" if d == "LONG" else "Bearish"
                opp = "Bearish" if d == "LONG" else "Bullish"
                votes = [_trend_of(aggregate(prefix, f)) for f in AGG.values()]
                if any(v is None for v in votes):
                    g = "UNKNOWN"
                else:
                    w, o = votes.count(want), votes.count(opp)
                    g = {(2, 0): "BOTH_WITH", (1, 0): "WEAK_WITH", (1, 1): "MIXED",
                         (0, 1): "WEAK_AGAINST", (0, 2): "BOTH_AGAINST",
                         (0, 0): "BOTH_RANGING"}[(w, o)]
                groups.setdefault(g, []).append(out)
                by_side.setdefault(f"{d} {g}", []).append(out)
                i = j + 1
        print(f"  {symbol:<9} done")

    print()
    order = ("BOTH_WITH", "WEAK_WITH", "MIXED", "BOTH_RANGING", "WEAK_AGAINST",
             "BOTH_AGAINST", "UNKNOWN")
    for g in order:
        if g in groups:
            print(f"  {g:<13} {stats(groups[g])}")
    kept_today = [r for g in ("BOTH_WITH", "WEAK_WITH", "MIXED", "BOTH_RANGING", "UNKNOWN")
                  for r in groups.get(g, [])]
    if len(groups.get("WEAK_AGAINST", [])) > 1 and len(kept_today) > 1:
        print(f"\n  WEAK_AGAINST (blocked today) vs kept today: "
              f"z = {z(groups['WEAK_AGAINST'], kept_today):+.2f}")
    if len(groups.get("BOTH_AGAINST", [])) > 1:
        print(f"  BOTH_AGAINST vs kept today:                 "
              f"z = {z(groups['BOTH_AGAINST'], kept_today):+.2f}")
    print("\n  by side:")
    for k in sorted(by_side):
        print(f"    {k:<20} {stats(by_side[k])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
