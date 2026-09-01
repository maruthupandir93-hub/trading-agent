"""Volatility engine — a market-regime layer, not another indicator.

WHY THIS IS ITS OWN LAYER
-------------------------
Volatility is not one more number for the debate to weigh. It decides things the
directional panel has no opinion about:

    whether trading is allowed at all
    how big a position may be
    how much leverage is permissible
    how far away the stop has to sit
    whether an existing position should be handled differently

A trend specialist that says "bullish" is saying something orthogonal to "this
market is moving four times its normal amount right now". Folding the second into
the first loses exactly the information that should have stopped the trade.

DETERMINISTIC, AND NEVER AN LLM CALL
------------------------------------
Every number here is arithmetic over candles. CLAUDE.md: *"Deterministic over LLM
where the math is real ... asking a model to 'reason over' numbers already on hand
adds hallucination risk to a financial decision for no benefit and isn't
reproducible."* The model may INTERPRET this output alongside trend, funding and
news. It must never produce it.

THRESHOLDS ARE PERCENTILES, NOT CONSTANTS — THIS IS THE IMPORTANT PART
----------------------------------------------------------------------
A fixed table like "ATR% < 1 is LOW, > 4 is EXTREME" is wrong the moment you
change instrument or timeframe. DOGE's ordinary 5-minute range is not ETH's, and
a rule calibrated on one silently mislabels the other — usually by calling a
normal DOGE session EXTREME and refusing to trade it, or by calling a genuinely
violent ETH session NORMAL and sizing into it.

So the regime comes from where today sits in ITS OWN recent distribution. The
constants below are percentile boundaries, which are unit-free and therefore
transfer across instruments; the raw thresholds exist only as a labelled fallback
for when there is not enough history to rank against, and that fallback says so
in `basis`.

EVERY FIELD CAN BE None
-----------------------
A short candle series cannot support realized volatility or a percentile, and
this module reports that rather than filling in a plausible number. A fabricated
volatility reading is worse than a missing one: it feeds position sizing, and the
failure mode is sizing into a market you have mismeasured.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# --- windows ---------------------------------------------------------------

# Wilder's ATR period. 14 is the standard and there is no reason to differ; using
# an unusual number here would make every ATR in this system incomparable to any
# chart the operator looks at.
ATR_PERIOD = 14

# Candles used for realized volatility. 20 returns is the smallest window that
# gives a stdev worth reporting; below it the estimate moves more than the thing
# it measures.
RV_PERIOD = 20

BOLLINGER_PERIOD = 20
BOLLINGER_STDEV = 2.0

# How much history the percentile ranks against. 240 fifteen-minute candles is
# about 2.5 days; long enough to contain both quiet and busy sessions, short
# enough that a regime shift a month ago does not define "normal" today.
PERCENTILE_LOOKBACK = 240

# Minimum ranked samples before a percentile is trusted. Below this the answer is
# noise dressed as a statistic, and the engine falls back to absolute thresholds
# and SAYS so.
MIN_PERCENTILE_SAMPLES = 60


# --- regime boundaries -----------------------------------------------------

# Percentile boundaries. Unit-free, so they transfer across instruments and
# timeframes — which is the entire reason the regime is computed this way.
PERCENTILE_BANDS = (
    (20.0, "VERY_LOW"),
    (40.0, "LOW"),
    (70.0, "NORMAL"),
    (90.0, "HIGH"),
    (100.1, "EXTREME"),
)

# FALLBACK ONLY, used when there is too little history to rank against. These are
# ATR-percent thresholds calibrated loosely on major crypto pairs, and they are
# the thing the percentile approach exists to replace — a result computed from
# these carries `basis="absolute"` so a reader knows not to trust it across
# instruments.
ABSOLUTE_ATR_PCT_BANDS = (
    (0.35, "VERY_LOW"),
    (0.75, "LOW"),
    (1.75, "NORMAL"),
    (3.50, "HIGH"),
    (float("inf"), "EXTREME"),
)

# Risk multiplier per regime. Applied to position size by the Risk Gateway.
#
# These reduce exposure as volatility rises; they never increase it. A multiplier
# above 1.0 would mean "this market is calm, so take a bigger position", which
# turns a risk control into a leverage source and is exactly backwards — calm
# markets are where the next expansion starts.
RISK_MULTIPLIER = {
    "VERY_LOW": 1.0,
    "LOW": 1.0,
    "NORMAL": 1.0,
    "HIGH": 0.6,
    "EXTREME": 0.25,
}

# Leverage ceiling per regime. A CAP, combined with the absolute ceiling by min()
# — it can only ever lower it. `ABSOLUTE_MAX_LEVERAGE` stays the hard limit and
# nothing here can raise it (CLAUDE.md invariant 2).
MAX_LEVERAGE_BY_REGIME = {
    "VERY_LOW": 5,
    "LOW": 5,
    "NORMAL": 4,
    "HIGH": 2,
    "EXTREME": 1,
}

# Stop distance in ATR multiples, per regime.
#
# Wider in quiet markets is deliberate and counter-intuitive: in a low-ATR market
# the ATR itself is small, so a 1.5x stop can sit inside ordinary noise. The
# multiple compensates so the stop stays outside the noise floor in both regimes.
STOP_ATR_MULTIPLE = {
    "VERY_LOW": 2.0,
    "LOW": 1.8,
    "NORMAL": 1.5,
    "HIGH": 1.5,
    "EXTREME": 2.0,
}

# Trading is refused outright in this regime.
BLOCKED_REGIMES = frozenset({"EXTREME"})

# A shock is an ATR expansion of at least this ratio against the recent baseline.
# 2.0 — the market is moving twice its recent normal. Below that is ordinary
# session variation; a lower threshold fires constantly and gets ignored.
SHOCK_EXPANSION_RATIO = 2.0
SHOCK_BASELINE_PERIOD = 20


@dataclass
class VolatilityReading:
    """What the volatility layer measured, and what it concluded.

    Every measurement is Optional because a short series genuinely cannot support
    it. `regime` is None when nothing could be measured at all — callers treat
    that as "unknown", never as "calm".
    """

    symbol: str
    timeframe: str

    atr: Optional[float] = None
    atr_percent: Optional[float] = None
    realized_volatility: Optional[float] = None
    bollinger_width: Optional[float] = None
    candle_range_percent: Optional[float] = None

    # Where `atr_percent` sits in its own recent history, 0-100.
    percentile: Optional[float] = None
    # "percentile" | "absolute" | None — HOW the regime was decided. A reader
    # must be able to tell a ranked verdict from a fallback one.
    basis: Optional[str] = None

    regime: Optional[str] = None
    # 0-100, a readable summary of the same thing the regime bands express.
    score: Optional[float] = None

    volatility_shock: bool = False
    expansion_ratio: Optional[float] = None

    trading_allowed: bool = True
    risk_multiplier: float = 1.0
    max_leverage: Optional[int] = None
    stop_atr_multiple: Optional[float] = None

    candles_used: int = 0
    # Anything that could NOT be computed, and why. Mirrors the convention every
    # specialist in this codebase follows.
    unavailable: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "atr": self.atr,
            "atrPercent": self.atr_percent,
            "realizedVolatility": self.realized_volatility,
            "bollingerWidth": self.bollinger_width,
            "candleRangePercent": self.candle_range_percent,
            "percentile": self.percentile,
            "basis": self.basis,
            "regime": self.regime,
            "score": self.score,
            "volatilityShock": self.volatility_shock,
            "expansionRatio": self.expansion_ratio,
            "tradingAllowed": self.trading_allowed,
            "riskMultiplier": self.risk_multiplier,
            "maxLeverage": self.max_leverage,
            "stopAtrMultiple": self.stop_atr_multiple,
            "candlesUsed": self.candles_used,
            "unavailable": self.unavailable,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _ohlc(candles: Sequence[Dict[str, Any]]):
    """Extract (high, low, close) floats, dropping structurally invalid bars.

    Dropped rather than repaired, matching `graphs/nodes/market._validate_candles`:
    a bar whose high is below its low makes every range meaningless, and
    forward-filling it would produce a confident number from invented data.
    """
    out = []
    for c in candles or []:
        try:
            h, l, cl = float(c["high"]), float(c["low"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if h <= 0 or l <= 0 or cl <= 0 or h < l:
            continue
        out.append((h, l, cl))
    return out


def true_ranges(candles: Sequence[Dict[str, Any]]) -> List[float]:
    """Wilder's True Range per bar. Needs a previous close, so it is one shorter."""
    bars = _ohlc(candles)
    if len(bars) < 2:
        return []
    ranges = []
    for i in range(1, len(bars)):
        h, l, _ = bars[i]
        prev_close = bars[i - 1][2]
        ranges.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return ranges


