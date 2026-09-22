"""MFE and MAE — the path a position took, which a trade log does not record.

WHY THIS EXISTS
---------------
Asked to replay a trailing stop against five days of real fills, the answer was
that it CANNOT BE DONE. A trailed stop sits at `peak - TRAILING_STOP_R`, so its
outcome depends entirely on where the peak was — and `trades` records entry and
exit, never the path between. 1,173 real positions, and the question was
unanswerable from all of them.

    of 657 positions that reached +1R, 157 went on to the target (23.9%)
    the rest died at break-even — and nothing says how far they first ran

So the path is measured now, per position, in R:

  * mfe_r  MAXIMUM FAVOURABLE EXCURSION. Decides whether a trail would have
           captured more than a fixed target or a break-even stop.
  * mae_r  MAXIMUM ADVERSE EXCURSION. Separates a stop that was genuinely hit
           from one that was merely too tight — a trade that dipped to -0.9R and
           then reached its target says the stop was nearly right; many of them
           say it is converting winners into losers.

IN R, NOT PERCENT, for the same reason the trail itself is in R: comparable
across instruments and volatility regimes. The denominator is `initial_risk`,
fixed at entry — using the CURRENT stop would make the scale move every time the
stop did, which is exactly what the partial take-profit does.
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


@pytest.fixture
def monitor(bus, monkeypatch):
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    # The scale-out and the trail both move the stop; neither is under test here.
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.0)
    monkeypatch.setattr("backend.agents.position_monitor.TRAILING_STOP_R", 0.0)
    return agent


def _open(monitor, entry, stop, side="buy", qty=1.0):
    tar_id = uuid.uuid4()
    asyncio.run(monitor.handle_event(TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT", direction="long" if side == "buy" else "short",
        approved_size=qty, approved_leverage=5, cro_rationale="ok",
        stop_loss=stop, tab="paper",
        take_profit=(entry + 6000) if side == "buy" else (entry - 6000),
    )))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="x", order_id=str(uuid.uuid4()), symbol="SOL/USDT",
        side=side, tab="paper", fill_price=entry, fill_quantity=qty,
        slippage_bps=1.0, fee=0.1,
    )))
    return monitor._open[str(tar_id)]


def _tick(monitor, price):
    asyncio.run(monitor._check_price("SOL/USDT", price))


# ---------------------------------------------------------------------------
# Tracking both extremes
# ---------------------------------------------------------------------------

def test_both_extremes_start_at_the_entry(monitor):
    """The entry is the one price guaranteed to have been touched."""
    pos = _open(monitor, entry=100.0, stop=98.0)
    assert pos.peak_price == pytest.approx(100.0)
    assert pos.worst_price == pytest.approx(100.0)


def test_a_long_records_the_high_as_peak_and_the_low_as_worst(monitor):
    pos = _open(monitor, entry=100.0, stop=98.0)   # 1R = 2.0
    _tick(monitor, 103.0)
    _tick(monitor, 99.0)
    _tick(monitor, 101.0)
    assert pos.peak_price == pytest.approx(103.0)
    assert pos.worst_price == pytest.approx(99.0)


def test_a_SHORT_mirrors_both(monitor):
    """A short's adverse direction is UP. Getting this backwards would record
    every short's best price as its worst."""
    pos = _open(monitor, entry=100.0, stop=102.0, side="sell")
    _tick(monitor, 97.0)     # favourable for a short
    _tick(monitor, 101.0)    # adverse for a short
    assert pos.peak_price == pytest.approx(97.0)
    assert pos.worst_price == pytest.approx(101.0)


# ---------------------------------------------------------------------------
# The R conversion
# ---------------------------------------------------------------------------

def test_excursions_are_expressed_in_R(monitor):
    pos = _open(monitor, entry=100.0, stop=98.0)   # 1R = 2.0
    _tick(monitor, 104.0)                           # +2R
    _tick(monitor, 99.0)                            # -0.5R
    mfe, mae = monitor._excursions(pos)
    assert mfe == pytest.approx(2.0)
    assert mae == pytest.approx(0.5)


