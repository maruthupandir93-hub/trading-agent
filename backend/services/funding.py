"""Perpetual funding — the other cost that never reached a P&L figure.

WHY THIS EXISTS
===============
This system trades perpetual futures and had no concept of funding at all. The
word appears in a `funding` SPECIALIST that reads the funding RATE as a
directional signal, and nowhere in any accounting path: no position was ever
charged for being held.

Funding is not a rounding error on a multi-hour hold. At a typical 0.01% per 8h
settlement a position held a day pays ~0.03% of NOTIONAL — and because the
account's exposure is `leverage x` its equity, that is ~0.3% of a 10x account per
day, in one direction, regardless of whether the trade works. In a strongly
trending market the rate routinely reaches 0.05-0.10% per settlement, at which
point holding the crowded side costs more per day than this system's entire
per-trade edge.

FUNDING IS DISCRETE. THIS IS THE WHOLE CORRECTNESS ARGUMENT OF THIS MODULE.
==========================================================================
It is NOT rent that accrues by the second. It is charged at fixed wall-clock
settlements — 00:00, 08:00 and 16:00 UTC on both Binance and Bybit linear
perpetuals — and ONLY to positions open at that instant. The consequences the
obvious implementation gets wrong:

    opened 09:00, closed 15:00  (6h)   -> crosses nothing        -> pays ZERO
    opened 15:30, closed 16:30  (1h)   -> crosses 16:00          -> pays ONE
    opened 09:00, closed 17:00  (8h)   -> crosses 16:00          -> pays ONE

A pro-rated `rate * hours / 8` model gets all three wrong, and gets them wrong in
different directions, so the error does not even average out. It would charge the
six-hour hold 0.75 settlements it never paid — and this system's own trades are
mostly short, so the majority of them would be billed for funding they never
owed.

SIGN: THE PAYER IS THE SIDE THE RATE FAVOURS AGAINST
====================================================
A POSITIVE funding rate means longs pay shorts. So a long is charged and a short
is CREDITED, and the credit is real income that must not be discarded — a short
held through a strongly positive funding regime earns money for doing nothing,
and a model that only ever subtracts would understate exactly the trades this
system takes most.

WHAT THIS MODULE DOES NOT DO
============================
No I/O, ever. The close path is safety-critical and must not wait on an HTTP
round trip to learn what a position cost — an unclosed position is a risk
problem, an unmeasured cost is an accounting one, which is the same trade-off
`services/fees` makes and for the same reason.

So the RATE is captured once at entry (where market data is already being
fetched) and carried on the position. That makes the result an ESTIMATE: the rate
floats between settlements, and a position held across several may be charged a
different rate at each. `FundingEstimate.measured` is therefore always False
here, in the same spirit as `fees.FeeResult.measured` — a modelled cost must
never read as a settled one. `Venue.fetch_funding_paid` is the measured
counterpart for a real position, and it is deliberately NOT on the close path.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Settlement hours, UTC. Binance USDⓈ-M and Bybit linear perpetuals both settle
# every 8 hours at these times for the overwhelming majority of symbols.
#
# SOME SYMBOLS SETTLE MORE OFTEN. Bybit runs 1h and 4h intervals on a handful of
# contracts, and Binance switches a symbol to 4h when its rate is persistently
# extreme. Both are UNDER-counted by this table, never over-counted — so the
# estimate errs toward reporting a position as cheaper than it was. That is the
# wrong direction for a cost, and it is accepted only because the alternative is
# an I/O call on the close path; `Venue.fetch_funding_paid` is the way to get the
# real number when it matters.
SETTLEMENT_HOURS = (0, 8, 16)

# What a settlement costs when no rate was captured. 0.01% per 8h is the venues'
# own baseline — the rate they revert to when the perpetual is trading close to
# spot — so it is a real default rather than a guess, and it is small enough that
# using it in place of a missing measurement cannot dominate a P&L figure.
DEFAULT_RATE_PER_SETTLEMENT = 0.0001


@dataclass(frozen=True)
class FundingEstimate:
    """What a hold cost (positive) or earned (negative) in funding."""

    cost: float               # SIGNED: >0 paid out, <0 received
    settlements: int
    rate: float
    measured: bool
    detail: str


def settlements_between(
    opened_at: Optional[datetime.datetime],
    closed_at: Optional[datetime.datetime],
) -> int:
    """How many funding settlements a position open over this window was charged.

    Counts settlement instants STRICTLY AFTER `opened_at` and AT OR BEFORE
    `closed_at`. The boundary rule matters and is not arbitrary:

      * a position opened exactly AT 16:00 is not charged for that settlement —
        it did not exist when the snapshot was taken;
      * a position closed exactly AT 16:00 IS charged — it was open at the
        instant the venue billed.

    Getting the half-open interval backwards double-charges every position opened
    on a settlement boundary, which is a common moment to enter because it is
    when the rate resets.

    Naive datetimes are assumed UTC, which is what the whole codebase uses
    (`datetime.utcnow()` everywhere). An aware datetime is converted rather than
    rejected, because `monitored_positions.opened_at` is `timestamptz` and comes
    back from asyncpg aware — the same boundary conversion
    `position_store._as_naive_utc` exists for.
    """
    if opened_at is None or closed_at is None:
        return 0

    start = _as_naive_utc(opened_at)
    end = _as_naive_utc(closed_at)
    if end <= start:
        return 0

    count = 0
    # Walk from the first midnight at or before `start`. A day has at most three
    # settlements, so this is a handful of iterations even for a long hold, and
    # it is exact — no modular arithmetic to get wrong around month boundaries or
    # a leap second.
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for hour in SETTLEMENT_HOURS:
            moment = day.replace(hour=hour)
            if start < moment <= end:
                count += 1
        day += datetime.timedelta(days=1)
    return count


def _as_naive_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is not None:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def estimate_funding(
    *,
    side: str,
    notional: float,
    opened_at: Optional[datetime.datetime],
    closed_at: Optional[datetime.datetime],
    rate: Optional[float] = None,
) -> FundingEstimate:
    """Funding for one hold. POSITIVE is a cost, NEGATIVE is income.

    `rate` is the per-settlement funding rate as captured at entry (e.g. 0.0001
    for 0.01%). A POSITIVE rate means longs pay shorts, which is the venues'
    convention, so a long is charged and a short is credited.
    """
    settlements = settlements_between(opened_at, closed_at)
    effective_rate = DEFAULT_RATE_PER_SETTLEMENT if rate is None else float(rate)

    if settlements == 0:
        # The common case for this system: its holds are typically well under 8
        # hours, so most trades genuinely owe nothing. Reported explicitly so a
        # zero reads as "crossed no settlement" rather than "not implemented".
        return FundingEstimate(
            cost=0.0, settlements=0, rate=effective_rate, measured=False,
            detail="held across no funding settlement, so no funding was charged",
        )

    # A long PAYS a positive rate; a short RECEIVES it.
    direction = 1.0 if str(side).lower() in ("buy", "long") else -1.0
    cost = direction * effective_rate * abs(float(notional or 0.0)) * settlements

    return FundingEstimate(
        cost=cost,
        settlements=settlements,
        rate=effective_rate,
        measured=False,
        detail=(
            f"{settlements} settlement(s) at {effective_rate:+.4%} on "
            f"{abs(notional or 0.0):.8g} notional "
            f"({'paid' if cost > 0 else 'received'} {abs(cost):.6g})"
            + ("" if rate is not None else " — no rate captured at entry, venue baseline assumed")
        ),
    )
