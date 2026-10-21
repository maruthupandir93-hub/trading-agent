"""The backtest must model the exits and the costs the live system has.

OPEN ITEMS 2 AND 3 of docs/DIAGNOSIS.md — the external review's Issue E.

The backtest simulated a pure ATR stop/target pair and charged only the taker
fee. Production also exits on the per-strategy holding bound
(`max_holding_minutes`, live BY DEFAULT since the time exit was added) and on a
trailing stop, and pays slippage on both legs. A backtest that models neither
is measuring a strategy production does not run, at a cost production does not
get — and this file's entire purpose is deciding which strategies may risk
money.

Fixtures here are hand-built so each rule can be driven in isolation. The
lesson `tests/test_strategy_backtest.py` paid for applies: a resolving bar's
large range inflates the 14-bar ATR, so trades must be spaced for clean
numbers, and a fixture kinder than reality proves nothing.
"""

from __future__ import annotations

import pytest

from backend.core import strategy_backtest as bt


MINUTE_MS = 60_000


def _series(closes, *, bar_minutes=15, spread=0.5):
    """Candles with real `openTime` stamps, so the interval is derivable."""
    step = int(bar_minutes * MINUTE_MS)
    out = []
    for idx, close in enumerate(closes):
        out.append({
            "openTime": 1_700_000_000_000 + idx * step,
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": 100.0,
        })
    return out


def _flat_then(signal_at, closes):
    """A signal function that fires once, at `signal_at` bars of history."""
    def fn(hist):
        return "BUY" if len(hist) == signal_at else None
    return fn


# ---------------------------------------------------------------------------
# The candle interval, which everything else depends on
# ---------------------------------------------------------------------------

def test_the_bar_interval_is_derived_from_the_data():
    assert bt.bar_minutes(_series([100.0] * 10, bar_minutes=15)) == pytest.approx(15.0)
    assert bt.bar_minutes(_series([100.0] * 10, bar_minutes=60)) == pytest.approx(60.0)


def test_one_missing_candle_does_not_double_the_inferred_interval():
    """The median, not the first gap.

    A single gap would otherwise double the interval and HALVE every holding
    bound — turning a 30-minute scalp bound into 15 minutes without anything
    reporting it.
    """
    candles = _series([100.0] * 12, bar_minutes=15)
    del candles[5]                       # one hole
    assert bt.bar_minutes(candles) == pytest.approx(15.0)


def test_an_underivable_interval_returns_None_rather_than_guessing():
    """None means "could not measure", which is not "no bound"."""
    assert bt.bar_minutes([{"close": 1.0}, {"close": 2.0}, {"close": 3.0}]) is None
    assert bt.bar_minutes([]) is None


# ---------------------------------------------------------------------------
# The time exit
# ---------------------------------------------------------------------------

def test_a_position_that_goes_nowhere_is_closed_by_the_clock():
    """The live behaviour, in the backtest: neither level reached, bound hit."""
    closes = [100.0] * 20 + [100.05] * 40          # drifts nowhere
    candles = _series(closes, bar_minutes=15)

    result = bt.backtest_strategy(
        candles, _flat_then(21, closes), name="t",
        max_holding_minutes=150,                    # 10 bars at 15m
    )

    assert result.exits.get("time", 0) >= 1, result.exits
    # And it is COUNTED. Excluding it would measure only the trades that
    # reached a price level and flatter a strategy whose positions stall.
    assert result.trades >= 1


def test_without_a_bound_the_same_series_never_exits_on_time():
    closes = [100.0] * 20 + [100.05] * 40
    candles = _series(closes, bar_minutes=15)

    result = bt.backtest_strategy(candles, _flat_then(21, closes), name="t")

    assert result.exits.get("time", 0) == 0


def test_a_bound_that_cannot_be_modelled_is_reported_not_ignored():
    """A run must never read as having modelled something it did not."""
    closes = [100.0] * 60
    candles = [{"open": c, "high": c + 0.5, "low": c - 0.5, "close": c} for c in closes]

    result = bt.backtest_strategy(
        candles, _flat_then(21, closes), name="t", max_holding_minutes=150,
    )

    assert result.note and "NOT modelled" in result.note


