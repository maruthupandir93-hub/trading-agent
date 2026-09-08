"""Selectivity: the tradeable universe, and higher-timeframe alignment.

WHAT THE LEDGER SAID
====================
Read from the live database before either of these was written:

    12 closed trades, 3 wins (25%), -57.01 net
    wins  average +59.13   losses average -26.04   payoff 2.27:1
    break-even win rate at that payoff: 30.6%

    BTC/USDT     2 trades, 0 wins, -43.59
    SOL/USDT    10 trades, 3 wins, -13.42

Two of twelve trades produced 76% of the loss, and all three wins landed inside
one 30-minute window on one day. The losses are tightly clustered in size —
stop-outs at a consistent risk, not disasters.

That is a trend-following edge being run in conditions that are not trending. The
lever is SELECTIVITY, and these two gates are it.

BOTH ARE HYPOTHESES AND THE TESTS SAY SO. Twelve trades cannot validate anything.
What is pinned here is the LOGIC — that the gates refuse what they claim to
refuse, allow what they claim to allow, and never block an exit.
"""

from __future__ import annotations

import math

import pytest

from backend.algorithms.market_context import (
    ALIGNED,
    COUNTER_TREND,
    MIXED,
    UNKNOWN,
    assess,
    build,
    percent_change,
)
from backend.services.tradeable_universe import (
    blocked_symbols,
    is_tradeable,
    refusal_reason,
    tradeable,
)


# ---------------------------------------------------------------------------
# Candle fixtures
# ---------------------------------------------------------------------------


def _bars(n: int = 60, *, slope: float, start: float = 100.0):
    """A trend with pullbacks. A straight ramp is degenerate for structure
    analysis, exactly as `test_supervisor_graph`'s fixture notes."""
    out, price = [], start
    for i in range(n):
        price += slope + 0.35 * math.sin(i / 2.2)
        out.append({
            "time": i, "open": price - 0.3, "high": price + 0.45,
            "low": price - 0.45, "close": price, "volume": 1000.0 + i,
        })
    return out


UP = lambda n=60: _bars(n, slope=0.55)      # noqa: E731
DOWN = lambda n=60: _bars(n, slope=-0.55)   # noqa: E731


# ---------------------------------------------------------------------------
# The tradeable universe
# ---------------------------------------------------------------------------


def test_BTC_is_excluded_by_default(monkeypatch):
    """The operator's stated preference, as a configured default.

    Arranged this way rather than by deleting BTC from a watch list, which would
    also destroy the market-regime signal every other symbol depends on.
    """
    monkeypatch.delenv("UNTRADEABLE_SYMBOLS", raising=False)
    assert is_tradeable("BTC/USDT") is False
    assert is_tradeable("SOL/USDT") is True


def test_the_perpetual_spelling_is_blocked_too(monkeypatch):
    """`BTC/USDT:USDT` is the same instrument. A blocklist matching one spelling
    is bypassed by whichever hop resolves the symbol first."""
    monkeypatch.delenv("UNTRADEABLE_SYMBOLS", raising=False)
    assert is_tradeable("BTC/USDT:USDT") is False
    assert is_tradeable("btc/usdt") is False


def test_an_explicitly_empty_list_blocks_nothing(monkeypatch):
    """Distinct from the variable being ABSENT. A setting the operator
    deliberately cleared must not be silently repopulated with the default."""
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "")
    assert is_tradeable("BTC/USDT") is True
    assert blocked_symbols() == set()


def test_the_list_is_read_at_call_time(monkeypatch):
    """Frozen-at-import is how `simulation_mode` let a safety toggle report
    success while doing nothing. Here the failure is milder but identical in
    shape: the operator excludes an instrument, is told it worked, and the agent
    keeps trading it until a restart."""
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "ETH/USDT")
    assert is_tradeable("BTC/USDT") is True
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "BTC/USDT")
    assert is_tradeable("BTC/USDT") is False


def test_the_refusal_says_the_signal_is_kept(monkeypatch):
    monkeypatch.delenv("UNTRADEABLE_SYMBOLS", raising=False)
    reason = refusal_reason("BTC/USDT") or ""
    assert "still watched" in reason
    assert "INSTRUMENT, not the information" in reason
    assert refusal_reason("SOL/USDT") is None


def test_filtering_keeps_order_and_drops_duplicates(monkeypatch):
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "BTC/USDT")
    assert tradeable(["SOL/USDT", "BTC/USDT", "ETH/USDT", "SOL/USDT"]) == [
        "SOL/USDT", "ETH/USDT",
    ]


# ---------------------------------------------------------------------------
# The gateway wiring
# ---------------------------------------------------------------------------


def test_the_gateway_refuses_an_untradeable_instrument(monkeypatch):
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "BTC/USDT")

    from backend.graphs.nodes import risk_gateway

    source = __import__("inspect").getsource(risk_gateway.gate)
    assert "untradeable_reason(symbol)" in source

    # And it sits AFTER the exit branch — invariant 4, a close is never blocked.
    exit_at = source.index('decision.action == "EXIT"')
    gate_at = source.index("untradeable_reason(symbol)")
    assert exit_at < gate_at, (
        "the instrument gate must come after the EXIT branch, or a position in a "
        "now-excluded symbol could not be closed — leaving the operator holding "
        "exactly the instrument they asked to stop holding"
    )


