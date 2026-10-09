"""Error bars for R-multiple results, so a thin cell cannot become a policy.

WHY THIS EXISTS
===============
`scripts/measure_htf_gate.py` and `scripts/measure_conviction.py` produce tables
of expectancy per bucket, and the first readings out of both were about to be
reported as findings:

    the HTF gate lets through +0.0230R and blocks -0.0868R
    the debate AGREEING with the setup did WORSE than disagreeing
    the 0.3-0.4 confidence bucket beat the 0.2-0.3 bucket

Two of those three are noise, and the tables gave no way to tell which. At 300
trades the standard error on an expectancy is 0.083R, so ANY gap under about
0.17R is indistinguishable from chance -- which covers the second and third
lines above and not the first.

Reporting a number without its error bar is how a 52-trade cell becomes a
change to a live risk gate.

WHY THE SD IS EXACT HERE RATHER THAN ESTIMATED
==============================================
A backtested outcome under this project's risk model is BIMODAL: a target is
`+target/stop` R and a stop is exactly -1R (`strategy_backtest` keeps those
exact on purpose -- "a target that reported +1.96 because of a fee would make
every outcome a slightly different number with no obvious meaning"). So the
distribution is fully described by the win rate and the payoff, and the
variance follows in closed form rather than from a sample estimate. That holds
regardless of sample size, which is what makes a 35-trade cell's error bar
trustworthy even though its expectancy is not.

`sample_std` is provided for the cases where it does NOT hold -- real closed
trades, partial exits, a trade still open at the end of the window -- and the
callers use it when they have the individual outcomes.

WHAT "SIGNIFICANT" MEANS HERE, AND WHAT IT DOES NOT
===================================================
`compare` reports a z and a two-sigma verdict. Two sigma on ONE comparison is
roughly a 1-in-20 coincidence; across the five comparisons a sweep of this kind
produces it is closer to 1 in 4. So a lone z of 2.3 is suggestive and a z of
3.1 survives that correction -- `significant_after` exists so a caller states
how many comparisons it made instead of quietly ignoring the question.

None of this makes a result out-of-sample valid, and it is not a substitute for
the in/out split the sweep already does. It only answers "is this gap bigger
than the noise in this window".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Expectancy:
    """One bucket's result, with the uncertainty attached to it."""

    trades: int
    win_rate: float
    expectancy_r: float
    #: Standard error OF THE MEAN, in R. None when it cannot be derived.
    standard_error: Optional[float]

    @property
    def interval_95(self) -> Optional[tuple]:
        """The 95% interval, or None when there is no usable error bar."""
        if self.standard_error is None:
            return None
        half = 1.96 * self.standard_error
        return (self.expectancy_r - half, self.expectancy_r + half)

    @property
    def is_distinguishable_from_zero(self) -> bool:
        """True when the interval excludes zero.

        A positive expectancy whose interval straddles zero is not an edge that
        has been measured; it is an edge that has not been ruled out.
        """
        interval = self.interval_95
        if interval is None:
            return False
        return interval[0] > 0.0 or interval[1] < 0.0


def binomial_std(win_rate: float, reward_per_r: float = 2.0) -> float:
    """Exact per-trade SD for a bimodal +reward / -1 outcome.

    Raises on a reward of zero or less: a payoff that does not pay is not a risk
    model, and returning a plausible number for one would hide the caller's bug.
    """
    if reward_per_r <= 0:
        raise ValueError(f"reward_per_r must be positive, got {reward_per_r}")
    p = min(max(float(win_rate), 0.0), 1.0)
    mean = p * reward_per_r + (1.0 - p) * (-1.0)
    var = p * (reward_per_r - mean) ** 2 + (1.0 - p) * (-1.0 - mean) ** 2
    return math.sqrt(max(var, 0.0))


