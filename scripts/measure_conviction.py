"""Does conviction predict outcome? If not, the whole selectivity premise is wrong.

THE QUESTION BEHIND THE OPERATOR'S QUESTION
===========================================
The standing request is "more trades AND more profit". Those pull against each
other through exactly one dial: the confidence a setup must reach before it is
traded. `dynamic_thresholding.get_required_confidence` sets it per regime --
Bull Trend 0.60, Bear Trend 0.65, Low Volatility 0.70, Range 0.75, High
Volatility 0.85 -- and in a quiet market the debate reaches ~0.1, so nothing
trades. Lowering the bar is the obvious way to get more trades.

It is only the right move IF conviction actually predicts outcome. If a 0.6
signal wins no more often than a 0.2 signal, the threshold is costing trades
and buying nothing, and it should come down. If it does predict, then every
trade bought by lowering it is bought at a known price, and that price can be
stated instead of argued about.

Nothing in this project had measured it. This does, over thousands of trades.

WHICH DEBATE THIS IS, STATED UP FRONT BECAUSE IT MATTERS
========================================================
`algorithms/debate.score_debate` is a PURE function of candles -- trend,
structure, momentum, volume and volatility, five weighted legs -- so it replays
exactly, with no lookahead, by passing the bars up to and including the entry.

It is NOT the same function as the graph's specialist panel (`run_debate` over
nine specialists), which needs an order book, a tape and news and cannot be
replayed offline at all. The live refusals an operator reads -- "the specialist
panel reads NEUTRAL at 0.09" -- come from that other one.

So the ABSOLUTE numbers here do not transfer to the live thresholds, and this
script does not pretend they do. What transfers is the SHAPE: whether a
deterministic conviction score computed from the same candles separates winners
from losers. That is the premise both gates rest on, and it is testable.

    .venv/Scripts/python.exe scripts/measure_conviction.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS  # noqa: E402
from backend.algorithms.debate import score_debate  # noqa: E402
from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER  # noqa: E402
from backend.core.strategy_backtest import backtest_strategy  # noqa: E402
from backend.services.market_data import fetch_klines  # noqa: E402

DEFAULT_SYMBOLS = ["XRP/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT", "SUI/USDT"]
DEFAULT_LIMIT = 1500

# The debate needs its own history before it can say anything; 200 bars is well
# clear of `debate.MIN_CANDLES` and of the indicator windows inside it.
WARMUP_BARS = 200

BUCKETS = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.6), (0.6, 1.01)]

# Below this a bucket's expectancy is noise, and reporting it as a finding is
# how a 52-trade cell becomes a policy.
MIN_TRADES_TO_TRUST = 60


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {"trades": 0, "winRate": 0.0, "expectancyR": 0.0, "totalR": 0.0}
    wins = sum(1 for r in rows if r["net_r"] > 0)
    total = sum(r["net_r"] for r in rows)
    return {"trades": n, "winRate": round(wins / n, 4),
            "expectancyR": round(total / n, 4), "totalR": round(total, 2)}


def _fmt(s: Dict[str, Any]) -> str:
    thin = "" if s["trades"] >= MIN_TRADES_TO_TRUST else "  (thin)"
    return (f"{s['expectancyR']:+7.4f}R  {s['winRate']*100:5.1f}%  "
            f"{s['trades']:>5} trades{thin}")


async def main_async(symbols: List[str], limit: int) -> int:
    rows: List[Dict[str, Any]] = []

    for symbol in symbols:
        print(f"  {symbol} 15m ({limit} candles)...", flush=True)
        try:
            candles = await fetch_klines(symbol, "15m", limit=limit)
        except Exception as exc:  # noqa: BLE001
            print(f"    {type(exc).__name__}: {exc}")
            continue
        if not candles or len(candles) < WARMUP_BARS + 200:
            print(f"    insufficient candles ({0 if not candles else len(candles)})")
            continue

        # The debate is scored ONCE PER BAR and shared across strategies. It is
        # a function of the candles alone, so computing it per strategy would be
        # eleven identical calls -- and on 1500 bars x 5 symbols that is the
        # difference between a minute and a quarter of an hour.
        verdicts: Dict[int, Any] = {}

        for name, fn in STRATEGY_FUNCTIONS.items():
            result = backtest_strategy(
                candles, fn, name=name,
                stop_mult=ATR_STOP_MULTIPLIER, target_mult=ATR_TARGET_MULTIPLIER,
            )
            for t in result.trade_log:
                idx = t["entry_index"]
                if idx < WARMUP_BARS:
                    continue
                if idx not in verdicts:
                    try:
                        verdicts[idx] = score_debate(candles[: idx + 1])
                    except Exception as exc:  # noqa: BLE001
                        print(f"    debate error at {symbol} {idx}: {exc}")
                        verdicts[idx] = None
                verdict = verdicts[idx]
                if verdict is None:
                    continue

                direction = "LONG" if t["direction"] == "long" else "SHORT"
                rows.append({
                    "symbol": symbol, "strategy": name, "direction": direction,
                    "debateDirection": verdict.direction,
                    "confidence": float(verdict.confidence or 0.0),
                    "agrees": verdict.direction == direction,
                    "net_r": float(t["r_multiple"]) - float(t.get("fee_r") or 0.0),
                })

    if not rows:
        print("\n  NOT STORED: no trades were measured.")
        return 1

    print(f"\n\033[1mDOES CONVICTION PREDICT OUTCOME?\033[0m   "
          f"{len(rows)} trades, stop {ATR_STOP_MULTIPLIER}x / target "
          f"{ATR_TARGET_MULTIPLIER}x, net of fees\n")

    agreeing = [r for r in rows if r["agrees"]]
    opposing = [r for r in rows
                if not r["agrees"] and r["debateDirection"] in ("LONG", "SHORT")]
    neutral = [r for r in rows if r["debateDirection"] not in ("LONG", "SHORT")]

    print("  By whether the debate AGREES with the setup's direction:")
    print(f"    agrees        {_fmt(_stats(agreeing))}")
    print(f"    disagrees     {_fmt(_stats(opposing))}")
    print(f"    no view       {_fmt(_stats(neutral))}")

    print("\n  AGREEING trades, by the debate's confidence:")
    by_bucket: Dict[str, Dict[str, Any]] = {}
    for lo, hi in BUCKETS:
        sel = [r for r in agreeing if lo <= r["confidence"] < hi]
        label = f"{lo:.1f}-{hi:.1f}"
        by_bucket[label] = _stats(sel)
        print(f"    {label:<10}  {_fmt(by_bucket[label])}")

    print("\n  The cost of a threshold (agreeing trades at or above it):")
    thresholds: Dict[str, Dict[str, Any]] = {}
    for floor in (0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4):
        sel = [r for r in agreeing if r["confidence"] >= floor]
        thresholds[f"{floor:.2f}"] = _stats(sel)
        share = len(sel) / len(agreeing) * 100 if agreeing else 0.0
        print(f"    >= {floor:.2f}    {_fmt(_stats(sel))}   keeps {share:5.1f}% of them")

    # THE VERDICT, and it is deliberately a comparison of ENDS rather than a
    # correlation: a monotonic relationship is a stronger claim than this data
    # can support, and all the decision needs is whether high conviction beats
    # low conviction by more than noise.
    trustworthy = {k: v for k, v in by_bucket.items()
                   if v["trades"] >= MIN_TRADES_TO_TRUST}
    print(f"\n\033[1mWHAT THIS SAYS\033[0m")
    if len(trustworthy) < 2:
        print("  Too few buckets have enough trades to compare. No conclusion.")
    else:
        lowest = trustworthy[min(trustworthy)]
        highest = trustworthy[max(trustworthy)]
        gap = highest["expectancyR"] - lowest["expectancyR"]
        print(f"  lowest usable bucket  {min(trustworthy)}  "
              f"{lowest['expectancyR']:+.4f}R over {lowest['trades']} trades")
        print(f"  highest usable bucket {max(trustworthy)}  "
              f"{highest['expectancyR']:+.4f}R over {highest['trades']} trades")
        if gap > 0.02:
            print(f"  Conviction PREDICTS: {gap:+.4f}R between the ends. Selectivity "
                  f"is buying something, so every trade gained by lowering the "
                  f"threshold is paid for at a measurable rate.")
        elif gap < -0.02:
            print(f"  \033[33mConviction is INVERTED ({gap:+.4f}R): the confident "
                  f"setups did WORSE. A threshold is costing trades and buying "
                  f"nothing.\033[0m")
        else:
            print(f"  \033[33mConviction is FLAT ({gap:+.4f}R between the ends). On "
                  f"this window the threshold separates nothing, and the case for "
                  f"keeping it high rests on something other than this evidence."
                  f"\033[0m")

    disagree_gap = _stats(agreeing)["expectancyR"] - _stats(opposing)["expectancyR"]
    print(f"  Agreement alone is worth {disagree_gap:+.4f}R per trade, which is the "
          f"supervisor's direction check -- a separate gate from the threshold.")

    date = dt.date.today().isoformat()
    out_dir = ROOT / "backtests" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "conviction.json"
    path.write_text(json.dumps({
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": symbols, "timeframe": "15m", "candlesPerSeries": limit,
        "warmupBars": WARMUP_BARS, "minTradesToTrust": MIN_TRADES_TO_TRUST,
        "riskModel": {"stopMult": ATR_STOP_MULTIPLIER, "targetMult": ATR_TARGET_MULTIPLIER},
        "agrees": _stats(agreeing), "disagrees": _stats(opposing),
        "noView": _stats(neutral),
        "byConfidenceBucket": by_bucket, "byThreshold": thresholds,
        "caveats": [
            "This is `algorithms.debate.score_debate` -- the EVENT path's "
            "five-leg debate, a PURE function of candles. It is NOT the graph's "
            "nine-specialist panel, which needs an order book, a tape and news "
            "and cannot be replayed. Absolute numbers do not transfer to the "
            "live per-regime thresholds; the SHAPE is what this measures.",
            "Net of taker fees. No slippage, no funding.",
            "Pools all eleven strategies; live, one is selected.",
            "Scored on bars up to and INCLUDING the entry bar. No lookahead.",
        ],
    }, indent=2), encoding="utf-8")
    print(f"\n  Stored: {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(main_async(symbols, args.limit))


if __name__ == "__main__":
    sys.exit(main())
