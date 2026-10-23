"""How often does the agent correctly decide to do nothing?

THE ONE ROW OF THE REVIEW'S TABLE WITH NOTHING BEHIND IT
=========================================================
    Area        What the agent should measure
    Abstention  How often the agent correctly rejects weak or conflicting
                opportunities

Every other row had something: regime and volatility are computed, slippage
and partial fills land in `execution_quality`, expectancy and win rate come out
of `strategy_performance`. Abstention had nothing — and on this system it is
arguably the most important of the six, because the agent declines far more
often than it trades. 1,995 rejections against a handful of fills was the live
ratio when the scope gate was found.

WHY "HOW OFTEN IT TRADES" IS THE WRONG NUMBER, AND WHY THIS IS NOT THAT
========================================================================
The obvious reading of a low trade count is that the agent is broken, and this
project has twice gone looking for a fault that was not there — 10,001 cycles
on a PAUSED system, and 46 of 50 decisions refused at a confidence of 0.00-0.12
against a 0.60 floor. Both times the answer was that the agent was working.

So this measures the SHAPE of the abstention, not its size:

    WHICH GATE          a funnel over `rejection_reasons`. "The agent did not
                        trade" is not a finding; "88% of refusals were the
                        confidence floor and 2% were the venue minimum" is,
                        and they have completely different remedies.
    HOW DECISIVE        a refusal at 0.05 against a 0.60 bar is not a close
                        call, and treating it as one is what sends an operator
                        to lower a threshold that was never the binding
                        constraint.
    WAS IT RIGHT        only answerable where the system later traded the same
                        instrument. See below — this is the honest part.

WHAT THIS CANNOT DO, STATED FIRST SO NOBODY READS MORE INTO IT
===============================================================
IT CANNOT SAY WHETHER A REFUSED TRADE WOULD HAVE LOST. That needs the forward
price path for a trade that was never taken, simulated under the same stop and
target — and this module deliberately does NOT do that, for two reasons:

  1. It would be a counterfactual backtest run on the live path, with all the
     in-sample flattery `strategy_priors` already warns about, and it would be
     reported beside real measurements where a reader could not tell them
     apart. `backtests/` is where a counterfactual belongs.
  2. The refusals this system makes are overwhelmingly NOT close calls (see
     above), so the interesting quantity is which gate is binding, not how a
     coin-flip resolved.

What it reports instead is a DECISION FUNNEL and the distribution of margins.
That is a measurement, not an estimate, and it answers the question an operator
actually has: "is it refusing for the reasons I would refuse for?"

DETERMINISTIC AND READ-ONLY. It reads `decisions`, computes arithmetic, and
makes no model call — a test asserts that, the same invariant-5 argument
`strategy_performance` and `strategy_priors` both make. It cannot change a
threshold, a strategy or a position.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How many recent decisions to summarise. Large enough that a quiet hour does
# not dominate, small enough that the answer describes the CURRENT
# configuration rather than one three changes ago.
WINDOW = 500

CACHE_TTL_S = 60.0
_cache: Dict[str, Any] = {"at": 0.0, "data": None}

# The outcomes that mean "the agent chose not to act". `rejected-by-risk` is
# the CRO's veto and `rejected` is everything upstream of it; both are
# abstentions and they are counted separately because they are different
# decisions by different components.
ABSTAINED = ("rejected", "rejected-by-risk")

# Which gate a refusal came from, matched against the reason text. Ordered:
# the FIRST pattern that matches wins, because a reason can mention more than
# one concept and the earlier entries are the more specific.
#
# A reason matching nothing lands in "other" and is REPORTED with an example
# rather than silently folded into a neighbouring bucket — a taxonomy that
# quietly absorbs what it does not recognise is how a new failure mode stays
# invisible.
_GATES: Tuple[Tuple[str, str], ...] = (
    ("venue-minimum", r"minimum|min notional|too small|below the venue"),
    ("tradeable-universe", r"may be watched|not tradeable|untradeable"),
    ("strategy-approval", r"not approved for real money"),
    ("entry-proximity", r"chasing an extended"),
    ("counter-trend", r"counter-trend|higher[- ]timeframe|htf"),
    ("fee-vs-edge", r"fee|round trip"),
    ("volatility-regime", r"volatility regime|volatility is"),
    ("confidence-floor", r"confidence|conviction|panel reads|do.?not.?trade"),
    ("position-limit", r"already hold|concurrent|position limit|one position"),
    ("daily-loss", r"daily loss|drawdown|halt"),
    ("var-limit", r"var limit|value at risk|global var"),
    ("liquidity-depth", r"depth|liquidity|slippage cap"),
    ("no-setup", r"no setup|no thesis|nothing worth|below the .* minimum"),
    ("scope", r"scope|not this session|session symbol"),
    ("stale-data", r"stale|no live price|feed"),
)

_COMPILED = tuple((name, re.compile(pattern, re.I)) for name, pattern in _GATES)

# Pulls a measured confidence and the bar it was compared against out of a
# refusal, so "how close was it" is a number rather than an impression.
_MARGIN = re.compile(
    r"(\d*\.?\d+)\s*(?:confidence)?\s*(?:against|below|vs\.?|under)\s*(?:a|the)?\s*"
    r"(\d*\.?\d+)",
    re.I,
)


def classify(reason: str) -> str:
    """Which gate refused this trade. Pure, so it is testable without a database."""
    text = (reason or "").strip()
    if not text:
        return "unstated"
    for name, pattern in _COMPILED:
        if pattern.search(text):
            return name
    return "other"


def margin(reason: str) -> Optional[float]:
    """How far short the refusal was, as (bar - measured). None when unstated.

    None is NOT zero. A refusal whose margin cannot be read is not a tie, and
    averaging it in as one would make the agent look like it is refusing on
    coin flips when the live distribution is nothing of the kind.
    """
    match = _MARGIN.search(reason or "")
    if not match:
        return None
    try:
        measured = float(match.group(1))
        bar = float(match.group(2))
    except (TypeError, ValueError):
        return None
    if bar <= 0 or bar > 1.5 or measured < 0 or measured > 1.5:
        # Not a confidence pair -- the regex caught two unrelated numbers.
        # Refused rather than reported, because a fabricated margin is worse
        # than an absent one.
        return None
    return round(bar - measured, 4)


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The funnel and the margins, from rows already in hand.

    Separated from the query so the arithmetic is testable with no database --
    the same split `_compare` uses in `reconciliation`, and for the same
    reason: the interesting logic is not the I/O.
    """
    total = len(rows)
    acted = 0
    abstained = 0
    by_gate: Dict[str, int] = {}
    examples: Dict[str, str] = {}
    margins: List[float] = []

    for row in rows:
        outcome = str(row.get("outcome") or "")
        if outcome.startswith("approved") or outcome == "manually-approved":
            acted += 1
            continue
        if outcome not in ABSTAINED:
            continue
        abstained += 1

        reasons = row.get("rejection_reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        text = "; ".join(str(r) for r in reasons if r)
        gate = classify(text)
        by_gate[gate] = by_gate.get(gate, 0) + 1
        # ONE EXAMPLE PER GATE, kept so "other" and "unstated" can be chased
        # rather than merely counted. A bucket with no example is a bucket
        # nobody can act on.
        if gate not in examples and text:
            examples[gate] = text[:300]

        gap = margin(text)
        if gap is not None:
            margins.append(gap)

    ranked = sorted(by_gate.items(), key=lambda kv: kv[1], reverse=True)
    near_misses = [m for m in margins if 0 <= m <= 0.05]

    return {
        "window": total,
        "acted": acted,
        "abstained": abstained,
        # The share of DECISIONS declined. Reported as a fraction with the
        # denominator beside it, because a rate with no sample size is how a
        # quiet hour reads as a change in behaviour.
        "abstentionRate": round(abstained / total, 4) if total else None,
        "byGate": [{"gate": g, "count": c, "example": examples.get(g)} for g, c in ranked],
        "bindingGate": ranked[0][0] if ranked else None,
        # HOW DECISIVE THE REFUSALS WERE. The live distribution is not close
        # calls -- 46 of 50 decisions ran 0.00-0.12 against a 0.60 floor -- and
        # knowing that is what stops an operator lowering a threshold that was
        # never the binding constraint.
        "marginsMeasured": len(margins),
        "medianMargin": (
            round(sorted(margins)[len(margins) // 2], 4) if margins else None
        ),
        "nearMisses": len(near_misses),
        "note": (
            "This measures WHICH gate is binding and HOW decisive the refusals "
            "were. It does NOT estimate whether a refused trade would have lost: "
            "that needs a counterfactual on a path never taken, which belongs in "
            "backtests/ where its in-sample caveats travel with it, not beside "
            "real measurements where a reader cannot tell them apart."
        ),
    }


async def _load(window: int = WINDOW) -> Optional[List[Dict[str, Any]]]:
    """Recent decisions, or None when the database could not be read.

    None, never [] -- "could not ask" and "there were no decisions" are
    different facts and this codebase has paid for conflating them five times.
    """
    try:
        from backend.core.db import get_db_pool

        pool = await get_db_pool()
        if pool is None:
            return None
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT outcome, rejection_reasons, ts
                FROM decisions
                ORDER BY ts DESC
                LIMIT $1
                """,
                window,
            )
        out: List[Dict[str, Any]] = []
        for r in rows:
            reasons = r["rejection_reasons"]
            if isinstance(reasons, str):
                try:
                    import json

                    reasons = json.loads(reasons)
                except Exception:  # noqa: BLE001
                    reasons = [reasons]
            out.append({"outcome": r["outcome"], "rejection_reasons": reasons})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("Abstention metrics could not read decisions: %s", exc)
        return None


async def metrics(force: bool = False) -> Dict[str, Any]:
    """The abstention funnel, cached. Never raises."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_TTL_S:
        return _cache["data"]

    rows = await _load()
    if rows is None:
        # UNREADABLE IS NOT "NEVER ABSTAINED". Reporting zeros here would read
        # as an agent that takes every trade it sees, which is the opposite of
        # what is true and the more alarming of the two misreadings.
        return {
            "window": 0,
            "available": False,
            "note": (
                "the decisions table could not be read, so abstention could not be "
                "measured. This is NOT a report that the agent never declines a trade."
            ),
        }

    data = {"available": True, **summarise(rows)}
    _cache.update({"at": now, "data": data})
    return data


def reset_cache() -> None:
    """Test hook, and after a reset so a cleared table is not still cached."""
    _cache.update({"at": 0.0, "data": None})
