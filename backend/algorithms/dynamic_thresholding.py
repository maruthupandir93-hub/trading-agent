"""Phase 39 — how much conviction a regime demands before a trade is allowed.

THE NUMBERS HERE ARE IN THE DEBATE'S OWN UNITS, AND THEY DID NOT USED TO BE.
===========================================================================
This module returned ABSOLUTE thresholds of 0.60-0.99. `score_debate` does not
emit numbers on that scale and never has, so every one of them was unreachable
and `agents/supervisor_agent` could not approve a trade under ANY market
condition. Read from the live `decisions` table, one 65-minute window:

    3,063 decisions     outcome = 'rejected' on every single one
        0 trades

    Bull Trend      n=1730   observed confidence max 0.44   required 0.60
    Range           n=513    observed confidence max 0.36   required 0.75
    Low Volatility  n=176    observed confidence max 0.18   required 0.70

Verbatim from that table:

    No TAR submitted: Confidence 0.38 does not meet the threshold 0.60
    required for regime 'Bull Trend'

The highest confidence this system produced across 2,419 samples was 0.44. The
LOWEST bar was 0.60. This was not a strict system declining marginal setups — it
was a gate that could not be passed, reporting itself as ordinary selectivity.

WHERE THE OLD SCALE CAME FROM
-----------------------------
`agents/debate_agent` used to return a hardcoded `confidence = 0.85` on every
call, in every market, on every symbol (its replacement, `algorithms/debate`,
documents this under "WHAT IT REPLACED"). 0.60-0.99 was calibrated against that
constant. When the debate became real arithmetic — coverage-scaled weighted
evidence, which lands in the 0.15-0.44 band — these numbers were not rescaled.
`graphs/nodes/supervisor.MIN_CONFIDENCE_TO_TRADE` WAS rescaled, with a test
asserting it stays reachable. That is why the graph path can trade and this one
could not.

WHAT CHANGED, AND WHAT DELIBERATELY DID NOT
-------------------------------------------
The POLICY is unchanged: the relative strictness of each regime is preserved
exactly, as the ratio each old threshold bore to Bull Trend's. Only the unit is
fixed. Bull Trend is still the most permissive regime, Range is still 1.25x
stricter than Bull Trend, Panic is still near-prohibitive.

This is a unit correction, not a loosening. The bar a trade must clear is still
"the debate found real directional evidence", and it is still measured against a
scale whose observed ceiling is 0.44 — so a trade still needs roughly half the
evidence the system is capable of producing, in the easiest regime.
"""

from typing import Dict

# The anchor. MUST EQUAL `graphs/nodes/supervisor.MIN_CONFIDENCE_TO_TRADE`.
#
# Two numbers that must agree, living in two files, kept in sync deliberately —
# the same arrangement (and the same hazard) as the ATR multipliers in
# `lib/riskManager.ts` and `core/risk_manager.py`. The import direction is what
# forces the duplication: `algorithms/` is a leaf layer and must not import from
# `graphs/`, so it cannot read that constant directly.
#
# `test_dynamic_thresholding.py::test_the_anchor_matches_the_graph_supervisor`
# fails if they drift, which is the whole reason it is safe to state twice.
#
# 0.18 is measured, not chosen: see MIN_CONFIDENCE_TO_TRADE's own note for the
# sweep behind it. It sits between the observed ceiling of market evidence alone
# (~0.153) and of both directional legs agreeing (~0.239), which is what makes
# "TRADE requires two independent legs to agree" the property it encodes.
BASE_CONFIDENCE_TO_TRADE = 0.18

# Above the 1.0 cap `score_debate` applies to confidence, so no verdict can ever
# meet it. Used for regimes where the intent is "do not trade here at all".
#
# An EXPLICIT sentinel rather than a very high number. The old table expressed
# the same intent as 0.99 with the comment "effectively disables trading" — and
# that is exactly how the reachable regimes came to be unreachable too, because
# nothing distinguished "deliberately impossible" from "a number nobody
# rechecked after the scale moved". A regime that is blocked should say so.
UNREACHABLE = 1.01