def average_true_range(candles: Sequence[Dict[str, Any]], period: int = ATR_PERIOD) -> Optional[float]:
    """Simple mean of the last `period` true ranges, or None.

    A simple mean rather than Wilder's smoothing: the difference over a 14-period
    window is small, and a plain mean is reproducible by anyone reading the code
    without having to match a recursive seed.
    """
    ranges = true_ranges(candles)
    if len(ranges) < period:
        return None
    window = ranges[-period:]
    return sum(window) / len(window)


def realized_volatility(candles: Sequence[Dict[str, Any]], period: int = RV_PERIOD) -> Optional[float]:
    """Standard deviation of log returns over `period` bars, as a percent.

    NOT annualised. The decision layer reasons over minutes to hours, and scaling
    a 15-minute stdev by root-time to a yearly figure produces a large number that
    means nothing at this horizon — it invites comparison against equity-market
    intuitions that do not apply.
    """
    bars = _ohlc(candles)
    if len(bars) < period + 1:
        return None
    closes = [b[2] for b in bars[-(period + 1):]]

    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            return None
        returns.append(math.log(closes[i] / closes[i - 1]))

    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * 100.0


def bollinger_width(
    candles: Sequence[Dict[str, Any]],
    period: int = BOLLINGER_PERIOD,
    stdevs: float = BOLLINGER_STDEV,
) -> Optional[float]:
    """(upper - lower) / middle, as a percent. Compression and expansion detector."""
    bars = _ohlc(candles)
    if len(bars) < period:
        return None
    closes = [b[2] for b in bars[-period:]]

    middle = sum(closes) / len(closes)
    if middle <= 0:
        return None
    variance = sum((c - middle) ** 2 for c in closes) / len(closes)
    sd = math.sqrt(variance)
    return ((middle + stdevs * sd) - (middle - stdevs * sd)) / middle * 100.0


