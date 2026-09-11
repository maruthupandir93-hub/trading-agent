"""Volume analysis — relative volume and SUDDEN-surge detection.

THE OPERATOR'S OBSERVATION, MADE DETERMINISTIC
----------------------------------------------
On Binance, when a candle's volume ticks up, price tends to move with it: a volume
surge is conviction behind a move, and the same move on THIN volume is not trusted.
This module turns that into numbers the debate and the feature set can act on.

Two views, deliberately, because they answer different questions:

  * BASELINE ratio — recent-5-bar average / prior-20-bar average. A smoothed "is
    participation rising?" read. Slow and robust; this is the pre-existing signal.
  * RELATIVE VOLUME (RVOL) — the LATEST candle's volume vs the average of the N bars
    before it. FAST. This is what catches a SUDDEN move: one heavy candle that a
    5-bar average would dilute to nothing. A sudden spike is exactly the event the
    operator asked to have acted on.

VOLUME HAS NO DIRECTION OF ITS OWN. It confirms whichever way price moved on the bar
it surged on. The direction therefore always comes from the price move, never from
the volume — otherwise heavy SELLING would read as bullish just because it was
heavy. A surge with NO price move contributes nothing: volume without a move is
indecision, not confirmation.

Pure and deterministic — no I/O, no clock, no model. Same candles always give the
same signal, so the debate stays reproducible and replay-safe (Section 39.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# RVOL at or above this on the LATEST candle is a SURGE — a sudden burst of
# participation. 1.5x is "half as much volume again as a normal bar": low enough to
# catch the slight increase the operator described, high enough not to fire on the
# ordinary bar-to-bar noise every candle carries.
SURGE_RVOL = 1.5

# How many candles BEFORE the latest one form the RVOL baseline.
RVOL_LOOKBACK = 20

# Below this the module refuses rather than guessing — RVOL needs a baseline.
MIN_CANDLES = RVOL_LOOKBACK + 1


@dataclass
class VolumeSignal:
    """What volume says about the latest move. `available=False` means "could not
    measure" (too few candles, or no real volume), never a neutral reading."""

    available: bool
    rvol: Optional[float] = None            # latest volume / prior-N average
    baseline_ratio: Optional[float] = None  # recent-5 avg / prior-20 avg
    surge: bool = False                     # rvol >= SURGE_RVOL
    direction: Optional[str] = None         # 'bullish' | 'bearish' | None (from the price move)
    strength: float = 0.0                   # signed [-1, 1]: + bullish, - bearish confirmation
    detail: str = ""


def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def analyze_volume(klines: List[Dict[str, Any]]) -> VolumeSignal:
    """Relative volume, surge detection, and a signed confirmation strength."""
    if len(klines) < MIN_CANDLES:
        return VolumeSignal(
            available=False,
            detail=f"only {len(klines)} candle(s); need {MIN_CANDLES} for RVOL",
        )

    volumes = [float(k.get("volume", 0.0)) for k in klines]
    closes = [float(k["close"]) for k in klines]

    latest_vol = volumes[-1]
    prior = volumes[-(RVOL_LOOKBACK + 1):-1]  # the N bars BEFORE the latest one
    baseline_prior = _mean(prior)

    if not baseline_prior or baseline_prior <= 0 or latest_vol <= 0:
        # All-zero or missing volume — the volume check simply cannot run. Reported,
        # not defaulted to a neutral confirmation (which would be a silent vote).
        return VolumeSignal(available=False, detail="no usable volume (zero baseline or latest)")

    rvol = latest_vol / baseline_prior

    recent5 = _mean(volumes[-5:])
    base20 = _mean(volumes[-20:])
    baseline_ratio = (recent5 / base20) if (recent5 and base20 and base20 > 0) else None

    # Direction from the LATEST candle — the bar that actually carried the volume.
    prev_close = closes[-2]
    move = (closes[-1] - prev_close) / prev_close if prev_close else 0.0
    direction = "bullish" if move > 0 else "bearish" if move < 0 else None

    surge = rvol >= SURGE_RVOL

    # Strength: how much MORE than a normal bar this candle traded, clamped to
    # [0, 1] (0 at rvol<=1, 1 at rvol>=2), signed by the move. No move => no
    # confirmation, however heavy the bar.
    magnitude = max(0.0, min(1.0, rvol - 1.0))
    sign = 1.0 if move > 0 else -1.0 if move < 0 else 0.0
    strength = magnitude * sign

    detail = (
        f"RVOL {rvol:.2f}x (latest vs {RVOL_LOOKBACK}-bar avg)"
        + (f", baseline {baseline_ratio:.2f}x" if baseline_ratio is not None else "")
        + f", {'SURGE ' if surge else ''}on a {move * 100:+.2f}% candle"
    )

    return VolumeSignal(
        available=True,
        rvol=rvol,
        baseline_ratio=baseline_ratio,
        surge=surge,
        direction=direction,
        strength=strength,
        detail=detail,
    )
