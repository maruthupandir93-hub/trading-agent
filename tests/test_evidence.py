"""Error bars on backtest results, and the overclaim they were written to stop.

THE THREE READINGS THAT PROMPTED THIS, all from the same afternoon's sweeps:

    the HTF gate lets through +0.0230R and blocks -0.0868R      z = +1.01
    the debate AGREEING with a setup did WORSE than disagreeing  z = -0.78
    the 0.3-0.4 confidence bucket beat the 0.2-0.3 bucket        z = +0.03

NONE OF THE THREE IS SIGNIFICANT, and all three were printed as flat numbers
with no uncertainty. The first was about to be reported as "the gate is
validated" and the third as "conviction predicts outcome".

A throwaway script written to check them got this WRONG IN THE OTHER DIRECTION
first -- it passed the expectancy as a positional argument where the reward
ratio belonged, inflating every z by about three times and turning all three
into findings. That is the whole argument for this module existing: the
arithmetic is easy to get wrong in a one-off, and the direction of the error is
whichever one the author was hoping for.

At 324 and 398 trades the two-sigma bar on a DIFFERENCE is 0.22R. The observed
HTF gap is 0.11R -- about half of what this sample can see. Demonstrating a gap
that size needs roughly 1,400 trades per side.
"""

from __future__ import annotations

import math

import pytest

from backend.core import evidence as ev


# ---------------------------------------------------------------------------
# The closed-form SD, which is what makes a thin cell's error bar trustworthy
# ---------------------------------------------------------------------------

def test_the_standard_deviation_is_exact_for_a_bimodal_outcome():
    """`strategy_backtest` keeps outcomes at exactly +2R / -1R on purpose, so
    the spread follows from the win rate rather than from a sample estimate.
    Checked against the definition rather than a remembered constant."""
    p, reward = 0.36, 2.0
    mean = p * reward + (1 - p) * -1.0
    expected = math.sqrt(p * (reward - mean) ** 2 + (1 - p) * (-1.0 - mean) ** 2)
    assert ev.binomial_std(p, reward) == pytest.approx(expected)


def test_a_certain_outcome_has_no_spread():
    assert ev.binomial_std(1.0) == pytest.approx(0.0)
    assert ev.binomial_std(0.0) == pytest.approx(0.0)


def test_a_payoff_that_does_not_pay_is_refused():
    """A reward of zero or less is a caller bug. Returning a plausible number
    for it would hide the bug inside an error bar that looks fine."""
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError):
            ev.binomial_std(0.4, bad)


def test_the_error_bar_shrinks_with_the_square_root_of_the_sample():
    a = ev.from_win_rate(trades=100, win_rate=0.36, expectancy_r=0.05)
    b = ev.from_win_rate(trades=400, win_rate=0.36, expectancy_r=0.05)
    assert b.standard_error == pytest.approx(a.standard_error / 2.0, rel=1e-9)


# ---------------------------------------------------------------------------
# "Could not measure" is never zero
# ---------------------------------------------------------------------------

def test_one_observation_has_unknown_spread_not_no_spread():
    """INVARIANT 6 in miniature. A single trade with SD 0.0 would claim an
    infinitely precise expectancy and make every comparison against it
    significant."""
    assert ev.sample_std([1.7]) is None
    assert ev.sample_std([]) is None
    assert ev.from_outcomes([2.0]).standard_error is None


def test_an_empty_bucket_is_empty_rather_than_a_zero_result():
    e = ev.from_outcomes([])
    assert e.trades == 0 and e.standard_error is None
    assert e.is_distinguishable_from_zero is False


def test_a_comparison_against_an_unmeasurable_side_reports_so():
    a = ev.from_win_rate(trades=300, win_rate=0.4, expectancy_r=0.1)
    b = ev.from_outcomes([1.0])
    c = ev.compare(a, b)
    assert c.z is None
    assert "not comparable" in c.verdict()


# ---------------------------------------------------------------------------
# The three real readings
# ---------------------------------------------------------------------------

