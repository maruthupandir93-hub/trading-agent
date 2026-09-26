#!/usr/bin/env python
"""Reconcile the persisted paper BOOK against the position monitor's WATCH LIST.

WHY THIS EXISTS
===============
CLAUDE.md's "two data stores, both real" rule has a sibling one level down: the
backend keeps the paper book in `agent_positions` / `agent_paper_account` and the
stop-loss watch list in `monitored_positions`, and they are written by different
components for different reasons. They are supposed to agree about whether a
position exists. When they do not, the failure is quiet and only shows up after a
restart:

    monitored_positions says OPEN   -> `monitor.restore()` starts enforcing a stop
    agent_positions says FLAT       -> `load_portfolio()` reports no position

and the operator sees a dashboard with an enforced stop on a position the book
does not have. Equity, session progress and every percentage derived from them
are then computed against the wrong capital.

This was hit for real on 2026-09-25 by running an end-to-end verification against
the live paper book: the test opened a SHORT on a symbol where the book already
held a LONG. `portfolio_store.apply_paper_fill` is direction-aware and keys
positions BY SYMBOL, so on a one-way book the short NETTED AGAINST the existing
long instead of opening beside it. The round trip left the book flat and pushed
the operator's margin back into cash. The position itself was never closed — the
watch list still had it, the live backend still reported it, and no closing row
was written to `trades`. Only the persisted book diverged.

WHAT IT DOES NOT DO
===================
It never closes, opens or forgets a position, and it never touches `trades`.
`reconciliation.py` makes the same promise for the venue comparison and for the
same reason: every automatic "fix" is itself a trade. This only rewrites the
BOOK's rows to match the watch list, which is the safety-critical record — the
one enforcing the stop.

It also refuses while `LIVE_TRADING=true`. Real positions are reconciled against
the VENUE by `services/reconciliation.py`, which reports and never repairs; a
local rewrite there would be inventing a real-money position from a local row.

RUN IT WITH THE BACKEND STOPPED, OR RESTART AFTERWARDS
======================================================
`portfolio_store._persist()` rewrites both tables from the running process's
in-memory `_portfolio` after every book write, so a repair applied underneath a
live backend is overwritten by whatever that process is holding — the same reason
`POST /api/admin/reset-paper` exists rather than a bare TRUNCATE. If the backend
is up and its memory is CORRECT, it will heal these tables by itself on its next
book write and this script is unnecessary; run it when the backend is down, or
when it is up and you intend to restart it before it writes again.

    python scripts/repair_paper_book.py            # report only
    python scripts/repair_paper_book.py --confirm  # apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="apply the repair; without it nothing is written")
    args = parser.parse_args()

    _load_env()

    if (os.getenv("LIVE_TRADING") or "").strip().lower() == "true":
        print("REFUSED: LIVE_TRADING is true.")
        print("  Real positions are reconciled against the VENUE by")
        print("  services/reconciliation.py, which reports and never repairs. Rewriting")
        print("  a real book from a local row would invent a position at the exchange.")
        return 2

    url = os.getenv("DATABASE_URL")
    if not url:
        print("REFUSED: DATABASE_URL is not set.")
        return 2

    import asyncpg

    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        watched = await conn.fetch(
            "select tar_id, symbol, side, qty, entry_price, status"
            " from monitored_positions where tab = 'paper'"
        )
        booked = await conn.fetch(
            "select symbol, side, qty, avg_cost, margin_locked"
            " from agent_positions where tab = 'paper'"
        )
        cash = await conn.fetchval("select cash from agent_paper_account where id = 'default'")

        # 'pending' rows are approvals whose fill never arrived. Nothing will ever
        # clear one, and it is not a position — it is a TAR that died between
        # approval and fill.
        open_rows = [r for r in watched if r["status"] == "open"]
        pending = [r for r in watched if r["status"] != "open"]

        print(f"WATCH LIST : {len(open_rows)} open, {len(pending)} pending")
        for r in open_rows:
            print(f"   open    {r['symbol']:<12} {r['side']:<4} qty {float(r['qty']):.6f} "
                  f"@ {float(r['entry_price']):.6f}")
        for r in pending:
            print(f"   PENDING  {r['symbol']:<12} tar {r['tar_id']} "
                  f"(never filled — nothing will clear this)")

        print(f"\nBOOK       : {len(booked)} position(s), cash {float(cash or 0):,.2f}")
        for r in booked:
            print(f"   {r['symbol']:<12} {r['side']:<4} qty {float(r['qty']):.6f} "
                  f"@ {float(r['avg_cost']):.6f}  margin {float(r['margin_locked'] or 0):,.2f}")

        watched_syms = {r["symbol"] for r in open_rows}
        booked_syms = {r["symbol"] for r in booked}
        missing = watched_syms - booked_syms
        extra = booked_syms - watched_syms

        if not missing and not extra and not pending:
            print("\nIN STEP. Nothing to repair.")
            return 0

        print("\nDIVERGENCE")
        for s in sorted(missing):
            print(f"   watched but NOT in the book : {s}")
        for s in sorted(extra):
            print(f"   in the book but NOT watched : {s}  (left alone — see below)")
        if pending:
            print(f"   {len(pending)} stale pending watch row(s)")

        if extra:
            print("\n   A position in the book with no watch row is NOT repaired here.")
            print("   It could equally mean the watch row was lost, and deleting it would")
            print("   discard a real position. That case needs a human.")

        if not args.confirm:
            print("\nReport only. Re-run with --confirm to write the missing book rows")
            print("and delete the stale pending watch rows.")
            return 0

        written = 0
        async with conn.transaction():
            for r in open_rows:
                if r["symbol"] not in missing:
                    continue
                qty = float(r["qty"])
                entry = float(r["entry_price"])
                # Margin is not on the watch row. 1x is the CONSERVATIVE choice and
                # the same fallback `_deployed_margin` makes for an unknown
                # leverage: it OVER-states locked margin, so the capital pool caps
                # sooner rather than later.
                await conn.execute(
                    "insert into agent_positions"
                    " (tab, symbol, qty, avg_cost, margin_locked, side, updated_at)"
                    " values ('paper', $1, $2, $3, $4, $5, now())",
                    r["symbol"], qty, entry, qty * entry, r["side"],
                )
                written += 1
                print(f"   restored {r['symbol']} {r['side']} qty {qty:.6f} @ {entry:.6f}")

            for r in pending:
                await conn.execute(
                    "delete from monitored_positions where tar_id = $1", r["tar_id"]
                )
                print(f"   removed stale pending row {r['tar_id']}")

        print(f"\nDONE. {written} book row(s) restored, {len(pending)} pending row(s) removed.")
        print("Restart the backend so load_portfolio() reads this state into memory.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