def test_a_short_excursion_is_measured_in_its_own_direction(monitor):
    pos = _open(monitor, entry=100.0, stop=102.0, side="sell")   # 1R = 2.0
    _tick(monitor, 96.0)     # +2R for a short
    _tick(monitor, 101.0)    # -0.5R for a short
    mfe, mae = monitor._excursions(pos)
    assert mfe == pytest.approx(2.0)
    assert mae == pytest.approx(0.5)


def test_mae_is_a_positive_magnitude(monitor):
    """Signing it would invite the sign being applied twice by a reader who
    assumed it was already negative."""
    pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 98.5)
    _, mae = monitor._excursions(pos)
    assert mae > 0


def test_an_excursion_never_goes_negative_in_its_own_direction(monitor):
    """A peak below entry means price never went favourable AT ALL — that is
    MFE 0, not a negative favourable excursion."""
    pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 99.0)     # never traded above entry
    mfe, mae = monitor._excursions(pos)
    assert mfe == pytest.approx(0.0)
    assert mae == pytest.approx(0.5)


def test_unmeasurable_excursions_are_None_not_zero(monitor):
    """A zero MFE is a real and rare fact — a trade that never went a tick into
    profit. It must not be confused with 'not measured' (invariant 6)."""
    pos = _open(monitor, entry=100.0, stop=98.0)
    pos.initial_risk = None
    assert monitor._excursions(pos) == (None, None)


# ---------------------------------------------------------------------------
# The question this was built to answer
# ---------------------------------------------------------------------------

def test_the_excursion_reveals_what_a_trail_would_have_captured(monitor):
    """THE WHOLE POINT, as one assertion.

    A position that peaks at +3R and exits at break-even is indistinguishable, in
    a trade log, from one that never moved — both record a ~0 P&L. MFE tells them
    apart, and a 1R trail on the first would have exited near +2R.
    """
    pos = _open(monitor, entry=100.0, stop=98.0)   # 1R = 2.0
    _tick(monitor, 106.0)                           # peaked at +3R
    _tick(monitor, 100.0)                           # gave it all back

    mfe, _ = monitor._excursions(pos)
    assert mfe == pytest.approx(3.0)
    # A trail at 1R behind the peak would have stopped out here:
    trailed_exit_r = mfe - 1.0
    assert trailed_exit_r == pytest.approx(2.0)


def test_a_near_miss_stop_is_distinguishable_from_a_decisive_one(monitor):
    """MAE's job. A trade stopped at -1R that never dipped further was decisively
    wrong; one that dipped to -0.98R on the way to a win says the stop is nearly
    too tight. The P&L alone cannot tell these apart."""
    near = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 98.05)                           # -0.975R, survived
    _tick(monitor, 104.0)
    mfe_near, mae_near = monitor._excursions(near)
    assert mae_near == pytest.approx(0.975, abs=0.01)
    assert mfe_near == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------

def test_the_adverse_extreme_survives_a_watch_row_round_trip(monitor):
    """Persisted for the same reason peak_price is: a restart that reset it to the
    entry would quietly understate how far the position went against us."""
    from backend.services.position_store import _FIELDS

    pos = _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 99.0)

    assert "worst_price" in _FIELDS, "worst_price must be persisted"
    rows = monitor._watch_rows()
    row = next(r for r in rows if r["tar_id"] == pos.tar_id)
    assert row["worst_price"] == pytest.approx(99.0)


def test_every_persisted_field_is_emitted_by_the_watch_rows():
    """`save_watch_list` binds positionally from `_FIELDS`. A field named there and
    never produced is written NULL on every row — the `stop_order_id` incident."""
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services.position_store import _FIELDS

    agent = PositionMonitorAgent()
    agent._pending["t"] = {"symbol": "SOL/USDT", "tab": "paper", "stop_loss": 1.0}
    keys = set()
    for row in agent._watch_rows():
        keys |= set(row.keys())
    missing = set(_FIELDS) - keys
    assert not missing, f"_FIELDS names {sorted(missing)} which _watch_rows never emits"
