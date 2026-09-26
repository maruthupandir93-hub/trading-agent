"""Erase the agent's accumulated trading data and start fresh.

WHY THIS IS A SCRIPT AND NOT A ONE-LINE TRUNCATE
================================================
Two things make a naive `DELETE FROM trades` ineffective, and both have bitten
this project before.

1. THE BACKEND MUST BE STOPPED FIRST. `portfolio_store` keeps the book in a
   module-level dict and `_persist()` REPLACES the stored rows from memory after
   every write; `PositionMonitorAgent` keeps its watch list in `self._open` and
   `save_watch_list` is a DELETE + re-INSERT of everything it is holding. So a
   truncate against a RUNNING backend is undone by the next fill — CLAUDE.md
   records this exact surprise ("the operator reset their database, and the
   dashboard still read 'Open positions: 1'").

2. SCHEMA BOOKKEEPING IS NOT DATA. `migrations` and `schema_migrations` record
   which schema steps have been applied. Wiping them does not reset anything
   useful; it makes the next `init_db` unable to tell what it has already done.

WHAT IS ERASED — the accumulated history and the books:
    trades, decisions, reflections, risk_events, execution_quality,
    monitored_positions, agent_positions, agent_paper_account,
    graph_traces (if present), autonomous_cycles (if present)

WHAT IS KEPT — configuration and schema:
    migrations, schema_migrations, watchlist, memory_prefs,
    trading_controls, paper_account

REFUSES WHILE LIVE_TRADING IS ON, and that refusal is the most important line
here: clearing `monitored_positions` stops watching without closing anything, so
on a real book it would abandon open positions at the venue with nothing
enforcing their stops. `api/admin.reset_paper` refuses for the same reason.

    python scripts/reset_database.py --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WIPE = (
    "trades", "decisions", "reflections", "risk_events", "execution_quality",
    "monitored_positions", "agent_positions", "agent_paper_account",
    "graph_traces", "autonomous_cycles", "debate_records", "hypotheses",
)
KEEP = ("migrations", "schema_migrations", "watchlist", "memory_prefs",
        "trading_controls", "paper_account")


async def wipe_everything(conn, confirm: bool) -> int:
    """TRUNCATE every table in `public`. A total reset, config included.

    SAFE TO DO BECAUSE `db/schema.sql` IS IDEMPOTENT AND RE-APPLIED ON EVERY
    STARTUP. `core/db.init_db()` runs the whole file each boot — every CREATE is
    `IF NOT EXISTS` and every seed row is `INSERT ... ON CONFLICT DO NOTHING` — so
    emptying `migrations` / `schema_migrations` costs nothing: the next start
    re-seeds them. That is exactly why this truncates rather than DROPs. Dropping
    would also work, but it leaves the database with no tables at all if the
    backend then fails to start for an unrelated reason, and an empty table is a
    far easier thing to be wrong about than a missing one.

    CASCADE because `decisions.trade_log_entry_id` references `trades`. Without
    it the statement fails on the foreign key and NOTHING is truncated, which
    reads as the script having silently done nothing.
    """
    tables = [
        r["tablename"]
        for r in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "ORDER BY tablename"
        )
    ]
    if not tables:
        print("No tables in `public`.")
        return 0

    print(f"FULL CLEAN — every table in `public` ({len(tables)}):")
    total = 0
    for t in tables:
        n = await conn.fetchval(f'SELECT count(*) FROM "{t}"')
        total += n
        if n:
            print(f"   {t:<28} {n:>8} rows")
    print(f"   {'':<28} {total:>8} rows total (tables not listed are already empty)")
    print()
    print("Schema is KEPT — `init_db` re-applies db/schema.sql on every startup,")
    print("so seed rows and migration bookkeeping come back on the next boot.")
    print()

    if not confirm:
        print("Dry run. Re-run with --confirm to actually erase.")
        return 0

    async with conn.transaction():
        await conn.execute(
            "TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"
        )
    print(f"ERASED {total} rows across {len(tables)} tables. Database is empty.")
    print("Start the backend — init_db will recreate seed data.")
    return 0


async def main(confirm: bool, starting_cash: float, everything: bool = False) -> int:
    from dotenv import load_dotenv

    load_dotenv(".env")
    import asyncpg

    if (os.getenv("LIVE_TRADING") or "").strip().lower() == "true":
        print("REFUSED: LIVE_TRADING=true.")
        print("  Clearing the watch list stops watching without CLOSING anything, so on a")
        print("  real book this abandons open positions at the venue with no stop enforced.")
        print("  Turn live trading off, or close your positions first.")
        return 2

    url = os.getenv("DATABASE_URL")
    if not url:
        print("No DATABASE_URL in .env")
        return 2

    conn = await asyncpg.connect(url, timeout=30)
    try:
        if everything:
            return await wipe_everything(conn, confirm)

        present = {
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        targets = [t for t in WIPE if t in present]

        print("WILL ERASE:")
        total = 0
        for t in targets:
            n = await conn.fetchval(f'SELECT count(*) FROM "{t}"')
            total += n
            print(f"   {t:<26} {n:>8} rows")
        print(f"   {'':<26} {total:>8} rows total")
        print()
        print("WILL KEEP:", ", ".join(k for k in KEEP if k in present))
        print()

        if not confirm:
            print("Dry run. Re-run with --confirm to actually erase.")
            return 0

        # TRUNCATE, not DELETE: it reclaims the space immediately and resets any
        # sequences. CASCADE because `decisions.trade_log_entry_id` references
        # `trades` — without it the whole statement fails on the foreign key and
        # nothing is erased, which reads as the script silently doing nothing.
        async with conn.transaction():
            await conn.execute(
                "TRUNCATE " + ", ".join(f'"{t}"' for t in targets) + " CASCADE"
            )
            # A fresh paper account, so the next session starts from a known
            # figure rather than from no row at all — `load_portfolio` treats a
            # missing row as "no book" and the first sizing call has no equity.
            if "agent_paper_account" in present:
                await conn.execute(
                    "INSERT INTO agent_paper_account (id, cash, updated_at) "
                    "VALUES ('default', $1, now()) "
                    "ON CONFLICT (id) DO UPDATE SET cash = EXCLUDED.cash, "
                    "updated_at = now()",
                    starting_cash,
                )
        print(f"ERASED {total} rows. Paper account reset to {starting_cash:,.2f}.")
        print()
        print("Now restart the backend so it reloads from the empty tables.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="actually erase (default is a dry run)")
    ap.add_argument("--cash", type=float, default=1000.0, help="starting paper cash")
    ap.add_argument("--all", action="store_true",
                    help="truncate EVERY table, config and migrations included")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.confirm, a.cash, a.all)))