# ---------------------------------------------------------------------------
# Higher-timeframe alignment
# ---------------------------------------------------------------------------


def test_a_long_with_the_higher_timeframe_trend_is_aligned():
    ctx = build(candles={"15m": UP(), "1h": UP(), "4h": UP()})
    assert ctx.higher_timeframe_trend == "Bullish"
    assert assess("LONG", ctx).verdict == ALIGNED
    assert assess("LONG", ctx).blocks is False


def test_a_long_AGAINST_the_higher_timeframe_trend_is_BLOCKED():
    """The gate's whole purpose.

    A 15m long into a 1h/4h downtrend is a pullback in a move that was never
    going this way — the shape of every loss in the ledger.
    """
    ctx = build(candles={"15m": UP(), "1h": DOWN(), "4h": DOWN()})
    assert ctx.higher_timeframe_trend == "Bearish"
    verdict = assess("LONG", ctx)
    assert verdict.verdict == COUNTER_TREND
    assert verdict.blocks is True


def test_a_short_with_a_bearish_higher_timeframe_is_aligned():
    ctx = build(candles={"15m": DOWN(), "1h": DOWN(), "4h": DOWN()})
    assert assess("SHORT", ctx).blocks is False


def test_a_short_into_a_bullish_higher_timeframe_is_blocked():
    ctx = build(candles={"15m": DOWN(), "1h": UP(), "4h": UP()})
    assert assess("SHORT", ctx).blocks is True


def test_an_UNMEASURABLE_trend_does_NOT_block():
    """Deliberately the opposite of the volatility gate, and the difference is
    the consequence.

    Volatility feeds sizing and stop distance, so unmeasured volatility means the
    loss cannot be bounded at all. An unmeasured higher-timeframe trend costs
    conviction — the stop is still computed, still enforced, still sized against
    measured ATR. Blocking here would halt trading whenever a 4h fetch was slow.
    """
    ctx = build(candles={"15m": UP(), "1h": [], "4h": []})
    assert ctx.higher_timeframe_trend is None
    verdict = assess("LONG", ctx)
    assert verdict.verdict == UNKNOWN
    assert verdict.blocks is False


def test_one_timeframe_alone_is_not_a_MULTI_timeframe_trend():
    """Matching `_multi_timeframe_trend`: a consensus of one is that timeframe's
    trend wearing a more authoritative name."""
    ctx = build(candles={"15m": UP(), "1h": UP(), "4h": []})
    assert ctx.higher_timeframe_trend is None


def test_MIXED_higher_timeframes_do_not_block():
    """A 1h/4h disagreement is a real market state — the beginning of a turn —
    and refusing every turn refuses the setups this strategy exists to take."""
    ctx = build(candles={"15m": UP(), "1h": UP(), "4h": DOWN()})
    assert ctx.higher_timeframe_trend == "Mixed"
    verdict = assess("LONG", ctx)
    assert verdict.verdict == MIXED
    assert verdict.blocks is False


# ---------------------------------------------------------------------------
# The benchmark — BTC as a signal, which is why excluding it as an instrument
# had to be a separate concept
# ---------------------------------------------------------------------------


def test_the_benchmark_trend_and_relative_strength_are_measured():
    ctx = build(
        candles={"15m": UP(), "1h": UP(), "4h": UP()},
        benchmark_symbol="BTC/USDT",
        benchmark_candles={"15m": DOWN(), "1h": DOWN(), "4h": DOWN()},
    )
    assert ctx.benchmark_trend == "Bearish"
    assert ctx.benchmark_change_pct is not None and ctx.benchmark_change_pct < 0
    # This coin rose while the market fell — genuinely strong, not just beta.
    assert ctx.relative_strength_pct is not None and ctx.relative_strength_pct > 0


def test_no_benchmark_leaves_those_fields_None_rather_than_zero():
    """Invariant 6. A 0.0 relative strength reads as 'measured, and it moves
    exactly with the market', which is a completely different claim from 'we did
    not have Bitcoin's candles'."""
    ctx = build(candles={"15m": UP(), "1h": UP(), "4h": UP()})
    assert ctx.benchmark_trend is None
    assert ctx.benchmark_change_pct is None
    assert ctx.relative_strength_pct is None


def test_the_description_omits_what_was_not_measured():
    ctx = build(candles={"15m": UP(), "1h": UP(), "4h": UP()})
    described = ctx.describe()
    assert "trend=Bullish" in described
    assert "rel.strength" not in described
    assert "None" not in described


def test_percent_change_is_None_on_unusable_candles():
    assert percent_change([]) is None
    assert percent_change([{"close": 0.0}, {"close": 5.0}]) is None


# ---------------------------------------------------------------------------
# The snapshot records it
# ---------------------------------------------------------------------------


