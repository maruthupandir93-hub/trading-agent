"""The learning loop: realised results reaching strategy selection.

WHAT WAS OPEN
=============
All nine profiles carried `historical_success_rate=None` and the scorer said so
on every run. The agent chose strategies purely on how well they FIT current
conditions; nothing it learned from an outcome ever reached that choice. It had
reflection, hypotheses and memory, and none of them closed the loop that decides
what gets traded.

THE TWO PROPERTIES THAT MAKE THIS SAFE
======================================
  1. THE SAMPLE FLOOR. A win rate over three trades is noise wearing a
     percentage sign. Below `MIN_SAMPLE` it is reported but must NOT influence
     selection — otherwise one lucky sequence entrenches a bad strategy, which
     is the classic way an "adaptive" system destroys itself.
  2. NO RECORD IS NEUTRAL, NOT ZERO. Scoring an unmeasured strategy as a failure
     would permanently freeze out every strategy that has not traded yet,
     including the one that would have worked.

DETERMINISTIC, SO INVARIANT 5 HOLDS. No model is consulted anywhere in this path.
The system counts its own results; it does not let a model rewrite its own rules.
"""

from __future__ import annotations

import pytest

from backend.graphs.nodes.opportunity import (
    WEIGHT_TRACK_RECORD,
    _score_one,
    _track_record_score,
)
from backend.services import strategy_performance


@pytest.fixture(autouse=True)
def _clean():
    strategy_performance.reset_cache()
    yield
    strategy_performance.reset_cache()


# ---------------------------------------------------------------------------
# The score mapping
# ---------------------------------------------------------------------------

def test_no_record_scores_NEUTRAL_not_zero():
    """A new strategy must still be able to compete on conditions.

    Zero would permanently freeze out everything that has not traded yet.
    """
    score, detail = _track_record_score(None)
    assert score == 0.5
    assert "not enough closed trades" in detail


def test_a_better_win_rate_scores_higher():
    poor, _ = _track_record_score(0.20)
    mid, _ = _track_record_score(0.35)
    good, _ = _track_record_score(0.55)
    assert poor < mid < good


def test_the_scale_is_clamped_at_both_ends():
    # A 5% and a 95% win rate are both far outside anything this system will
    # produce; neither should be able to dominate the other three components.
    assert _track_record_score(0.0)[0] == 0.0
    assert _track_record_score(1.0)[0] == 1.0


def test_the_operators_current_33_percent_lands_mid_scale():
    """Anchored on break-even for a 2:1 payoff, not on 50% being 'good'.

    At 2:1, roughly a third of trades winning is break-even — so 33% should read
    as neither good nor bad, which is what the account is actually doing.
    """
    score, _ = _track_record_score(0.3333)
    assert 0.3 < score < 0.5


# ---------------------------------------------------------------------------
# Integration with the score
# ---------------------------------------------------------------------------

def test_track_record_actually_moves_the_score():
    """The loop is only closed if the number changes the outcome."""
    bars = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 + i * 0.1,
             "volume": 1000.0} for i in range(120)]

    unproven, _ = _score_one("Trend", bars, "Bullish", "NORMAL", None)
    winning, _ = _score_one("Trend", bars, "Bullish", "NORMAL", 0.55)
    losing, _ = _score_one("Trend", bars, "Bullish", "NORMAL", 0.20)

    assert winning > unproven > losing
    # And the whole spread is bounded by the weight, so a track record cannot
    # override conditions entirely.
    assert (winning - losing) == pytest.approx(WEIGHT_TRACK_RECORD, abs=0.01)


# ---------------------------------------------------------------------------
# The sample floor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_strategy_below_the_floor_is_not_usable(monkeypatch):
    """Reported, but not allowed to steer selection."""
    async def fake_load():
        return {
            "Trend": {
                "strategy": "Trend", "sampleSize": 3, "wins": 3, "losses": 0,
                "winRate": 1.0, "totalPnl": 300.0, "avgWin": 100.0, "avgLoss": 0.0,
                "expectancy": 100.0,
                "usable": 3 >= strategy_performance.MIN_SAMPLE,
            }
        }

    monkeypatch.setattr(strategy_performance, "_load", fake_load)

    # A perfect 3-for-3 record must NOT reach scoring.
    assert await strategy_performance.success_rate("Trend") is None


@pytest.mark.asyncio
async def test_a_strategy_past_the_floor_is_usable(monkeypatch):
    n = strategy_performance.MIN_SAMPLE

    async def fake_load():
        return {
            "Trend": {
                "strategy": "Trend", "sampleSize": n, "wins": n // 2, "losses": n - n // 2,
                "winRate": 0.5, "totalPnl": 10.0, "avgWin": 20.0, "avgLoss": 10.0,
                "expectancy": 5.0, "usable": True,
            }
        }

    monkeypatch.setattr(strategy_performance, "_load", fake_load)
    assert await strategy_performance.success_rate("Trend") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_no_database_is_reported_as_None_not_as_no_history(monkeypatch):
    """None means "could not read". Returning {} would claim, falsely, that no
    strategy has ever traded — and scoring would proceed as if that were known."""
    async def no_db():
        return None

    monkeypatch.setattr(strategy_performance, "_load", no_db)

    assert await strategy_performance.performance() is None
    assert await strategy_performance.success_rate("Trend") is None

    summary = await strategy_performance.summary()
    assert summary["available"] is False


@pytest.mark.asyncio
async def test_the_summary_ranks_by_expectancy_not_win_rate(monkeypatch):
    """Win rate alone decides nothing: 33% at 2:1 and 60% at 0.5:1 are both
    roughly break-even, and ranking on win rate would prefer the wrong one."""
    async def fake_load():
        return {
            "HighWinRate": {
                "strategy": "HighWinRate", "sampleSize": 50, "wins": 30, "losses": 20,
                "winRate": 0.6, "totalPnl": -50.0, "avgWin": 10.0, "avgLoss": 20.0,
                "expectancy": 0.6 * 10 - 0.4 * 20, "usable": True,
            },
            "LowWinRate": {
                "strategy": "LowWinRate", "sampleSize": 50, "wins": 17, "losses": 33,
                "winRate": 0.34, "totalPnl": 200.0, "avgWin": 60.0, "avgLoss": 20.0,
                "expectancy": 0.34 * 60 - 0.66 * 20, "usable": True,
            },
        }

    monkeypatch.setattr(strategy_performance, "_load", fake_load)
    summary = await strategy_performance.summary()

    assert summary["strategies"][0]["strategy"] == "LowWinRate"
    assert summary["usableCount"] == 2


def test_the_loop_consults_no_model():
    """Invariant 5's boundary, asserted against the module's own source.

    Measured arithmetic over closed trades is not a model rewriting a strategy.
    If an LLM call ever appeared here, that distinction would be gone.
    """
    import inspect

    source = inspect.getsource(strategy_performance)
    for forbidden in ("get_provider", "complete(", "ModelTier", "llm"):
        assert forbidden not in source, (
            f"the learning loop must stay deterministic; found {forbidden!r}"
        )
