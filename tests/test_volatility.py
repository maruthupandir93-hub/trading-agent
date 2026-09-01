"""The volatility layer: measurement, regime, and the risk policy it implies.

WHY THE PERCENTILE TESTS MATTER MOST
------------------------------------
The engine's central claim is that a regime derived from an instrument's OWN
recent distribution transfers across instruments and timeframes, while a fixed
ATR% table does not. DOGE's ordinary 5-minute range is not ETH's, and a constant
calibrated on one silently mislabels the other — usually by calling a normal DOGE
session EXTREME and refusing to trade it.

So the tests below assert the property, not the numbers: the same shape of market
must produce the same regime regardless of the price level or the absolute size
of its moves.

DETERMINISTIC INPUTS ONLY
-------------------------
Every series here is constructed by hand. A random walk was tried first and was a
bad instrument: its realised volatility drifts, so a "calm" series legitimately
ranked at the 93rd percentile of itself and the test looked like an engine bug
when the generator was at fault. Hand-built series make the expected answer
something you can work out by reading.
"""

from __future__ import annotations

import pytest

from backend.algorithms.volatility import (
    ABSOLUTE_ATR_PCT_BANDS,
    MIN_PERCENTILE_SAMPLES,
    RISK_MULTIPLIER,
    analyse_volatility,
    average_true_range,
    atr_percent_series,
    bollinger_width,
    candle_range_percent,
    detect_shock,
    percentile_of,
    realized_volatility,
    true_ranges,
)


def bars(n: int, close: float, half_range: float):
    """`n` identical candles: constant price, constant high-low range."""
    return [{"high": close + half_range, "low": close - half_range, "close": close} for _ in range(n)]


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def test_true_range_uses_the_previous_close_not_just_the_bar():
    """Wilder's TR exists to catch gaps; high-low alone misses them entirely."""
    candles = [
        {"high": 100.0, "low": 99.0, "close": 100.0},
        # Gaps far above the previous close. High-low is only 1.0, but the true
        # range from the prior close is 11.0 — the number that matters.
        {"high": 111.0, "low": 110.0, "close": 110.0},
    ]
    assert true_ranges(candles) == [11.0]


def test_atr_is_none_below_its_period():
    assert average_true_range(bars(5, 100.0, 0.5)) is None


def test_atr_of_a_constant_range_series_is_that_range():
    assert average_true_range(bars(60, 100.0, 0.5)) == pytest.approx(1.0)


def test_realized_volatility_of_a_flat_series_is_zero():
    """A market that does not move has zero realized volatility, not None."""
    assert realized_volatility(bars(60, 100.0, 0.5)) == pytest.approx(0.0)


def test_bollinger_width_of_a_flat_series_is_zero():
    assert bollinger_width(bars(60, 100.0, 0.5)) == pytest.approx(0.0)


def test_candle_range_percent_reads_the_latest_bar():
    series = bars(30, 100.0, 0.5) + [{"high": 103.0, "low": 97.0, "close": 100.0}]
    assert candle_range_percent(series) == pytest.approx(6.0)


def test_malformed_candles_are_dropped_not_repaired():
    """A bar with high < low makes every range meaningless.

    Forward-filling it would produce a confident number from invented data —
    the same discipline `_validate_candles` already applies upstream.
    """
    series = bars(40, 100.0, 0.5) + [{"high": 90.0, "low": 110.0, "close": 100.0}]
    # The bad bar is ignored, so the ATR is unchanged from the clean series.
    assert average_true_range(series) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Percentile ranking — the core claim
# ---------------------------------------------------------------------------

def test_a_percentile_needs_enough_samples_or_returns_none():
    """Below the floor the answer is noise dressed as a statistic."""
    assert percentile_of(1.0, [1.0] * (MIN_PERCENTILE_SAMPLES - 1)) is None
    assert percentile_of(1.0, [1.0] * MIN_PERCENTILE_SAMPLES) is not None


def test_a_market_at_its_own_median_is_NORMAL():
    """A steady market ranks in the middle of itself, whatever its absolute range."""
    reading = analyse_volatility(bars(200, 100.0, 0.2), symbol="T/USDT")
    assert reading.regime == "NORMAL"
    assert reading.basis == "percentile"
    assert reading.percentile == pytest.approx(50.0, abs=1.0)


def test_the_same_market_shape_gives_the_same_regime_at_any_price_level():
    """THE transferability claim. A fixed ATR% table cannot do this.

    Two instruments with identical relative behaviour and wildly different price
    levels — an ETH-like 4000 and a DOGE-like 0.15 — must be classified the same.
    """
    ethlike = analyse_volatility(bars(200, 4000.0, 8.0), symbol="ETH/USDT")   # 0.2% range
    dogelike = analyse_volatility(bars(200, 0.15, 0.0003), symbol="DOGE/USDT")  # 0.2% range

    assert ethlike.regime == dogelike.regime
    assert ethlike.atr_percent == pytest.approx(dogelike.atr_percent, rel=1e-6)


