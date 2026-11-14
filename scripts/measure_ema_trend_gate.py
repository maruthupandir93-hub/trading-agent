"""Does the EMA20/EMA50 higher-timeframe rule separate winners from losers?

WHY (2026-11-12 review, issues 4 and 7). The live trend gate
(`market_context.assess`) labelled every one of the 15 live trades "aligned",
and 13 lost; `measure_htf_consensus.py` shows its effect flipping sign between
windows. The review found a plain EMA rule — close vs EMA20 vs EMA50 on 1h and
4h — separating strongly and asked for it to be measured properly before
anything was replaced. It also proposed a PULLBACK entry: in an EMA trend, fade
a 15m RSI extreme back into the trend.

Measured the way this project requires:

  * the REAL functions — `market_context.ema_alignment` and the existing
    `market_context.assess` — not a copy (a gate measured by a copy of itself
    measures the copy);
  * on what the live graph SEES: 120 clock-aligned 1h and 4h candles up to and
    including the entry bar, the last one still forming, exactly as
    `validate_market_data` fetches them. No lookahead;
  * every strategy's real signal function, the live entry-extension,
    relative-strength-chase and fee gates, the 2.5/5.0 ATR model,
    NON-OVERLAPPING trades, net of taker fees;
  * per time-third, so a one-regime result is visible.

The question that decides shipping is the INCREMENTAL one: of the trades the
existing gate already lets through, are the ones the EMA rule would additionally
refuse losers?

No database, no key, no orders. Public Binance klines only.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@192.0.2.1:5432/none")

import measure_entry_extension as mee  # noqa: E402

TAKER = 0.0005
HTF_BARS = 120                      # what `validate_market_data` fetches
TF_MS = {"1h": 3_600_000, "4h": 14_400_000}
SYMBOLS = ("XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT", "LTCUSDT", "LINKUSDT",
           "SUIUSDT", "ETHUSDT", "BNBUSDT")


def htf_candles(bars: List[Dict[str, Any]], i: int) -> Dict[str, List[Dict[str, Any]]]:
    """The 1h and 4h candles the live graph would hold at 15m bar `i`."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for tf, ms in TF_MS.items():
        per = ms // 900_000
        chunk = bars[max(0, i + 1 - (HTF_BARS + 1) * per):i + 1]
        grouped: List[Dict[str, Any]] = []
        key = None
        for b in chunk:
            k = b["openTime"] // ms
            if k != key:
                grouped.append({"open": b["open"], "high": b["high"], "low": b["low"],
                                "close": b["close"], "volume": b["volume"]})
                key = k
            else:
                g = grouped[-1]
                g["high"] = max(g["high"], b["high"])
                g["low"] = min(g["low"], b["low"])
                g["close"] = b["close"]
                g["volume"] += b["volume"]
        out[tf] = grouped[-HTF_BARS:]
    return out


def rsi_series(closes: List[float], n: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    ag = sum(max(closes[i] - closes[i - 1], 0.0) for i in range(1, n + 1)) / n
    al = sum(max(closes[i - 1] - closes[i], 0.0) for i in range(1, n + 1)) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def stat(xs: List[float]) -> str:
    n = len(xs)
    if n < 2:
        return f"n={n}"
    m = sum(xs) / n
    se = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) / math.sqrt(n)
    return (f"n={n:5d}  {m:+.4f}R +/-{1.96 * se:.4f}  z={m / se:+.2f}  "
            f"win {sum(x > 0 for x in xs) / n * 100:4.1f}%")