def sample_std(values: Sequence[float]) -> Optional[float]:
    """Sample SD (n-1), or None below two values.

    For the cases the closed form does not cover: real closed trades, partial
    exits, a position still open at the end of a window. None rather than 0.0,
    because a single observation has unknown spread, not no spread -- the same
    rule the rest of this codebase follows for "could not measure".
    """
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def from_outcomes(values: Sequence[float], *, wins: Optional[int] = None) -> Expectancy:
    """Build an `Expectancy` from the individual per-trade R values."""
    n = len(values)
    if n == 0:
        return Expectancy(trades=0, win_rate=0.0, expectancy_r=0.0, standard_error=None)
    won = wins if wins is not None else sum(1 for v in values if v > 0)
    sd = sample_std(values)
    return Expectancy(
        trades=n,
        win_rate=won / n,
        expectancy_r=sum(values) / n,
        standard_error=(sd / math.sqrt(n)) if sd is not None else None,
    )


def from_win_rate(
    *, trades: int, win_rate: float, expectancy_r: float, reward_per_r: float = 2.0
) -> Expectancy:
    """Build an `Expectancy` from a summary row, using the closed-form SD.

    `expectancy_r` is taken from the caller rather than re-derived, because the
    caller's figure is net of fees and the closed form is not.
    """
    if trades <= 0:
        return Expectancy(trades=0, win_rate=0.0, expectancy_r=0.0, standard_error=None)
    sd = binomial_std(win_rate, reward_per_r)
    return Expectancy(
        trades=trades,
        win_rate=win_rate,
        expectancy_r=expectancy_r,
        standard_error=sd / math.sqrt(trades),
    )


@dataclass(frozen=True)
class Comparison:
    gap_r: float
    standard_error: Optional[float]
    z: Optional[float]

    def verdict(self, comparisons_made: int = 1) -> str:
        """A sentence, not a boolean, because the honest answer has a caveat."""
        if self.z is None:
            return "not comparable (one side has no error bar)"
        if abs(self.z) < 2.0:
            return f"NOT significant (z={self.z:+.2f}); this gap is within the noise"
        if significant_after(self.z, comparisons_made):
            return (f"significant (z={self.z:+.2f}) and it survives a correction "
                    f"for {comparisons_made} comparison(s)")
        return (f"suggestive (z={self.z:+.2f}) but NOT after correcting for "
                f"{comparisons_made} comparisons -- a gap this size is expected "
                f"by chance when this many are made")


def compare(a: Expectancy, b: Expectancy) -> Comparison:
    """Is `a` genuinely different from `b`, or is the gap noise?"""
    gap = a.expectancy_r - b.expectancy_r
    if a.standard_error is None or b.standard_error is None:
        return Comparison(gap_r=gap, standard_error=None, z=None)
    se = math.sqrt(a.standard_error ** 2 + b.standard_error ** 2)
    return Comparison(gap_r=gap, standard_error=se, z=(gap / se) if se > 0 else None)


def significant_after(z: float, comparisons_made: int) -> bool:
    """Does |z| survive a Bonferroni correction for `comparisons_made` tests?

    Bonferroni rather than anything cleverer because it is the CONSERVATIVE
    choice and the cost of being wrong here is a live risk gate changed on a
    coincidence. It is also the one a reader can check by hand.
    """
    if comparisons_made <= 1:
        return abs(z) >= 2.0
    # Two-sided alpha of 0.05 split across the comparisons, via the normal
    # quantile. `erfcinv` is not in the stdlib, so this inverts erfc by bisection
    # -- a dozen iterations is plenty for a threshold we only compare against.
    alpha = 0.05 / comparisons_made
    lo, hi = 0.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        # Two-sided tail probability for |Z| > mid.
        tail = math.erfc(mid / math.sqrt(2.0))
        if tail > alpha:
            lo = mid
        else:
            hi = mid
    return abs(z) >= hi


def required_gap(trades: int, win_rate: float = 0.36, reward_per_r: float = 2.0) -> float:
    """How big a gap this many trades can even detect, at two sigma.

    For putting a number on "we need more data" instead of asserting it. At 300
    trades nothing under 0.17R is visible, which is most of what a bucket table
    shows.
    """
    if trades <= 0:
        return float("inf")
    return 2.0 * binomial_std(win_rate, reward_per_r) / math.sqrt(trades)
