"""Run every voting strategy over real historical candles and STORE the result.

The audit's §2.6: the backtest engine exists but nothing reproducible was ever
stored, so no strategy has been validated on this system's own logic — every
profile carries `historical_success_rate=None`. This produces that evidence.

WHAT IT DOES
============
For each symbol and timeframe it fetches historical klines (public Binance, no
key), then runs each of the eleven `STRATEGY_FUNCTIONS` through the deterministic
`core/strategy_backtest`, which simulates every signal under the SAME risk model
the live agent places (2.5x-ATR stop / 5.0x-ATR target). It writes one JSON file
per symbol/timeframe under `backtests/<date>/` plus a combined summary, and prints
a ranked table.

REPRODUCIBLE. The simulation is pure; the only non-determinism is which candles
the exchange returns, and those are stamped into the output (symbol, timeframe,
count, first/last close) so a result can be checked or re-run.

READ THE NUMBERS HONESTLY. Results are gross of fees and slippage and in-sample.
Expectancy in R is the figure that matters — positive means an edge per unit
risked. A thin positive is break-even once costs are paid, and Scalping especially
cannot be trusted from a gross backtest (its own profile says so). This is
evidence for which strategies deserve a place in the ensemble, not a promotion to
live — `strategy_performance.MIN_SAMPLE` still governs that on REAL closed trades.

    .venv/Scripts/python.exe scripts/run_backtests.py
    .venv/Scripts/python.exe scripts/run_backtests.py --symbols SOL/USDT,ETH/USDT --timeframes 15m,1h --limit 1000
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
from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER  # noqa: E402
from backend.core.strategy_backtest import backtest_strategy  # noqa: E402
from backend.services.market_data import fetch_klines  # noqa: E402

DEFAULT_SYMBOLS = ["SOL/USDT", "ETH/USDT", "XRP/USDT"]
DEFAULT_TIMEFRAMES = ["15m", "1h"]
DEFAULT_LIMIT = 1000


async def _run_one(symbol: str, timeframe: str, limit: int) -> Dict[str, Any]:
    candles = await fetch_klines(symbol, timeframe, limit=limit)
    if not candles or len(candles) < 60:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "error": f"insufficient candles ({0 if not candles else len(candles)})",
        }

    results = []
    for name, fn in STRATEGY_FUNCTIONS.items():
        r = backtest_strategy(candles, fn, name=name)
        results.append(r.summary())

    results.sort(key=lambda r: r["expectancy_r"], reverse=True)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": len(candles),
        "firstClose": float(candles[0]["close"]),
        "lastClose": float(candles[-1]["close"]),
        "riskModel": {"stopMult": ATR_STOP_MULTIPLIER, "targetMult": ATR_TARGET_MULTIPLIER},
        "strategies": results,
    }


def _print_table(run: Dict[str, Any]) -> None:
    if run.get("error"):
        print(f"\n  {run['symbol']} {run['timeframe']}: {run['error']}")
        return
    print(f"\n\033[1m{run['symbol']} {run['timeframe']}\033[0m "
          f"({run['candles']} candles, {run['firstClose']:g} -> {run['lastClose']:g})")
    print(f"  {'strategy':<18}{'trades':>7}{'win%':>7}{'exp.R':>8}{'payoff':>8}{'totalR':>9}{'open':>6}")
    for s in run["strategies"]:
        payoff = f"{s['payoff']:.2f}" if s["payoff"] is not None else "  -"
        colour = "\033[32m" if s["expectancy_r"] > 0 else "\033[31m" if s["expectancy_r"] < 0 else ""
        print(f"  {s['strategy']:<18}{s['trades']:>7}{s['win_rate']*100:>6.0f}%"
              f"{colour}{s['expectancy_r']:>8.3f}\033[0m{payoff:>8}{s['total_r']:>9.2f}{s['open_at_end']:>6}")


def _aggregate(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pool each strategy's trades across every symbol/timeframe. The per-market
    numbers are thin; the pooled expectancy is the one worth ranking on."""
    agg: Dict[str, Dict[str, float]] = {}
    for run in runs:
        for s in run.get("strategies", []):
            a = agg.setdefault(s["strategy"], {"trades": 0, "wins": 0, "losses": 0, "totalR": 0.0})
            a["trades"] += s["trades"]
            a["wins"] += s["wins"]
            a["losses"] += s["losses"]
            a["totalR"] += s["total_r"]
    rows = []
    for name, a in agg.items():
        trades = int(a["trades"])
        rows.append({
            "strategy": name,
            "trades": trades,
            "wins": int(a["wins"]),
            "winRate": round(a["wins"] / trades, 4) if trades else 0.0,
            "totalR": round(a["totalR"], 3),
            "expectancyR": round(a["totalR"] / trades, 4) if trades else 0.0,
        })
    rows.sort(key=lambda r: r["expectancyR"], reverse=True)
    return rows


async def main_async(symbols: List[str], timeframes: List[str], limit: int) -> int:
    runs: List[Dict[str, Any]] = []
    for symbol in symbols:
        for tf in timeframes:
            print(f"  fetching {symbol} {tf} ({limit} candles)...", flush=True)
            try:
                runs.append(await _run_one(symbol, tf, limit))
            except Exception as exc:  # noqa: BLE001
                runs.append({"symbol": symbol, "timeframe": tf, "error": f"{type(exc).__name__}: {exc}"})

    for run in runs:
        _print_table(run)

    pooled = _aggregate(runs)
    print("\n\033[1mPOOLED across every symbol/timeframe (rank by expectancy in R)\033[0m")
    print(f"  {'strategy':<18}{'trades':>7}{'win%':>7}{'exp.R':>8}{'totalR':>9}")
    for r in pooled:
        colour = "\033[32m" if r["expectancyR"] > 0 else "\033[31m" if r["expectancyR"] < 0 else ""
        print(f"  {r['strategy']:<18}{r['trades']:>7}{r['winRate']*100:>6.0f}%"
              f"{colour}{r['expectancyR']:>8.3f}\033[0m{r['totalR']:>9.2f}")

    # STORE IT — the whole point of the exercise.
    date = dt.date.today().isoformat()
    out_dir = ROOT / "backtests" / date
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "riskModel": {"stopMult": ATR_STOP_MULTIPLIER, "targetMult": ATR_TARGET_MULTIPLIER,
                      "source": "backend/core/risk_manager.py"},
        "symbols": symbols, "timeframes": timeframes, "candlesPerSeries": limit,
        "runs": runs,
        "pooled": pooled,
        "caveats": [
            "Gross of fees and slippage. A thin positive expectancy is break-even net.",
            "In-sample over the fetched window. Evidence, not proof.",
            "Ambiguous bars assume the stop filled first (conservative).",
            "MIN_SAMPLE in strategy_performance still governs live promotion, on REAL closes.",
        ],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  Stored: {summary_path.relative_to(ROOT)}")

    negative = [r["strategy"] for r in pooled if r["trades"] >= 20 and r["expectancyR"] < 0]
    if negative:
        print(f"\n  \033[33mNet-negative over >=20 trades (candidates to down-weight): "
              f"{', '.join(negative)}\033[0m")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    print(f"Backtesting {len(STRATEGY_FUNCTIONS)} strategies over "
          f"{len(symbols)} symbol(s) x {len(timeframes)} timeframe(s), "
          f"risk {ATR_STOP_MULTIPLIER}x/{ATR_TARGET_MULTIPLIER}x ATR")
    return asyncio.run(main_async(symbols, timeframes, args.limit))


if __name__ == "__main__":
    sys.exit(main())
