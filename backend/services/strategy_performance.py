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
import re
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


# How the regime is recovered from a closed trade. `trades.entry_context` is the
# snapshot the Risk Gateway wrote at decision time, and `regime=<name>,` is one
# of its fields -- the same text `learningDashboard.classifyEntryContext` and
# `lib/viz/entryContext.ts` already parse.
#
# A SEPARATE COLUMN WOULD BE THE BETTER HOME and is deliberately not built
# here: `entry_context` already carries this for every row the gateway wrote,
# including historical ones, and adding a column would leave every existing
# trade NULL -- so the regime split would report "no data" on exactly the
# ledger it exists to analyse. Parsing reads the rows that exist.
_REGIME_IN_CONTEXT = re.compile(r"regime=([^,]+)", re.I)


def regime_of(entry_context: Optional[str]) -> Optional[str]:
    """The market regime a trade was opened in, or None.

    None when the row predates the snapshot or the field is absent. NOT a
    default: attributing a trade to a regime it was not taken in would poison
    exactly the measurement this feeds, and "unknown" is a real answer here.
    """
    if not entry_context:
        return None
    match = _REGIME_IN_CONTEXT.search(str(entry_context))
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


async def by_regime() -> Optional[Dict[str, Dict[str, Dict[str, Any]]]]:
    """Realised stats per strategy, SPLIT BY THE REGIME THEY TRADED IN.

    THE REVIEW'S SECTION 6: *"Strategy performance -- net expectancy, profit
    factor, drawdown, win/loss size, and PERFORMANCE BY REGIME."* The pooled
    figure `performance()` returns is the right number for promotion and the
    wrong one for understanding: this project's own backtest shows the range
    strategies losing in a trending window, which is a statement about the
    WINDOW as much as about the strategies, and a pooled expectancy cannot
    express that.

    It does NOT feed selection. `_track_record_score` reads the pooled rate and
    continues to: a per-regime sample is `MIN_SAMPLE` divided by the number of
    regimes, and steering on a handful of trades is how naive adaptive systems
    destroy themselves. This is for reading.

    None when the database could not be read -- never `{}`.
    """
    from backend.core.db import get_db_pool

    pool = get_db_pool()
    if pool is None:
        return None

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT strategy, pnl, entry_context
                  FROM trades
                 WHERE pnl IS NOT NULL AND strategy IS NOT NULL
                """
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not read per-regime strategy performance: %s", exc)
        return None

    buckets: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        strategy = r["strategy"]
        regime = regime_of(r["entry_context"]) or "unrecorded"
        cell = buckets.setdefault(strategy, {}).setdefault(regime, {
            "sampleSize": 0, "wins": 0, "totalPnl": 0.0, "_wins": [], "_losses": [],
        })
        pnl = float(r["pnl"])
        cell["sampleSize"] += 1
        cell["totalPnl"] += pnl
        if pnl > 0:
            cell["wins"] += 1
            cell["_wins"].append(pnl)
        else:
            cell["_losses"].append(pnl)

    for strategy, regimes in buckets.items():
        for regime, cell in regimes.items():
            n = cell["sampleSize"]
            wins = cell["_wins"]
            losses = cell["_losses"]
            avg_win = (sum(wins) / len(wins)) if wins else 0.0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
            rate = (cell["wins"] / n) if n else 0.0
            cell["winRate"] = rate
            cell["avgWin"] = avg_win
            cell["avgLoss"] = avg_loss
            cell["expectancy"] = rate * avg_win - (1 - rate) * avg_loss
            # PROFIT FACTOR, the review's own term: gross wins over gross
            # losses. None rather than infinity when nothing was lost -- a
            # strategy with no losing trade yet has an UNMEASURED profit
            # factor, and rendering infinity reads as a perfect record.
            gross_loss = abs(sum(losses))
            cell["profitFactor"] = (
                round(sum(wins) / gross_loss, 4) if gross_loss > 0 else None
            )
            # Below MIN_SAMPLE this cell describes a handful of trades. Said on
            # the cell itself, because a per-regime split is exactly where a
            # reader is most tempted to over-read three outcomes.
            cell["usable"] = n >= MIN_SAMPLE
            cell.pop("_wins", None)
            cell.pop("_losses", None)

    return buckets


def cached_performance() -> Optional[Dict[str, Dict[str, Any]]]:
    """The cached stats WITHOUT touching the database. None when nothing is cached.

    For callers inside a graph node. `validate_market_data` is the single fetch
    point (Section 39.4) and a node that reached for the database mid-run would
    not be replay-safe: a resumed checkpoint would reason over a different
    ledger than the original decision.

    None means "not cached", which is NOT "this strategy has no record" — the
    distinction this codebase has paid for in four other places. A caller that
    cannot tell must report that it could not tell.
    """
    return _cache["data"]


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