def zgap(a: List[float], b: List[float]) -> float:
    def ms(x):
        m = sum(x) / len(x)
        return m, sum((v - m) ** 2 for v in x) / (len(x) - 1) / len(x)
    (ma, va), (mb, vb) = ms(a), ms(b)
    return (ma - mb) / math.sqrt(va + vb)


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess as ext_assess
    from backend.algorithms.market_context import (
        assess as repo_assess, build, chases_relative_strength, ema_alignment,
        ema_trend_of, percent_change,
    )
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER
    from backend.graphs.nodes.risk_gateway import max_fee_fraction_of_r

    args = sys.argv[1:]
    mee.CANDLES = int(args[args.index("--candles") + 1]) if "--candles" in args else 10000
    print(f"candles {mee.CANDLES} per symbol, {len(SYMBOLS)} symbols, non-overlapping, "
          f"extension + RS-chase + fee gates, net of fees")

    btc = mee.klines("BTCUSDT")
    btc_at = {b["openTime"]: k for k, b in enumerate(btc)}

    class _Ctx:
        benchmark_symbol = "BTC/USDT"

    rows: List[Dict[str, Any]] = []
    pull: Dict[int, List[Any]] = {}
    warm = (HTF_BARS + 1) * 16          # a full 120-candle 4h window

    for symbol in SYMBOLS:
        try:
            bars = mee.klines(symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: {exc}")
            continue
        n = len(bars)
        r14 = rsi_series([b["close"] for b in bars])

        def trade(i, d):
            a = mee.atr(bars[:i + 1])
            if not a:
                return None
            entry = bars[i]["close"]
            r = ATR_STOP_MULTIPLIER * a
            stop = entry - r if d == "LONG" else entry + r
            target = entry + 2 * r if d == "LONG" else entry - 2 * r
            if 2 * TAKER / (r / entry) > max_fee_fraction_of_r():
                return None
            prefix = bars[:i + 1]
            if ext_assess(d, prefix, entry_price=entry, stop_loss=stop, atr=a).chasing:
                return None
            bi = btc_at.get(bars[i]["openTime"])
            if bi is not None:
                ctx = _Ctx()
                c1, c2 = percent_change(prefix), percent_change(btc[:bi + 1])
                ctx.relative_strength_pct = None if c1 is None or c2 is None else c1 - c2
                if chases_relative_strength(d, ctx) is not None:
                    return None
            res = mee.resolve(bars, i, d, stop, target)
            if res is None:
                return None
            return res[0] - 2 * TAKER / (r / entry), res[1]

        third = lambda i: min(2, 3 * (i - warm) // max(1, n - warm))

        for name, fn in STRATEGY_FUNCTIONS.items():
            i = max(mee.WINDOW, warm)
            while i < n - 1:
                try:
                    sig = (fn(bars[max(0, i - mee.WINDOW):i + 1]) or "").upper()
                except Exception:  # noqa: BLE001
                    i += 1
                    continue
                if sig not in ("BUY", "SELL", "LONG", "SHORT"):
                    i += 1
                    continue
                d = "LONG" if sig in ("BUY", "LONG") else "SHORT"
                t = trade(i, d)
                if t is None:
                    i += 1
                    continue
                cand = htf_candles(bars, i)
                rows.append({
                    "s": name, "d": d, "r": t[0], "third": third(i),
                    "ema": ema_alignment(d, cand).verdict,
                    "repo_blocks": repo_assess(d, build(candles=cand)).blocks,
                })
                i = t[1] + 1

        for thr in (65, 70):
            i = max(mee.WINDOW, warm)
            while i < n - 1:
                x, d = r14[i], None
                if x is not None and (x >= thr or x <= 100 - thr):
                    cand = htf_candles(bars, i)
                    states = {ema_trend_of(cand["1h"]), ema_trend_of(cand["4h"])}
                    if x >= thr and states == {"Bearish"}:
                        d = "SHORT"
                    elif x <= 100 - thr and states == {"Bullish"}:
                        d = "LONG"
                if d is None:
                    i += 1
                    continue
                t = trade(i, d)
                if t is None:
                    i += 1
                    continue
                pull.setdefault(thr, []).append((third(i), t[0]))
                i = t[1] + 1
        print(f"  {symbol:<9} signals so far {len(rows)}")

    R = lambda sel: [x["r"] for x in rows if sel(x)]

    print("\nEMA20/EMA50 STATE (the real `ema_alignment`, 120 clock-aligned candles)")
    for st in ("aligned", "mixed", "counter", "unknown"):
        xs = R(lambda x, st=st: x["ema"] == st)
        if xs:
            print(f"  {st:<8} {stat(xs)}")
    print("  by time-third (oldest -> newest), aligned / mixed / counter mean R (n):")
    for th in range(3):
        cells = []
        for st in ("aligned", "mixed", "counter"):
            xs = R(lambda x, st=st, th=th: x["ema"] == st and x["third"] == th)
            cells.append(f"{sum(xs) / len(xs):+.3f} ({len(xs)})" if xs else "n/a")
        print(f"    third {th + 1}:  " + "   ".join(cells))

    print("\nWHAT WOULD SHIP — kept sets, net of fees")
    sets = {
        "no trend gate at all":            lambda x: True,
        "existing gate only (live today)": lambda x: not x["repo_blocks"],
        "EMA gate only":                   lambda x: x["ema"] != "counter",
        "BOTH gates (proposed)":           lambda x: not x["repo_blocks"] and x["ema"] != "counter",
    }
    for label, sel in sets.items():
        print(f"  {label:<34} {stat(R(sel))}")

    passed = lambda x: not x["repo_blocks"]
    extra = R(lambda x: passed(x) and x["ema"] == "counter")
    kept = R(lambda x: passed(x) and x["ema"] != "counter")
    print("\nTHE INCREMENTAL QUESTION — of trades the existing gate passes:")
    print(f"  EMA rule would ALSO refuse   {stat(extra)}")
    print(f"  EMA rule keeps               {stat(kept)}")
    if len(extra) > 1 and len(kept) > 1:
        print(f"  kept vs refused: z = {zgap(kept, extra):+.2f}")
    print("  by time-third, kept / refused mean R (n):")
    for th in range(3):
        k = R(lambda x, th=th: passed(x) and x["ema"] != "counter" and x["third"] == th)
        e = R(lambda x, th=th: passed(x) and x["ema"] == "counter" and x["third"] == th)
        f = lambda v: f"{sum(v) / len(v):+.3f} ({len(v)})" if v else "n/a"
        print(f"    third {th + 1}:  {f(k)}   /   {f(e)}")

    print("\n  BOTH gates, by direction:")
    for d in ("LONG", "SHORT"):
        print(f"    {d:<6} {stat(R(lambda x, d=d: sets['BOTH gates (proposed)'](x) and x['d'] == d))}")

    print("\n  per strategy: existing gate only -> BOTH gates   (mean R, n)")
    for name in sorted({x["s"] for x in rows}):
        a = R(lambda x, name=name: x["s"] == name and passed(x))
        b = R(lambda x, name=name: x["s"] == name and passed(x) and x["ema"] != "counter")
        f = lambda v: f"{sum(v) / len(v):+.3f} ({len(v):4d})" if v else "   n/a      "
        print(f"    {name:<19} {f(a)} -> {f(b)}")

    print("\nPULLBACK INTO THE EMA TREND (15m RSI extreme against an aligned 1h+4h trend)")
    for thr in sorted(pull):
        xs = [p[1] for p in pull[thr]]
        print(f"  RSI {thr}/{100 - thr}:  {stat(xs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