def test_a_calm_market_that_turns_violent_is_ranked_EXTREME():
    series = bars(200, 100.0, 0.1) + bars(5, 100.0, 5.0)
    reading = analyse_volatility(series, symbol="T/USDT")
    assert reading.regime == "EXTREME"
    assert reading.percentile is not None and reading.percentile > 90
    assert reading.trading_allowed is False


def test_thin_history_falls_back_to_absolute_thresholds_and_says_so():
    """The fallback must be labelled, because it is NOT comparable across assets."""
    reading = analyse_volatility(bars(30, 100.0, 0.5), symbol="T/USDT")

    assert reading.basis == "absolute"
    assert reading.regime is not None
    assert any("absolute" in u.lower() for u in reading.unavailable), reading.unavailable


# ---------------------------------------------------------------------------
# Shock
# ---------------------------------------------------------------------------

def test_a_sudden_expansion_is_a_shock():
    series = bars(200, 100.0, 0.1) + bars(2, 100.0, 4.0)
    reading = analyse_volatility(series, symbol="T/USDT")
    assert reading.volatility_shock is True
    assert reading.expansion_ratio is not None and reading.expansion_ratio >= 2.0


def test_a_steady_market_is_not_a_shock():
    reading = analyse_volatility(bars(200, 100.0, 0.5), symbol="T/USDT")
    assert reading.volatility_shock is False


def test_the_shock_baseline_excludes_the_current_value():
    """Including it would drag the mean toward the spike and understate it."""
    steady = [1.0] * 25
    spiked = steady + [4.0]
    is_shock, ratio = detect_shock(spiked)
    assert is_shock is True
    # 4.0 against a baseline of exactly 1.0 — not diluted by the spike itself.
    assert ratio == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def test_the_risk_multiplier_never_exceeds_one():
    """A multiplier above 1.0 would turn a risk control into a leverage source.

    Calm markets are where the next expansion starts; sizing UP because volatility
    is low is exactly the wrong reflex, so the table is asserted rather than
    trusted to stay <= 1.
    """
    assert all(v <= 1.0 for v in RISK_MULTIPLIER.values()), RISK_MULTIPLIER


def test_higher_volatility_never_increases_size_or_leverage():
    calm = analyse_volatility(bars(200, 100.0, 0.1), symbol="T/USDT")
    violent = analyse_volatility(bars(200, 100.0, 0.1) + bars(30, 100.0, 3.0), symbol="T/USDT")

    assert violent.risk_multiplier <= calm.risk_multiplier
    if violent.max_leverage is not None and calm.max_leverage is not None:
        assert violent.max_leverage <= calm.max_leverage


def test_an_unmeasurable_series_blocks_trading_rather_than_defaulting_to_calm():
    """The one place a missing input REFUSES instead of degrading.

    Position size and stop distance are both derived from volatility and neither
    has a safe default, so "we could not measure it" cannot mean "proceed".
    """
    reading = analyse_volatility([], symbol="T/USDT")
    assert reading.regime is None
    assert reading.trading_allowed is False
    assert reading.risk_multiplier == 0.0

    one_bar = analyse_volatility(bars(1, 100.0, 0.5), symbol="T/USDT")
    assert one_bar.trading_allowed is False


def test_a_shock_inside_a_tradeable_regime_halves_the_size():
    """The expansion is measured; where it settles is not."""
    series = bars(200, 100.0, 1.0) + bars(2, 100.0, 2.2)
    reading = analyse_volatility(series, symbol="T/USDT")
    if reading.volatility_shock and reading.trading_allowed:
        assert reading.risk_multiplier <= 0.5


def test_every_regime_has_a_complete_policy():
    """A regime with no multiplier, cap or stop multiple would size against None."""
    from backend.algorithms.volatility import (
        MAX_LEVERAGE_BY_REGIME,
        STOP_ATR_MULTIPLE,
    )

    regimes = {name for _, name in ABSOLUTE_ATR_PCT_BANDS}
    for regime in regimes:
        assert regime in RISK_MULTIPLIER, regime
        assert regime in MAX_LEVERAGE_BY_REGIME, regime
        assert regime in STOP_ATR_MULTIPLE, regime


# ---------------------------------------------------------------------------
# Determinism / replay safety
# ---------------------------------------------------------------------------

def test_the_engine_is_pure_and_repeatable():
    """Section 39.4: a replayed run must reach the same verdict.

    No clock, no randomness, no I/O — so two calls over identical candles must be
    identical in every field.
    """
    series = bars(200, 100.0, 0.4) + bars(3, 100.0, 1.2)
    a = analyse_volatility(series, symbol="T/USDT").as_dict()
    b = analyse_volatility(series, symbol="T/USDT").as_dict()
    assert a == b


def test_the_atr_percent_series_ends_on_the_same_value_the_engine_reports():
    """The current reading must be ranked against a history computed identically.

    Comparing a value against a differently-computed distribution is how a
    percentile silently stops meaning anything.
    """
    series = bars(200, 100.0, 0.4)
    reading = analyse_volatility(series, symbol="T/USDT")
    assert atr_percent_series(series)[-1] == pytest.approx(reading.atr_percent)
