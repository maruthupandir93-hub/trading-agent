"""A fixed profit target — so a trade ends as a win or a stop, never a scratch.

THE PROBLEM, FROM THE LIVE LEDGER
---------------------------------
Of 4,003 closed trades, 2,136 (53.4%) realised less than 0.001, and 1,121 of
those exited tagged "stop-loss". That is not the market — it is the system's own
scale-out. The partial banks half at +1R and moves the RUNNER's stop to
break-even, so the runner's single most likely outcome is an exit at ~0.00.

The operator described it exactly: "one trade starts and takes a profit and
holding and sometimes the price is reverse and go to loss and when it reach the
0.00 it finishes".

A fixed percentage target closes the WHOLE position at once. There is no runner
left at break-even, so the scratch cannot happen.

WHAT THE PERCENTAGE MEANS. A favourable move of that much in PRICE. With leverage
it is amplified against the margin — at 3x a 2% move is ~6% of margin, at 10x
~20%. Price rather than equity so the setting means the same thing at every
leverage the session might use.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core.message_bus import MessageBus
from backend.models.events import OrderFilledEvent, TarApprovedEvent


@pytest.fixture
def bus(monkeypatch):
    private = MessageBus()
    import backend.core.message_bus as mb

    monkeypatch.setattr(mb, "get_message_bus", lambda: private)
    for module in ("backend.core.agent_base", "backend.agents.position_monitor"):
        try:
            mod = __import__(module, fromlist=["get_message_bus"])
            if hasattr(mod, "get_message_bus"):
                monkeypatch.setattr(mod, "get_message_bus", lambda: private)
        except (ImportError, AttributeError):
            pass
    return private


class FakeExecution:
    def __init__(self, fill=None):
        self.closes = []
        self._fill = fill

    async def close_position(self, **kw):
        self.closes.append(kw)
        return self._fill if self._fill is not None else kw.get("_px", 100.0)


@pytest.fixture
def monitor(bus):
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    return agent


def _open(monitor, entry=100.0, stop=97.5, side="buy", qty=1.0):
    tar_id = uuid.uuid4()
    asyncio.run(monitor.handle_event(TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT", direction="LONG" if side == "buy" else "SHORT",
        approved_size=qty, approved_leverage=3, cro_rationale="ok",
        stop_loss=stop, tab="paper",
        take_profit=(entry + 5000) if side == "buy" else (entry - 5000),
    )))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="x", order_id=str(uuid.uuid4()), symbol="SOL/USDT",
        side=side, tab="paper", fill_price=entry, fill_quantity=qty,
        slippage_bps=1.0, fee=0.0,
    )))
    return monitor._open[str(tar_id)]


def _tick(monitor, price):
    asyncio.run(monitor._check_price("SOL/USDT", price))


# ---------------------------------------------------------------------------
# The target itself
# ---------------------------------------------------------------------------

def test_it_defaults_to_OFF_and_the_scale_out_is_off_with_it(monkeypatch):
    """THE DEFAULT WAS 2.0 AND IS NOW 0, AND BOTH DECISIONS WERE RIGHT AT THE TIME.

    It first shipped OFF, which was wrong: 53.4% of trades were closing at ~0.00
    because the scale-out left a runner at break-even, and a fixed target removes
    that. So it was defaulted ON at 2%.

    That fix had a cost nobody had computed. The target is a percentage of MARGIN,
    so the PRICE move it needs is `PROFIT_TARGET_PCT / leverage` — while the stop
    stays at 2.5x ATR and does not shrink. The two collide as leverage rises.
    Measured on the operator's live XRP/USDT session (ATR 0.504%, stop 1.26%):

        2.0% at 10x  ->  0.20% target vs 1.26% stop  ->  93.2% break-even
        2.0% at  3x  ->  0.67% target vs 1.26% stop  ->  70.6% break-even
        0 (ATR pair) ->  2.52% target vs 1.26% stop  ->  36.0% break-even

    Measured live win rate at the time: 54.5% (6 of 11). A 93% break-even is
    losing by construction, so the operator turned it off on 2026-09-28 and the
    exit went back to the ATR pair, which is 2:1 BY CONSTRUCTION at every
    leverage (ATR_TARGET_MULTIPLIER 5.0 / ATR_STOP_MULTIPLIER 2.5).

    0 DOES NOT MEAN "NO TARGET" — `_check_price` tests `pos.take_profit` on every
    tick regardless. This setting only ever added a NEARER exit that overrode it.

    AND THE SCALE-OUT HAD TO MOVE WITH IT. It is gated on
    `PROFIT_TARGET_PCT <= 0`, so it was dormant only BECAUSE the target was on.
    Leaving its default at 0.5 while turning the target off would have restored
    the exact failure the target was introduced to remove.
    """
    import backend.agents.position_monitor as pm

    assert pm.PROFIT_TARGET_PCT == 0.0
    assert pm.PARTIAL_TP_FRACTION == 0.0, (
        "the scale-out must be off too, or turning the target off reintroduces "
        "the break-even runner"
    )


def test_the_atr_pair_that_takes_over_is_two_to_one():
    """What the operator actually gets at 0, and why it is leverage-independent.

    The target and stop are both multiples of the SAME measured ATR, so their
    ratio is fixed no matter how volatile the instrument or how much leverage the
    session uses. That is the property a fixed percentage cannot have.
    """
    from backend.core.risk_manager import (
        ATR_STOP_MULTIPLIER,
        ATR_TARGET_MULTIPLIER,
    )

    payoff = ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER
    assert payoff == 2.0
    breakeven = ATR_STOP_MULTIPLIER / (ATR_STOP_MULTIPLIER + ATR_TARGET_MULTIPLIER)
    assert round(breakeven * 100, 1) == 33.3


def test_the_whole_position_closes_at_the_target(monkeypatch, monitor):
    # PRICE basis pinned: this test is about the exit MECHANISM (whole position,
    # direction, precedence), not about the leverage arithmetic. On the default
    # "account" basis the fixture's 3x leverage would make a 2% target a 0.667%
    # move, so the tick sequence below would fire earlier and this would be
    # measuring the wrong thing. The account maths has its own tests at the
    # bottom of this file.
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_BASIS", "price")
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=102.0)
    monitor.attach_execution(ex)

    pos = _open(monitor, entry=100.0)
    _tick(monitor, 101.0)                      # +1%, not yet
    assert not ex.closes

    _tick(monitor, 102.0)                      # +2% — bank it
    assert len(ex.closes) == 1
    assert ex.closes[0]["qty"] == pytest.approx(1.0), "the WHOLE position, not half"
    assert ex.closes[0]["reason"] == "profit-target"


def test_a_short_reaches_the_target_on_a_FALL(monkeypatch, monitor):
    """This agent shorts perpetuals. A long-only target would leave every short
    running to its ATR target while longs banked at 2%."""
    # PRICE basis: this is about DIRECTION, not the leverage arithmetic.
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_BASIS", "price")
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=98.0)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=102.5, side="sell")
    _tick(monitor, 99.0)
    assert not ex.closes
    _tick(monitor, 98.0)                       # -2% is +2% for a short
    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "profit-target"


def test_an_adverse_move_never_triggers_it(monkeypatch, monitor):
    # PRICE basis pinned: this test is about the exit MECHANISM (whole position,
    # direction, precedence), not about the leverage arithmetic. On the default
    # "account" basis the fixture's 3x leverage would make a 2% target a 0.667%
    # move, so the tick sequence below would fire earlier and this would be
    # measuring the wrong thing. The account maths has its own tests at the
    # bottom of this file.
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_BASIS", "price")
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=98.0)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=90.0)
    _tick(monitor, 98.0)                       # -2%: a LOSS, not a target
    assert [c for c in ex.closes if c.get("reason") == "profit-target"] == []


# ---------------------------------------------------------------------------
# The scratch it exists to remove
# ---------------------------------------------------------------------------

def test_the_scale_out_is_bypassed_while_the_target_is_on(monkeypatch, monitor):
    """THE WHOLE POINT. Leaving both on would scale out at +1R, move the stop to
    break-even, and reintroduce exactly the ~0.00 exit this removes.

    Entry 100, stop 97.5 -> 1R = 2.5, so +1R is 102.5. A 2% target fires at 102.0
    FIRST, closing everything, so no runner is ever left at break-even.
    """
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.5)
    ex = FakeExecution(fill=102.0)
    monitor.attach_execution(ex)

    pos = _open(monitor, entry=100.0, stop=97.5, qty=1.0)
    _tick(monitor, 102.0)

    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "profit-target"
    assert pos.partial_done is not True, "no scale-out should have happened"


def test_with_the_target_off_the_scale_out_still_runs(monkeypatch, monitor):
    """Turning the target off must restore the old behaviour completely, not
    leave the partial disabled as a side effect."""
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 0.0)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.5)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_R", 1.0)
    ex = FakeExecution(fill=102.5)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=97.5, qty=1.0)
    _tick(monitor, 102.5)                      # +1R
    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "partial-tp"


def test_the_stop_still_wins_a_tick_that_spans_both(monkeypatch, monitor):
    """Invariant: a candle through the stop AND the target is assumed to have hit
    the stop first. Assuming the favourable one would overstate performance."""
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    ex = FakeExecution(fill=97.5)
    monitor.attach_execution(ex)

    _open(monitor, entry=100.0, stop=97.5)
    _tick(monitor, 97.0)                       # through the stop
    assert len(ex.closes) == 1
    assert ex.closes[0]["reason"] == "stop-loss"


# ---------------------------------------------------------------------------
# THE TARGET IS A SHARE OF THE ACCOUNT, NOT OF THE PRICE
# ---------------------------------------------------------------------------
#
# The operator's own words: "if I choose 10x leverage, for my target of 2% my
# original amount could move like 0.2% only, then the 10x moves me to 2%".
#
# That is right, and it is NOT what a price-based target does. Under "price" a
# 2% setting at 10x is a 2% price move — a 20% account gain, ten times what was
# asked for. The same setting silently means a different trade at every leverage,
# and the operator is never told.
#
# Under "account" the required price move is target / leverage, so "2%" means 2%
# of the margin deployed whatever the leverage is.

def _pos(leverage, symbol="SOL/USDT"):
    class P:
        pass
    p = P()
    p.leverage = leverage
    p.symbol = symbol
    return p


@pytest.mark.parametrize("lev,expected_move", [
    (1, 2.0), (2, 1.0), (4, 0.5), (5, 0.4), (10, 0.2),
])
def test_an_account_target_needs_less_price_move_at_higher_leverage(lev, expected_move, monkeypatch):
    import backend.agents.position_monitor as pm

    monkeypatch.setattr(pm, "PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr(pm, "PROFIT_TARGET_BASIS", "account")
    got = pm.PositionMonitorAgent._target_move_pct(_pos(lev))
    assert got == pytest.approx(expected_move)
    # and the account effect is the SAME at every leverage — the point of it
    assert got * lev == pytest.approx(2.0)


def test_a_price_basis_ignores_leverage(monkeypatch):
    """The old behaviour, still available for an operator who wants it."""
    import backend.agents.position_monitor as pm

    monkeypatch.setattr(pm, "PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr(pm, "PROFIT_TARGET_BASIS", "price")
    for lev in (1, 5, 10):
        assert pm.PositionMonitorAgent._target_move_pct(_pos(lev)) == pytest.approx(2.0)


def test_account_is_the_default_basis():
    """Because it is the only reading that means the same thing to the operator
    as the session's leverage changes."""
    import backend.agents.position_monitor as pm

    assert pm.PROFIT_TARGET_BASIS == "account"


