"""Does the higher-timeframe gate actually make money? Measured, not assumed.

THE QUESTION, AND WHY IT IS THE RIGHT ONE TO ASK
================================================
`scripts/sweep_risk_model.py` swept twenty (stop, target) pairs over 10,000
candles of the live rotation and found NINETEEN negative out of sample --
including the pair in production. That reads as "the system has no edge", and
the follow-up measurement says something much more useful:

    IN-SAMPLE  (a broad UPTREND)    long win 46.4%   short win 30.0%
                                    ...and it took 52.8% SHORTS
    OUT-OF-SAMPLE (a broad DOWNTREND) long win 30.8%   short win 41.3%
                                    ...and it took 55.1% LONGS

The signals are directionally INFORMATIVE -- each side wins in the regime that
favours it, comfortably above the 33.3% break-even a 2:1 payoff needs. What
loses money is the MIX: the raw ensemble is systematically on the wrong side of
the prevailing trend, because mean-reversion and range strategies fire hardest
exactly when a trend is extended.

So the lever is not the risk model. It is refusing the counter-trend half --
which is precisely what `algorithms/market_context.assess` already does in
`risk_gateway.gate`, behind `REQUIRE_HTF_ALIGNMENT`. CLAUDE.md added that gate
as an explicitly stated HYPOTHESIS: *"ALL THREE ARE HYPOTHESES. They will
reduce the number of trades and should raise the win rate. Whether they raise
EXPECTANCY -- the number that actually matters -- depends on how many removed
trades would have won, and 12 trades cannot say."*

This answers it with thousands.

NO LOOKAHEAD, AND THAT IS THE WHOLE DESIGN
==========================================
"Trades aligned with the half's direction" would be a hindsight filter and
would prove nothing -- of course the winning side wins. The gate must be
evaluated on what it could SEE at entry, so this aggregates the 15m candles up
to (and including) the entry bar into 1h and 4h series and asks the REAL
`market_context.build` + `assess` -- the same functions the live gateway calls,
not a re-implementation. A gate measured by a copy of itself measures the copy.

WHAT THE NUMBERS ARE NOT
========================
  * Still gross of slippage and funding; fees ARE included.
  * Still pools all eleven strategies. Live, the scorer picks ONE and the panel
    must agree, so trade COUNT here is not a forecast of live frequency.
  * The gate is one of several live filters. This isolates its contribution,
    not the system's.

    .venv/Scripts/python.exe scripts/measure_htf_gate.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS  # noqa: E402
from backend.algorithms import market_context  # noqa: E402
from backend.core import evidence  # noqa: E402
from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER  # noqa: E402
from backend.core.strategy_backtest import backtest_strategy  # noqa: E402
from backend.services.market_data import fetch_klines  # noqa: E402

DEFAULT_SYMBOLS = ["XRP/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT", "SUI/USDT"]
DEFAULT_TIMEFRAMES = ["15m"]
DEFAULT_LIMIT = 1500

# 15m -> 1h is four bars, 15m -> 4h is sixteen. Aggregating from the primary
# series rather than fetching the higher timeframes separately keeps the bar
# boundaries exactly aligned with the entry index, so "the 4h trend at entry"
# cannot accidentally include the bar the entry is inside.
AGGREGATION = {"1h": 4, "4h": 16}

# `market_context.MIN_BARS` is 20 per timeframe and `_consensus` needs two
# timeframes, so the 4h series is binding: 20 x 16 = 320 fifteen-minute bars
# before the gate can say anything at all.
WARMUP_BARS = 20 * max(AGGREGATION.values())


def _aggregate(bars: Sequence[Dict[str, Any]], factor: int) -> List[Dict[str, Any]]:
    """Roll `factor` consecutive bars into one. OHLCV, oldest-first.

    Built from the END backwards so the LAST bucket always ends exactly at the
    entry bar. Bucketing forwards would leave a ragged final bucket whose
    contents depend on how many bars happen to precede it, which makes the
    measured trend jitter for reasons that have nothing to do with the market.
    """
    out: List[Dict[str, Any]] = []
    n = len(bars)
    start = n % factor
    for i in range(start, n, factor):
        chunk = bars[i: i + factor]
        if len(chunk) < factor:
            continue
        out.append({
            "open": float(chunk[0]["open"]),
            "high": max(float(b["high"]) for b in chunk),
            "low": min(float(b["low"]) for b in chunk),
            "close": float(chunk[-1]["close"]),
            "volume": sum(float(b.get("volume") or 0.0) for b in chunk),
        })
    return out


def _gate_at(bars_to_entry: Sequence[Dict[str, Any]], direction: str) -> Tuple[str, bool]:
    """The REAL gate's verdict, using only bars up to and including the entry."""
    candles = {"15m": list(bars_to_entry)}
    for tf, factor in AGGREGATION.items():
        candles[tf] = _aggregate(bars_to_entry, factor)
    ctx = market_context.build(candles=candles, primary_timeframe="15m")
    alignment = market_context.assess(direction, ctx)
    return alignment.verdict, alignment.blocks


