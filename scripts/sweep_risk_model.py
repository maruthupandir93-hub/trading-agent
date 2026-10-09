"""Which stop/target pair actually makes money? Measured, split in and out of sample.

THE QUESTION THIS ANSWERS, and why it is worth a script rather than an opinion.

`ATR_STOP_MULTIPLIER = 2.5` / `ATR_TARGET_MULTIPLIER = 5.0` were set from NINE
live trades (CLAUDE.md, "The three changes that came out of reading the live
ledger"): five of six losses were stop-outs of 0.45-0.79% while SOL's 15m ATR%
was ~0.43, so the stop sat INSIDE the noise band and was widened 1.5 -> 2.5,
with the target moved to 5.0 to keep the payoff at 2:1. That file says plainly
that all three changes were HYPOTHESES and that nine trades cannot settle them.

This settles them with thousands of simulated trades instead -- on the same
deterministic engine the live risk model uses, net of fees.

TWO THINGS MAKE THE ANSWER NON-OBVIOUS, and they pull in opposite directions:

  * A WIDER STOP TAKES FEWER NOISE STOP-OUTS, which raises the win rate.
  * A WIDER TARGET IS REACHED LESS OFTEN, which lowers it.

and a third that is easy to miss entirely:

  * FEES ARE PAID IN PRICE AND MEASURED IN R. `fee_r = 2 x taker x entry /
    risk_per_unit`, so a wider stop makes the SAME dollar fee a SMALLER
    fraction of 1R. On this system's distances that is ~0.09R against edges of
    0.13-0.16R -- large enough to flip a ranking on its own.

Expectancy net of fees is the only figure ranked here. Gross is reported beside
it because the GAP is the interesting quantity.

IN-SAMPLE AND OUT-OF-SAMPLE, REPORTED SEPARATELY AND ALWAYS BOTH
================================================================
A sweep over a single window finds the pair that best fits that window's noise.
That is not a discovery, it is curve-fitting, and acting on it is how a system
that looks optimised starts losing money the week after.

So each series is split: the first `--split` fraction chooses, the remainder
judges. A pair is only worth considering when it is positive in BOTH halves and
its out-of-sample rank is close to its in-sample one. The script prints the
in-sample winner AND how that winner did out of sample, which is the comparison
that matters -- a winner that collapses out of sample is the headline finding,
not a footnote.

IT DEPLOYS NOTHING. Invariant 5. The multipliers live in
`backend/core/risk_manager.py` and `lib/riskManager.ts` and changing them is a
deliberate human edit to BOTH -- they are two numbers that must agree, kept in
sync by hand on purpose (`lib/riskManager.ts` and the Python file drifted apart
on exactly these constants once already). This prints evidence for that
decision and stores it; it never writes a config.

    .venv/Scripts/python.exe scripts/sweep_risk_model.py
    .venv/Scripts/python.exe scripts/sweep_risk_model.py --symbols SOL/USDT --limit 1500
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS  # noqa: E402
from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER  # noqa: E402
from backend.core.strategy_backtest import backtest_strategy  # noqa: E402
from backend.services.market_data import fetch_klines  # noqa: E402

# The live rotation. Chosen because these are the five instruments the operator's
# account can actually open (see `trading_session._venue_minimum_refusal`), so a
# result here is about trades that could be placed.
DEFAULT_SYMBOLS = ["XRP/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT", "SUI/USDT"]
DEFAULT_TIMEFRAMES = ["15m", "1h"]
DEFAULT_LIMIT = 1500

# Stop distances in ATR, and the reward ratio applied on top. The target
# multiplier is ALWAYS `stop x ratio`, never swept independently -- the two are
# one decision. Widening only the stop turns a 2:1 payoff into 1.2:1, which at a
# 33% win rate is reliably losing, and that mistake is already recorded in
# CLAUDE.md as the reason the target moved with the stop the first time.
STOPS = [1.5, 2.0, 2.5, 3.0, 3.5]
RATIOS = [1.5, 2.0, 2.5, 3.0]

# Below this a cell's expectancy is noise. Pooled across five symbols and two
# timeframes and eleven strategies a real pair clears it comfortably; a cell that
# does not is reported as thin rather than ranked.
MIN_TRADES_TO_RANK = 150


def _pool(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pool per-strategy results into one cell.

    Expectancy is re-derived from the TOTALS, not averaged across strategies: a
    strategy that took three trades must not weigh the same as one that took
    three hundred.
    """
    trades = sum(r["trades"] for r in results)
    wins = sum(r["wins"] for r in results)
    net_r = sum(r["expectancy_r"] * r["trades"] for r in results)
    gross_r = sum(r["gross_expectancy_r"] * r["trades"] for r in results)
    return {
        "trades": trades,
        "wins": wins,
        "winRate": round(wins / trades, 4) if trades else 0.0,
        "expectancyR": round(net_r / trades, 4) if trades else 0.0,
        "grossExpectancyR": round(gross_r / trades, 4) if trades else 0.0,
        "totalR": round(net_r, 2),
    }


