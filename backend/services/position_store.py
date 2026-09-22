"""Durable storage for the position monitor's watch list.

THE GAP THIS CLOSES
-------------------
`PositionMonitorAgent` held every open position in a dict on the instance, and
`services/portfolio_store` was a module-level dict. So a restart forgot every
open position. Spec Section 22.8 names the consequence directly: *"the worst
case is not 'the bot makes a bad trade' but 'the bot goes silent while holding a
leveraged position'."* For paper that costs P&L continuity. For real money the
position is still open at the exchange, and the stop this process was the only
thing enforcing is gone — `execution_agent` says so out loud when it fills:
"resting stop orders are NOT IMPLEMENTED ... protected only while this process is
running and watching the price."

WHY THIS DOES NOT WRITE THE `positions` TABLE
---------------------------------------------
Because that table already has a writer. `lib/portfolioStore.server.ts::saveBook`
does `DELETE FROM positions` and re-inserts the browser's entire book, since
"absent from the payload" is how the browser expresses a close. A second writer
there would have the operator's next save delete every position the agent holds,
and the agent's next write resurrect a position the operator just closed. See
`db/schema.sql` SECTION 3b.

WRITE THE WHOLE LIST, NOT A DIFF
--------------------------------
`save_watch_list` replaces the table's contents inside one transaction. That is
deliberate:

  * the watch list is small (one row per open position), so the cost is nil;
  * it makes the write idempotent — replaying it converges rather than
    accumulating;
  * a per-row diff has to decide what a *missing* row means, and getting that
    wrong drops a position from the watch list, which is the exact failure this
    module exists to prevent.

DEGRADES HONESTLY WITH NO DATABASE
----------------------------------
Every function returns a falsy/empty value and logs when there is no pool, and
never raises into a caller. The agent keeps working exactly as it did before —
in memory, losing state on restart — and says so, rather than appearing durable.
`get_db_pool()` returns None in the whole test suite and on any checkout without
`DATABASE_URL`, so this is the common path, not an edge case.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from backend.core.db import get_db_pool

logger = logging.getLogger(__name__)

# The columns a row carries, in the order the INSERT below binds them. Kept as
# one list so the row dicts, the INSERT and the SELECT cannot drift apart —
# which is how a persisted stop-loss silently becomes NULL on restore.
_FIELDS = (
    "tar_id",
    "status",
    "symbol",
    "tab",
    "side",
    "qty",
    "entry_price",
    "stop_loss",
    "take_profit",
    "peak_price",
    "opened_at",
    "stop_order_id",
    "tp_order_id",
    "strategy",
    "run_id",
    "entry_context",
    # The fee paid to OPEN, so the close can net the whole round trip rather than
    # only its own side. See `backend/services/fees.py`.
    "entry_fee",
    # The entry-to-stop distance AT ENTRY. The trailing stop measures progress in
    # R, and the current `stop_loss` stops being a usable denominator as soon as
    # anything moves it — the partial take-profit sets it to break-even, making
    # `abs(entry - stop)` zero.
    "initial_risk",
    # The funding rate captured at entry, so a close can charge the settlements
    # the position lived through without an HTTP call on the close path.
    "funding_rate",
    # The adverse extreme, the mirror of peak_price. Persisted so a restart does
    # not reset a position's excursion record to its entry and quietly understate
    # how far it went against us.
    "worst_price",
)


# THE SQL IS GENERATED FROM `_FIELDS`, NOT WRITTEN OUT BESIDE IT.
#
# `_FIELDS`' own comment has always claimed it keeps "the row dicts, the INSERT
# and the SELECT" from drifting apart. It did not: the INSERT named its sixteen
# columns literally and bound `*[row.get(f) for f in _FIELDS]` positionally, and
# the SELECT listed them a third time. Adding a field to the tuple therefore
# produced a silent mismatch — the values would shift by one against the columns,
# writing each position's `strategy` into `run_id` and so on, or fail the
# statement outright once the counts diverged.
#
# That is the same class of bug as `stop_order_id` being named where it was
# consumed and never produced, which cost a real incident: the column existed,
# the schema comment explained why it mattered, and every row was NULL.
#
# Deriving all three from one tuple makes the drift impossible rather than
# merely tested-against. `updated_at` is appended separately because it is
# generated here, not carried on the row.
_UPDATABLE = tuple(f for f in _FIELDS if f != "tar_id")
_ALL_COLUMNS = _FIELDS + ("updated_at",)
_INSERT_SQL = "\n".join((
    "INSERT INTO monitored_positions (" + ", ".join(_ALL_COLUMNS) + ")",
    "VALUES (" + ", ".join(f"${i}" for i in range(1, len(_ALL_COLUMNS) + 1)) + ")",
    "ON CONFLICT (tar_id) DO UPDATE SET",
    ",\n".join(
        f"  {f} = EXCLUDED.{f}" for f in _UPDATABLE + ("updated_at",)
    ),
))
_SELECT_SQL = "SELECT " + ", ".join(_FIELDS) + " FROM monitored_positions"


def _as_naive_utc(value: Any) -> Optional[datetime.datetime]:
    """Strip the tzinfo off a stored timestamp, keeping the instant in UTC.

    NOT COSMETIC, AND IT COST A REAL BUG TO FIND.

    `monitored_positions.opened_at` is `timestamptz`, so asyncpg returns an
    AWARE datetime. Everything the agent creates in-process comes from
    `datetime.datetime.utcnow()`, which is NAIVE. Mixing them is a TypeError on
    subtraction — and the subtraction is `held = utcnow() - pos.opened_at` in
    `PositionMonitorAgent._close`, which runs AFTER the exchange has already
    filled the closing order.

    So the failure mode was: a restored position stops out, the close really
    executes, and then the agent raises before removing it from the watch list
    or publishing POSITION_CLOSED. The next tick sees it still open and closes it
    again. A flat position closed repeatedly, and no reflection ever written.

    Converting HERE rather than making the agent timezone-aware is deliberate:
    the whole codebase is naive-UTC (`models/events.py` uses `utcnow()` as a
    Pydantic default_factory, and pytest.ini documents why that is left alone),
    so the storage boundary is the one place the two conventions meet and the
    one place the conversion belongs.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def _as_float(value: Any) -> Optional[float]:
    """Postgres `numeric` comes back as Decimal. Return None for None.

    None must survive as None. A pending row has no entry price, and coercing
    that to 0.0 would give a restored position an entry of zero — every P&L
    figure derived from it would then be wrong in a way that looks precise.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def save_watch_list(rows: List[Dict[str, Any]]) -> bool:
    """Replace the stored watch list with `rows`. Returns False if not stored.

    False means "still in memory only". The caller must not treat it as an
    error worth aborting on: the position is open either way, and refusing to
    monitor it because the database is down would be strictly worse than
    monitoring it without durability.
    """
    pool = get_db_pool()
    if not pool:
        logger.debug(
            "No database pool — %d monitored position(s) held in memory only. "
            "A restart will forget them.",
            len(rows),
        )
        return False

    try:
        async with pool.acquire() as conn:
            # One transaction. A delete that committed without its inserts would
            # leave an EMPTY watch list on disk while positions are open — the
            # worst possible intermediate state, because the next restart would
            # read it as "nothing to watch" and be confident about it.
            async with conn.transaction():
                await conn.execute("DELETE FROM monitored_positions")
                for row in rows:
                    await conn.execute(
                        _INSERT_SQL,
                        *[row.get(f) for f in _FIELDS],
                        datetime.datetime.now(datetime.timezone.utc),
                    )
        return True
    except Exception as e:
        # Logged at ERROR, not raised. The position is already open; unwinding
        # the caller would abandon the in-memory watch instead of just losing
        # its durability.
        logger.error(
            "Failed to persist the monitored-position watch list (%d row(s)): %s. "
            "Positions are still watched in this process, but a restart will lose them.",
            len(rows),
            e,
        )
        return False


async def load_watch_list() -> List[Dict[str, Any]]:
    """Every stored row, or [] when there is nothing to read.

    Returns [] both for "no database" and for "database with no open positions".
    The caller cannot act differently on those two — there is nothing to restore
    either way — so they are not distinguished here. `save_watch_list`'s return
    value is what tells a caller whether durability is actually on.
    """
    pool = get_db_pool()
    if not pool:
        logger.warning(
            "No database pool — the monitored-position watch list could NOT be restored. "
            "If this process was restarted while holding positions, nothing is enforcing "
            "their stops."
        )
        return []

    try:
        async with pool.acquire() as conn:
            records = await conn.fetch(_SELECT_SQL)
    except Exception as e:
        logger.error(
            "Failed to read the monitored-position watch list: %s. Treating it as EMPTY, "
            "which means any position open before this restart is unwatched.",
            e,
        )
        return []

    out: List[Dict[str, Any]] = []
    for r in records:
        out.append(
            {
                "tar_id": r["tar_id"],
                "status": r["status"],
                "symbol": r["symbol"],
                "tab": r["tab"],
                "side": r["side"],
                "qty": _as_float(r["qty"]),
                "entry_price": _as_float(r["entry_price"]),
                "stop_loss": _as_float(r["stop_loss"]),
                "take_profit": _as_float(r["take_profit"]),
                "peak_price": _as_float(r["peak_price"]),
                "opened_at": _as_naive_utc(r["opened_at"]),
                # Tolerant read. The SELECT names this column, so a real row
                # always carries it — but a database whose `ALTER TABLE ... ADD
                # COLUMN` has not been applied yet would raise mid-loop and lose
                # the whole watch list, which is a far worse outcome than a null
                # stop order id on one restored position.
                "stop_order_id": (r["stop_order_id"] if "stop_order_id" in r.keys() else None),
                "tp_order_id": (r["tp_order_id"] if "tp_order_id" in r.keys() else None),
                # Same tolerant read, same reason: a database that has not yet
                # applied the ALTERs must lose attribution, never the watch list.
                "strategy": (r["strategy"] if "strategy" in r.keys() else None),
                "run_id": (r["run_id"] if "run_id" in r.keys() else None),
                "entry_context": (r["entry_context"] if "entry_context" in r.keys() else None),
            }
        )
    return out
