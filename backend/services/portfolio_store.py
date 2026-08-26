"""The backend agent's paper book — cash and open positions, now durable.

Starting paper cash is THE SAME FIGURE AS EVERYWHERE ELSE — 25,000.

This was 1,000,000, making it the third disagreeing definition of one quantity:
`lib/types.ts` opened the browser's book with 1,000,000, `PAPER_STARTING_EQUITY`
said 25,000, and `db/schema.sql` seeds `paper_account` with 25,000 under a comment
that explicitly claims it "matches PAPER_STARTING_EQUITY". Two of the three were
wrong and the comment asserting agreement was the only thing that was right.

40x of phantom buying power changes what the agent can size into, so this is not
cosmetic.

PERSISTENCE — WHAT CHANGED AND WHICH TABLES IT USES
---------------------------------------------------
This was a module-level dict with no persistence of any kind, so a restart reset
cash to the starting figure and forgot every open position. CLAUDE.md recorded
the gap and pointed at the `positions` table as the place to fix it.

IT IS NOT THAT TABLE, and following that pointer would have caused a worse bug
than the one it fixed. `lib/portfolioStore.server.ts::saveBook` already writes
`positions` — it runs `DELETE FROM positions` and re-inserts the browser's entire
book, because "absent from the payload" is how the browser expresses a close. Two
writers there would mean the operator's next save deletes every position the
agent holds, and the agent's next write resurrects a position the operator just
closed.

So the agent gets its own tables, `agent_paper_account` and `agent_positions`.
Same reasoning as the JSON-vs-Postgres split CLAUDE.md already documents: two
actors, two books, and a page that shows one under a heading implying the other
is the failure that split has already caused once.

THE IN-MEMORY DICT IS STILL THE WORKING COPY
--------------------------------------------
Reads answer from memory; writes go to memory first and are then mirrored. That
ordering is deliberate — a database that is down must not be able to block a
close or stall the sizing path, and every caller here is on a trading hot path.
The cost is that a crash between the two loses the last mutation, which for a
paper book is a recoverable inconvenience. The safety-critical durability is the
stop-loss watch list, and that lives in `services/position_store.py`.

With no `DATABASE_URL` every write is a no-op and every load returns nothing, so
behaviour is exactly what it was before this change — in memory, lost on restart
— and `load_portfolio()` says so at WARNING rather than implying durability.
"""

from typing import Any, Dict, List
import copy
import datetime
import logging

from backend.core.db import get_db_pool

logger = logging.getLogger(__name__)

PAPER_STARTING_CASH = 25_000.0

# Global in-memory portfolio for backend agents — the working copy, mirrored to
# Postgres by `_persist()` after every mutation and repopulated by
# `load_portfolio()` at startup.
_portfolio = {
    "paper": {
        "cash": PAPER_STARTING_CASH,
        "positions": []
    },
    "real": {
        "positions": []
    }
}