def _run_cell(
    series: List[Tuple[str, str, List[Dict[str, Any]]]],
    stop: float,
    ratio: float,
) -> Dict[str, Any]:
    """Every strategy over every series at one (stop, ratio), pooled."""
    per_strategy: Dict[str, List[Dict[str, Any]]] = {}
    for _symbol, _tf, candles in series:
        for name, fn in STRATEGY_FUNCTIONS.items():
            r = backtest_strategy(
                candles, fn, name=name,
                stop_mult=stop, target_mult=stop * ratio,
            )
            per_strategy.setdefault(name, []).append(r.summary())

    pooled = _pool([s for rows in per_strategy.values() for s in rows])
    pooled["byStrategy"] = {
        name: _pool(rows) for name, rows in sorted(per_strategy.items())
    }
    return pooled


def _split(candles: List[Dict[str, Any]], fraction: float) -> Tuple[List, List]:
    """In-sample head, out-of-sample tail. Chronological, never shuffled -- a
    shuffled split would let the chooser see the future of its own window."""
    cut = int(len(candles) * fraction)
    return candles[:cut], candles[cut:]


def _fmt(cell: Dict[str, Any]) -> str:
    thin = "" if cell["trades"] >= MIN_TRADES_TO_RANK else " thin"
    return (f"{cell['expectancyR']:+7.4f}R  {cell['winRate']*100:5.1f}%  "
            f"{cell['trades']:>5} trades{thin}")


