"""TP rests from entry; the SL appears at the venue only once the trade turns.

THE OPERATOR'S ARRANGEMENT, in their own words: "when the trade is executed via
API it also sets only the TP for the trade, and in my agent backend continuously
monitor and if any reverse feels it could make the SL by my agent".

WHAT THE RESTING STOP IS FOR, so the trade-off is explicit. It does nothing while
this process is alive — the in-process monitor fires first on every tick. It
exists for the window when the process is NOT alive: a deploy, a restart, an OOM
kill, a reboot. In that window a position with no venue stop has no protection at
all, and at 10x leverage liquidation is only ~9.5% away.

"on_adverse" is the honest middle. A winning position never needs a venue stop
and never spends the API call; a losing one is protected at 0.5R, long before the
stop itself could be reached and far longer before liquidation.

INVARIANT 3 IS UNTOUCHED BY ANY MODE. Every position still requires a COMPUTED
stop — `risk_gateway` refuses a trade without one and the monitor enforces it on
every tick. This only decides whether a copy also sits at the exchange.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core.message_bus import MessageBus
from backend.models.events import OrderFilledEvent, TarApprovedEvent


class R:
    ok = True
    order_id = "venue-stop-1"
    error = None
    filled_qty = None
    average_price = None


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
def venue(monkeypatch):
    calls = []

    class FakeVenue:
        id = "binance"

        def has_credentials(self):
            return True

        async def ensure_leverage(self, s, lev):
            return True

        async def ensure_margin_mode(self, s, m=None):
            return True

        async def funding_rate(self, s):
            return 0.0001

        async def place_stop_loss(self, **k):
            calls.append(("stop", k["symbol"], round(k["stop_price"], 2)))
            return R()

        async def place_take_profit(self, **k):
            calls.append(("tp", k["symbol"], round(k["take_profit_price"], 2)))
            return R()

        async def cancel_order(self, oid, sym):
            calls.append(("cancel", sym, oid))
            return True

    import backend.services.venue as vm

    monkeypatch.setattr(vm, "get_venue", lambda: FakeVenue())
    return calls


@pytest.fixture
def monitor(bus, monkeypatch):
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    monkeypatch.setattr("backend.agents.position_monitor.PARTIAL_TP_FRACTION", 0.0)
    monkeypatch.setattr("backend.agents.position_monitor.TRAILING_STOP_R", 0.0)
    monkeypatch.setattr("backend.agents.position_monitor.PROFIT_TARGET_PCT", 0.0)
    return agent


def _open(monitor, entry=100.0, stop=98.0, side="buy", tab="real"):
    tar_id = uuid.uuid4()
    asyncio.run(monitor.handle_event(TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT", direction="long" if side == "buy" else "short",
        approved_size=10.0, approved_leverage=3, cro_rationale="v",
        stop_loss=stop, take_profit=(entry + 5000) if side == "buy" else (entry - 5000),
        tab=tab,
    )))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="binance", order_id="o", symbol="SOL/USDT",
        side=side, tab=tab, fill_price=entry, fill_quantity=10.0,
        slippage_bps=0.0, fee=0.5,
    )))
    return monitor._open[str(tar_id)]


def _tick(monitor, price):
    asyncio.run(monitor._check_price("SOL/USDT", price))


# ---------------------------------------------------------------------------
# The arrangement itself
# ---------------------------------------------------------------------------

def test_on_adverse_places_ONLY_the_take_profit_at_entry(monkeypatch, monitor, venue):
    """The operator's requirement, in one assertion."""
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "on_adverse")
    pos = _open(monitor)
    kinds = [c[0] for c in venue]
    assert "tp" in kinds, "the take-profit must rest from entry"
    assert "stop" not in kinds, "no stop should rest until the trade turns"
    assert pos.stop_order_id is None
    assert pos.tp_order_id is not None


def test_the_stop_appears_once_the_trade_moves_against_us(monkeypatch, monitor, venue):
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "on_adverse")
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_ARM_R", 0.5)
    pos = _open(monitor, entry=100.0, stop=98.0)      # 1R = 2.0
    venue.clear()

    _tick(monitor, 101.0)                              # winning — no stop needed
    assert [c for c in venue if c[0] == "stop"] == []
    assert pos.stop_order_id is None

    _tick(monitor, 99.0)                               # -0.5R — arm it
    assert [c for c in venue if c[0] == "stop"], "the stop must be placed at the venue"
    assert pos.stop_order_id == "venue-stop-1"


def test_a_winning_position_never_spends_the_api_call(monkeypatch, monitor, venue):
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "on_adverse")
    pos = _open(monitor, entry=100.0, stop=98.0)
    venue.clear()
    for px in (100.5, 101.0, 102.0, 103.0):
        _tick(monitor, px)
    assert [c for c in venue if c[0] == "stop"] == []
    assert pos.stop_order_id is None


def test_arming_is_idempotent(monkeypatch, monitor, venue):
    """Without this a losing position places a new stop on EVERY tick — dozens of
    live reduce-only orders, and after the first fires the rest are orders to OPEN
    the opposite position."""
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "on_adverse")
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_ARM_R", 0.5)
    _open(monitor, entry=100.0, stop=98.0)
    venue.clear()
    for px in (99.0, 98.9, 98.8, 98.7):
        _tick(monitor, px)
    assert len([c for c in venue if c[0] == "stop"]) == 1


def test_a_short_arms_when_price_RISES(monkeypatch, monitor, venue):
    """Direction comes from `mae_r`, so it is inherited rather than re-derived —
    a long arms on a fall, a short on a rise."""
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "on_adverse")
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_ARM_R", 0.5)
    pos = _open(monitor, entry=100.0, stop=102.0, side="sell")    # 1R = 2.0
    venue.clear()
    _tick(monitor, 99.0)                                          # winning for a short
    assert pos.stop_order_id is None
    _tick(monitor, 101.0)                                         # -0.5R for a short
    assert pos.stop_order_id == "venue-stop-1"


# ---------------------------------------------------------------------------
# The other modes, and what never changes
# ---------------------------------------------------------------------------

def test_always_is_the_default_and_places_both_at_entry(monkeypatch, monitor, venue):
    """The safe default is unchanged: an operator who sets nothing keeps both legs."""
    import backend.agents.position_monitor as pm

    assert pm.RESTING_STOP_MODE == "always"
    monkeypatch.setattr(pm, "RESTING_STOP_MODE", "always")
    pos = _open(monitor)
    kinds = [c[0] for c in venue]
    assert "stop" in kinds and "tp" in kinds
    assert pos.stop_order_id is not None


def test_paper_never_places_venue_orders_in_any_mode(monkeypatch, monitor, venue):
    """A paper fill has no venue order behind it to protect."""
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "on_adverse")
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_ARM_R", 0.5)
    pos = _open(monitor, tab="paper")
    venue.clear()
    _tick(monitor, 99.0)
    assert venue == []
    assert pos.stop_order_id is None


def test_the_in_process_stop_still_fires_in_every_mode(monkeypatch, monitor, venue):
    """INVARIANT 3. The venue copy is a backup for when this process is down; the
    computed stop is enforced on every tick regardless of mode."""
    monkeypatch.setattr("backend.agents.position_monitor.RESTING_STOP_MODE", "never")

    closes = []

    class Ex:
        async def close_position(self, **k):
            closes.append(k)
            return 97.9

    monitor.attach_execution(Ex())
    _open(monitor, entry=100.0, stop=98.0)
    _tick(monitor, 97.9)
    assert closes and closes[0]["reason"] == "stop-loss"
