"""Does refusing a LATE entry make money? Measured, and the answer is no.

THE HYPOTHESIS, AND WHERE IT CAME FROM
======================================
An external review's Section 6C: *"Avoid chasing an extended candle where the
stop is too far away or the reward-to-risk ratio has deteriorated."* And this
system's own `strategy_profiles.Breakout`, which has named it as that
strategy's worst condition since it was written:

    worst_conditions="A market already extended in the breakout direction —
                      the move is over."

Both are plausible and both agree. `algorithms/entry_extension` measures how
far price has already travelled, in ATR, from the swing the move began at, and
`risk_gateway` can refuse an entry past `MAX_ENTRY_EXTENSION_ATR` (3.0).

THE MEASUREMENT SAYS THE OPPOSITE
=================================
17,157 signals, six instruments, 1,000 real 15m candles each, every strategy in
`STRATEGY_FUNCTIONS`:

    every signal          -0.0182R +/-0.0211   32.7%   17,157 trades
    the gate would KEEP   -0.0503R +/-0.0259   31.7%   11,144 trades
    the gate would BLOCK  +0.0412R +/-0.0361   34.7%    6,013 trades

    gap = -0.0915R, z = -4.04, surviving a correction for the comparisons made.
    `evidence.required_gap(6013)` = 0.0371R, so the gap is 2.5x the smallest
    one this sample could detect.

**THE GATE BLOCKS THE BETTER HALF.** Entering 3+ ATR into a move is momentum,
and on these instruments momentum is the thing that has worked — the same
ledger that shows the range and mean-reversion strategies losing, and that
showed the HTF gate removing a genuinely losing half.

So `REQUIRE_ENTRY_PROXIMITY` defaults OFF. That is the finding, not a failure
to build the gate. The result ran AGAINST the author's hypothesis, which is the
only direction this kind of result is worth much: `core/evidence.py` exists
because a throwaway script once turned three noise readings into "findings",
all of them in the direction the author was hoping for.

NO LOOKAHEAD, THE SAME DESIGN AS `measure_htf_gate.py`
======================================================
The gate is evaluated on what it could SEE at entry: the REAL
`entry_extension.assess`, on bars up to and INCLUDING the entry bar, with the
ATR computed from the same prefix. A gate measured by a copy of itself measures
the copy. Outcomes are walked forward under the live risk model
(`ATR_STOP_MULTIPLIER` imported, never restated) and an ambiguous bar — one
containing both the stop and the target — is resolved AS THE STOP, the same
conservative rule `strategy_backtest` applies.

WHAT THIS IS NOT
================
A replay of the DETERMINISTIC signal functions, not of the nine-specialist
panel, which needs an order book, a tape and news and cannot be replayed
offline. It says the premise is wrong on the strategies that can be measured.
It does NOT license the opposite gate — "prefer late entries" is a second
hypothesis and would need its own test.

Public Binance klines, no key, no database. Run it and it prints the table
above (within whatever the market has done since).
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.getcwd())
# TEST-NET-1. Nothing here touches a database, and a `.env` carrying the live
# Supabase URL must not make that accidentally untrue — the rule
# `full_system_exercise.py` learned after a harness left 11 rows in the
# operator's live `trades`.
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@192.0.2.1:5432/none")

SYMBOLS = ("SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "ETHUSDT", "BNBUSDT")
INTERVAL = "15m"
LIMIT = 1000
WINDOW = 120          # the trailing window each signal function is given
MAX_FORWARD = 200     # bars to wait for a resolution before dropping the trade
ATR_PERIOD = 14


def klines(symbol: str) -> List[Dict[str, Any]]:
    url = (
        f"https://api.binance.com/api/v3/klines?symbol={symbol}"
        f"&interval={INTERVAL}&limit={LIMIT}"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = json.load(response)
    return [
        {
            "openTime": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
        }
        for k in raw
    ]


def atr(bars: Sequence[Dict[str, Any]], period: int = ATR_PERIOD) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        trs.append(max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        ))
    return (sum(trs) / len(trs)) if trs else None


def outcome_r(
    bars: Sequence[Dict[str, Any]], start: int, direction: str,
    entry: float, stop: float, target: float,
) -> Optional[float]:
    """+2R at the target, -1R at the stop, None if neither is reached.

    AN AMBIGUOUS BAR RESOLVES AS THE STOP. Without intrabar data there is no
    way to know which came first, and assuming the target is how a simulator
    flatters itself. Applied to both groups identically, so it cannot create
    the difference being measured.
    """
    for j in range(start + 1, min(start + MAX_FORWARD, len(bars))):
        high, low = bars[j]["high"], bars[j]["low"]
        if direction == "LONG":
            if low <= stop:
                return -1.0
            if high >= target:
                return 2.0
        else:
            if high >= stop:
                return -1.0
            if low <= target:
                return 2.0
    return None


def describe(label: str, rs: List[float]) -> str:
    if not rs:
        return f"{label:<22} n=0"
    n = len(rs)
    mean = sum(rs) / n
    wins = sum(1 for r in rs if r > 0)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / n)
    se = sd / math.sqrt(n)
    return (
        f"{label:<22} n={n:6d}  expectancy={mean:+.4f}R +/-{1.96 * se:.4f}  "
        f"win={wins / n * 100:.1f}%"
    )


def main() -> int:
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS
    from backend.algorithms.entry_extension import assess, max_extension_atr
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER

    kept: List[float] = []
    blocked: List[float] = []

    for symbol in SYMBOLS:
        try:
            bars = klines(symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: could not fetch candles ({exc})")
            continue

        for name, fn in STRATEGY_FUNCTIONS.items():
            i = WINDOW
            while i < len(bars) - 1:
                try:
                    signal = (fn(bars[max(0, i - WINDOW):i + 1]) or "").upper()
                except Exception:  # noqa: BLE001
                    i += 1
                    continue
                if signal not in ("BUY", "SELL", "LONG", "SHORT"):
                    i += 1
                    continue

                direction = "LONG" if signal in ("BUY", "LONG") else "SHORT"
                a = atr(bars[:i + 1])
                if not a:
                    i += 1
                    continue

                entry = bars[i]["close"]
                if direction == "LONG":
                    stop = entry - ATR_STOP_MULTIPLIER * a
                    target = entry + ATR_TARGET_MULTIPLIER * a
                else:
                    stop = entry + ATR_STOP_MULTIPLIER * a
                    target = entry - ATR_TARGET_MULTIPLIER * a

                # THE REAL FUNCTION, on the real prefix. A gate measured by a
                # re-implementation measures the re-implementation.
                verdict = assess(
                    direction, bars[:i + 1],
                    entry_price=entry, stop_loss=stop, atr=a,
                )

                r = outcome_r(bars, i, direction, entry, stop, target)
                if r is None:
                    i += 1
                    continue

                (blocked if verdict.chasing else kept).append(r)
                i += 1

        print(f"  {symbol:<9} kept={len(kept):6d}  blocked={len(blocked):6d}")

    print()
    print(f"Limit: {max_extension_atr():.1f} ATR of travel before an entry counts as chasing")
    print(describe("every signal", kept + blocked))
    print(describe("the gate KEEPS", kept))
    print(describe("the gate BLOCKS", blocked))

    if kept and blocked:
        ek = sum(kept) / len(kept)
        eb = sum(blocked) / len(blocked)
        sk = math.sqrt(sum((r - ek) ** 2 for r in kept) / len(kept)) / math.sqrt(len(kept))
        sb = math.sqrt(sum((r - eb) ** 2 for r in blocked) / len(blocked)) / math.sqrt(len(blocked))
        z = (ek - eb) / math.sqrt(sk ** 2 + sb ** 2)
        print()
        print(f"kept vs blocked : gap={ek - eb:+.4f}R   z={z:+.2f}")
        print(f"share removed   : {len(blocked) / (len(kept) + len(blocked)) * 100:.1f}%")
        try:
            from backend.core.evidence import required_gap

            print(f"smallest gap this sample could detect: {required_gap(len(blocked)):.4f}R")
        except Exception:  # noqa: BLE001
            pass
        print()
        if z < 0:
            print(
                "READ THIS AS IT IS. A NEGATIVE z means the gate removes the BETTER\n"
                "half: late entries outperformed early ones here, so refusing them\n"
                "costs money. REQUIRE_ENTRY_PROXIMITY defaults OFF for that reason.\n"
                "This replays the DETERMINISTIC signal functions, not the\n"
                "nine-specialist panel, which cannot be replayed offline."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