def candle_range_percent(candles: Sequence[Dict[str, Any]]) -> Optional[float]:
    """The most recent bar's high-low range as a percent of its close."""
    bars = _ohlc(candles)
    if not bars:
        return None
    h, l, c = bars[-1]
    if c <= 0:
        return None
    return (h - l) / c * 100.0


def atr_percent_series(
    candles: Sequence[Dict[str, Any]], period: int = ATR_PERIOD
) -> List[float]:
    """A rolling ATR%, one value per bar once enough history exists.

    This is the distribution the percentile ranks against. Built here rather than
    recomputed by the caller so the current reading and its history are produced
    by identical arithmetic — comparing a value against a differently-computed
    history is how a percentile silently becomes meaningless.
    """
    bars = _ohlc(candles)
    ranges = true_ranges(candles)
    if len(ranges) < period:
        return []

    series = []
    for i in range(period, len(ranges) + 1):
        atr = sum(ranges[i - period:i]) / period
        # `ranges[k]` is the range of bars[k+1], so the close aligned with the
        # window ending at index i-1 is bars[i].
        close = bars[i][2] if i < len(bars) else bars[-1][2]
        if close > 0:
            series.append(atr / close * 100.0)
    return series


def percentile_of(value: float, history: Sequence[float]) -> Optional[float]:
    """Where `value` sits in `history`, 0-100. None when there is too little.

    Uses the fraction of samples strictly below the value plus half the ties,
    which is the standard mid-rank definition and behaves sensibly when a flat
    market produces many identical readings.
    """
    if len(history) < MIN_PERCENTILE_SAMPLES:
        return None
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return (below + 0.5 * equal) / len(history) * 100.0


def _regime_from_percentile(pct: float) -> str:
    for boundary, name in PERCENTILE_BANDS:
        if pct < boundary:
            return name
    return "EXTREME"


def _regime_from_absolute(atr_pct: float) -> str:
    for boundary, name in ABSOLUTE_ATR_PCT_BANDS:
        if atr_pct < boundary:
            return name
    return "EXTREME"


