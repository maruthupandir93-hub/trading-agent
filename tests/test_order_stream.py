"""The order stream OBSERVES. It must never close, open or resize a position.

WHY IT EXISTS AT ALL
--------------------
This system learned about order state from one place: the return value of its own
`create_order` call. Nothing listened to the exchange. So a RESTING STOP FIRING —
the whole point of placing one, since it protects the position when this process
is not watching — was invisible for up to 60 seconds, until `reconciliation`'s
once-a-minute poll happened to notice. During that window the local book shows
the position open and the monitor keeps ticking against a stop that has already
executed.

WHY IT MUST NOT ACT
-------------------
The temptation is obvious: the stream KNOWS the stop filled, so why not close the
position locally? Because every automatic "fix" is a trading decision made on one
message from one socket, with no Risk Gateway and no audit trail behind it. A
reconnect mid-gap, a message for an order the operator placed by hand in the
Binance app, or a partial fill of a stop would each have this module mutate the
book. `reconciliation` documents the same rule and `tests/test_reconciliation.py`
asserts it the same way — against the module's own source, because a behavioural
test can only prove that it did not act THIS time.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from backend.services.order_stream import (
    OrderStream,
    get_order_stream,
    reset_order_stream,
    stream_enabled,
)

SOURCE = pathlib.Path("backend/services/order_stream.py")


# ---------------------------------------------------------------------------
# The safety property, asserted structurally
# ---------------------------------------------------------------------------

# Everything that could change a book or reach an exchange. If the stream ever
# needs one of these, that is a design change and this list is where the argument
# has to be made.
FORBIDDEN_CALLS = (
    "close_position",
    "apply_paper_fill",
    "update_portfolio",
    "buy_paper",
    "sell_paper",
    "market_order",
    "create_order",
    "cancel_order",
    "clear_all",
    "track_manual_position",
    "tighten_stop",
    "place_stop_loss",
    "place_take_profit",
)


@pytest.mark.parametrize("name", FORBIDDEN_CALLS)
def test_the_stream_never_calls_anything_that_changes_a_position(name):
    src = SOURCE.read_text(encoding="utf-8")
    # Strip comments and docstrings: the module's prose NAMES these functions
    # while explaining why it does not call them, and matching on that would
    # make this test fail for the documentation rather than for the code.
    tree = ast.parse(src)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                called.add(fn.attr)
            elif isinstance(fn, ast.Name):
                called.add(fn.id)
    assert name not in called, (
        f"order_stream calls {name}() — it must REPORT only. Acting on a single "
        f"socket message would make it a second closing authority with no gate "
        f"behind it; see the module docstring."
    )


def test_the_stream_holds_no_reference_to_any_book():
    """Report-only is structural here, not a rule someone has to remember.

    It cannot mutate a book because it is never given one.
    """
    stream = OrderStream()
    for attr in ("_monitor", "_execution", "_portfolio", "monitor", "execution"):
        assert not hasattr(stream, attr), f"OrderStream holds {attr}"


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_the_stream_is_off_while_live_trading_is_off(monkeypatch):
    """A paper fill has no venue order behind it, so there is nothing to watch.

    Connecting anyway would open an authenticated socket to observe a book the
    exchange has never heard of.
    """
    from backend.core.config import settings

    monkeypatch.setattr(settings, "_live_trading", False, raising=False)
    assert stream_enabled() is False


def test_the_stream_can_be_disabled_while_live(monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "_live_trading", True, raising=False)
    monkeypatch.setenv("ORDER_STREAM_ENABLED", "false")
    assert stream_enabled() is False

    monkeypatch.setenv("ORDER_STREAM_ENABLED", "true")
    assert stream_enabled() is True


def test_start_is_a_noop_when_disabled(monkeypatch):
    """Must not create a task that immediately dies — that logs a spurious
    'Task exception was never retrieved' at shutdown."""
    from backend.core.config import settings

    monkeypatch.setattr(settings, "_live_trading", False, raising=False)
    stream = OrderStream()
    stream.start()
    assert stream._task is None


# ---------------------------------------------------------------------------
# What it records
# ---------------------------------------------------------------------------

def _order(**kw):
    base = {
        "id": "1", "clientOrderId": "c1", "symbol": "SOL/USDT", "side": "sell",
        "status": "closed", "filled": 1.0, "average": 100.0,
        "fee": {"cost": 0.05, "currency": "USDT"}, "reduceOnly": True,
    }
    base.update(kw)
    return base


def test_a_reduce_only_fill_is_recorded_as_a_venue_close():
    """The one update the operator actually needs: a resting stop or TP fired."""
    stream = OrderStream()
    stream._record(_order())
    snap = stream.snapshot()
    assert snap["eventsSeen"] == 1
    assert snap["recent"][0]["reduceOnly"] is True
    assert snap["recent"][0]["symbol"] == "SOL/USDT"


def test_the_venues_own_commission_is_captured():
    """The real fee arrives on the trade update, not the create-order response.

    `services/fees` falls back to a modelled taker rate without it, which on a
    market order is the common case.
    """
    stream = OrderStream()
    stream._record(_order())
    assert stream.snapshot()["recent"][0]["feeCost"] == pytest.approx(0.05)


def test_open_orders_are_not_recorded_as_notable():
    """An order that is merely resting produces traffic and tells nobody anything
    they did not already know from placing it."""
    stream = OrderStream()
    stream._record(_order(status="open"))
    assert stream.snapshot()["recent"] == []


def test_an_unparseable_update_is_ignored_not_raised():
    """This runs inside a reconnecting loop. A malformed message must not kill it
    — that would take down the observation this module exists to provide."""
    stream = OrderStream()
    stream._record({"id": object()})   # not stringifiable into a clean event
    stream._record(_order())           # the loop keeps working
    assert stream.snapshot()["eventsSeen"] >= 1


def test_the_recent_ring_is_bounded():
    """A diagnostic buffer in a process expected to run for weeks. `trades` is the
    record of truth; this must not grow without limit."""
    stream = OrderStream()
    for i in range(500):
        stream._record(_order(id=str(i)))
    assert len(stream._recent) <= 200


def test_the_singleton_is_shared():
    """Two instances would mean two authenticated sockets on one API key, each
    consuming the venue's connection allowance and logging every fill twice."""
    reset_order_stream()
    try:
        assert get_order_stream() is get_order_stream()
    finally:
        reset_order_stream()