async def _persist() -> bool:
    """Mirror the in-memory book to Postgres. Never raises. Returns False if unstored.

    Cash and positions are written in ONE transaction, for the same reason
    `lib/portfolioStore.server.ts` does it: they are two tables describing one
    state — cash went down *because* a position was opened. A partial write
    leaves cash that does not match the positions it paid for, and every equity
    figure derived from it is then wrong in a way that looks precise.

    Positions are replaced rather than merged, because "absent" is how this
    module expresses a close: `sell_paper` pops the entry out of the list.
    """
    pool = get_db_pool()
    if not pool:
        return False

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO agent_paper_account (id, cash, updated_at)
                    VALUES ('default', $1, $2)
                    ON CONFLICT (id) DO UPDATE
                      SET cash = EXCLUDED.cash, updated_at = EXCLUDED.updated_at
                    """,
                    _portfolio["paper"]["cash"],
                    now,
                )
                await conn.execute("DELETE FROM agent_positions")
                for tab in ("paper", "real"):
                    for pos in _portfolio[tab]["positions"]:
                        qty = pos.get("qty")
                        # A zero-quantity row would appear on every exposure
                        # table as a holding of nothing. Skipped rather than
                        # stored, matching the browser-side store.
                        if not qty:
                            continue
                        await conn.execute(
                            """
                            INSERT INTO agent_positions
                              (tab, symbol, qty, avg_cost, margin_locked, updated_at)
                            VALUES ($1,$2,$3,$4,$5,$6)
                            ON CONFLICT (tab, symbol) DO UPDATE SET
                              qty = EXCLUDED.qty,
                              avg_cost = EXCLUDED.avg_cost,
                              margin_locked = EXCLUDED.margin_locked,
                              updated_at = EXCLUDED.updated_at
                            """,
                            tab,
                            pos["symbol"],
                            qty,
                            pos["avgCost"],
                            pos.get("marginLocked"),
                            now,
                        )
        return True
    except Exception as e:
        # Logged, not raised. The in-memory book is already updated and the
        # caller's trade decision has been made; failing it here would turn a
        # storage outage into a trading outage.
        logger.error(
            "Failed to persist the agent portfolio: %s. The book is correct in this "
            "process but a restart will reset it to %.2f cash and no positions.",
            e,
            PAPER_STARTING_CASH,
        )
        return False


async def load_portfolio() -> bool:
    """Repopulate the in-memory book from Postgres at startup.

    Returns True only when a stored book was actually read. An ABSENT
    `agent_paper_account` row is not an error — it means this process has never
    traded — and is why the table carries no seed row: a seeded 25,000 would be
    indistinguishable from an account that had genuinely traded its way back to
    exactly 25,000.
    """
    global _portfolio

    pool = get_db_pool()
    if not pool:
        logger.warning(
            "No database pool — the agent portfolio starts at %.2f cash with no positions. "
            "Nothing from a previous run is restored.",
            PAPER_STARTING_CASH,
        )
        return False

    try:
        async with pool.acquire() as conn:
            cash_row = await conn.fetchrow(
                "SELECT cash FROM agent_paper_account WHERE id = 'default'"
            )
            position_rows = await conn.fetch(
                "SELECT tab, symbol, qty, avg_cost, margin_locked FROM agent_positions ORDER BY symbol"
            )
    except Exception as e:
        logger.error(
            "Failed to read the stored agent portfolio: %s. Starting from %.2f cash with no "
            "positions — any position held before this restart is NOT in this book.",
            e,
            PAPER_STARTING_CASH,
        )
        return False

    if cash_row is None and not position_rows:
        logger.info("No stored agent portfolio; starting at %.2f cash.", PAPER_STARTING_CASH)
        return False

    restored: Dict[str, Any] = {
        "paper": {
            "cash": float(cash_row["cash"]) if cash_row is not None else PAPER_STARTING_CASH,
            "positions": [],
        },
        "real": {"positions": []},
    }

    for r in position_rows:
        tab = r["tab"]
        if tab not in restored:
            continue
        restored[tab]["positions"].append({
            "symbol": r["symbol"],
            "qty": float(r["qty"]),
            "avgCost": float(r["avg_cost"]),
            # Falls back to notional when the column is NULL, matching the
            # `.get(..., qty*avgCost)` default the mutators already use for rows
            # written before margin was tracked.
            "marginLocked": (
                float(r["margin_locked"]) if r["margin_locked"] is not None
                else float(r["qty"]) * float(r["avg_cost"])
            ),
        })

    _portfolio = restored
    logger.warning(
        "Restored agent portfolio: %.2f paper cash, %d paper position(s), %d real position(s).",
        restored["paper"]["cash"],
        len(restored["paper"]["positions"]),
        len(restored["real"]["positions"]),
    )
    return True


async def get_portfolio() -> Dict[str, Any]:
    return copy.deepcopy(_portfolio)

async def update_portfolio(updates: Dict[str, Any]):
    global _portfolio
    _portfolio = copy.deepcopy(updates)
    await _persist()
    return _portfolio

async def buy_paper(symbol: str, qty: float, price: float, leverage: float = 1.0) -> bool:
    global _portfolio
    notional = qty * price
    margin_required = notional / leverage if leverage > 0 else notional

    if margin_required > _portfolio["paper"]["cash"]:
        return False

    _portfolio["paper"]["cash"] -= margin_required

    # Check if position already exists
    positions = _portfolio["paper"]["positions"]
    idx = next((i for i, p in enumerate(positions) if p["symbol"] == symbol), -1)

    if idx >= 0:
        ex = positions[idx]
        new_qty = ex["qty"] + qty
        new_avg = (ex["qty"] * ex["avgCost"] + notional) / new_qty
        new_margin = ex.get("marginLocked", ex["qty"] * ex["avgCost"]) + margin_required
        positions[idx] = {
            "symbol": symbol,
            "qty": new_qty,
            "avgCost": new_avg,
            "marginLocked": new_margin
        }
    else:
        positions.append({
            "symbol": symbol,
            "qty": qty,
            "avgCost": price,
            "marginLocked": margin_required
        })

    # Persisted AFTER the in-memory update, and its result deliberately does not
    # affect the return value: the position exists either way, and reporting a
    # successful buy as failed because storage is down would leave the caller
    # believing it holds nothing while it holds something.
    await _persist()
    return True

async def sell_paper(symbol: str, qty: float, price: float) -> bool:
    global _portfolio
    positions = _portfolio["paper"]["positions"]
    idx = next((i for i, p in enumerate(positions) if p["symbol"] == symbol), -1)

    if idx < 0 or positions[idx]["qty"] < qty:
        return False

    ex = positions[idx]
    realized_pnl = (price - ex["avgCost"]) * qty
    remaining_qty = ex["qty"] - qty
    proportion_closed = qty / ex["qty"]
    margin_released = ex.get("marginLocked", ex["qty"] * ex["avgCost"]) * proportion_closed

    if remaining_qty > 0.0000001:
        positions[idx]["qty"] = remaining_qty
        positions[idx]["marginLocked"] = ex.get("marginLocked", ex["qty"] * ex["avgCost"]) - margin_released
    else:
        positions.pop(idx)

    _portfolio["paper"]["cash"] += margin_released + realized_pnl
    await _persist()
    return True
