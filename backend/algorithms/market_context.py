"""What else is true about this coin right now — the context every agent was missing.

WHY THIS EXISTS
===============
The agent decided on a 15-minute chart of ONE symbol. Two kinds of information it
already had access to never reached the decision:

  1. ITS OWN HIGHER TIMEFRAMES. `validate_market_data` has fetched 15m, 1h and 4h
     since the beginning, and the module comment says the higher ones "give the
     higher-timeframe context the debate uses to cut conviction on a counter-trend
     read". `_multi_timeframe_trend` computes that consensus and writes it to
     `TechnicalAnalysis.multi_timeframe_trend` — where `build_entry_context`
     RECORDS it and nothing GATES on it. A 15m long taken against a 1h and 4h
     downtrend is the single most reliable way to be stopped out by a pullback in
     a move that was never going your way.

  2. WHAT BITCOIN IS DOING. Alts follow BTC. `triggers.py` already treats BTC's
     regime as market-wide rather than symbol-specific, and `REGIME_WATCH` polls
     it for that reason — but that signal only ever produced BTC triggers. It
     never became context for a SOL decision.

WHAT THE LEDGER SAYS THIS IS FOR
================================
Twelve closed trades: 3 wins averaging +59.13, 9 losses averaging -26.04. A
2.27:1 payoff needs a 30.6% win rate to break even and got 25%. The losses are
tightly clustered in size — they are stop-outs at a consistent risk, not
disasters. All three wins landed inside one 30-minute window on one day.

That is the signature of a trend-follower being run in conditions that are not
trending: the edge is real when the market moves and negative when it does not.
The lever is therefore SELECTIVITY, not a bigger target or a tighter stop —
trade less often, in conditions that agree.

HONESTY ABOUT WHAT THIS IS
==========================
A HYPOTHESIS, and it is stated as one because nine or twelve trades cannot
validate anything. Requiring higher-timeframe agreement will reduce the number of
trades and should raise the win rate; whether it raises EXPECTANCY depends on how
many of the trades it removes would have been winners. That is empirical, and the
strategy-performance loop (now that it actually records a strategy) is what will
answer it.

Everything here is pure and deterministic — no model call, no I/O. It reads
candles that `validate_market_data` already fetched. A node that fetched its own
data would not be replay-safe (Section 39.4), and this is read by nodes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# The timeframes that constitute "higher" relative to the 15m decision chart.
HIGHER_TIMEFRAMES = ("1h", "4h")

# Below this many candles a timeframe has no assessable trend. Matches the
# structure analyser's own floor rather than picking a second number.
MIN_BARS = 20

# A coin whose recent move differs from the benchmark's by less than this is
# simply following the market. Relative strength only means something outside it.
RS_NEUTRAL_BAND_PCT = 1.0

# How far back "recent" is, in candles of the primary timeframe. 24 x 15m = 6h:
# long enough to be a move rather than noise, short enough to still be current.
LOOKBACK_BARS = 24


@dataclass(frozen=True)
class MarketContext:
    """Everything known about the setting this trade would be taken in.

    Every field is Optional and None means NOT MEASURABLE, never a neutral
    reading. Invariant 6: a fabricated "Neutral" here would look exactly like a
    measured one and would silently satisfy an alignment gate.
    """

    # The coin's own higher-timeframe consensus: Bullish / Bearish / Mixed.
    higher_timeframe_trend: Optional[str] = None
    # Which timeframes actually contributed, so a "Mixed" from one reading is
    # distinguishable from a real disagreement.
    higher_timeframes_read: Sequence[str] = ()

    # The benchmark (BTC) — the market's beta.
    benchmark_symbol: Optional[str] = None
    benchmark_trend: Optional[str] = None
    benchmark_change_pct: Optional[float] = None

    # This coin's own move over the same window, and the difference.
    change_pct: Optional[float] = None
    relative_strength_pct: Optional[float] = None

    def describe(self) -> str:
        """One line, for a rationale or an entry-context snapshot.

        Absences are OMITTED rather than rendered as a dash or a default — the
        same rule `build_entry_context` follows, and for the same reason: an
        invented reading is the most persuasive kind of fabrication because it
        looks exactly like evidence.
        """
        parts: List[str] = []
        if self.higher_timeframe_trend:
            tfs = "/".join(self.higher_timeframes_read) or "HTF"
            parts.append(f"{tfs} trend={self.higher_timeframe_trend}")
        if self.benchmark_trend and self.benchmark_symbol:
            bench = f"{self.benchmark_symbol.split('/')[0]} {self.benchmark_trend}"
            if self.benchmark_change_pct is not None:
                bench += f" ({self.benchmark_change_pct:+.2f}%)"
            parts.append(bench)
        if self.relative_strength_pct is not None:
            parts.append(f"rel.strength {self.relative_strength_pct:+.2f}%")
        return ", ".join(parts)


def _trend_of(bars: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Bullish / Bearish / Neutral for one timeframe, or None if unassessable."""
    if len(bars) < MIN_BARS:
        return None

    # Imported at call time to avoid a circular import (market_intelligence
    # imports from algorithms), and NOT inside the try below — an ImportError is
    # a programming error, and swallowing it here would report "not measurable"
    # for every timeframe forever while every gate silently passed.
    #
    # That is not hypothetical: the first version of this function had the wrong
    # module path inside a bare `except Exception`, and the whole alignment gate
    # was inert with no error anywhere. It is the same shape as the
    # `getattr(tar, "strategy", None)` bug — a defensive read of something that
    # does not exist is indistinguishable from a legitimate absence.
    from backend.agents.market_intelligence import analyze_market_structure

    try:
        return analyze_market_structure(list(bars)).get("trend") or "Neutral"
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        # Bad candle DATA is a genuinely missing reading, not a neutral one.
        logger.debug("structure analysis could not read the candles: %s", exc)
        return None


