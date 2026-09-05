"""Realised performance per strategy — the feedback the agent never had.

WHAT WAS MISSING
================
All nine profiles in `algorithms/strategy_profiles.py` carry
`historical_success_rate=None`, and the scoring node reports it on every run:

    strategy scoring excludes historical success rate: no profile has been
    validated on this system's own data (all 9 carry historical_success_rate=None),
    so scores reflect current-conditions fit only

So the agent chose strategies purely on how well they FIT current conditions, and
nothing it learned from an outcome ever reached that choice. It had a reflection
layer, a hypothesis layer and a memory system, and none of them closed the one
loop that matters: which of my strategies actually makes money.

Two things blocked it, and both are fixed:

  1. `trades` did not record WHICH STRATEGY produced a fill, so no outcome could
     be attributed to one. `trades.strategy` now exists and the execution agent
     writes it.
  2. Nothing aggregated. This module does.

WHY THIS IS NOT A VIOLATION OF INVARIANT 5
==========================================
CLAUDE.md: *"Learning never auto-deploys. Reflection -> Hypothesis produces
understanding... Nothing in `lib/hypothesis*` or `lib/curiosityEngine.ts` may
write to production risk config or strategy selection."*

That invariant is about an LLM-authored HYPOTHESIS rewriting a strategy —
`Loss -> AI rewrites strategy -> Live`. This is a different thing and stays on the
right side of it, deliberately:

  * It is DETERMINISTIC ARITHMETIC over closed trades. No model is consulted, no
    text is interpreted, and the same ledger always produces the same numbers.
  * It cannot invent, edit or disable a strategy. It only supplies a measured
    win rate that scoring weights — the field `strategy_profiles` already
    declares and Section 11.3 already lists as required for a score.
  * It never touches risk config, leverage, stops or sizing.

The distinction is between the system COUNTING ITS OWN RESULTS and a model
rewriting its own rules. This is the first.

THE SAMPLE FLOOR IS THE WHOLE SAFETY ARGUMENT
=============================================
A win rate over three trades is noise wearing a percentage sign, and feeding it
into selection would let one lucky sequence entrench a bad strategy — the exact
failure mode that makes naive backtest-driven systems dangerous. Below
`MIN_SAMPLE` the rate is reported but explicitly NOT usable, and the scorer
excludes it and says so, exactly as it does today.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Below this many closed trades a strategy's win rate is not used for scoring.
#
# 20 is not a tuned number and is not defended as statistically sufficient — it
# is the point below which a single run of luck dominates the estimate. At 20
# trades a 50%-true strategy still shows anywhere from ~30% to ~70% one time in
# twenty, so this is a floor against nonsense, not a claim of significance.
MIN_SAMPLE = 20

# Cached because the scorer runs on every analysis cycle and this is a table scan
# whose answer changes only when a position closes.
CACHE_TTL_S = 300.0

_cache: Dict[str, Any] = {"at": 0.0, "data": None}


async def _load() -> Optional[Dict[str, Dict[str, Any]]]:
    """Realised stats per strategy, or None when the database cannot be read.

    None, not `{}`: "no database" and "no strategy has ever traded" are different
    facts, and only the second justifies scoring without history.
    """
    from backend.core.db import get_db_pool

    pool = get_db_pool()
    if pool is None:
        return None

    try:
        async with pool.acquire() as conn:
            # Only CLOSED trades carry a realised pnl, and only rows tagged with a
            # strategy can be attributed. Both filters are the point: an opening
            # fill has no outcome yet, and an untagged one belongs to no strategy.
            rows = await conn.fetch(
                """
                SELECT strategy,
                       count(*)                                     AS n,
                       count(*) FILTER (WHERE pnl > 0)              AS wins,
                       coalesce(sum(pnl), 0)                        AS total_pnl,
                       coalesce(avg(pnl) FILTER (WHERE pnl > 0), 0) AS avg_win,
                       coalesce(avg(pnl) FILTER (WHERE pnl <= 0), 0) AS avg_loss
                  FROM trades
                 WHERE pnl IS NOT NULL AND strategy IS NOT NULL
                 GROUP BY strategy
                """
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not read strategy performance: %s", exc)
        return None

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        n = int(r["n"])
        wins = int(r["wins"])
        avg_loss = abs(float(r["avg_loss"]))
        avg_win = float(r["avg_win"])
        win_rate = wins / n if n else 0.0
        out[r["strategy"]] = {
            "strategy": r["strategy"],
            "sampleSize": n,
            "wins": wins,
            "losses": n - wins,
            "winRate": win_rate,
            "totalPnl": float(r["total_pnl"]),
            "avgWin": avg_win,
            "avgLoss": avg_loss,
            # Expectancy per trade in dollars. The number that decides whether a
            # strategy is worth running, and it is NOT the win rate: 33% at 2:1
            # and 60% at 0.5:1 are both roughly break-even.
            "expectancy": win_rate * avg_win - (1 - win_rate) * avg_loss,
            # Usable for SCORING only past the floor. Reported either way, so an
            # operator can see a young strategy's record without it steering
            # selection.
            "usable": n >= MIN_SAMPLE,
        }
    return out


async def performance(force: bool = False) -> Optional[Dict[str, Dict[str, Any]]]:
    """Cached per-strategy stats. None when the database could not be read."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_TTL_S:
        return _cache["data"]

    data = await _load()
    if data is not None:
        _cache.update({"at": now, "data": data})
    return data


async def success_rate(strategy: str) -> Optional[float]:
    """A strategy's realised win rate, or None when it may not be used.

    None covers three genuinely different cases and the caller treats them the
    same way — score without history:

        the database could not be read
        this strategy has never closed a trade
        it has, but fewer than MIN_SAMPLE

    They are collapsed here because the SCORING decision is identical in all
    three; `performance()` keeps them apart for anything that wants to report.
    """
    data = await performance()
    if not data:
        return None
    entry = data.get(strategy)
    if entry is None or not entry["usable"]:
        return None
    return float(entry["winRate"])


def reset_cache() -> None:
    """Test hook, and used after a reset so a cleared ledger is not still cached."""
    _cache.update({"at": 0.0, "data": None})


async def summary() -> Dict[str, Any]:
    """Everything, for the API and the operator. Never raises."""
    data = await performance()
    if data is None:
        return {
            "available": False,
            "reason": "the trade database could not be read",
            "minSample": MIN_SAMPLE,
            "strategies": [],
        }

    ranked = sorted(data.values(), key=lambda s: s["expectancy"], reverse=True)
    usable = [s for s in ranked if s["usable"]]
    return {
        "available": True,
        "minSample": MIN_SAMPLE,
        "strategies": ranked,
        "usableCount": len(usable),
        "meaning": (
            f"Realised results per strategy, from closed trades only. A strategy needs "
            f"{MIN_SAMPLE} closed trades before its win rate is allowed to influence "
            f"selection — below that the rate is noise and one lucky run would entrench "
            f"a bad strategy. Expectancy, not win rate, is what says whether a strategy "
            f"is worth running."
        ),
    }