# Strictness RELATIVE to Bull Trend, preserving the old table's ordering exactly.
# Each value is the old absolute threshold divided by the old Bull Trend
# threshold (0.60), so the policy this module encoded is carried over intact:
#
#     Bull Trend     0.60 / 0.60 = 1.00
#     Bear Trend     0.65 / 0.60 = 1.08     shorts get a slightly higher bar
#     Accumulation   0.65 / 0.60 = 1.08
#     Low Volatility 0.70 / 0.60 = 1.17
#     Range          0.75 / 0.60 = 1.25     chop is where this system loses
#     Distribution   0.80 / 0.60 = 1.33
#     High Vol       0.85 / 0.60 = 1.42
#     Panic/Euphoria 0.95 / 0.60 = 1.58
REGIME_STRICTNESS: Dict[str, float] = {
    "Bull Trend": 1.00,
    "Bear Trend": 1.08,
    "Accumulation": 1.08,
    "Low Volatility": 1.17,
    "Range": 1.25,
    "Distribution": 1.33,
    "High Volatility": 1.42,
    "Panic": 1.58,
    "Euphoria": 1.58,
}

# Regimes in which no amount of conviction authorises an entry.
#
# "Liquidity Crisis" carried the old table's 0.99 and its "effectively disables
# trading" comment; it is now blocked explicitly rather than by arithmetic.
#
# "Unknown" is the eleventh value `regime_agent.detect_market_regime` can return
# and the old table did not list it at all, so it silently took the 0.80 default
# — unreachable, i.e. blocked, but by accident. It is blocked on purpose now:
# this module's entire job is to pick a bar from the regime, and an unidentified
# regime offers nothing to pick from. `detect_market_regime` returns it only when
# there are too few candles to classify, which is a data problem, not a setup.
BLOCKED_REGIMES = frozenset({"Liquidity Crisis", "Unknown"})

# The fallback for a regime name this table has never seen. Stricter than every
# listed regime but NOT blocked, because an unrecognised name is a vocabulary
# drift between two modules (which `strategy_profiles.REGIME_ALIASES` exists to
# catch) rather than a statement about the market.
UNLISTED_STRICTNESS = 1.58


def get_required_confidence(regime: str) -> float:
    """Minimum debate confidence to open a position in `regime`.

    Returned in the SAME units `algorithms/debate.score_debate` emits — see the
    module docstring for why that sentence is the whole point of this function.

    A blocked regime returns `UNREACHABLE`, which is above the 1.0 cap on
    confidence, so the caller's ordinary `confidence < required` comparison
    refuses it without needing to know about blocking.
    """
    if regime in BLOCKED_REGIMES:
        return UNREACHABLE
    strictness = REGIME_STRICTNESS.get(regime, UNLISTED_STRICTNESS)
    return round(BASE_CONFIDENCE_TO_TRADE * strictness, 4)


def get_regime_risk_multiplier(regime: str) -> float:
    """
    Phase 40: Position Sizing AI
    Determine the risk multiplier based on the safety of the current regime.

    UNTOUCHED BY THE RESCALING ABOVE, and deliberately so: this is a fraction OF
    a risk budget, not a comparison against a confidence score. It was already on
    the correct scale (0.0-1.0 of `RISK_PER_TRADE`) and shares no units with
    `get_required_confidence`. Changing it alongside would have been a sizing
    change riding along with a bug fix.
    """
    multipliers = {
        "Bull Trend": 1.0,
        "Bear Trend": 0.9,
        "Range": 0.5,
        "Low Volatility": 0.8,
        "High Volatility": 0.25,
        "Accumulation": 1.0,
        "Distribution": 0.3,
        "Panic": 0.1,
        "Euphoria": 0.1,
        "Liquidity Crisis": 0.0,
    }
    return multipliers.get(regime, 0.5)