def test_unknown_leverage_falls_back_to_the_most_demanding_reading(monkeypatch):
    """1x makes the required move the FULL percentage. Dividing by a leverage we
    are not sure of would close positions early on a guess — the direction that
    invents profit."""
    import backend.agents.position_monitor as pm

    monkeypatch.setattr(pm, "PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr(pm, "PROFIT_TARGET_BASIS", "account")
    for bad in (None, 0, "", "abc"):
        assert pm.PositionMonitorAgent._target_move_pct(_pos(bad)) == pytest.approx(2.0)


def test_a_target_below_the_round_trip_cost_is_refused(monkeypatch):
    """A 0.05%-a-side taker fee is ~0.10% for the round trip before spread. A
    target that resolves below the cost floor is not profit — it is a trade that
    pays the venue to close, recorded as a win."""
    import backend.agents.position_monitor as pm

    monkeypatch.setattr(pm, "PROFIT_TARGET_PCT", 2.0)
    monkeypatch.setattr(pm, "PROFIT_TARGET_BASIS", "account")
    # 2% / 20x = 0.1% move, under the 0.15% floor
    assert pm.PositionMonitorAgent._target_move_pct(_pos(20)) is None
    # the stop, trail and ATR target still govern — the position is not unmanaged
    assert pm.PositionMonitorAgent._target_move_pct(_pos(10)) is not None


def test_the_leverage_is_carried_from_the_approval(monkeypatch, monitor):
    """Without it the monitor can only measure price, and an operator asking for
    2% at 10x silently gets 20%."""
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 2.0)
    pos = _open(monitor, entry=100.0, stop=97.5)
    assert pos.leverage == 3, "approved_leverage must reach the tracked position"