def _consensus(votes: Sequence[str]) -> Optional[str]:
    """Bullish/Bearish/Mixed across timeframes.

    Returns None below two votes, matching `_multi_timeframe_trend`: a
    "multi-timeframe" trend derived from one timeframe is that timeframe's trend
    wearing a more authoritative name.
    """
    if len(votes) < 2:
        return None
    bullish, bearish = votes.count("Bullish"), votes.count("Bearish")
    if bullish > bearish:
        return "Bullish"
    if bearish > bullish:
        return "Bearish"
    return "Mixed"


def percent_change(bars: Sequence[Dict[str, Any]], lookback: int = LOOKBACK_BARS) -> Optional[float]:
    """Percent change over the last `lookback` candles. None when unmeasurable."""
    if len(bars) < 2:
        return None
    window = list(bars)[-(lookback + 1):]
    try:
        first = float(window[0]["close"])
        last = float(window[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if first <= 0:
        return None
    return (last - first) / first * 100.0


def build(
    *,
    candles: Dict[str, List[Dict[str, Any]]],
    benchmark_symbol: Optional[str] = None,
    benchmark_candles: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    primary_timeframe: str = "15m",
) -> MarketContext:
    """Assemble the context from candles already fetched. Pure."""
    votes: List[str] = []
    read: List[str] = []
    for tf in HIGHER_TIMEFRAMES:
        trend = _trend_of(candles.get(tf, []))
        if trend is not None:
            votes.append(trend)
            read.append(tf)

    change = percent_change(candles.get(primary_timeframe, []))

    bench_trend: Optional[str] = None
    bench_change: Optional[float] = None
    rel: Optional[float] = None

    if benchmark_candles:
        bench_votes = [
            t for t in (_trend_of(benchmark_candles.get(tf, [])) for tf in HIGHER_TIMEFRAMES)
            if t is not None
        ]
        bench_trend = _consensus(bench_votes)
        bench_change = percent_change(benchmark_candles.get(primary_timeframe, []))
        if change is not None and bench_change is not None:
            rel = change - bench_change

    return MarketContext(
        higher_timeframe_trend=_consensus(votes),
        higher_timeframes_read=tuple(read),
        benchmark_symbol=benchmark_symbol,
        benchmark_trend=bench_trend,
        benchmark_change_pct=bench_change,
        change_pct=change,
        relative_strength_pct=rel,
    )


# ---------------------------------------------------------------------------
# The alignment verdict
# ---------------------------------------------------------------------------

ALIGNED = "aligned"
COUNTER_TREND = "counter_trend"
UNKNOWN = "unknown"
MIXED = "mixed"


@dataclass(frozen=True)
class Alignment:
    verdict: str
    detail: str
    #: True only for a CLEAR conflict with a measured higher-timeframe trend.
    blocks: bool = False


def assess(direction: str, context: MarketContext) -> Alignment:
    """Does this direction agree with the wider picture?

    THE UNKNOWN CASE DOES NOT BLOCK, and that is the opposite of the volatility
    gate's choice. The reasoning differs because the consequence differs:
    volatility feeds position sizing and stop distance, so an unmeasured
    volatility means the loss cannot be bounded at all. An unmeasured
    higher-timeframe trend costs conviction, not bounding — the stop is still
    computed, still enforced, and still sized against measured ATR. Blocking on
    it would halt trading whenever a 4h fetch was slow, which is a fragility, not
    a safety property.

    MIXED does not block either. A genuine disagreement between 1h and 4h is a
    real market state — the beginning of a turn — and refusing to trade every
    turn is refusing the trades this strategy exists to take.
    """
    want = (direction or "").strip().upper()
    if want not in ("LONG", "SHORT"):
        return Alignment(UNKNOWN, f"direction {direction!r} is not LONG or SHORT")

    htf = context.higher_timeframe_trend
    if htf is None:
        return Alignment(
            UNKNOWN,
            "higher-timeframe trend could not be measured (fewer than two timeframes "
            "had enough candles), so alignment is unknown rather than absent",
        )
    if htf == "Mixed":
        return Alignment(
            MIXED,
            f"1h and 4h disagree, which is a real market state rather than a missing "
            f"reading. Not blocked: a turn is exactly when a directional trade is worth "
            f"taking, and refusing every turn refuses this strategy's own setups.",
        )

    wants_up = want == "LONG"
    trend_up = htf == "Bullish"
    tfs = "/".join(context.higher_timeframes_read) or "higher timeframes"

    if wants_up == trend_up:
        return Alignment(
            ALIGNED, f"{want} agrees with the {tfs} {htf} trend"
        )

    return Alignment(
        COUNTER_TREND,
        (
            f"{want} is against the {tfs} {htf} trend. On the live ledger every loss "
            f"was a stop-out of under 1% while the instrument's own ATR was about that "
            f"wide — the signature of a pullback in a move that was never going this "
            f"way. Counter-trend entries are the ones that produce it."
        ),
        blocks=True,
    )
