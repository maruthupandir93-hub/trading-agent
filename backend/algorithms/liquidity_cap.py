"""How large an order this book can absorb — so size scales with the MARKET too.

WHY THIS EXISTS
---------------
The operator's requirement was that the agent "trade equally like a pro" at
$2, $20,000 or $2,000,000. Risk sizing is already proportional — every trade
risks `equity x MAX_PORTFOLIO_VAR_FRACTION`, so the PERCENTAGE at stake is the
same at every balance. That is the right property and it is not the whole one.

Proportional sizing breaks at BOTH ends, and nothing in this system noticed
either:

    $2          the venue's $5 minimum is below the whole account, so the
                order is approved and silently refused at quantisation. That
                end is handled: `trading_session._venue_minimum_refusal`.

    $2,000,000  at the live VaR, a 2% stop sizes an $8,000,000 notional MARKET
                order on SOL/USDT. There was NO maximum size check and NO
                liquidity check anywhere in the system. That order would walk
                the entire visible book, fill percent away from the price the
                decision was made at, and make the computed stop meaningless —
                the position would already be past it on entry.

A stop is a promise about how much a trade can lose. An order that moves the
market breaks that promise before the stop is ever tested, which is why this
belongs beside the risk gateway rather than in execution.

WHAT IT MEASURES
----------------
`absorbable_notional` walks the real resting depth this system already fetches
(`microstructure_feed` supplies 50 levels on every run) and returns the
notional that can be taken before the average fill slips more than
`max_slippage_pct` from the touch. That is a measurement of THIS book at THIS
moment, not a constant — which is the point, because the same $8M is nothing
on BTC and impossible on a small alt.

`PARTICIPATION_FRACTION` then takes a fraction of it. Filling every last
resting order at the tolerance would move the price by exactly the tolerance
and leave nothing for the stop to exit into — and the exit is the half that
matters, because it happens when the market is already against the position.

HONEST LIMITS, STATED RATHER THAN IMPLIED
-----------------------------------------
* Visible depth is not all liquidity. Icebergs, hidden orders and market
  makers replenishing mean a real fill is often better than this predicts.
  Under-estimating is the safe direction and is chosen deliberately.
* A snapshot is instantaneous. The book at fill time is not the book that was
  measured, which is another reason for the participation fraction.
* It says nothing about whether the trade is GOOD. It is a ceiling, never a
  reason to trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# How far the AVERAGE fill may slip from the touch before the book is called
# exhausted. 0.15% is well inside this system's tightest stop (a 2.5-ATR stop
# on a quiet instrument is ~1%), so an order sized to it cannot arrive already
# a meaningful fraction of the way to its own stop.
MAX_SLIPPAGE_PCT = 0.15

# What share of that absorbable depth one order may take.
#
# Not a tuning constant: taking all of it would move the price by the full
# tolerance and leave nothing resting for the STOP to exit into, and the exit
# is the half that happens when the market is already against the position.
# A fifth leaves the book able to absorb the round trip.
PARTICIPATION_FRACTION = 0.20


@dataclass(frozen=True)
class DepthVerdict:
    """A ceiling, with everything needed to explain it.

    `notional` is None when the book could not be read — NOT zero. Zero would
    mean "this market can absorb nothing", which would block every trade on a
    feed outage; None means "unmeasured" and the caller leaves its size alone.
    This is the True/False/None rule this codebase has now paid for five
    times, in the one direction that matters here: a missing measurement must
    not become a refusal, because the gateway's other limits still apply.
    """

    notional: Optional[float]
    levels_used: int = 0
    reason: str = ""


def _levels(rows: Sequence[Any]) -> List[tuple]:
    """Normalise `[{price, qty}]` or `[[price, qty]]` into tuples."""
    out: List[tuple] = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                price = float(row.get("price"))
                qty = float(row.get("qty"))
            else:
                price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if price > 0 and qty > 0:
            out.append((price, qty))
    return out


def absorbable_notional(
    book_side: Sequence[Any],
    *,
    max_slippage_pct: float = MAX_SLIPPAGE_PCT,
) -> DepthVerdict:
    """Notional fillable before the AVERAGE fill slips past the tolerance.

    `book_side` is the side being TAKEN: asks for a buy, bids for a sell. The
    distinction matters — a book can be deep on one side and thin on the other,
    and that asymmetry is exactly when a large order is most dangerous.

    The average is the right measure rather than the worst level: a trade's P&L
    is set by what it actually paid across the whole fill, not by its last
    touch.
    """
    levels = _levels(book_side)
    if len(levels) < 2:
        return DepthVerdict(None, 0, "the order book could not be read")

    # Assume the venue's own ordering (best first) rather than re-sorting: a
    # side sorted the wrong way would otherwise look infinitely deep.
    touch = levels[0][0]
    if touch <= 0:
        return DepthVerdict(None, 0, "the book's best price is not usable")

    limit = touch * (1.0 + max_slippage_pct / 100.0) if levels[-1][0] >= touch \
        else touch * (1.0 - max_slippage_pct / 100.0)

    filled_qty = 0.0
    filled_cost = 0.0
    used = 0
    for price, qty in levels:
        prospective_qty = filled_qty + qty
        prospective_cost = filled_cost + price * qty
        avg = prospective_cost / prospective_qty if prospective_qty > 0 else price
        # Past the tolerance ON AVERAGE: take the part of this level that fits
        # rather than discarding it, so the answer does not jump at a level
        # boundary.
        beyond = (avg > limit) if levels[-1][0] >= touch else (avg < limit)
        if beyond:
            target_avg = limit
            denom = price - target_avg
            if denom != 0:
                partial = (filled_cost - target_avg * filled_qty) / -denom
                if partial > 0:
                    filled_qty += min(partial, qty)
                    filled_cost += min(partial, qty) * price
                    used += 1
            break
        filled_qty = prospective_qty
        filled_cost = prospective_cost
        used += 1

    if filled_qty <= 0:
        return DepthVerdict(None, 0, "no level was within the slippage tolerance")

    return DepthVerdict(
        notional=filled_cost,
        levels_used=used,
        reason=(
            f"{filled_qty:.6g} units ({filled_cost:,.2f} quote) rest within "
            f"{max_slippage_pct:.2f}% of {touch:.8g} across {used} level(s)"
        ),
    )


def cap_for(
    side: str,
    snapshot: Any,
    *,
    participation: float = PARTICIPATION_FRACTION,
    max_slippage_pct: float = MAX_SLIPPAGE_PCT,
) -> DepthVerdict:
    """The notional ceiling for a `buy`/`sell` against this snapshot's book.

    Reads the book the single fetch point already put on `MarketSnapshot`, so
    this adds no market-data call and stays replay-safe (Section 39.4) — a node
    that fetched its own depth would reason over a different book on a resumed
    checkpoint.
    """
    taking = "asks" if str(side).lower() in ("buy", "long") else "bids"
    rows = getattr(snapshot, f"order_book_{taking}", None) if snapshot is not None else None
    verdict = absorbable_notional(rows or [], max_slippage_pct=max_slippage_pct)
    if verdict.notional is None:
        return verdict
    return DepthVerdict(
        notional=verdict.notional * participation,
        levels_used=verdict.levels_used,
        reason=(
            f"{participation:.0%} of the {taking} depth within "
            f"{max_slippage_pct:.2f}%: {verdict.reason}"
        ),
    )
