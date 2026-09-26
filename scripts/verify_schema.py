#!/usr/bin/env python
"""Compare `db/schema.sql` against what the database actually has.

WHY THIS IS WORTH HAVING, AND WHY "init_db RAN" IS NOT THE SAME ANSWER
======================================================================
`init_db` applies `db/schema.sql` on every startup and logs "schema applied".
That line means the FILE EXECUTED WITHOUT ERROR — it does not mean the live
database matches the file, and this project has been bitten by the difference
twice:

  * `CREATE TABLE IF NOT EXISTS` is a NO-OP on a table that already exists, so a
    column added to the file later never reached the live table. That is exactly
    how `execution_quality` came to be missing while the schema declared it, and
    every write to it failed after the order had already reached the exchange.
  * `CREATE TABLE IF NOT EXISTS` also CANNOT WIDEN A CHECK CONSTRAINT. The
    `trades_origin_tag_check` omitted two tags the code writes, so every closing
    trade was REJECTED and rolled back — the positions closed correctly and only
    the record of them was lost, which blanked the whole P&L dashboard three
    pages away.

Both were silent. This reads both sides and names the difference.

READ-ONLY. It never alters anything — it reports, and the fix is a migration in
`schema.sql` (a `DO $$ ... $$` block for a constraint, an `ALTER TABLE ... ADD
COLUMN IF NOT EXISTS` for a column), which `init_db` then applies idempotently.

    python scripts/verify_schema.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCHEMA = os.path.join(ROOT, "db", "schema.sql")

# Types are deliberately NOT compared. Postgres normalises them (`text` vs
# `character varying`, `numeric` vs `numeric(12,2)`), and a type mismatch that
# matters shows up as a failed write rather than as a silent absence — which is
# the failure mode this script exists for.
_CREATE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][\w]*)\s*\((.*?)\n\);",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_ADD = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def declared() -> dict:
    """{table: {columns}} as `schema.sql` declares them."""
    sql = _strip_comments(open(SCHEMA, encoding="utf-8").read())
    out: dict = {}
    for table, body in _CREATE.findall(sql):
        cols = set()
        depth = 0
        for raw in body.split("\n"):
            line = raw.strip()
            if not line:
                continue
            # A column definition is a line starting with an identifier at the
            # top level. Table-level constraints and the insides of a CHECK are
            # not columns.
            if depth == 0 and not re.match(
                r"(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE
            ):
                m = re.match(r"([A-Za-z_][\w]*)\s+\S", line)
                if m:
                    cols.add(m.group(1).lower())
            depth += raw.count("(") - raw.count(")")
        out[table.lower()] = cols
    for table, col in _ALTER_ADD.findall(sql):
        out.setdefault(table.lower(), set()).add(col.lower())
    return out


async def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
    import asyncpg

    url = os.getenv("DATABASE_URL")
    if not url:
        print("No DATABASE_URL in .env")
        return 2

    want = declared()
    conn = await asyncpg.connect(url, timeout=30)
    try:
        live_tables = {
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        live_cols: dict = {}
        for r in await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public'"
        ):
            live_cols.setdefault(r["table_name"], set()).add(r["column_name"])

        print(f"schema.sql declares {len(want)} table(s); "
              f"`public` has {len(live_tables)}")

        missing_tables = sorted(t for t in want if t not in live_tables)
        extra_tables = sorted(t for t in live_tables if t not in want)
        problems = 0

        if missing_tables:
            problems += len(missing_tables)
            print(f"\nMISSING TABLES ({len(missing_tables)}) — declared but not present:")
            for t in missing_tables:
                print(f"   {t}")

        if extra_tables:
            # Not a fault: tables created outside schema.sql (LangGraph's
            # checkpointer makes its own) live here legitimately.
            print(f"\nPresent but not in schema.sql ({len(extra_tables)}) — "
                  f"expected for the checkpointer's own tables:")
            print("   " + ", ".join(extra_tables))

        drift = []
        for t in sorted(want):
            if t not in live_tables:
                continue
            gone = sorted(want[t] - live_cols.get(t, set()))
            if gone:
                drift.append((t, gone))
        if drift:
            problems += sum(len(c) for _, c in drift)
            print(f"\nMISSING COLUMNS — the `execution_quality` failure mode:")
            for t, cols in drift:
                print(f"   {t:<24} {', '.join(cols)}")

        # The constraint that silently discarded every closing trade.
        tags = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'trades_origin_tag_check'"
        )
        if tags:
            need = {"debate", "chat-trade-action", "agent-plan", "agent-close",
                    "user-command", "manual-click", "manual-panel"}
            absent = sorted(t for t in need if f"'{t}'" not in tags)
            if absent:
                problems += len(absent)
                print(f"\nTRADE ORIGIN TAGS REJECTED BY THE LIVE CONSTRAINT: "
                      f"{', '.join(absent)}")
                print("   A CHECK that omits a tag the code writes does not degrade —")
                print("   it REJECTS the INSERT, and the close is lost after the money moved.")
            else:
                print(f"\ntrades_origin_tag_check permits all {len(need)} tags the code writes")
        else:
            print("\ntrades_origin_tag_check is ABSENT — no tag is enforced")

        print()
        if problems:
            print(f"{problems} difference(s). Add the migration to db/schema.sql; "
                  f"init_db applies it on the next start.")
            return 1
        print("The live database matches db/schema.sql.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