def test_the_entry_snapshot_carries_the_context_without_breaking_the_parsers():
    """The two frontend parsers read the existing fields by regex. Appending the
    context must leave every one of those patterns matching — a snapshot the
    frontend cannot read is the same as no snapshot at all."""
    import re

    from backend.graphs.nodes.risk_gateway import build_entry_context
    from backend.graphs.state import MarketRegimeState, TechnicalAnalysis, VolatilityState

    ctx = build(
        candles={"15m": UP(), "1h": UP(), "4h": UP()},
        benchmark_symbol="BTC/USDT",
        benchmark_candles={"15m": DOWN(), "1h": DOWN(), "4h": DOWN()},
    )
    snapshot = build_entry_context(
        symbol="SOL/USDT",
        technical=TechnicalAnalysis(rsi=36.4, atr=0.462, multi_timeframe_trend="Bearish"),
        regime_state=MarketRegimeState(regime="Range"),
        volatility=VolatilityState(regime="LOW", percentile=33.0),
        strategy="MeanReversion",
        market_context=ctx,
    )

    assert "context:" in snapshot
    # Every existing parser pattern still matches.
    for pattern in (
        r"RSI\(\d+\)=([\d.]+)", r"ATR\(\d+\)=([\d.]+)",
        r"structure trend=([A-Za-z]+)", r"regime=([A-Za-z ]+?)(?:,|$)",
        r"volatility=([A-Z_]+(?: \(\d+th pct\))?)", r"strategy=([A-Za-z]+)",
    ):
        assert re.search(pattern, snapshot), f"{pattern} no longer matches"


def test_the_snapshot_is_unchanged_when_no_context_was_measured():
    """Backwards compatible: a run with no context produces exactly what it did
    before, not a trailing empty label."""
    from backend.graphs.nodes.risk_gateway import build_entry_context
    from backend.graphs.state import MarketRegimeState

    snapshot = build_entry_context(
        symbol="SOL/USDT", technical=None,
        regime_state=MarketRegimeState(regime="Range"),
        volatility=None, strategy="Trend", market_context=None,
    )
    assert "context:" not in snapshot
    assert snapshot == "SOL/USDT @ 15m: regime=Range, strategy=Trend"


# ---------------------------------------------------------------------------
# Regression: the counter-trend rejection must RETURN, not crash
# ---------------------------------------------------------------------------
#
# The gateway built the context under a local name `context` and then referenced
# `market_context` in the rejection branch — a NameError that crashed the exact
# path this gate exists for (a clear counter-trend entry), and which no test drove
# into. Found by an external source review, not by the suite. This drives it.


def _bear(n):
    import math
    out, p = [], 200.0
    for i in range(n):
        p -= 0.6 + 0.3 * math.sin(i / 2.2)
        out.append({"time": i, "open": p + 0.3, "high": p + 0.5, "low": p - 0.5,
                    "close": p, "volume": 1000.0 + i})
    return out


def test_the_counter_trend_rejection_returns_instead_of_crashing(monkeypatch):
    from backend.graphs.nodes.risk_gateway import gate
    from backend.graphs.state import (
        MarketSnapshot, PortfolioStateSnapshot, TechnicalAnalysis,
        TradeDecision, TradeThesis, new_state,
    )
    from backend.graphs.triggers import TriggerReason

    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "")
    monkeypatch.setattr(
        "backend.services.ai_memory.get_memory_stats", lambda: {"trade_ledger": []}
    )

    st = new_state(run_id="x", symbol="SOL/USDT",
                   trigger=TriggerReason(kind="manual", symbol="SOL/USDT", detail="t"),
                   started_at=0.0)
    st.update(
        decision=TradeDecision(action="TRADE", direction="LONG", probability=None),
        trade_thesis=TradeThesis(direction="LONG", strategy="Trend", entry_price=100.0,
                                 stop_loss=98.0, take_profit=104.0),
        technical_analysis=TechnicalAnalysis(atr=1.3),
        market_data=MarketSnapshot(symbol="SOL/USDT", price=100.0,
                                   candles={"15m": _bear(60), "1h": _bear(60), "4h": _bear(60)}),
        portfolio_state=PortfolioStateSnapshot(tab="paper", equity=10_000.0,
                                               cash=10_000.0, open_positions=[]),
    )

    out = gate(st)  # must not raise NameError
    ra = out["risk_assessment"]
    assert ra.approved is False
    assert "HigherTimeframeAlignment" in ra.checks
    # And the describe() that once crashed is in the detail.
    assert "Context:" in ra.checks["HigherTimeframeAlignment"]["detail"]


def test_the_supervisor_rationale_uses_no_undefined_sizing_dict():
    """The event-driven Supervisor's approval rationale referenced `sizing['rule']`
    / `sizing['detail']`, a dict that method never defines — a NameError the
    instant a trade was approved. There is no such dict; the fix uses the risk
    fraction it actually computed. Guard against the reference returning."""
    import inspect

    from backend.agents import supervisor_agent

    # Strip comment lines before scanning — the fix's own comment names the bad
    # reference to explain it, and that must not count as the bug returning.
    src = "\n".join(
        line for line in inspect.getsource(supervisor_agent).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "sizing['rule']" not in src
    assert "sizing['detail']" not in src
    assert 'sizing["rule"]' not in src
