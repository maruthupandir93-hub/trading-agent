"""The entry-context snapshot: what the agent saw, recorded at decision time.

WHY IT HAD TO BE RECORDED RATHER THAN JOINED
============================================
The trade-detail page's "How this trade happened" showed market data and
execution with an unknown middle, and that was not a display bug:
`trades.entry_context` existed and NOTHING WROTE IT.

A join could not have recovered it either. `trades` now carries `run_id`, but the
run trace records which node ran and what state KEYS it wrote — not the values.
The indicators, the regime and the volatility reading live in graph state, and
that state is gone by the time a fill is booked.

So the Risk Gateway writes a compact snapshot, because it is the last node
holding `technical_analysis`, `market_regime` and `volatility` together.

TESTED AS A PURE FUNCTION, DELIBERATELY. An earlier version of this file drove
the whole `gate()` node with synthetic state and every test SKIPPED — the gateway
reads the live book and ledger and refused the fabricated inputs. A test that
always skips proves nothing. `build_entry_context` is pure, so the things worth
pinning — the FORMAT, and that absences stay absent — are testable directly.
"""

from __future__ import annotations

import re

import pytest

from backend.graphs.nodes.risk_gateway import build_entry_context
from backend.graphs.state import MarketRegimeState, TechnicalAnalysis, VolatilityState

# The exact expressions `lib/viz/entryContext.parseEntryContext` uses. Duplicated
# here on purpose: if the Python format and the TypeScript parser drift apart, a
# snapshot the frontend cannot read is the same as no snapshot at all.
PARSER_PATTERNS = {
    "rsi": r"RSI\(\d+\)=([\d.]+)",
    "atr": r"ATR\(\d+\)=([\d.]+)",
    "trend": r"structure trend=([A-Za-z]+)",
    "regime": r"regime=([A-Za-z ]+?)(?:,|$)",
    "volatility": r"volatility=([A-Z_]+(?: \(\d+th pct\))?)",
    "strategy": r"strategy=([A-Za-z]+)",
}


def _full() -> str:
    return build_entry_context(
        symbol="SOL/USDT",
        technical=TechnicalAnalysis(rsi=36.4, atr=0.462, multi_timeframe_trend="Bearish"),
        regime_state=MarketRegimeState(regime="Range"),
        volatility=VolatilityState(regime="LOW", percentile=33.0),
        strategy="MeanReversion",
    )


def test_it_records_everything_the_agent_saw():
    ctx = _full()
    assert "RSI(14)=36.4" in ctx
    assert "ATR(14)=0.462" in ctx
    assert "structure trend=Bearish" in ctx
    assert "regime=Range" in ctx
    assert "volatility=LOW (33th pct)" in ctx
    assert "strategy=MeanReversion" in ctx


@pytest.mark.parametrize("field,pattern", list(PARSER_PATTERNS.items()))
def test_every_field_matches_the_frontend_parser(field, pattern):
    """One format, not two. A drift here silently empties the journey view."""
    assert re.search(pattern, _full()), f"the frontend cannot parse {field} out of this"


def test_a_missing_indicator_is_OMITTED_not_defaulted():
    """An invented RSI would be the most persuasive fabrication in this system,
    because it would look exactly like evidence."""
    ctx = build_entry_context(
        symbol="SOL/USDT",
        technical=TechnicalAnalysis(),          # nothing computed
        regime_state=MarketRegimeState(regime="Range"),
        volatility=None,
        strategy="Trend",
    )
    assert "RSI" not in ctx
    assert "ATR" not in ctx
    assert "volatility" not in ctx
    # ...but what WAS known survives.
    assert "regime=Range" in ctx
    assert "strategy=Trend" in ctx


def test_everything_missing_still_produces_a_readable_line():
    """No crash, and no empty string that would read as a recording failure."""
    ctx = build_entry_context(
        symbol="BTC/USDT", technical=None, regime_state=None,
        volatility=None, strategy=None,
    )
    assert ctx.startswith("BTC/USDT")
    assert not ctx.endswith(",")


def test_a_volatility_reading_with_no_percentile_still_records_the_regime():
    """The percentile is the comparable part, but the label alone is not nothing."""
    ctx = build_entry_context(
        symbol="X/USDT", technical=None, regime_state=None,
        volatility=VolatilityState(regime="HIGH", percentile=None),
        strategy=None,
    )
    assert "volatility=HIGH" in ctx
    assert "pct" not in ctx


def test_a_two_word_regime_does_not_swallow_the_next_field():
    """`regime=Trending Bullish, volatility=...` — the comma is the boundary, and
    a greedy parser would take the volatility into the regime."""
    ctx = build_entry_context(
        symbol="ETH/USDT", technical=None,
        regime_state=MarketRegimeState(regime="Trending Bullish"),
        volatility=VolatilityState(regime="HIGH", percentile=91.0),
        strategy="Trend",
    )
    assert re.search(PARSER_PATTERNS["regime"], ctx).group(1) == "Trending Bullish"
    assert re.search(PARSER_PATTERNS["volatility"], ctx).group(1) == "HIGH (91th pct)"


def test_the_gateway_attaches_it_to_the_plan():
    """The wiring, not just the formatter — a builder nothing calls records nothing.

    Read from the node's source rather than by driving it: `gate()` depends on the
    live book and the ledger, and a synthetic approval is brittle enough that an
    earlier version of this test skipped every single time.
    """
    import inspect

    from backend.graphs.nodes import risk_gateway

    source = inspect.getsource(risk_gateway.gate)
    assert "entry_context=build_entry_context(" in source, (
        "the gateway must attach the snapshot to the execution plan, or the trade "
        "row records WHAT happened with no link to WHY"
    )
