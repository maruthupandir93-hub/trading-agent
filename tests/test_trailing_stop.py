"""Trailing stop — ratchet the stop behind the peak so a run is not capped at the target.

WHY THIS EXISTS
---------------
This system's own ledger is the argument: 12 closed trades, 3 wins, and all three
landed inside a single 30-minute trending window. A fixed 5-ATR target caps exactly
the runs that are supposed to pay for the stop-outs, and the existing protection
stops ratcheting the moment the partial take-profit sets the stop to break-even —
so from +1R to the target the runner has no protection above entry at all.

The trail is measured in R, not in percent, and that is the load-bearing choice.
A percentage trail is the same distance on a quiet coin and a violent one, so it is
either inside the noise band — the failure this system already diagnosed and fixed
by widening 1.5 -> 2.5 ATR — or uselessly wide. R is the ATR-derived risk the
position was actually sized against, so the trail scales with volatility for free.

THE PROPERTIES THAT MATTER, and what each prevents:

  * it never loosens a stop            — invariant 3; a widened stop is not a stop
  * it trails the PEAK, not the price  — trailing price follows a pullback back down
  * its denominator never moves        — or the trail ratchets into the price
  * it arms only after the trade proves itself
  * the venue's resting stop moves too — the exchange's copy survives a crash
"""

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


@pytest.fixture
def monitor(bus, monkeypatch):
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    # The scale-out is a separate feature with its own tests, and letting it fire
    # here would move the stop to break-even for reasons unrelated to the trail.
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.0)
    # AND THE FIXED PROFIT TARGET, which now defaults to 2% and BYPASSES the trail
    # by design — a target closes the whole position, so there is no runner left
    # for a stop to follow. Without pinning it to 0 these tests would be measuring
    # the target's exit and calling it the trail's.
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 0.0)
    return agent


def _open(monitor, entry, stop, qty=1.0, side="buy", tab="paper"):
    """Register an approved fill and return (tar_id, tracked position)."""
    tar_id = uuid.uuid4()
    asyncio.run(monitor.handle_event(TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT", direction="long" if side == "buy" else "short",
        approved_size=qty, approved_leverage=5, cro_rationale="ok",
        stop_loss=stop, tab=tab,
        take_profit=(entry + 6000) if side == "buy" else (entry - 6000),
    )))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="x", order_id=str(uuid.uuid4()), symbol="SOL/USDT",
        side=side, tab=tab, fill_price=entry, fill_quantity=qty, slippage_bps=1.0, fee=0.1,
    )))
    return str(tar_id), monitor._open[str(tar_id)]


def _tick(monitor, price):
    asyncio.run(monitor._check_price("SOL/USDT", price))


# ---------------------------------------------------------------------------
# The scale it measures on
# ---------------------------------------------------------------------------

def test_initial_risk_is_captured_at_entry(monitor):
    """Captured once, from the stop the Risk Gateway approved."""
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    assert pos.initial_risk == pytest.approx(2.0)


def test_the_trail_denominator_does_not_move_when_the_stop_does(monitor):
    """THE BUG THIS PREVENTS: a self-tightening runaway.

    `_r_multiple` divides by the CURRENT entry-to-stop distance, which is correct
    for gating the scale-out precisely because it collapses to zero at break-even.
    If the trail reused it, the trail distance would shrink every time the trail
    tightened, and the stop would ratchet into the price — closing a healthy
    position faster the better it was doing.
    """
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    assert monitor._r_from_initial_risk(pos, 104.0) == pytest.approx(2.0)

    monitor.tighten_stop(pos.tar_id, 101.0)   # stop now above entry
    # _r_multiple's denominator moved; the trail's did not.
    assert pos.initial_risk == pytest.approx(2.0)
    assert monitor._r_from_initial_risk(pos, 104.0) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------