def _stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Expectancy WITH ITS ERROR BAR.

    The first version returned the bare number, and "+0.0230R kept against
    -0.0868R blocked" was about to be reported as a validated gate. It is
    z = +1.01 -- the sort of gap a fair coin produces about a third of the
    time. See `backend/core/evidence.py`: a figure printed without its
    uncertainty is how a coin flip becomes a change to a live risk gate.
    """
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "expectancyR": 0.0,
                "totalR": 0.0, "standardError": None}
    e = evidence.from_outcomes([t["net_r"] for t in trades])
    return {
        "trades": n,
        "wins": sum(1 for t in trades if t["net_r"] > 0),
        "winRate": round(e.win_rate, 4),
        "expectancyR": round(e.expectancy_r, 4),
        "totalR": round(sum(t["net_r"] for t in trades), 2),
        "standardError": None if e.standard_error is None else round(e.standard_error, 4),
    }


def _as_expectancy(s: Dict[str, Any]) -> evidence.Expectancy:
    return evidence.Expectancy(
        trades=s["trades"], win_rate=s["winRate"],
        expectancy_r=s["expectancyR"], standard_error=s.get("standardError"),
    )


def _fmt(s: Dict[str, Any]) -> str:
    err = "" if s.get("standardError") is None else f" +/-{s['standardError']:.4f}"
    return (f"{s['expectancyR']:+7.4f}R{err}  {s['winRate']*100:5.1f}%  "
            f"{s['trades']:>5} trades  {s['totalR']:>+9.1f}R total")


async def main_async(symbols: List[str], timeframes: List[str], limit: int) -> int:
    kept: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    unmeasurable: List[Dict[str, Any]] = []
    by_state: Dict[str, List[Dict[str, Any]]] = {}

    for symbol in symbols:
      for timeframe in timeframes:
        print(f"  {symbol} {timeframe} ({limit} candles)...", flush=True)
        try:
            candles = await fetch_klines(symbol, timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            print(f"    {type(exc).__name__}: {exc}")
            continue
        if not candles or len(candles) < WARMUP_BARS + 200:
            print(f"    insufficient candles ({0 if not candles else len(candles)})")
            continue

        for name, fn in STRATEGY_FUNCTIONS.items():
            result = backtest_strategy(
                candles, fn, name=name,
                stop_mult=ATR_STOP_MULTIPLIER, target_mult=ATR_TARGET_MULTIPLIER,
            )
            for t in result.trade_log:
                idx = t["entry_index"]
                if idx < WARMUP_BARS:
                    # The gate could not have had an opinion this early. Dropping
                    # these from BOTH sides rather than counting them as passes
                    # keeps the comparison honest.
                    continue
                direction = "LONG" if t["direction"] == "long" else "SHORT"
                try:
                    state, blocks = _gate_at(candles[: idx + 1], direction)
                except Exception as exc:  # noqa: BLE001
                    print(f"    gate error at {symbol} {name} {idx}: {exc}")
                    continue

                row = {
                    "symbol": symbol, "timeframe": timeframe,
                    "strategy": name, "direction": t["direction"],
                    "outcome": t["outcome"], "state": state,
                    # NET of fees: `r_multiple` is the gross R the risk model
                    # defines (+2 / -1) and `fee_r` is what taking it cost.
                    "net_r": float(t["r_multiple"]) - float(t.get("fee_r") or 0.0),
                }
                by_state.setdefault(state, []).append(row)
                if blocks:
                    blocked.append(row)
                elif state == market_context.UNKNOWN:
                    unmeasurable.append(row)
                    kept.append(row)
                else:
                    kept.append(row)

    everything = kept + blocked
    if not everything:
        print("\n  NOT STORED: no trades were measured.")
        return 1

    all_s, kept_s, blocked_s = _stats(everything), _stats(kept), _stats(blocked)

    print(f"\n\033[1mTHE HIGHER-TIMEFRAME ALIGNMENT GATE\033[0m   "
          f"(stop {ATR_STOP_MULTIPLIER}x ATR, target {ATR_TARGET_MULTIPLIER}x, "
          f"net of fees)\n")
    print(f"  every signal          {_fmt(all_s)}")
    print(f"  the gate LETS THROUGH {_fmt(kept_s)}")
    print(f"  the gate BLOCKS       {_fmt(blocked_s)}")

    print(f"\n  by alignment state:")
    for state in sorted(by_state):
        print(f"    {state:<14} {_fmt(_stats(by_state[state]))}")

    delta = kept_s["expectancyR"] - all_s["expectancyR"]
    print(f"\n\033[1mWHAT THIS SAYS\033[0m")
    print(f"  expectancy moves {delta:+.4f}R per trade when the gate is applied, "
          f"and it removes {blocked_s['trades']} of {all_s['trades']} signals "
          f"({blocked_s['trades']/all_s['trades']*100:.1f}%).")

    # THE DECISIVE TEST, and it is about the BLOCKED set rather than the kept
    # one. A gate is only worth having if what it removes is worse than what it
    # keeps; a gate that removes a PROFITABLE subset is destroying edge even
    # while the kept set looks fine.
    comparison = None
    if blocked_s["trades"] == 0:
        print("  The gate blocked NOTHING in this window, so it cannot be judged "
              "here -- that is a finding about the window, not about the gate.")
    else:
        comparison = evidence.compare(_as_expectancy(kept_s), _as_expectancy(blocked_s))
        direction = ("WORSE" if blocked_s["expectancyR"] < kept_s["expectancyR"]
                     else "NO WORSE")
        print(f"  What it blocks is {direction} than what it keeps: "
              f"{blocked_s['expectancyR']:+.4f}R vs {kept_s['expectancyR']:+.4f}R.")
        print(f"  {comparison.verdict(comparisons_made=1)}")
        if abs(comparison.z or 0.0) < 2.0 and abs(comparison.gap_r) > 1e-9:
            sd = evidence.binomial_std(max(kept_s["winRate"], 1e-6))
            need = int(round(8.0 * sd * sd / (comparison.gap_r ** 2)))
            print(f"  \033[33mSo this does NOT yet demonstrate the gate works. A gap "
                  f"of {comparison.gap_r:+.4f}R needs about {need:,} trades per side "
                  f"to show at two sigma; this run has {kept_s['trades']} and "
                  f"{blocked_s['trades']}.\033[0m")

    if not _as_expectancy(kept_s).is_distinguishable_from_zero:
        print(f"  And the kept set's own {kept_s['expectancyR']:+.4f}R is not "
              f"distinguishable from zero -- its 95% interval includes it. "
              f"Positive, not demonstrated.")

    if unmeasurable:
        print(f"  {len(unmeasurable)} signals had an UNMEASURABLE trend and were let "
              f"through, which is the gate's documented choice -- an unmeasured "
              f"higher-timeframe trend costs conviction, not bounding.")

    date = dt.date.today().isoformat()
    out_dir = ROOT / "backtests" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "htf-gate.json"
    path.write_text(json.dumps({
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": symbols, "timeframes": timeframes, "candlesPerSeries": limit,
        "warmupBars": WARMUP_BARS,
        "riskModel": {"stopMult": ATR_STOP_MULTIPLIER, "targetMult": ATR_TARGET_MULTIPLIER},
        "all": all_s, "kept": kept_s, "blocked": blocked_s,
        "byState": {k: _stats(v) for k, v in sorted(by_state.items())},
        "caveats": [
            "Net of taker fees. No slippage, no funding.",
            "The gate is evaluated on bars up to and INCLUDING the entry bar only.",
            "Pools all eleven strategies; live, one is selected and the panel "
            "must also agree, so trade count is not a live frequency forecast.",
            "This isolates ONE live filter's contribution, not the system's.",
        ],
    }, indent=2), encoding="utf-8")
    print(f"\n  Stored: {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    return asyncio.run(main_async(symbols, timeframes, args.limit))


if __name__ == "__main__":
    sys.exit(main())
