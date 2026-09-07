"""The backtest simulation core, on synthetic candles with known outcomes.

The network fetch is in `scripts/run_backtests.py`; this pins the deterministic
simulation logic offline — that a target hit is +2R, a stop is -1R, an ambiguous
bar assumes the stop, and the expectancy math is right. A backtester whose
arithmetic is wrong produces confident, precise, false numbers, which is worse
than no backtest.
"""

from __future__ import annotations

from typing import Any, Dict, List

from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER
from backend.core.strategy_backtest import backtest_strategy


def _bars(n: int, *, tr: float = 1.0, close: float = 100.0) -> List[Dict[str, Any]]:
    """Flat candles with a constant true range `tr`, so ATR == tr exactly."""
    out = []
    for _ in range(n):
        out.append({"open": close, "high": close + tr / 2, "low": close - tr / 2,
                    "close": close, "volume": 1000.0, "signal": "HOLD"})
    return out


def _signal_from_bar(hist: List[Dict[str, Any]]) -> str:
    """Reads the marker the fixtures plant on the last candle."""
    return hist[-1].get("signal", "HOLD") if hist else "HOLD"


def test_the_risk_model_matches_the_live_python_manager():
    """The backtest must use the SAME stop/target the agent places, or it measures
    a strategy nobody trades."""
    assert ATR_STOP_MULTIPLIER == 2.5
    assert ATR_TARGET_MULTIPLIER == 5.0


def test_a_reached_target_is_plus_two_R():
    bars = _bars(20, tr=1.0)          # ATR == 1.0
    bars[15]["signal"] = "BUY"        # enter long at close 100
    # Target = 100 + 5.0*1.0 = 105. Make bar 17 reach it.
    bars[17]["high"] = 106.0
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.trades == 1
    assert result.wins == 1
    assert result.expectancy_r == 2.0        # 5.0/2.5 reward per R
    assert result.trade_log[0]["outcome"] == "target"


def test_a_reached_stop_is_minus_one_R():
    bars = _bars(20, tr=1.0)
    bars[15]["signal"] = "BUY"
    # Stop = 100 - 2.5*1.0 = 97.5. Make bar 17 reach it.
    bars[17]["low"] = 97.0
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.trades == 1
    assert result.losses == 1
    assert result.expectancy_r == -1.0
    assert result.trade_log[0]["outcome"] == "stop"


def test_an_ambiguous_bar_assumes_the_stop_filled_first():
    """One bar whose range covers BOTH levels is counted as a loss — the
    conservative assumption without intrabar data."""
    bars = _bars(20, tr=1.0)
    bars[15]["signal"] = "BUY"
    # Bar 17 spans both 97.5 (stop) and 105 (target).
    bars[17]["low"] = 97.0
    bars[17]["high"] = 106.0
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.trade_log[0]["outcome"] == "stop"
    assert result.expectancy_r == -1.0


def test_a_short_targets_downward():
    bars = _bars(20, tr=1.0)
    bars[15]["signal"] = "SELL"       # short at 100
    # Short target = 100 - 5.0 = 95. Make bar 17 reach it.
    bars[17]["low"] = 94.0
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.trade_log[0]["direction"] == "short"
    assert result.wins == 1
    assert result.trade_log[0]["outcome"] == "target"


def test_no_signal_means_no_trades():
    bars = _bars(30, tr=1.0)          # every bar HOLD
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.trades == 0
    assert result.expectancy_r == 0.0


def test_a_position_open_at_the_end_is_marked_not_dropped():
    """A signal near the end that never resolves is counted at its mark, so a
    strategy cannot flatter itself by entering right before the data stops."""
    bars = _bars(20, tr=1.0)
    bars[18]["signal"] = "BUY"        # enters at 100 near the end, never resolves
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.open_at_end == 1
    # It never hit stop or target, so it is not a closed win or loss.
    assert result.trades == 0


def test_expectancy_over_a_mixed_sequence():
    """Two wins (+2R each) and two losses (-1R each) -> expectancy (2+2-1-1)/4 = 0.5R.

    Trades are spaced 20 bars apart ON PURPOSE: a resolving bar's large range
    inflates the 14-bar ATR, so a too-close next entry would be sized against a
    polluted ATR. That is real behaviour, not a bug — the spacing lets ATR settle
    back to 1.0 before each entry so the arithmetic is clean and exact."""
    bars = _bars(100, tr=1.0)
    plan = [(15, "win"), (35, "win"), (55, "loss"), (75, "loss")]
    for idx, kind in plan:
        bars[idx]["signal"] = "BUY"
        if kind == "win":
            bars[idx + 2]["high"] = 106.0     # >= 105 target, ATR==1.0 at entry
        else:
            bars[idx + 2]["low"] = 97.0       # <= 97.5 stop
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    assert result.trades == 4
    assert result.wins == 2
    assert result.losses == 2
    assert result.win_rate == 0.5
    assert result.expectancy_r == 0.5
    assert result.payoff == 2.0               # avg win 2.0 / avg loss 1.0
    assert result.total_r == 2.0


def test_only_one_position_at_a_time():
    """A second signal while a trade is open is ignored until the first resolves."""
    bars = _bars(40, tr=1.0)
    bars[15]["signal"] = "BUY"
    bars[16]["signal"] = "BUY"        # should be ignored — position still open
    bars[25]["high"] = 106.0          # first trade resolves here (target)
    result = backtest_strategy(bars, _signal_from_bar, name="t")
    # Exactly one entry was taken from the two adjacent signals.
    assert result.trades == 1