def test_the_htf_gate_reading_is_promising_and_not_yet_demonstrated():
    """THE MOST IMPORTANT TEST IN THIS FILE, because it pins the result that was
    about to be announced as a validated gate.

    +0.0230R kept against -0.0868R blocked looks decisive written down. It is
    z = +1.01 -- one standard error, the sort of gap a fair coin produces about
    a third of the time. The gate may well be doing exactly what it was built to
    do; this sample cannot show it.
    """
    kept = ev.from_win_rate(trades=324, win_rate=0.383, expectancy_r=+0.0230)
    blocked = ev.from_win_rate(trades=398, win_rate=0.354, expectancy_r=-0.0868)
    c = ev.compare(kept, blocked)
    assert c.z == pytest.approx(1.01, abs=0.05)
    assert not ev.significant_after(c.z, comparisons_made=1)
    assert "NOT significant" in c.verdict()

    # And the number that says what WOULD settle it, so "we need more data" is
    # a quantity rather than a shrug.
    import math

    sd = ev.binomial_std(0.37)
    per_side = 1400
    assert 2 * sd * math.sqrt(2.0 / per_side) <= abs(c.gap_r) + 1e-9
    assert 2 * sd * math.sqrt(2.0 / 700) > abs(c.gap_r)


def test_the_conviction_buckets_are_a_coin_flip():
    """+0.0779R vs +0.0703R on 108 and 71 trades reads like a trend and is
    nothing. If this ever starts passing as significant, the sample grew --
    check that before believing the conclusion changed."""
    high = ev.from_win_rate(trades=108, win_rate=0.398, expectancy_r=+0.0779)
    low = ev.from_win_rate(trades=71, win_rate=0.380, expectancy_r=+0.0703)
    c = ev.compare(high, low)
    assert abs(c.z) < 0.2
    assert "NOT significant" in c.verdict()


def test_the_backwards_looking_result_is_also_just_noise():
    """The debate AGREEING with a setup appeared to do WORSE than disagreeing,
    which contradicts the premise the supervisor's direction check rests on.

    z = -0.78. Reporting it as a finding would have argued for weakening a
    safety gate on the strength of a coin flip -- and a separate check confirmed
    the debate's direction is not inverted, just weak (hit rate 46-51% over
    1-12h horizons).
    """
    agrees = ev.from_win_rate(trades=266, win_rate=0.353, expectancy_r=-0.0378)
    disagrees = ev.from_win_rate(trades=312, win_rate=0.397, expectancy_r=+0.0569)
    c = ev.compare(agrees, disagrees)
    assert abs(c.z) == pytest.approx(0.78, abs=0.05)
    assert not ev.significant_after(c.z, comparisons_made=1)
    assert "NOT significant" in c.verdict()


# ---------------------------------------------------------------------------
# "We need more data" as a number
# ---------------------------------------------------------------------------

def test_required_gap_puts_a_number_on_how_blind_a_sample_is():
    assert ev.required_gap(300) == pytest.approx(0.166, abs=0.005)
    assert ev.required_gap(2000) == pytest.approx(0.064, abs=0.005)
    # Monotonic, and a sample of nothing can detect nothing.
    assert ev.required_gap(100) > ev.required_gap(1000)
    assert ev.required_gap(0) == float("inf")


def test_an_edge_whose_interval_straddles_zero_is_not_a_measured_edge():
    """The kept set's +0.0230R over 324 trades is POSITIVE and is NOT an edge
    that has been demonstrated -- its interval includes zero. The gate is
    justified by the gap to the blocked set, not by this number."""
    kept = ev.from_win_rate(trades=324, win_rate=0.383, expectancy_r=+0.0230)
    assert kept.expectancy_r > 0
    assert kept.is_distinguishable_from_zero is False
    lo, hi = kept.interval_95
    assert lo < 0 < hi


def test_a_real_edge_over_a_big_sample_is_distinguishable():
    big = ev.from_win_rate(trades=5000, win_rate=0.40, expectancy_r=+0.20)
    assert big.is_distinguishable_from_zero is True


# ---------------------------------------------------------------------------
# The correction itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,expected_threshold", [(1, 1.96), (5, 2.58), (20, 3.02)])
def test_the_correction_raises_the_bar_as_more_comparisons_are_made(n, expected_threshold):
    """Checked at the boundary from both sides, because an off-by-a-little
    threshold is invisible until it passes something it should not."""
    assert ev.significant_after(expected_threshold + 0.05, comparisons_made=n)
    assert not ev.significant_after(expected_threshold - 0.05, comparisons_made=n)