async def main_async(
    symbols: List[str], timeframes: List[str], limit: int, split: float,
) -> int:
    series: List[Tuple[str, str, List[Dict[str, Any]]]] = []
    for symbol in symbols:
        for tf in timeframes:
            print(f"  fetching {symbol} {tf} ({limit} candles)...", flush=True)
            try:
                candles = await fetch_klines(symbol, tf, limit=limit)
            except Exception as exc:  # noqa: BLE001
                print(f"    {type(exc).__name__}: {exc}")
                continue
            if not candles or len(candles) < 300:
                print(f"    insufficient candles ({0 if not candles else len(candles)})")
                continue
            series.append((symbol, tf, candles))

    # SAME REFUSAL AS `run_backtests.py`, for the same reason: a run that
    # measured nothing must not be stored over one that did, and it must not
    # exit 0 and look like a result.
    if not series:
        print("\n  NOT STORED: no series could be fetched, so this run measured")
        print("  nothing. The data source failed, the risk model did not.")
        return 1

    print(f"\n  {len(series)} series, "
          f"{sum(len(c) for _, _, c in series):,} candles, "
          f"{len(STRATEGY_FUNCTIONS)} strategies, "
          f"{len(STOPS) * len(RATIOS)} (stop, ratio) pairs\n")

    in_series = [(s, t, _split(c, split)[0]) for s, t, c in series]
    out_series = [(s, t, _split(c, split)[1]) for s, t, c in series]

    grid: Dict[str, Dict[str, Any]] = {}
    for stop in STOPS:
        for ratio in RATIOS:
            key = f"{stop}x{ratio}"
            grid[key] = {
                "stopMult": stop, "ratio": ratio, "targetMult": round(stop * ratio, 3),
                "inSample": _run_cell(in_series, stop, ratio),
                "outOfSample": _run_cell(out_series, stop, ratio),
            }
            print(f"  {key:<9} IS {_fmt(grid[key]['inSample'])}"
                  f"   |   OOS {_fmt(grid[key]['outOfSample'])}", flush=True)

    rankable = [
        g for g in grid.values()
        if g["inSample"]["trades"] >= MIN_TRADES_TO_RANK
        and g["outOfSample"]["trades"] >= MIN_TRADES_TO_RANK
    ]
    best_is = max(rankable, key=lambda g: g["inSample"]["expectancyR"], default=None)
    best_oos = max(rankable, key=lambda g: g["outOfSample"]["expectancyR"], default=None)
    live = grid.get(f"{ATR_STOP_MULTIPLIER}x"
                    f"{round(ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER, 1)}")

    print("\n\033[1mWHAT THIS SAYS\033[0m")
    if best_is:
        print(f"  best IN-SAMPLE      {best_is['stopMult']}x stop, "
              f"{best_is['ratio']}:1  -> {_fmt(best_is['inSample'])}")
        print(f"    the same pair OUT of sample          "
              f"-> {_fmt(best_is['outOfSample'])}")
    if best_oos:
        print(f"  best OUT-OF-SAMPLE  {best_oos['stopMult']}x stop, "
              f"{best_oos['ratio']}:1  -> {_fmt(best_oos['outOfSample'])}")
    if live:
        print(f"  LIVE today          {ATR_STOP_MULTIPLIER}x stop, "
              f"{ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER:.1f}:1  "
              f"-> IS {_fmt(live['inSample'])}")
        print(f"                                              "
              f"-> OOS {_fmt(live['outOfSample'])}")

    # THE ROBUSTNESS LINE, and it is the one to read first. A pair that wins
    # in-sample and loses out of sample has found this window's noise. Saying
    # so explicitly is the whole reason the split exists.
    if best_is and best_is["outOfSample"]["expectancyR"] <= 0:
        print("\n  \033[33mThe in-sample winner is NEGATIVE out of sample. That pair is")
        print("  curve-fitted to the first half of this window and must not be")
        print("  adopted on the strength of the in-sample number.\033[0m")

    both_positive = [
        g for g in rankable
        if g["inSample"]["expectancyR"] > 0 and g["outOfSample"]["expectancyR"] > 0
    ]
    print(f"\n  {len(both_positive)} of {len(rankable)} rankable pairs are positive "
          f"in BOTH halves:")
    for g in sorted(both_positive,
                    key=lambda g: min(g["inSample"]["expectancyR"],
                                      g["outOfSample"]["expectancyR"]),
                    reverse=True):
        print(f"    {g['stopMult']}x {g['ratio']}:1   "
              f"IS {g['inSample']['expectancyR']:+.4f}R   "
              f"OOS {g['outOfSample']['expectancyR']:+.4f}R")

    date = dt.date.today().isoformat()
    out_dir = ROOT / "backtests" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": symbols, "timeframes": timeframes, "candlesPerSeries": limit,
        "splitFraction": split,
        "seriesFetched": [{"symbol": s, "timeframe": t, "candles": len(c)}
                          for s, t, c in series],
        "liveRiskModel": {"stopMult": ATR_STOP_MULTIPLIER,
                          "targetMult": ATR_TARGET_MULTIPLIER,
                          "source": "backend/core/risk_manager.py"},
        "minTradesToRank": MIN_TRADES_TO_RANK,
        "grid": grid,
        "caveats": [
            "Expectancy is NET of taker fees. Gross is reported beside it.",
            "Ambiguous bars assume the stop filled first (conservative).",
            "No slippage, no funding, no partial fills.",
            "Pools all eleven strategies; the live agent trades the ONE the "
            "scorer picks and only when the panel agrees, so trade COUNT here "
            "is not a forecast of live frequency.",
            "A pair must be positive in BOTH halves to be worth considering.",
            "This informs; it does not deploy. The multipliers are a human edit "
            "to backend/core/risk_manager.py AND lib/riskManager.ts.",
        ],
    }
    path = out_dir / "risk-model-sweep.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  Stored: {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--split", type=float, default=0.6,
                    help="fraction of each series used to CHOOSE; the rest judges")
    args = ap.parse_args()

    if not (0.2 <= args.split <= 0.8):
        print("--split must leave a usable half on each side (0.2-0.8)")
        return 2

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    return asyncio.run(main_async(symbols, timeframes, args.limit, args.split))


if __name__ == "__main__":
    sys.exit(main())
