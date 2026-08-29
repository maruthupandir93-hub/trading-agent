"""Order-book and trade-tape measurement — the two feed-blocked Section 9 specialists.

WHAT THIS UNBLOCKS, AND WHY THE OLD BLOCKER TEXT WAS TRUE WHEN IT WAS WRITTEN
----------------------------------------------------------------------------
`graphs/nodes/specialists.py` shipped three specialists hardcoded to
`available=False`:

    orderflow   "no order-book or trade-tape feed is subscribed: aggressor side
                 and bid/ask imbalance require level-2 depth and per-trade taker
                 flags, and this system consumes only OHLCV candles and mark price"
    liquidity   "no order-book depth feed is subscribed: executable depth and
                 spread require level-2 quotes, and traded volume is not a
                 substitute for them"

Both statements were accurate about the GRAPH, and both stopped being accurate
about the SYSTEM. `api/marketdata.py::get_orderflow` fetches
`/api/v3/depth?limit=50` and `/api/v3/aggTrades?limit=200` from Binance, and it
works — verified live returning 50 bid levels, 50 ask levels and 200 trades each
carrying `buyerIsMaker`, which is exactly the per-trade taker flag the orderflow
blocker said did not exist. The feed was built for the dashboard and never handed
to the reasoning layer.

The cost of leaving it that way was not cosmetic. `run_debate` scales confidence
by COVERAGE — the fraction of directional panel weight that could be measured —
and orderflow carries 1.5 of 7.0. With orderflow and news both dark, coverage sat
at 0.57 and every verdict was multiplied by it, so a clean directional read of
0.35 arrived at the Supervisor as 0.20 against a 0.18 minimum. The agent was not
being cautious; it was being throttled by two specialists that had data available
the whole time.

WHY THE MATH IS HERE AND THE FETCHING IS NOT
--------------------------------------------
`services/` does I/O, `algorithms/` is pure and unit-tested — the Python mirror of
CLAUDE.md's "pure logic in lib/, side effects in components/". These functions take
already-fetched levels and trades and return measurements. They perform no network
call, read no clock, and are therefore replay-safe under Section 39.4, which
matters because a graph node calling out to an exchange would produce a different
answer on a checkpoint replay than it did on the original run.

WHAT THESE MEASUREMENTS ARE NOT
-------------------------------
A 50-level depth snapshot is a SNAPSHOT. It says what was resting at one instant,
not what will still be there when an order arrives, and a large share of visible
depth on a liquid venue is cancelled rather than filled. So:

  * `depth_score` is scored against a notional the caller supplies, and is
    described as "visible resting size", never as guaranteed fill.
  * Nothing here claims to detect spoofing, icebergs or hidden liquidity. Those
    need depth CHANGES over time, and one snapshot cannot see them.
  * `aggressor_imbalance` is over a 200-trade window, which on a busy pair is a
    few seconds. It is a very short-horizon reading and is labelled as one.

Every function returns `available=False` with a stated reason rather than a
neutral-looking number when its input cannot support a measurement. A zero
imbalance and an unmeasured imbalance are different facts, and only one of them
is evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# How far from the mid price counts as "near touch" for executable depth.
#
# 10 bps is where a taker order on a major pair stops being a fill and starts
# being an event. Depth beyond it exists but is not what a normal entry consumes,
# and counting it would flatter every liquidity reading.
NEAR_TOUCH_BPS = 10.0

# A spread wider than this is a real obstacle to acting, not a rounding detail.
# On BTC/USDT the spread is typically well under 1 bp, so 5 bps already means
# something unusual is happening.
SPREAD_CONCERN_BPS = 5.0

# Aggressor imbalance beyond this is treated as directional evidence rather than
# noise. Below it, buy and sell aggression are close enough that calling a side
# would be reading a coin flip.
AGGRESSOR_NEUTRAL_BAND = 0.15

# Book imbalance beyond this is directional evidence. Deliberately wider than the
# aggressor band: resting size is far easier to place and cancel than an executed
# trade is to undo, so it deserves more scepticism per unit of imbalance.
BOOK_NEUTRAL_BAND = 0.25

# Minimum inputs below which nothing is measured rather than measured badly.
MIN_LEVELS_PER_SIDE = 5
MIN_TRADES = 20


@dataclass
class BookReading:
    """One order-book snapshot, measured."""

    available: bool
    reason: Optional[str] = None
    mid_price: Optional[float] = None
    spread_bps: Optional[float] = None
    # +1.0 = all near-touch resting size is on the bid (buyers), -1.0 = all ask.
    imbalance: Optional[float] = None
    # Quote-currency notional resting within NEAR_TOUCH_BPS of mid, per side.
    bid_notional_near: Optional[float] = None
    ask_notional_near: Optional[float] = None
    levels_used: int = 0
    # The price range the feed actually returned, in bps from mid, taken as the
    # SMALLER of the two sides. See `analyse_order_book` for why this is reported
    # instead of the requested window.
    covered_bps: Optional[float] = None
    evidence: List[str] = field(default_factory=list)


@dataclass
class TapeReading:
    """A window of executed trades, measured by aggressor side."""

    available: bool
    reason: Optional[str] = None
    # +1.0 = every unit traded was taker-buy, -1.0 = every unit taker-sell.
    imbalance: Optional[float] = None
    buy_volume: Optional[float] = None
    sell_volume: Optional[float] = None
    trades_used: int = 0
    # 'buyers' | 'sellers' | 'balanced', or None when unavailable.
    aggressor_side: Optional[str] = None
    evidence: List[str] = field(default_factory=list)


def _levels(rows: Optional[Sequence[Any]]) -> List[Tuple[float, float]]:
    """Coerce `[{price, qty}, ...]` (or `[[price, qty], ...]`) to floats.

    Malformed rows are DROPPED, not defaulted to zero. A level with an
    unparseable size is unknown size; treating it as no size would understate
    depth and treating it as some size would invent it.
    """
    out: List[Tuple[float, float]] = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                price, qty = float(row["price"]), float(row["qty"])
            else:
                price, qty = float(row[0]), float(row[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if price > 0 and qty > 0:
            out.append((price, qty))
    return out


def analyse_order_book(
    bids: Optional[Sequence[Any]],
    asks: Optional[Sequence[Any]],
) -> BookReading:
    """Spread, near-touch depth and resting-size imbalance from one snapshot.

    `bids` must be descending by price and `asks` ascending — the order every
    exchange returns them in. Rather than trusting that, the best bid and ask are
    taken as max/min, so a reversed feed produces a correct reading instead of a
    negative spread nobody notices.
    """
    bid_levels = _levels(bids)
    ask_levels = _levels(asks)

    if len(bid_levels) < MIN_LEVELS_PER_SIDE or len(ask_levels) < MIN_LEVELS_PER_SIDE:
        return BookReading(
            available=False,
            reason=(
                f"order book too thin to measure: {len(bid_levels)} bid and "
                f"{len(ask_levels)} ask level(s), need {MIN_LEVELS_PER_SIDE} each"
            ),
        )

    best_bid = max(p for p, _ in bid_levels)
    best_ask = min(p for p, _ in ask_levels)

    if best_ask <= best_bid:
        # A crossed book is a broken or stale snapshot, not a tradeable market.
        # Reported rather than measured: every number derived from it would be
        # wrong in a way that looks plausible.
        return BookReading(
            available=False,
            reason=(
                f"crossed book: best bid {best_bid:.8g} is at or above best ask "
                f"{best_ask:.8g}, so the snapshot is stale or malformed"
            ),
        )

    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid) * 10_000.0

    window = mid * (NEAR_TOUCH_BPS / 10_000.0)
    bid_notional = sum(p * q for p, q in bid_levels if p >= mid - window)
    ask_notional = sum(p * q for p, q in ask_levels if p <= mid + window)

    # HOW MUCH BOOK THE FEED ACTUALLY RETURNED, which is usually far less than
    # the window asked for and must not be described as though it were the same.
    #
    # Measured on BTC/USDT: a 50-level depth call spans about 1.3 bps on the bid
    # and 1.5 bps on the ask, so every level lands inside a 10 bps window and the
    # window does no filtering at all. Saying "depth within 10 bps" there would
    # imply the book was sampled to 10 bps and found to look like this, when what
    # actually happened is that the feed ran out at 1.5 and nothing beyond it was
    # ever seen. On a thin altcoin the same 50 levels may span far more than 10
    # bps and the window does bind. Both cases are reported as what they are.
    bid_span_bps = ((mid - min(p for p, _ in bid_levels)) / mid) * 10_000.0
    ask_span_bps = ((max(p for p, _ in ask_levels) - mid) / mid) * 10_000.0
    covered_bps = min(bid_span_bps, ask_span_bps)
    truncated = covered_bps < NEAR_TOUCH_BPS

    total = bid_notional + ask_notional
    if total <= 0:
        return BookReading(
            available=False,
            reason=(
                f"no resting size within {NEAR_TOUCH_BPS:.0f} bps of mid, so there "
                f"is no near-touch depth to measure"
            ),
            mid_price=mid,
            spread_bps=spread_bps,
        )

    imbalance = (bid_notional - ask_notional) / total

    evidence = [
        f"spread {spread_bps:.2f} bps at mid {mid:.8g}",
        (
            f"resting size over the {covered_bps:.2f} bps the feed returned "
            f"({len(bid_levels)}+{len(ask_levels)} levels): "
            f"bid {bid_notional:,.0f} vs ask {ask_notional:,.0f} (quote ccy)"
        ),
        (
            f"resting-size imbalance {imbalance:+.2f} "
            f"(+1 all bid, -1 all ask; |{BOOK_NEUTRAL_BAND}| neutral band)"
        ),
        (
            "snapshot only — visible resting size is not guaranteed fill, and one "
            "snapshot cannot distinguish real depth from orders that will be pulled"
        ),
    ]
    if truncated:
        evidence.append(
            f"the {NEAR_TOUCH_BPS:.0f} bps window did NOT bind: the feed's "
            f"{len(bid_levels)} levels per side only reach {covered_bps:.2f} bps, so "
            f"nothing beyond that was observed and this says nothing about depth further out"
        )

    return BookReading(
        available=True,
        mid_price=mid,
        spread_bps=spread_bps,
        imbalance=imbalance,
        bid_notional_near=bid_notional,
        ask_notional_near=ask_notional,
        levels_used=len(bid_levels) + len(ask_levels),
        covered_bps=covered_bps,
        evidence=evidence,
    )


def analyse_tape(trades: Optional[Sequence[Dict[str, Any]]]) -> TapeReading:
    """Aggressor-side volume imbalance over a window of executed trades.

    `buyerIsMaker` is Binance's flag and its sense is easy to invert, so it is
    spelled out: True means the BUYER was resting and the SELLER crossed the
    spread, i.e. the trade was taker-SELL. This is the single most likely place
    for a sign error in this module, and a sign error here would hand the debate
    a confident reading of exactly the wrong direction.
    """
    rows = [t for t in (trades or []) if isinstance(t, dict)]
    if len(rows) < MIN_TRADES:
        return TapeReading(
            available=False,
            reason=(
                f"trade tape too short to measure: {len(rows)} trade(s), "
                f"need {MIN_TRADES}"
            ),
        )

    buy_volume = 0.0
    sell_volume = 0.0
    used = 0
    for t in rows:
        try:
            qty = float(t["qty"])
            price = float(t["price"])
            buyer_is_maker = bool(t["buyerIsMaker"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        notional = qty * price
        # buyerIsMaker=True  -> the seller crossed -> taker SELL
        # buyerIsMaker=False -> the buyer crossed  -> taker BUY
        if buyer_is_maker:
            sell_volume += notional
        else:
            buy_volume += notional
        used += 1

    if used < MIN_TRADES:
        return TapeReading(
            available=False,
            reason=(
                f"only {used} of {len(rows)} trade(s) carried a usable price, size "
                f"and taker flag, below the {MIN_TRADES} needed"
            ),
        )

    total = buy_volume + sell_volume
    if total <= 0:
        return TapeReading(
            available=False,
            reason="every usable trade had zero notional, so there is no flow to measure",
            trades_used=used,
        )

    imbalance = (buy_volume - sell_volume) / total
    if imbalance > AGGRESSOR_NEUTRAL_BAND:
        side = "buyers"
    elif imbalance < -AGGRESSOR_NEUTRAL_BAND:
        side = "sellers"
    else:
        side = "balanced"

    evidence = [
        (
            f"taker flow over the last {used} trade(s): buy {buy_volume:,.0f} vs "
            f"sell {sell_volume:,.0f} (quote ccy)"
        ),
        (
            f"aggressor imbalance {imbalance:+.2f} -> {side} "
            f"(neutral band |{AGGRESSOR_NEUTRAL_BAND}|)"
        ),
        (
            "a 200-trade window is seconds to minutes on a liquid pair — this is "
            "very short-horizon evidence and does not describe the session"
        ),
    ]

    return TapeReading(
        available=True,
        imbalance=imbalance,
        buy_volume=buy_volume,
        sell_volume=sell_volume,
        trades_used=used,
        aggressor_side=side,
        evidence=evidence,
    )


def orderflow_stance(book: BookReading, tape: TapeReading) -> Tuple[Optional[str], Optional[float], List[str]]:
    """Combine book and tape into a directional stance for the panel.

    Returns `(stance, confidence, evidence)`, or `(None, None, evidence)` when
    neither leg could be measured.

    THE TAPE IS WEIGHTED MORE THAN THE BOOK, DELIBERATELY.
    Executed trades are commitments; resting orders are intentions and can be
    withdrawn for free. Weighting them equally would let a wall of resting size
    — the cheapest thing on an exchange to fake — carry the same authority as
    money that actually changed hands.

    Confidence is scaled by how many of the two legs were available, so a stance
    derived from the tape alone cannot present itself as strongly as one where
    both agree. That mirrors what `run_debate` does with panel coverage.
    """
    TAPE_WEIGHT = 0.65
    BOOK_WEIGHT = 0.35

    # Conviction ceiling, for the same reason `news_sentiment.MAX_CONFIDENCE`
    # exists: the horizon of the evidence has to match the horizon of the claim.
    #
    # A 200-trade tape is seconds on BTC/USDT and a depth snapshot is one instant
    # of a book that is mostly cancellable. Measured live, that combination
    # produced a stance of 0.87 — a near-maximum directional vote about a
    # position the agent intends to hold for hours, derived from the last few
    # seconds of aggression. The measurement is real; the conviction it implies
    # over that horizon is not, so it is capped here rather than in the debate,
    # where the cap would be invisible to anyone reading this function.
    MAX_ORDERFLOW_CONFIDENCE = 0.70

    evidence: List[str] = []
    weighted = 0.0
    available_weight = 0.0

    if tape.available and tape.imbalance is not None:
        weighted += TAPE_WEIGHT * tape.imbalance
        available_weight += TAPE_WEIGHT
        evidence.extend(tape.evidence)
    elif tape.reason:
        evidence.append(f"tape not measured: {tape.reason}")

    if book.available and book.imbalance is not None:
        weighted += BOOK_WEIGHT * book.imbalance
        available_weight += BOOK_WEIGHT
        evidence.extend(book.evidence)
    elif book.reason:
        evidence.append(f"book not measured: {book.reason}")

    if available_weight <= 0.0:
        return None, None, evidence

    # Normalised to the legs that were actually available, then scaled back down
    # by coverage. Without the second step, one available leg would produce the
    # same confidence as two agreeing ones.
    net = weighted / available_weight
    coverage = available_weight / (TAPE_WEIGHT + BOOK_WEIGHT)

    if net > AGGRESSOR_NEUTRAL_BAND:
        stance = "supports_long"
    elif net < -AGGRESSOR_NEUTRAL_BAND:
        stance = "supports_short"
    else:
        stance = "neutral"

    confidence = min(MAX_ORDERFLOW_CONFIDENCE, abs(net)) * coverage
    evidence.append(
        f"combined flow {net:+.2f} (tape {TAPE_WEIGHT}, book {BOOK_WEIGHT}) at "
        f"{coverage:.0%} leg coverage -> {stance} @ {confidence:.2f} "
        f"(ceiling {MAX_ORDERFLOW_CONFIDENCE}: a seconds-long window cannot support "
        f"full conviction about an hours-long position)"
    )
    return stance, confidence, evidence


def liquidity_concern(book: BookReading, intended_notional: Optional[float]) -> Tuple[Optional[float], List[str]]:
    """How strongly depth and spread argue against acting at full size.

    Returns `(concern, evidence)` where concern is 0.0-1.0, or `(None, evidence)`
    when the book could not be measured.

    A CONSTRAINT, NOT A VOTE. Thin depth is a reason to size down or wait; it is
    never a reason to pick a side. `SpecialistFinding.role` enforces that
    distinction and this function only ever produces a `concern`.

    `intended_notional` is what the caller is actually thinking of trading. When
    it is unknown the depth half is skipped rather than assumed — scoring depth
    against a guessed order size would produce a concern number derived from a
    number nobody supplied.
    """
    if not book.available:
        return None, [f"depth not measured: {book.reason}"]

    evidence: List[str] = list(book.evidence)
    concerns: List[float] = []

    if book.spread_bps is not None:
        # Linear from 0 at a zero spread to 1.0 at twice the concern threshold.
        spread_concern = min(1.0, book.spread_bps / (SPREAD_CONCERN_BPS * 2.0))
        concerns.append(spread_concern)
        evidence.append(
            f"spread concern {spread_concern:.2f} "
            f"({book.spread_bps:.2f} bps against a {SPREAD_CONCERN_BPS:.0f} bps threshold)"
        )

    if intended_notional and intended_notional > 0:
        # Only the side that would be consumed matters. Depth on the side you are
        # not taking is irrelevant to your fill, and averaging both would flatter
        # a one-sided book.
        near = min(
            v for v in (book.bid_notional_near, book.ask_notional_near) if v is not None
        )
        if near > 0:
            ratio = intended_notional / near
            depth_concern = min(1.0, ratio)
            concerns.append(depth_concern)
            evidence.append(
                f"size concern {depth_concern:.2f}: intended {intended_notional:,.0f} "
                f"against {near:,.0f} of visible near-touch depth on the thinner side"
            )
    else:
        evidence.append(
            "no intended size supplied, so depth was not scored against one — "
            "guessing an order size would invent the number the concern is derived from"
        )

    if not concerns:
        return None, evidence

    # max(), not mean(). Concerns are obstacles: a book that is tight but far too
    # thin is still too thin, and averaging would let one healthy dimension
    # cancel out a blocking one.
    return max(concerns), evidence
