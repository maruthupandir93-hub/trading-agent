"""How far into the move are we entering? — the question nothing asked.

THE REVIEW'S SECTION 6C, AND THIS SYSTEM'S OWN PROFILE SAID IT FIRST
====================================================================
    *"Require a confirmed move, sufficient liquidity, and a reasonable entry
    distance. Avoid chasing an extended candle where the stop is too far away
    or the reward-to-risk ratio has deteriorated."*

`strategy_profiles.Breakout` has named this as its own worst condition since it
was written:

    worst_conditions="A market already extended in the breakout direction —
                      the move is over."

Nothing enforced it. That is the shape this codebase keeps finding: a fact
written down, correct, and never reached by a decision.

WHY IT IS NOT ALREADY COVERED BY THE GATES BESIDE IT
=====================================================
Three nearby checks look like they might cover this and none does:

    the HTF gate         refuses a CLEAR COUNTER-trend entry. Chasing is the
                         opposite error — the entry agrees with the trend, it
                         is simply late.
    the fee gate         refuses a stop too TIGHT to survive the round trip.
                         Chasing produces a stop too WIDE.
    the volatility gate  scales SIZE by realised volatility. A late entry at a
                         smaller size is still a late entry; the payoff is
                         what deteriorated, not the dollar risk.

So the whole family of "right direction, wrong moment" entries passed every
gate. On a fixed 2:1 risk model that matters more than it sounds: the target is
a multiple of the STOP, so a stop widened by an extended entry moves the target
further away by the same multiple, into the part of the move least likely to
still be there.

WHAT IT MEASURES
================
Two independent quantities, because they fail differently and a single number
would hide which one fired:

    EXTENSION   how far price has already travelled in the entry's direction,
                measured in ATR from the recent swing the move began at. This
                is "how late am I", and it is scale-free, so it is comparable
                across instruments exactly as R is.
    STRETCH     how far price sits from the entry's own invalidation (the
                stop), in ATR. This is "how much am I risking to find out I
                was wrong", and it is what the review calls a reasonable entry
                distance.

PURE AND DETERMINISTIC. It reads candles `validate_market_data` already
fetched; a node that fetched its own data would not be replay-safe
(Section 39.4), and this is read by a node.

IT WAS A HYPOTHESIS AND IT WAS MEASURED, AND THE MEASUREMENT SAYS THE OPPOSITE
=============================================================================
The gate was built on the review's recommendation and on this system's own
Breakout profile, both of which say a late entry is a worse entry. Before
shipping it on by default it was replayed through the REAL `assess` against
17,157 signals on six instruments and 1,000 real 15m candles each, calling
each strategy's REAL signal function on bars up to and including the entry bar
— never with hindsight. `scripts/measure_entry_extension.py` reproduces it:

    every signal          -0.0182R +/-0.0211   32.7%   17,157 trades
    the gate would KEEP   -0.0503R +/-0.0259   31.7%   11,144 trades
    the gate would BLOCK  +0.0412R +/-0.0361   34.7%    6,013 trades

    gap = -0.0915R, z = -4.04, and it survives a correction for the
    comparisons made. `evidence.required_gap(6013)` puts the smallest
    detectable gap at 0.0371R — the measured gap is 2.5x that.

**THE GATE BLOCKS THE BETTER HALF.** Entering 3+ ATR into a move is momentum,
and on this account's instruments momentum has been the thing that works:
the same ledger that shows the range and mean-reversion strategies losing, and
that showed the HTF gate removing a genuinely losing half, shows late entries
outperforming early ones by a margin four standard errors wide.

SO THE GATE DEFAULTS **OFF**, and that is the finding, not a failure to build
it. The measurement ran against the author's hypothesis, which is the only
direction this kind of result is ever worth trusting — `core/evidence.py`
exists because a throwaway script once produced three "findings" that all ran
the way the author was hoping.

WHAT STAYS ON IS THE MEASUREMENT. The extension is computed and written into
every entry-context snapshot regardless, because turning a gate off must not
also stop recording the data that would judge it — the arrangement
`REQUIRE_HTF_ALIGNMENT` already uses. If a future ledger disagrees,
`REQUIRE_ENTRY_PROXIMITY=true` turns the refusal on and
`MAX_ENTRY_EXTENSION_ATR` moves the bar, both read at call time.

ONE LIMIT, STATED: this is a replay of a DETERMINISTIC signal function, not of
the nine-specialist panel, which needs an order book, a tape and news and
cannot be replayed offline. So it says the premise is wrong on the strategies
that can be measured; it does not license the opposite gate.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# How many candles back to look for the swing the current move began at.
# Twenty 15m bars is five hours — long enough to contain a breakout's base and
# short enough that it is still THIS move rather than last week's.
SWING_LOOKBACK = 20

# Below this many candles there is no move to measure the extension of.
MIN_CANDLES = SWING_LOOKBACK + 2

# How far price may already have travelled, in ATR, before an entry in that
# direction counts as chasing.
#
# 3.0 is derived rather than picked: the risk model places its stop at 2.5 ATR
# and its target at 5.0. An entry already 3 ATR into a move needs another 5 to
# reach its target, so the move must more than double from a point it has
# already run — while the stop sits 2.5 ATR back, inside the move's own
# retracement range. Below 3 the arithmetic still works; above it the trade is
# asking the market for a bigger second half than its first.
DEFAULT_MAX_EXTENSION_ATR = 3.0
EXTENSION_VAR = "MAX_ENTRY_EXTENSION_ATR"

# The gate, env-readable at CALL time (the `simulation_mode` rule: an operator
# who turns this on expects the next decision to see it, not the next restart).
#
# DEFAULT **OFF**, unlike the HTF gate, and for the opposite reason: the HTF
# gate was measured to remove a losing half and this one was measured to remove
# the WINNING half (-0.0915R, z = -4.04, 17,157 trades). See the module
# docstring. Shipping it on would be acting on a plausible story against the
# only evidence available.
GATE_VAR = "REQUIRE_ENTRY_PROXIMITY"


def max_extension_atr() -> float:
    """Read at call time, never frozen at import."""
    raw = os.getenv(EXTENSION_VAR)
    if not raw:
        return DEFAULT_MAX_EXTENSION_ATR
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %.1f.", EXTENSION_VAR, raw,
                       DEFAULT_MAX_EXTENSION_ATR)
        return DEFAULT_MAX_EXTENSION_ATR
    return value if value > 0 else DEFAULT_MAX_EXTENSION_ATR


def gate_enabled() -> bool:
    """Default OFF — the measurement overturned the hypothesis.

    The extension is still MEASURED and recorded on every entry; only the
    refusal is off. `REQUIRE_ENTRY_PROXIMITY=true` turns it on for anyone whose
    own ledger disagrees with the 17,157-trade replay in the module docstring.
    """
    raw = (os.getenv(GATE_VAR) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class EntryExtension:
    """How late this entry is, with every number that produced the verdict.

    `extension_atr` is None when it could not be measured — too few candles, no
    usable ATR, or a swing that does not exist. None is NOT zero and must never
    be read as "not extended": an unmeasurable extension is a reason to say so,
    not a reason to claim the entry is early. Same True/False/None rule the
    rest of this codebase runs on.
    """

    direction: str                      # "LONG" | "SHORT"
    extension_atr: Optional[float]
    stretch_atr: Optional[float]
    swing_price: Optional[float]
    entry_price: Optional[float]
    atr: Optional[float]
    limit_atr: float
    chasing: bool
    reason: str

    def as_context(self) -> str:
        """One line for the entry-context snapshot.

        APPENDED to the existing context, never inserted, because four frontend
        parsers match that string with regexes written against its current
        shape — the lesson `build_entry_context` already records.
        """
        if self.extension_atr is None:
            return f"entry extension unmeasured ({self.reason})"
        return (
            f"entry {self.extension_atr:.2f} ATR into the move, "
            f"{self.stretch_atr:.2f} ATR from invalidation"
            if self.stretch_atr is not None else
            f"entry {self.extension_atr:.2f} ATR into the move"
        )


def _closes(candles: Sequence[Dict[str, Any]]) -> List[float]:
    out: List[float] = []
    for c in candles:
        try:
            value = float(c.get("close"))
        except (TypeError, ValueError, AttributeError):
            continue
        if value > 0:
            out.append(value)
    return out


def _extremes(candles: Sequence[Dict[str, Any]], key: str) -> List[float]:
    out: List[float] = []
    for c in candles:
        try:
            value = float(c.get(key))
        except (TypeError, ValueError, AttributeError):
            continue
        if value > 0:
            out.append(value)
    return out


def assess(
    direction: str,
    candles: Optional[Sequence[Dict[str, Any]]],
    *,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    atr: Optional[float],
) -> EntryExtension:
    """How far into the move is this entry, and is it chasing?

    `direction` is the TRADE's direction, not the market's. A LONG measures its
    extension up from the recent LOW; a SHORT measures down from the recent
    HIGH. Measuring from the wrong extreme would report the most extended
    entries as the least, which is the one error here that would be worse than
    no check at all.
    """
    limit = max_extension_atr()
    side = (direction or "").strip().upper()
    if side not in ("LONG", "SHORT"):
        return EntryExtension(
            direction=side or "UNKNOWN", extension_atr=None, stretch_atr=None,
            swing_price=None, entry_price=entry_price, atr=atr, limit_atr=limit,
            chasing=False,
            reason="no usable trade direction, so there is no move to measure along",
        )

    rows = list(candles or [])
    if len(rows) < MIN_CANDLES:
        return EntryExtension(
            direction=side, extension_atr=None, stretch_atr=None, swing_price=None,
            entry_price=entry_price, atr=atr, limit_atr=limit, chasing=False,
            reason=f"fewer than {MIN_CANDLES} candles, so the move has no measurable start",
        )

    try:
        atr_value = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        atr_value = 0.0
    if atr_value <= 0:
        return EntryExtension(
            direction=side, extension_atr=None, stretch_atr=None, swing_price=None,
            entry_price=entry_price, atr=atr, limit_atr=limit, chasing=False,
            reason="no usable ATR, so extension cannot be expressed in a scale-free unit",
        )

    try:
        entry = float(entry_price) if entry_price is not None else 0.0
    except (TypeError, ValueError):
        entry = 0.0
    if entry <= 0:
        # Fall back to the last close, which IS the entry on a market order
        # decided at the bar close. Named in the reason rather than assumed, so
        # a reader can tell which price the measurement used.
        closes = _closes(rows)
        entry = closes[-1] if closes else 0.0
    if entry <= 0:
        return EntryExtension(
            direction=side, extension_atr=None, stretch_atr=None, swing_price=None,
            entry_price=entry_price, atr=atr_value, limit_atr=limit, chasing=False,
            reason="no usable entry price",
        )

    window = rows[-(SWING_LOOKBACK + 1):-1] or rows[-SWING_LOOKBACK:]
    if side == "LONG":
        lows = _extremes(window, "low") or _closes(window)
        swing = min(lows) if lows else None
        travelled = (entry - swing) if swing is not None else None
    else:
        highs = _extremes(window, "high") or _closes(window)
        swing = max(highs) if highs else None
        travelled = (swing - entry) if swing is not None else None

    if swing is None or travelled is None:
        return EntryExtension(
            direction=side, extension_atr=None, stretch_atr=None, swing_price=None,
            entry_price=entry, atr=atr_value, limit_atr=limit, chasing=False,
            reason="the recent swing could not be located in the candles provided",
        )

    # A NEGATIVE EXTENSION IS NOT AN ERROR AND IS NOT CLAMPED. It means the
    # entry sits BELOW the window's low for a long — price has come back
    # through the base — which is a pullback entry, the opposite of chasing and
    # exactly what this gate should let through.
    extension = travelled / atr_value

    stretch: Optional[float] = None
    try:
        if stop_loss is not None and float(stop_loss) > 0:
            stretch = abs(entry - float(stop_loss)) / atr_value
    except (TypeError, ValueError):
        stretch = None

    chasing = extension > limit
    if chasing:
        reason = (
            f"price has already travelled {extension:.2f} ATR in the {side} direction "
            f"from {swing:.8g} to {entry:.8g}, past the {limit:.1f} ATR at which an "
            f"entry is chasing rather than joining. On a 2:1 model the target sits "
            f"5 ATR further still, so the move would have to more than double from a "
            f"point it has already run"
        )
    else:
        reason = (
            f"entry is {extension:.2f} ATR into the move (limit {limit:.1f}), a "
            f"reasonable distance from where it began"
        )

    return EntryExtension(
        direction=side,
        extension_atr=round(extension, 4),
        stretch_atr=round(stretch, 4) if stretch is not None else None,
        swing_price=swing,
        entry_price=entry,
        atr=atr_value,
        limit_atr=limit,
        chasing=chasing,
        reason=reason,
    )
