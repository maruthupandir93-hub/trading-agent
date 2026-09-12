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

from typing import Any, Dict, List, Optional
import copy
import datetime
import logging

from backend.core.db import get_db_pool
from backend.services.fees import modelled_fee

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
                              (tab, symbol, qty, avg_cost, margin_locked, side, updated_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7)
                            ON CONFLICT (tab, symbol) DO UPDATE SET
                              qty = EXCLUDED.qty,
                              avg_cost = EXCLUDED.avg_cost,
                              margin_locked = EXCLUDED.margin_locked,
                              side = EXCLUDED.side,
                              updated_at = EXCLUDED.updated_at
                            """,
                            tab,
                            pos["symbol"],
                            qty,
                            pos["avgCost"],
                            pos.get("marginLocked"),
                            pos.get("side") or "buy",
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
            # Defaults to a long for rows written before the column existed,
            # which is what they were.
            "side": (r["side"] if "side" in r.keys() and r["side"] else "buy"),
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


# ===========================================================================
# THE SIGNED PAPER BOOK
#
# WHY THIS EXISTS, AND IT IS THE BUG BEHIND HALF THE DASHBOARD
# ------------------------------------------------------------
# `buy_paper` / `sell_paper` above are the OPERATOR's manual API and they are
# long-only by construction: a buy opens, a sell closes, and `sell_paper` refuses
# outright when no long exists. Nothing wrong with that for a human clicking Buy
# and Sell.
#
# But the AGENT never called them at all. `execution_agent` wrote a row to
# `trades`, handed the position to `PositionMonitorAgent`, and never touched the
# book. So on the paper account:
#
#   * cash sat at its starting figure forever, however many trades filled
#   * `positions` stayed empty, so there was no unrealized P&L to move when
#     price moved, and the dashboard's "Open positions" read 0
#   * `current_equity()` — cash plus marked positions — therefore never changed,
#     so a session's progress toward its target never moved either
#
# Every one of those reads as a separate broken panel and is one missing write.
#
# AND IT COULD NOT SIMPLY CALL `buy_paper`, because this agent SHORTS. A short
# opens on a sell, which `sell_paper` rejects, and closes on a buy, whose P&L is
# (entry - exit) — the opposite sign to the formula there. Feeding agent fills
# through the long-only API would have booked every short backwards.
#
# So this is the general implementation: direction-aware, margin-aware, and the
# single place position arithmetic happens for the agent.
# ===========================================================================

_DUST = 1e-12


def _find(positions: List[Dict[str, Any]], symbol: str) -> int:
    return next((i for i, p in enumerate(positions) if p.get("symbol") == symbol), -1)


def position_pnl(pos: Dict[str, Any], mark: float) -> Optional[float]:
    """Unrealized P&L for one position at `mark`. None when it cannot be valued.

    SIGNED BY DIRECTION. A long gains when price rises; a short gains when it
    falls. Returning the long formula for both is the single most expensive
    arithmetic error available here — it reports a losing short as a winner and
    would have the risk layer add to it.

    None, never 0.0, when there is no usable mark: "we could not value this" and
    "this position is exactly flat" are different facts and only one of them
    should enter an equity total.
    """
    try:
        qty = abs(float(pos.get("qty") or 0.0))
        cost = float(pos.get("avgCost") or 0.0)
    except (TypeError, ValueError):
        return None
    if qty <= _DUST or cost <= 0 or not mark or mark <= 0:
        return None
    direction = 1.0 if (pos.get("side") or "buy") == "buy" else -1.0
    return (mark - cost) * qty * direction


def book_equity(book: Dict[str, Any], marks: Dict[str, float]) -> Dict[str, Any]:
    """Equity for one book: FREE CASH + LOCKED MARGIN + UNREALIZED P&L.

    THE OLD FORMULA WAS `cash + qty * price` AND IT IS ONLY RIGHT AT 1x.
    `buy_paper` deducts MARGIN from cash and records `marginLocked`, so cash is
    free cash, not total capital. Adding the full notional back on top double
    counts the leveraged part: at 10x a $7,000 position funded by $700 of margin
    reported $6,300 of equity that does not exist. Every percentage derived from
    it — session progress, drawdown, risk-per-trade — was wrong by the same
    factor, and wrong in the direction that flatters the account.
    """
    cash = book.get("cash")
    cash_f = float(cash) if isinstance(cash, (int, float)) else None

    locked = 0.0
    unrealized = 0.0
    unpriced: List[str] = []

    for pos in book.get("positions") or []:
        qty = abs(float(pos.get("qty") or 0.0))
        if qty <= _DUST:
            continue
        cost = float(pos.get("avgCost") or 0.0)
        # Falls back to the notional, matching the default the mutators use for
        # rows written before `marginLocked` existed.
        locked += float(pos.get("marginLocked") or (qty * cost))

        symbol = pos.get("symbol")
        pnl = position_pnl(pos, marks.get(symbol, 0.0) if symbol else 0.0)
        if pnl is None:
            unpriced.append(str(symbol))
        else:
            unrealized += pnl

    return {
        "cash": cash_f,
        "marginLocked": locked,
        "unrealized": unrealized,
        # None when anything is unvaluable — a partial equity presented as the
        # total understates or overstates the account by an unknown amount.
        "equity": None if cash_f is None or unpriced else cash_f + locked + unrealized,
        "unpricedSymbols": unpriced,
        "complete": cash_f is not None and not unpriced,
    }


async def apply_paper_fill(
    *,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    leverage: float = 1.0,
    reduce_only: bool = False,
) -> Dict[str, Any]:
    """Apply one paper fill to the book. Direction-aware, margin-aware.

    `reduce_only=True` means this fill CLOSES (or reduces) whatever is open,
    whichever way it points — the same meaning it has at a venue. `False` opens
    or adds.

    Returns what happened, including `realized`, so the caller can log the P&L it
    actually booked rather than recomputing it from a second copy of this
    arithmetic.

    A REDUCE WITH NOTHING TO REDUCE IS REPORTED, NOT INVENTED. It would mean the
    book and the monitor disagree, and silently opening a reversed position — the
    thing `reduceOnly` exists at the venue to prevent — must not happen here
    either.
    """
    global _portfolio

    book = _portfolio.setdefault("paper", {"cash": 0.0, "positions": []})
    positions = book.setdefault("positions", [])
    qty = abs(float(qty))
    price = float(price)
    if qty <= _DUST or price <= 0:
        return {"ok": False, "reason": "a fill needs a positive quantity and price", "realized": None}

    idx = _find(positions, symbol)
    existing = positions[idx] if idx >= 0 else None

    if not reduce_only:
        notional = qty * price
        margin = notional / leverage if leverage and leverage > 0 else notional

        # THE MARGIN MUST ACTUALLY BE THERE.
        #
        # This check was MISSING and it is the operator's question in code form:
        # "if one trade takes the whole amount, does the agent open another with
        # no money?" It did. `buy_paper` — the operator's manual path — refuses
        # when margin exceeds cash; this path, which every AGENT fill takes, just
        # subtracted and let free cash go NEGATIVE.
        #
        # A negative cash balance is not a small accounting blemish. `book_equity`
        # is free cash + locked margin + unrealized, so a negative first term
        # understates equity, which understates the next position's size, which
        # makes every subsequent risk calculation wrong in a compounding way. And
        # it silently models leverage the venue never granted.
        #
        # Refused rather than clamped: a partial fill nobody asked for is its own
        # kind of wrong, and the caller already handles `ok: False` by logging and
        # not recording a position.
        free_cash = float(book.get("cash") or 0.0)
        # THE FEE IS PART OF WHAT THIS ENTRY COSTS, so it belongs in the
        # affordability check and not only in the deduction below.
        #
        # Checking `margin > free_cash` and THEN subtracting `margin + fee` lets an
        # entry sized to exactly the available cash pass the check and push the
        # balance negative — which is precisely what this check exists to prevent,
        # and the comment above says why that is not a cosmetic problem: `book_equity`
        # is free cash + locked margin + unrealized, so a negative first term
        # understates equity, which understates the next position's size, and every
        # subsequent risk calculation is wrong in a compounding way.
        #
        # Broker-style sizing makes this reachable rather than theoretical: at 100%
        # capital allocation the Risk Gateway deliberately sizes to the whole pool
        # (less the 1.2x margin buffer), so "exactly affordable before fees" is the
        # normal case at the top of the range, not an edge one.
        entry_fee = modelled_fee(notional).cost
        required = margin + entry_fee
        if required > free_cash:
            return {
                "ok": False,
                "reason": (
                    f"insufficient free cash for {symbol}: needs {required:,.2f} "
                    f"({margin:,.2f} margin on {notional:,.2f} notional at {leverage:g}x "
                    f"plus {entry_fee:,.2f} fee) but only {free_cash:,.2f} is free. "
                    f"Capital already committed to open positions is not available to "
                    f"open another."
                ),
                "realized": None,
                "requiredMargin": margin,
                "fee": entry_fee,
                "freeCash": free_cash,
            }

        if existing is not None and abs(float(existing.get("qty") or 0.0)) > _DUST:
            if (existing.get("side") or "buy") != side:
                # An opposite-side fill that was not flagged reduce_only. Treated
                # as the reduction it functionally is rather than as a second
                # position, because one symbol carries one aggregated position in
                # this book and pretending otherwise would double-count margin.
                return await apply_paper_fill(
                    symbol=symbol, side=side, qty=qty, price=price,
                    leverage=leverage, reduce_only=True,
                )
            prev_qty = abs(float(existing["qty"]))
            prev_cost = float(existing["avgCost"])
            new_qty = prev_qty + qty
            existing["qty"] = new_qty
            existing["avgCost"] = (prev_qty * prev_cost + notional) / new_qty
            existing["marginLocked"] = float(existing.get("marginLocked") or (prev_qty * prev_cost)) + margin
        else:
            positions.append({
                "symbol": symbol,
                "qty": qty,
                "avgCost": price,
                "marginLocked": margin,
                "side": side,
            })

        # THE FEE COMES OUT OF CASH, on the open as well as the close.
        #
        # Margin is LOCKED and comes back on the close; the fee is SPENT and does
        # not. Deducting only margin — which is what this did — left the paper
        # book reporting the full stake as still available and made a book that
        # had traded a hundred times look identical to one that had never traded.
        book["cash"] = float(book.get("cash") or 0.0) - margin - entry_fee
        await _persist()
        return {"ok": True, "action": "open", "realized": None, "marginLocked": margin,
                "fee": entry_fee}

    # ---- a REDUCE / CLOSE ----------------------------------------------
    if existing is None or abs(float(existing.get("qty") or 0.0)) <= _DUST:
        return {
            "ok": False,
            "reason": (
                f"nothing open on {symbol} to reduce. The book and the position monitor "
                f"disagree; NOT opening a reversed position to absorb it."
            ),
            "realized": None,
        }

    open_qty = abs(float(existing["qty"]))
    cost = float(existing["avgCost"])
    closing = min(qty, open_qty)
    direction = 1.0 if (existing.get("side") or "buy") == "buy" else -1.0
    gross = (price - cost) * closing * direction

    # NET of this leg's fee. The ENTRY fee was already charged to cash when the
    # position opened, so subtracting it again here would double-count it — the
    # two legs are accounted where each is actually paid rather than both at the
    # close. `position_monitor` nets both into the figure it REPORTS because a
    # trade row has to state the whole round trip; this book instead moves cash
    # at each event, and the two must not be conflated.
    exit_fee = modelled_fee(closing * price).cost
    realized = gross - exit_fee

    total_margin = float(existing.get("marginLocked") or (open_qty * cost))
    released = total_margin * (closing / open_qty)

    remaining = open_qty - closing
    if remaining > _DUST:
        existing["qty"] = remaining
        existing["marginLocked"] = total_margin - released
    else:
        positions.pop(idx)

    book["cash"] = float(book.get("cash") or 0.0) + released + realized
    await _persist()

    return {
        "ok": True,
        "action": "close" if remaining <= _DUST else "reduce",
        "realized": realized,
        "grossRealized": gross,
        "fee": exit_fee,
        "closedQty": closing,
        # Surfaced so an over-sized close is visible rather than silently clamped.
        "unmatchedQty": qty - closing,
        "marginReleased": released,
    }