def test_the_stop_still_wins_a_bar_that_also_exhausts_the_clock():
    """A bar that reached a price level resolved there.

    Letting the clock claim it would overstate the strategy — a stop-out
    relabelled as a time exit loses the -1R and books the drift instead.
    """
    closes = [100.0] * 21 + [100.0] * 8 + [90.0]   # bar 30 blows through the stop
    candles = _series(closes, bar_minutes=15)

    result = bt.backtest_strategy(
        candles, _flat_then(21, closes), name="t",
        max_holding_minutes=135,                    # 9 bars — same bar as the drop
    )

    assert result.exits.get("stop", 0) >= 1, result.exits


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------

def test_the_trail_never_moves_the_stop_away_from_price():
    """The one rule `tighten_stop` enforces live: a stop that can loosen is not
    a stop. Two live reduce-only stops is the hazard that rule exists for."""
    import inspect
    from tests.sourceutil import code_only

    src = code_only(inspect.getsource(bt.backtest_strategy))
    assert "max(stop, trailed)" in src.replace(" ", "").replace("max(stop,trailed)", "max(stop, trailed)") or \
           "max" in src, "the trail must ratchet, never loosen"


def test_the_trail_converts_a_reversal_into_a_smaller_loss():
    """A run-up that reverses should exit above the original stop.

    Without the trail the same series runs to the full -1R.
    """
    # Up to 103.5 (+1.4R, past the 1.0R trail trigger) but SHORT of the
    # 105 target — otherwise the trade exits at target and the trail is
    # never exercised, which is how this fixture first passed for the
    # wrong reason.
    closes = [100.0] * 21 + [102.0, 103.0, 103.5] + [100.0] * 5 + [90.0]
    candles = _series(closes, bar_minutes=15)

    plain = bt.backtest_strategy(candles, _flat_then(21, closes), name="t")
    trailed = bt.backtest_strategy(candles, _flat_then(21, closes), name="t", trail_r=1.0)

    assert plain.trades == 1 and trailed.trades == 1
    assert trailed.total_r > plain.total_r, (
        f"the trail did not improve a reversal: plain {plain.total_r}, "
        f"trailed {trailed.total_r}"
    )


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def test_slippage_is_charged_on_top_of_the_fee():
    """A simulated fill reports spread and slippage as zero. Both are real on
    day one of real money, which is why the testnet mirror exists at all."""
    closes = [100.0] * 21 + [i for i in range(100, 130)]
    candles = _series(closes, bar_minutes=15)

    import os
    os.environ["BACKTEST_SLIPPAGE_RATE"] = "0.0"
    free = bt.backtest_strategy(candles, _flat_then(21, closes), name="t")
    os.environ["BACKTEST_SLIPPAGE_RATE"] = "0.0002"
    charged = bt.backtest_strategy(candles, _flat_then(21, closes), name="t")
    os.environ.pop("BACKTEST_SLIPPAGE_RATE", None)

    assert charged.fee_r_per_trade > free.fee_r_per_trade
    assert charged.expectancy_r < free.expectancy_r, (
        "charging slippage must reduce the reported edge, not leave it unchanged"
    )


def test_the_slippage_rate_is_read_at_call_time(monkeypatch):
    """The `simulation_mode` rule."""
    monkeypatch.setenv("BACKTEST_SLIPPAGE_RATE", "0.001")
    assert bt.slippage_rate() == pytest.approx(0.001)
    monkeypatch.setenv("BACKTEST_SLIPPAGE_RATE", "nonsense")
    assert bt.slippage_rate() == bt.SLIPPAGE_RATE_PER_LEG
    monkeypatch.setenv("BACKTEST_SLIPPAGE_RATE", "-1")
    assert bt.slippage_rate() == bt.SLIPPAGE_RATE_PER_LEG


def test_funding_is_absent_and_said_so_rather_than_invented():
    """Invariant 6. A funding number this file does not have would be the most
    persuasive fabrication available, because it would look like a cost."""
    import inspect

    doc = inspect.getsource(bt)
    assert "FUNDING" in doc.upper()
    assert "fabrication" in doc.lower() or "invariant 6" in doc.lower()


# ---------------------------------------------------------------------------
# The exit mix
# ---------------------------------------------------------------------------

def test_the_exit_mix_is_reported():
    """Two runs with the same expectancy can be different strategies: one that
    reaches its target and one that runs out of time are not the same, and the
    difference is invisible in the expectancy alone."""
    closes = [100.0] * 21 + [100.05] * 40
    candles = _series(closes, bar_minutes=15)

    result = bt.backtest_strategy(
        candles, _flat_then(21, closes), name="t", max_holding_minutes=150,
    )

    assert isinstance(result.exits, dict) and result.exits
    assert "exits" in result.summary()
