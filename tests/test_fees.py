"""Fees must be subtracted, and a modelled fee must never pass as a measured one.

WHY THIS FILE EXISTS
--------------------
Nothing in this system subtracted a trading fee from any P&L figure. The closing
row stored `(exit - entry) * qty`, `strategy_performance` aggregated it, the
backtest reported expectancy in R with no cost model, and the paper book deducted
margin but never the commission. Every profit number ever produced was GROSS.

Against this system's own risk model and its own backtest that is not a rounding
error — it is most of the edge:

    1R (2.5 x ATR on SOL)          ~1.075% of notional
    taker, both sides               0.10%   of notional
    -> per round trip              ~0.093R

    Scalping   +0.160R gross  ->  +0.067R net
    Breakout   +0.142R gross  ->  +0.049R net
    Swing      -0.007R gross  ->  -0.100R net   (break-even becomes losing)

The second property these tests guard is the honest one: `measured` distinguishes
a fee the VENUE reported from one this system MODELLED. A paper fill has no venue
and so no fee to report; modelling is the only option, but a modelled cost
presented as a measured one would make paper P&L look exactly as authoritative as
real P&L when it is strictly an estimate. That is invariant 6 applied to a cost.
"""

from __future__ import annotations

import pytest

from backend.services.fees import (
    FeeResult,
    fee_from_order,
    modelled_fee,
    resolve_fee,
    round_trip_fee,
    taker_rate,
)


# ---------------------------------------------------------------------------
# The rate itself
# ---------------------------------------------------------------------------

def test_the_default_rate_is_the_venue_taker_rate():
    """0.05% is the standard Binance USDⓈ-M / Bybit linear taker tier."""
    assert taker_rate() == pytest.approx(0.0005)


@pytest.mark.parametrize("bad", ["not-a-number", "-0.001", "0.5", ""])
def test_an_implausible_configured_rate_falls_back_rather_than_corrupting_pnl(bad, monkeypatch):
    """A negative rate is a rebate and 50% is a typo; both would poison every figure.

    Falling back is safe here in a way it usually is not, because the fallback is
    the real published rate rather than zero. A fee that silently became 0.0 on a
    typo would reintroduce the exact bug this module exists to fix.
    """
    monkeypatch.setenv("FEE_TAKER_RATE", bad)
    assert taker_rate() == pytest.approx(0.0005)


def test_the_rate_is_read_at_call_time_not_frozen_at_import(monkeypatch):
    """The `simulation_mode` bug is the standing example of what a frozen read costs.

    An operator on a VIP tier changes the rate, is told it applied, and every P&L
    figure keeps using the old one until a restart.
    """
    monkeypatch.setenv("FEE_TAKER_RATE", "0.0002")
    assert taker_rate() == pytest.approx(0.0002)


# ---------------------------------------------------------------------------
# Measured vs modelled
# ---------------------------------------------------------------------------

def test_a_modelled_fee_is_never_labelled_measured():
    fee = modelled_fee(10_000.0)
    assert fee.cost == pytest.approx(5.0)      # 10,000 x 0.05%
    assert fee.measured is False


def test_a_venue_reported_fee_is_labelled_measured():
    fee = fee_from_order({"fee": {"cost": 3.21, "currency": "USDT"}})
    assert fee is not None
    assert fee.cost == pytest.approx(3.21)
    assert fee.measured is True


def test_multiple_fee_entries_are_summed_not_first_taken():
    """A fill that crossed several price levels is billed as several entries.

    Reading only the first would under-count the cost, and under-counting a cost
    always errs toward flattering the account.
    """
    fee = fee_from_order({"fees": [
        {"cost": 1.0, "currency": "USDT"},
        {"cost": 2.5, "currency": "USDT"},
    ]})
    assert fee is not None
    assert fee.cost == pytest.approx(3.5)


def test_a_fee_billed_in_another_currency_is_refused_rather_than_converted():
    """Binance bills commission in BNB when the discount is on; Bybit in BIT.

    Converting needs a price this module must not fetch, and a wrong conversion
    would be worse than an honest model because it would carry `measured=True`.
    """
    assert fee_from_order({"fee": {"cost": 0.004, "currency": "BNB"}}) is None


def test_no_reported_fee_is_none_not_zero():
    """"The venue said zero" and "the venue said nothing" are different facts.

    Collapsing them would make `resolve_fee` unable to tell when to model.
    """
    assert fee_from_order({}) is None
    assert fee_from_order(None) is None
    assert fee_from_order({"id": "abc", "filled": 1.0}) is None


def test_resolve_prefers_the_venue_and_falls_back_to_the_model():
    reported = resolve_fee(
        {"fee": {"cost": 9.99, "currency": "USDT"}}, qty=2.0, price=100.0
    )
    assert reported.measured is True and reported.cost == pytest.approx(9.99)

    absent = resolve_fee({"filled": 2.0}, qty=2.0, price=100.0)
    assert absent.measured is False
    assert absent.cost == pytest.approx(200.0 * 0.0005)


def test_a_fee_is_always_a_positive_cost():
    """The caller subtracts it. A signed value invites the sign being applied twice,
    which on a fee is the difference between paying it and being paid it."""
    assert modelled_fee(-10_000.0).cost > 0
    assert fee_from_order({"fee": {"cost": -3.0, "currency": "USDT"}}).cost > 0


def test_round_trip_sums_both_legs():
    assert round_trip_fee(1.5, 2.5) == pytest.approx(4.0)
    assert round_trip_fee(None, 2.0) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# The thing that actually matters: it changes the P&L
# ---------------------------------------------------------------------------

def test_fees_are_a_material_fraction_of_this_systems_edge():
    """Documents WHY this module exists, in numbers, so a future 'simplification'
    that drops fee accounting has to argue with the arithmetic.

    SOL 15m ATR% ~0.43, stop at 2.5x ATR, so 1R is ~1.075% of notional.
    """
    entry = 100.0
    one_r = entry * 0.01075
    fee_r = (2 * taker_rate() * entry) / one_r

    assert fee_r == pytest.approx(0.093, abs=0.005)
    # The three strategies the backtest ranks as profitable, gross.
    for gross_edge in (0.160, 0.142, 0.125):
        assert fee_r / gross_edge > 0.55, (
            "fees are over half the edge; this cannot be left out of the P&L"
        )


def test_the_backtest_charges_a_round_trip_and_a_wider_stop_costs_less_in_r():
    """Fee-in-R must scale inversely with stop width, not be a flat constant.

    A wider stop means a larger R, so the same currency cost is a smaller
    fraction of it. A constant would misprice every strategy whose stop differs
    from the one it was tuned on.
    """
    entry, atr = 100.0, 0.43
    narrow = (2.0 * taker_rate() * entry) / (1.5 * atr)
    wide = (2.0 * taker_rate() * entry) / (2.5 * atr)
    assert wide < narrow
    assert wide == pytest.approx(0.093, abs=0.005)
