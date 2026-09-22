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

# ---------------------------------------------------------------------------
# THE GRAPH CHECKPOINT STORE — the one thing here that filled a disk
# ---------------------------------------------------------------------------
#
# `.data/graph_checkpoints.sqlite` reached 43 MB on a development machine and
# 100% of the disk on the production server. Nothing pruned it, and nothing
# could: the pruning above operates on Postgres, and this is a SQLite file
# LangGraph owns.
#
# WHY IT GROWS SO FAST. The monitoring graph checkpoints, and it runs ONCE PER
# TICK PER OPEN POSITION. Each checkpoint serialises the whole graph state —
# which includes the candle arrays (120 bars x 3 timeframes, plus the benchmark's)
# and the order book. Measured locally: 619 checkpoints + 3,562 writes = 43.3 MB,
# roughly 10 KB per row. A position held for an hour at a 2-second tick writes
# hundreds of them, and the rows for a position CLOSED LAST WEEK are still there.
#
# WHY NOT JUST MOVE IT TO POSTGRES. That was the obvious idea and it is worse:
# the Supabase free tier is 500 MB, so relocating an unbounded store moves the
# outage from the disk to the database, where it also takes the trade history
# down with it. The store needs a BOUND, and it needs one wherever it lives.
#
# TWO BOUNDS, AND BOTH ARE NEEDED:
#
#   PER THREAD   LangGraph resumes from the LATEST checkpoint of a thread. Older
#                ones are history, not function. Keeping a handful preserves the
#                ability to inspect how the reasoning moved without keeping every
#                tick of it.
#
#   BY AGE       A thread is keyed on a position. Once that position closes the
#                thread is never resumed again, so its rows are pure residue —
#                and residue is most of the file. Age is the right bound because
#                this store has no view of which positions are still open (it is
#                a different database), and a position open longer than the
#                window keeps writing fresh checkpoints anyway, so its newest
#                ones survive regardless.
CHECKPOINTS_PER_THREAD = 5
CHECKPOINT_MAX_AGE_DAYS = 3


def prune_checkpoints() -> Dict[str, Any]:
    """Bound the LangGraph checkpoint file. Never raises.

    SYNCHRONOUS and using sqlite3 directly rather than the async saver, because
    this is maintenance on a file rather than part of any graph run — going
    through LangGraph's API would mean holding its connection while deleting the
    rows underneath it.

    VACUUM is what actually returns the space. SQLite marks deleted pages free
    but does not shrink the file, so a prune without it reports thousands of rows
    removed while the disk stays exactly as full — which is the bug report this
    would otherwise generate.
    """
    import os
    import sqlite3

    from backend.graphs.runtime import SQLITE_CHECKPOINT_PATH

    path = SQLITE_CHECKPOINT_PATH
    if not os.path.exists(path):
        return {"ok": True, "skipped": "no checkpoint file"}

    before = os.path.getsize(path)
    try:
        conn = sqlite3.connect(path, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"could not open the checkpoint store: {exc}"}

    deleted = 0
    try:
        with conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "checkpoints" not in tables:
                return {"ok": True, "skipped": "no checkpoints table"}

            # Threads whose newest checkpoint is older than the window. Ordering
            # by rowid rather than a timestamp column because LangGraph's schema
            # has varied across versions and rowid is monotonic in insert order
            # regardless — the newest row of a thread always has its highest
            # rowid.
            cutoff = conn.execute(
                "SELECT MAX(rowid) FROM checkpoints"
            ).fetchone()[0] or 0
            # Approximate the age window by row position: keep everything in the
            # most recent slice, prune whole threads that have nothing in it.
            keep_from = max(0, cutoff - (CHECKPOINTS_PER_THREAD * 2000))

            stale = [
                r[0] for r in conn.execute(
                    "SELECT thread_id FROM checkpoints GROUP BY thread_id "
                    "HAVING MAX(rowid) < ?", (keep_from,)
                )
            ]
            for thread in stale:
                for table in ("writes", "checkpoints"):
                    if table in tables:
                        cur = conn.execute(
                            f"DELETE FROM {table} WHERE thread_id = ?", (thread,)
                        )
                        deleted += cur.rowcount or 0

            # Then trim each surviving thread to its most recent checkpoints.
            for (thread,) in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ).fetchall():
                keep = [
                    r[0] for r in conn.execute(
                        "SELECT rowid FROM checkpoints WHERE thread_id = ? "
                        "ORDER BY rowid DESC LIMIT ?",
                        (thread, CHECKPOINTS_PER_THREAD),
                    )
                ]
                if not keep:
                    continue
                cur = conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ? AND rowid < ?",
                    (thread, min(keep)),
                )
                deleted += cur.rowcount or 0

            # ORPHANED WRITES. `writes` holds one row per channel written per
            # superstep and is by far the larger table — 3,562 rows against 619
            # checkpoints locally, and 3,175 of them belonged to checkpoints that
            # had just been deleted. Trimming only `checkpoints` freed a third of
            # the file and left the rest as unreachable residue, which is the
            # version of this fix that looks like it worked and does not.
            if "writes" in tables:
                cur = conn.execute(
                    "DELETE FROM writes WHERE NOT EXISTS ("
                    "  SELECT 1 FROM checkpoints c"
                    "  WHERE c.thread_id = writes.thread_id"
                    "    AND c.checkpoint_id = writes.checkpoint_id)"
                )
                deleted += cur.rowcount or 0

        # OUTSIDE the transaction: VACUUM cannot run inside one.
        conn.execute("VACUUM")
    except Exception as exc:  # noqa: BLE001
        logger.error("Checkpoint prune failed: %s", exc)
        return {"ok": False, "reason": str(exc), "deleted": deleted}
    finally:
        conn.close()

    after = os.path.getsize(path)
    freed = before - after
    if deleted or freed:
        logger.info(
            "Checkpoint store pruned: %d row(s) removed, %.1f MB -> %.1f MB (freed %.1f MB).",
            deleted, before / 1e6, after / 1e6, freed / 1e6,
        )
    return {
        "ok": True, "deleted": deleted,
        "bytesBefore": before, "bytesAfter": after, "bytesFreed": freed,
    }


async def prune_once() -> Dict[str, Any]:
    """One retention pass. Never raises — returns what it did, or why it could not."""
    from backend.core.db import get_db_pool

    # THE CHECKPOINT STORE IS PRUNED FIRST, AND UNCONDITIONALLY.
    #
    # Before the Postgres check, deliberately: that file is on local disk and
    # filled the production server to 100%, and it must still be bounded on a
    # deployment running without a database pool. Returning early on "no pool"
    # would leave the one store that actually caused an outage unpruned.
    checkpoints = prune_checkpoints()

    pool = get_db_pool()
    if pool is None:
        return {"ok": False, "reason": "no database pool", "checkpoints": checkpoints}

    report: Dict[str, Any] = {"ok": True, "checkpoints": checkpoints}

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
