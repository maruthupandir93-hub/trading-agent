"""Funding is DISCRETE. Every test here exists because pro-rating it is wrong.

WHY THIS FILE EXISTS
--------------------
This system trades perpetual futures and had no concept of funding anywhere in
any accounting path. The word appeared only in a specialist reading the funding
RATE as a directional signal; no position was ever charged for being held.

The obvious implementation — `rate * hours_held / 8` — is wrong, and wrong in
both directions, so the error does not average out. Funding is charged at fixed
wall-clock settlements (00:00 / 08:00 / 16:00 UTC) and only to positions open at
that instant:

    opened 09:00, closed 15:00  (6h)   crosses nothing  -> pays ZERO
    opened 15:30, closed 16:30  (1h)   crosses 16:00    -> pays ONE
    opened 09:00, closed 17:00  (8h)   crosses 16:00    -> pays ONE

A pro-rated model bills the six-hour hold for 0.75 settlements it never owed —
and this system's holds are mostly short, so the MAJORITY of its trades would be
charged for funding they never paid.
"""

from __future__ import annotations

import datetime

import pytest

from backend.services.funding import (
    DEFAULT_RATE_PER_SETTLEMENT,
    SETTLEMENT_HOURS,
    estimate_funding,
    settlements_between,
)


def _at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 9, day, hour, minute)


# ---------------------------------------------------------------------------
# Counting settlements — the whole correctness argument
# ---------------------------------------------------------------------------

def test_a_hold_that_crosses_no_settlement_pays_nothing():
    """The common case for this system, whose holds are typically under 8 hours.

    A pro-rated model would charge 0.75 of a settlement here.
    """
    assert settlements_between(_at(10, 9), _at(10, 15)) == 0


def test_a_one_hour_hold_across_a_settlement_pays_one():
    """Duration is irrelevant; crossing the instant is what is charged.

    One hour pays MORE than the six-hour hold above. That is not a bug and it is
    exactly what pro-rating gets backwards.
    """
    assert settlements_between(_at(10, 15, 30), _at(10, 16, 30)) == 1


def test_an_eight_hour_hold_can_cross_only_one():
    assert settlements_between(_at(10, 9), _at(10, 17)) == 1


def test_a_full_day_crosses_three():
    assert settlements_between(_at(10, 0, 1), _at(11, 0, 1)) == 3


def test_a_multi_day_hold_counts_every_settlement():
    assert settlements_between(_at(10, 0, 1), _at(13, 0, 1)) == 9


def test_the_boundary_is_half_open_so_an_entry_on_a_settlement_is_not_charged():
    """Opening exactly AT 16:00 is not billed for that settlement — the position
    did not exist when the venue took its snapshot.

    Getting this backwards double-charges every position opened on a settlement
    boundary, which is a common moment to enter precisely because the rate resets
    there.
    """
    assert settlements_between(_at(10, 16), _at(10, 17)) == 0


def test_a_close_exactly_on_a_settlement_IS_charged():
    """The other half of the same rule: open at the instant the venue bills."""
    assert settlements_between(_at(10, 15), _at(10, 16)) == 1


def test_a_zero_or_negative_window_charges_nothing():
    assert settlements_between(_at(10, 12), _at(10, 12)) == 0
    assert settlements_between(_at(10, 15), _at(10, 9)) == 0
    assert settlements_between(None, _at(10, 15)) == 0
    assert settlements_between(_at(10, 9), None) == 0


def test_an_aware_timestamp_is_converted_not_rejected():
    """`monitored_positions.opened_at` is timestamptz, so asyncpg returns an AWARE
    datetime while the rest of the codebase is naive UTC.

    Mixing them raised a TypeError in `_close` once already — AFTER the exchange
    had filled the closing order — so a restored position was closed repeatedly.
    """
    aware = _at(10, 15).replace(tzinfo=datetime.timezone.utc)
    assert settlements_between(aware, _at(10, 17)) == 1