def test_the_trail_does_not_arm_before_the_activation_point(monitor):
    """Below +1R the original ATR stop is the right protection.

    Trailing here would convert the ordinary noise this system deliberately
    widened its stop to survive back into stop-outs.
    """
    _, pos = _open(monitor, entry=100.0, stop=98.0)      # 1R = 2.0
    _tick(monitor, 101.5)                                 # +0.75R
    assert pos.stop_loss == pytest.approx(98.0)


def test_the_trail_arms_and_ratchets_once_the_trade_proves_itself(monitor):
    """At +2R with a 1R trail, the stop lands 1R behind the peak — at break-even."""
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 104.0)                                 # +2R, peak 104
    assert pos.stop_loss == pytest.approx(102.0)          # 104 - 1R
    assert pos.trail_armed is True


def test_the_trail_follows_the_peak_not_the_current_price(monitor):
    """Trailing the current price would follow a pullback back down.

    That is not a ratchet — it gives up ground already gained, which is the whole
    failure the operator described.
    """
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 106.0)                                 # peak 106 -> stop 104
    assert pos.stop_loss == pytest.approx(104.0)

    _tick(monitor, 105.0)                                 # pullback, still above stop
    assert pos.stop_loss == pytest.approx(104.0), "the stop must not follow price down"


def test_the_trail_can_never_loosen_a_stop(monitor):
    """Invariant 3. Routed through `tighten_stop`, which is the one-way ratchet."""
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 108.0)
    tightened = pos.stop_loss
    assert tightened == pytest.approx(106.0)

    _tick(monitor, 104.5)   # a lower price proposes a looser stop; must be refused
    assert pos.stop_loss == pytest.approx(tightened)


# ---------------------------------------------------------------------------
# Shorts
# ---------------------------------------------------------------------------

def test_the_trail_works_in_the_short_direction(monitor):
    """This agent trades perpetual futures and takes shorts; a long-only trail
    would leave every short on its original stop forever."""
    _, pos = _open(monitor, entry=100.0, stop=102.0, side="sell")   # 1R = 2.0
    _tick(monitor, 96.0)                                            # +2R, trough 96
    assert pos.stop_loss == pytest.approx(98.0)                     # 96 + 1R
    assert pos.trail_armed is True

    _tick(monitor, 97.0)                                            # retrace
    assert pos.stop_loss == pytest.approx(98.0)


# ---------------------------------------------------------------------------
# Degradation and configuration
# ---------------------------------------------------------------------------

def test_a_position_without_an_initial_risk_keeps_its_fixed_stop(monitor):
    """Positions restored from before this column existed have no scale to trail on.

    They must keep exactly the protection they already had — degraded, never less
    protected, and never crashing the tick loop that enforces every other stop.
    """
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    pos.initial_risk = None
    _tick(monitor, 108.0)
    assert pos.stop_loss == pytest.approx(98.0)


def test_setting_the_trail_to_zero_disables_it(monkeypatch, monitor):
    """Fully opt-out, back to a fixed stop and target."""
    monkeypatch.setattr("backend.agents.position_monitor.TRAILING_STOP_R", 0.0)
    _, pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 108.0)
    assert pos.stop_loss == pytest.approx(98.0)


def test_the_trail_never_fires_the_stop_it_just_set(monitor):
    """A stop at or through the peak would close on the next tick.

    `tighten_stop` refuses that case and asks for an EXIT instead, so a trail
    distance of zero cannot turn into a market exit disguised as a stop-out.
    """
    monitor_stop_r = 0.0
    import backend.agents.position_monitor as pm

    original = pm.TRAILING_STOP_R
    pm.TRAILING_STOP_R = monitor_stop_r
    try:
        _, pos = _open(monitor, entry=100.0, stop=98.0)
        pm.TRAILING_STOP_R = 0.000001   # effectively at the peak
        _tick(monitor, 104.0)
        assert pos.stop_loss < 104.0, "a stop at the peak would fire immediately"
    finally:
        pm.TRAILING_STOP_R = original