def detect_shock(series: Sequence[float]) -> tuple[bool, Optional[float]]:
    """Is the latest ATR% a sharp expansion against its recent baseline?

    Returns `(is_shock, expansion_ratio)`. The baseline EXCLUDES the current
    value: including it would drag the mean toward the spike and understate
    exactly the expansion being measured.
    """
    if len(series) < SHOCK_BASELINE_PERIOD + 1:
        return False, None
    current = series[-1]
    baseline_window = series[-(SHOCK_BASELINE_PERIOD + 1):-1]
    baseline = sum(baseline_window) / len(baseline_window)
    if baseline <= 0:
        return False, None
    ratio = current / baseline
    return ratio >= SHOCK_EXPANSION_RATIO, ratio


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def analyse_volatility(
    candles: Sequence[Dict[str, Any]],
    *,
    symbol: str,
    timeframe: str = "15m",
) -> VolatilityReading:
    """Measure volatility and derive the regime and its risk policy.

    Pure: no I/O, no clock, no randomness. Safe to replay (Section 39.4).
    """
    reading = VolatilityReading(symbol=symbol, timeframe=timeframe)
    bars = _ohlc(candles)
    reading.candles_used = len(bars)

    if len(bars) < 2:
        reading.unavailable.append(
            f"only {len(bars)} usable candle(s); volatility cannot be measured at all"
        )
        # Unknown volatility is NOT permission to trade. A missing reading here
        # means the sizing and stop policy below have nothing to stand on.
        reading.trading_allowed = False
        reading.risk_multiplier = 0.0
        reading.evidence.append(
            "trading blocked: volatility is unknown, and an unknown regime cannot "
            "size a position or place a stop"
        )
        return reading

    last_close = bars[-1][2]

    reading.atr = average_true_range(candles)
    if reading.atr is None:
        reading.unavailable.append(f"ATR needs {ATR_PERIOD + 1} candles, have {len(bars)}")
    elif last_close > 0:
        reading.atr_percent = reading.atr / last_close * 100.0

    reading.realized_volatility = realized_volatility(candles)
    if reading.realized_volatility is None:
        reading.unavailable.append(f"realized volatility needs {RV_PERIOD + 1} candles")

    reading.bollinger_width = bollinger_width(candles)
    if reading.bollinger_width is None:
        reading.unavailable.append(f"Bollinger width needs {BOLLINGER_PERIOD} candles")

    reading.candle_range_percent = candle_range_percent(candles)

    # -- regime ------------------------------------------------------------
    if reading.atr_percent is None:
        reading.unavailable.append("no regime: ATR% could not be computed")
        reading.trading_allowed = False
        reading.risk_multiplier = 0.0
        reading.evidence.append("trading blocked: no ATR% means no measurable regime")
        return reading

    series = atr_percent_series(candles)[-PERCENTILE_LOOKBACK:]
    reading.percentile = percentile_of(reading.atr_percent, series)

    if reading.percentile is not None:
        reading.regime = _regime_from_percentile(reading.percentile)
        reading.basis = "percentile"
        reading.score = round(reading.percentile, 1)
        reading.evidence.append(
            f"ATR% {reading.atr_percent:.3f} sits at the {reading.percentile:.0f}th "
            f"percentile of its own last {len(series)} readings -> {reading.regime}"
        )
    else:
        reading.regime = _regime_from_absolute(reading.atr_percent)
        reading.basis = "absolute"
        reading.unavailable.append(
            f"percentile needs {MIN_PERCENTILE_SAMPLES} ranked samples, have "
            f"{len(series)} — regime fell back to ABSOLUTE ATR% thresholds, which "
            f"are not comparable across instruments or timeframes"
        )
        reading.evidence.append(
            f"ATR% {reading.atr_percent:.3f} against absolute thresholds -> "
            f"{reading.regime} (fallback basis; see unavailable)"
        )

    # -- shock -------------------------------------------------------------
    reading.volatility_shock, reading.expansion_ratio = detect_shock(series)
    if reading.volatility_shock:
        reading.evidence.append(
            f"VOLATILITY SHOCK: ATR% is {reading.expansion_ratio:.2f}x its "
            f"{SHOCK_BASELINE_PERIOD}-bar baseline"
        )

    # -- policy ------------------------------------------------------------
    regime = reading.regime or "NORMAL"
    reading.risk_multiplier = RISK_MULTIPLIER.get(regime, 1.0)
    reading.max_leverage = MAX_LEVERAGE_BY_REGIME.get(regime)
    reading.stop_atr_multiple = STOP_ATR_MULTIPLE.get(regime)
    reading.trading_allowed = regime not in BLOCKED_REGIMES

    if not reading.trading_allowed:
        reading.evidence.append(
            f"trading blocked: {regime} volatility. A stop placed in this regime is "
            f"as likely to be gapped through as touched, so position sizing cannot "
            f"bound the loss it is supposed to bound."
        )
    elif reading.volatility_shock:
        # A shock inside an otherwise tradeable regime does not block, but it does
        # halve the size. The expansion has happened; where it settles has not.
        reading.risk_multiplier = min(reading.risk_multiplier, 0.5)
        reading.evidence.append(
            "size halved on the shock: the expansion is measured, its resolution is not"
        )

    reading.evidence.append(
        f"policy: risk x{reading.risk_multiplier}, leverage cap {reading.max_leverage}x, "
        f"stop {reading.stop_atr_multiple}x ATR"
    )
    return reading