# ---------------------------------------------------------------------------
# Sign: a short is CREDITED
# ---------------------------------------------------------------------------

def test_a_long_pays_a_positive_rate():
    est = estimate_funding(
        side="buy", notional=10_000.0,
        opened_at=_at(10, 15), closed_at=_at(10, 17), rate=0.0001,
    )
    assert est.settlements == 1
    assert est.cost == pytest.approx(1.0)      # 10,000 * 0.01%
    assert est.cost > 0, "a long pays when the rate is positive"


def test_a_short_RECEIVES_a_positive_rate():
    """Income, not a cost, and discarding it would understate exactly the trades
    this system takes most — it shorts perpetual futures."""
    est = estimate_funding(
        side="sell", notional=10_000.0,
        opened_at=_at(10, 15), closed_at=_at(10, 17), rate=0.0001,
    )
    assert est.cost == pytest.approx(-1.0)
    assert est.cost < 0


def test_a_negative_rate_flips_both_sides():
    long_est = estimate_funding(
        side="buy", notional=10_000.0,
        opened_at=_at(10, 15), closed_at=_at(10, 17), rate=-0.0001,
    )
    short_est = estimate_funding(
        side="sell", notional=10_000.0,
        opened_at=_at(10, 15), closed_at=_at(10, 17), rate=-0.0001,
    )
    assert long_est.cost < 0 < short_est.cost


def test_cost_scales_with_the_number_of_settlements():
    one = estimate_funding(side="buy", notional=10_000.0,
                           opened_at=_at(10, 15), closed_at=_at(10, 17), rate=0.0001)
    three = estimate_funding(side="buy", notional=10_000.0,
                             opened_at=_at(10, 0, 1), closed_at=_at(11, 0, 1), rate=0.0001)
    assert three.settlements == 3
    assert three.cost == pytest.approx(one.cost * 3)


# ---------------------------------------------------------------------------
# Honesty about what the number is
# ---------------------------------------------------------------------------

def test_the_estimate_is_never_labelled_measured():
    """The rate is captured at ENTRY and floats between settlements, so this is
    always an estimate — the same rule `fees.FeeResult.measured` follows."""
    est = estimate_funding(side="buy", notional=1_000.0,
                           opened_at=_at(10, 15), closed_at=_at(10, 17), rate=0.0001)
    assert est.measured is False


def test_a_missing_rate_falls_back_and_says_so():
    """No captured rate means the venue baseline is assumed, and the detail
    string states it — so an estimate is never dressed up as a measurement."""
    est = estimate_funding(side="buy", notional=10_000.0,
                           opened_at=_at(10, 15), closed_at=_at(10, 17), rate=None)
    assert est.rate == DEFAULT_RATE_PER_SETTLEMENT
    assert "baseline" in est.detail


def test_a_zero_settlement_result_explains_itself():
    """A zero must read as "crossed no settlement", not as "not implemented"."""
    est = estimate_funding(side="buy", notional=10_000.0,
                           opened_at=_at(10, 9), closed_at=_at(10, 15), rate=0.0001)
    assert est.cost == 0.0
    assert "no funding settlement" in est.detail


def test_the_settlement_hours_are_the_venues_actual_schedule():
    assert SETTLEMENT_HOURS == (0, 8, 16)


def test_funding_is_material_on_a_leveraged_multi_day_hold():
    """Documents WHY this module exists, in numbers.

    At 10x, a day of funding on a crowded long is ~0.3% of the ACCOUNT — in one
    direction, regardless of whether the trade works.
    """
    equity, leverage = 1_000.0, 10
    est = estimate_funding(
        side="buy", notional=equity * leverage,
        opened_at=_at(10, 0, 1), closed_at=_at(11, 0, 1), rate=0.0001,
    )
    assert est.settlements == 3
    assert est.cost / equity == pytest.approx(0.003, abs=1e-6)
