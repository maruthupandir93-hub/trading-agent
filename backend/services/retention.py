"""Retention — bound the tables that grow without limit.

WHY THIS EXISTS
===============
`decisions` reached 80,150 rows on the operator's database. The Decisions page
reads that table, so it loaded slowly and then laid out tens of thousands of rows,
and the whole page lagged. Nothing had ever deleted anything.

That is not a Decisions-page problem, it is a growth problem: the agent records a
decision on every evaluation, most of which are "considered and did not trade".
Those are worth having for a while and worthless forever.

WHAT IS PRUNED AND WHAT IS NOT — THE DISTINCTION IS THE WHOLE DESIGN
====================================================================
A row that led to a TRADE is evidence. A row that led to nothing is telemetry.

    KEPT INDEFINITELY   decisions whose outcome executed a trade, every
                        reflection, every row in `trades`
    PRUNED BY COUNT     non-executing decisions past the most recent MIN_KEEP

So the audit trail of "why did the agent take this position" survives forever,
while "the agent looked at SOL 4,000 times and declined" does not. Pruning by age
without that split would eventually remove the record behind a real position,
which is exactly the evidence a post-mortem needs.

WHY A COUNT AND NOT AN AGE
==========================
The first version of this used a 14-day window and would have deleted NOTHING on
the operator's database: all 80,525 rows were rejections from a single day. An age
window is sensitive to how hard the agent happened to be working, which is the one
thing the bound must not depend on. A count is both a floor — a quiet week cannot
blank the page — and a cap.

NEVER DELETES `trades`
======================
Not by age, not by volume. It is the P&L, the win rate and the tax record, it is
small (one row per fill, not per evaluation), and `learningDashboard` already
documents what happens when a closing row's opening leg disappears from the
window. If `trades` ever needs bounding, it needs archiving, not deletion.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# How many NON-EXECUTING decisions are kept. Both the floor and the cap — see
# the query for why an age window was the wrong bound here.
#
# 2,000 is roughly a day of this agent's declines: enough to answer "what has it
# been rejecting and why", small enough that the page renders instantly.
MIN_KEEP = 2_000

# Runs once an hour. Pruning is a DELETE over an indexed range; doing it per
# request would put a write on the path of a page load.
INTERVAL_S = 3600.0

# Outcomes that mean a trade actually happened. These are never pruned.
EXECUTED_OUTCOMES = ("approved-executed", "manually-approved")


async def prune_once() -> Dict[str, Any]:
    """One retention pass. Never raises — returns what it did, or why it could not."""
    from backend.core.db import get_db_pool

    pool = get_db_pool()
    if pool is None:
        return {"ok": False, "reason": "no database pool"}

    report: Dict[str, Any] = {"ok": True}

    try:
        async with pool.acquire() as conn:
            # DECISIONS — BOUNDED BY COUNT, NOT BY AGE.
            #
            # Age alone deleted nothing on the operator's database: all 80,525
            # rows were `rejected` and all were less than a day old, because the
            # agent records a decision on every evaluation it declines. A
            # 14-day rule would not have touched them for a fortnight while the
            # page stayed slow.
            #
            # So the bound is a COUNT: keep the most recent MIN_KEEP
            # non-executing decisions and delete the rest. That is both a floor
            # (a quiet week cannot empty the table) and a cap (a busy day cannot
            # grow it without limit), and unlike an age window it is not
            # sensitive to how hard the agent happened to be working.
            deleted = await conn.fetchval(
                f"""
                WITH keep AS (
                    SELECT id FROM decisions
                     WHERE outcome <> ALL($1::text[])
                     ORDER BY ts DESC
                     LIMIT {MIN_KEEP}
                ),
                doomed AS (
                    DELETE FROM decisions
                     WHERE outcome <> ALL($1::text[])
                       AND id NOT IN (SELECT id FROM keep)
                    RETURNING 1
                )
                SELECT count(*) FROM doomed
                """,
                list(EXECUTED_OUTCOMES),
            )
            report["decisionsDeleted"] = int(deleted or 0)
            report["decisionsRemaining"] = int(
                await conn.fetchval("SELECT count(*) FROM decisions") or 0
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Decision retention pass failed: %s", exc)
        report.update({"ok": False, "reason": str(exc)[:200]})

    if report.get("decisionsDeleted"):
        logger.warning(
            "Retention: deleted %s non-executing decision(s) beyond the most recent %s; "
            "%s remain. Decisions that executed a trade are never pruned.",
            report["decisionsDeleted"], MIN_KEEP, report.get("decisionsRemaining"),
        )

    return report


async def run_forever(interval_s: float = INTERVAL_S) -> None:
    """Prune on a timer. Started from `main.py`'s lifespan.

    Runs once shortly after startup as well as on the interval, because the
    operator's table is already large by the time this ships and waiting an hour
    to first act would leave the page slow for that hour.
    """
    import asyncio

    try:
        await asyncio.sleep(30)  # let startup finish first
        await prune_once()
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive
        logger.exception("First retention pass failed.")

    while True:
        try:
            await asyncio.sleep(interval_s)
            await prune_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            logger.exception("Retention pass failed; will retry on the next interval.")
