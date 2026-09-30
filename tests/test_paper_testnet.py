"""Paper trades mirrored onto Bybit's testnet: the fill is real, the money is not.

WHY THE FEATURE EXISTS
======================
A simulated fill books at the last observed price, instantly, in full. That is
honest bookkeeping and also the most flattering possible execution: no spread
crossed, no slippage, no partial fill, no minimum size, no leverage rejection, no
rate limit, no venue outage. Every one of those is a real cost that appears on
day one of real money and on none of the paper days before it.

With the mirror on, a paper entry places a REAL market order on Bybit's testnet
and the paper book is credited with the price the exchange returned. Same book,
same P&L, same panels — the fill is simply no longer a model of one.

WHAT THESE TESTS ARE ACTUALLY GUARDING
======================================
Not the happy path. The four safety properties, because each one is a way for
this to become dangerous or dishonest:

  1. it is unreachable while LIVE_TRADING is on
  2. it can never touch mainnet
  3. it never blocks a paper trade
  4. a fallback is visible in the ledger, never silent

The network is blocked for every test here (`tests/conftest.py`), so the venue is
always a stub — which is the point: these assert the ROUTING and the FALLBACK,
and `scripts/bybit_testnet_roundtrip.py` is what exercises the real sandbox.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import backend.services.paper_testnet as pt


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(pt.ENV_VAR, raising=False)
    pt.reset()
    yield
    pt.reset()


def _creds(monkeypatch):
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET", "s")


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_off_by_default():
    assert pt.enabled() is False
    assert pt.active() is False


def test_enabled_without_credentials_is_not_active(monkeypatch, caplog):
    """A switch that reports success while every order it routes is refused is the
    `simulation_mode` failure again: a control that reports success while doing
    nothing is worse than no control."""
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_SECRET", raising=False)
    monkeypatch.setenv(pt.ENV_VAR, "true")

    assert pt.enabled() is True
    assert pt.active() is False
    assert any("no Bybit testnet credentials" in r.message for r in caplog.records)


def test_active_when_enabled_and_configured(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setenv(pt.ENV_VAR, "true")
    assert pt.active() is True


def test_the_flag_is_read_at_call_time(monkeypatch):
    """The `simulation_mode` rule. A module-level getenv would mean the operator
    flips the switch, is told it worked, and the running agent keeps the old
    behaviour until a restart."""
    _creds(monkeypatch)
    monkeypatch.setenv(pt.ENV_VAR, "true")
    assert pt.active() is True
    monkeypatch.setenv(pt.ENV_VAR, "false")
    assert pt.active() is False


# ---------------------------------------------------------------------------
# Property 2: it can never touch mainnet
# ---------------------------------------------------------------------------

def test_the_venue_is_hardcoded_to_bybit_testnet():
    """NOT read from EXCHANGE_ID. A mirror that a config typo could point at
    mainnet is not a testing aid — it is a way to lose money by accident. Binance
    is not an option either: ccxt dropped its futures testnet, which is exactly
    why this project's Binance order path is still unverified."""
    src = inspect.getsource(pt)
    assert 'VENUE_ID = "bybit"' in src
    # Asserted as a CALL, not a mention: the module docstring explains why
    # EXCHANGE_ID is deliberately not read, so a bare substring test matches the
    # explanation and passes for the wrong reason.
    assert 'getenv("EXCHANGE_ID")' not in src
    assert "EXCHANGE_ID)" not in src
    assert "testnet=True" in src
    assert "testnet=False" not in src


def test_it_reads_the_testnet_key_variable_only():
    """`_credentials` never reads a mainnet variable in sandbox mode, and the two
    are separate so that verifying on testnet never requires pasting a testnet
    key over a live one — putting the live one back is where a real key ends up
    in play by accident."""
    from backend.services.venue import key_variable

    assert key_variable("bybit", testnet=True) == "BYBIT_TESTNET_API_KEY"
    assert key_variable("bybit", testnet=False) == "BYBIT_API_KEY"


# ---------------------------------------------------------------------------
# Property 1: unreachable while live trading is on
# ---------------------------------------------------------------------------

def test_the_execution_agent_gates_the_mirror_on_live_trading_being_off():
    """`simulation_mode` is false whenever LIVE_TRADING is on, so live trading and
    the mirror cannot both be routing an order.

    THIS USED TO BE A TEST OF THE BRANCH'S SHAPE — that `paper_testnet` appeared
    somewhere after `if self.simulation_mode:` — and that was the weaker test in
    exactly the way that mattered. The branch is now chosen by the position's TAB
    (`routes_to_venue`), because deciding the venue from a process-wide flag sent
    a paper position's close to MAINNET the moment the operator enabled live
    trading. A guarantee that holds only as long as nobody restructures the
    branch is not a guarantee, so the condition is now spelled out in the code
    and asserted as a condition here.
    """
    from backend.agents.execution_agent import ExecutionAgent

    found = 0
    for method in ("_execute_tar", "close_position"):
        fn = getattr(ExecutionAgent, method, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        if "paper_testnet.active()" not in src:
            continue
        found += 1
        for line in src.splitlines():
            if "paper_testnet.active()" in line and line.strip().startswith("if "):
                assert "self.simulation_mode" in line, (
                    f"{method} reaches the mirror without checking that live trading "
                    f"is off: {line.strip()}"
                )
                assert 'paper"' in line or "paper'" in line, (
                    f"{method} reaches the mirror without checking the tab: {line.strip()}"
                )
                break
        else:  # pragma: no cover - the guard is on one line by construction
            raise AssertionError(f"{method} calls active() outside an `if`")
    assert found == 2, "both the open and the close paths must gate the mirror"


def test_status_reports_that_live_trading_suppresses_it(monkeypatch):
    """An operator seeing the toggle ON while nothing mirrors deserves to know why
    without reading the source."""
    _creds(monkeypatch)
    monkeypatch.setenv(pt.ENV_VAR, "true")
    from backend.core.config import settings

    # LIVE_TRADING is a read-only property over `_live_trading`; the admin
    # toggle writes the backing field through `set_live_trading`. Patching the
    # property directly raises "has no setter".
    monkeypatch.setattr(settings, "_live_trading", True, raising=False)
    st = pt.status()
    assert st["enabled"] is True
    assert st["liveTradingOn"] is True
    assert st["reachable"] is False


# ---------------------------------------------------------------------------
# Property 3 and 4: never blocks, never silent
# ---------------------------------------------------------------------------

class _Venue:
    """A stand-in for the testnet client. The network is blocked in tests."""

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises
        self.orders = []

    async def ensure_leverage(self, symbol, leverage):
        return True

    async def market_order(self, **kw):
        self.orders.append(kw)
        if self._raises:
            raise RuntimeError("testnet unreachable")
        return self._result


class _Result:
    def __init__(self, ok=True, price=1.234, qty=10.0, order_id="tn-1", error=None):
        self.ok = ok
        self.average_price = price
        self.filled_qty = qty
        self.order_id = order_id
        self.error = error


def _armed(monkeypatch, venue):
    _creds(monkeypatch)
    monkeypatch.setenv(pt.ENV_VAR, "true")
    monkeypatch.setattr(pt, "get_venue", lambda: venue)


def test_a_filled_order_returns_the_exchanges_price(monkeypatch):
    v = _Venue(_Result(price=1.2345, qty=9.5))
    _armed(monkeypatch, v)
    out = asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=10.0, leverage=3))
    assert out == {"order_id": "tn-1", "price": 1.2345, "filled_qty": 9.5}
    # The PARTIAL fill is carried through, not rounded back up to what was asked.
    assert out["filled_qty"] != 10.0


@pytest.mark.parametrize("venue,label", [
    (_Venue(_Result(ok=False, error="qty below minimum")), "a refused order"),
    (_Venue(raises=True), "an unreachable venue"),
    (_Venue(_Result(price=0.0)), "a fill with no price"),
    (_Venue(_Result(price=None)), "a fill with a null price"),
])
def test_every_failure_falls_back_rather_than_blocking(monkeypatch, venue, label):
    """Property 3. A test venue being down must not stop paper trading — the
    alternative is a paper account that stops working because a sandbox is
    offline, which is the worse failure.

    The no-price cases matter separately: booking the simulated price against a
    real order id would attach a made-up fill to a real trade, which is worse
    than either alone (invariant 6)."""
    _armed(monkeypatch, venue)
    assert asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=10.0)) is None, label


def test_a_failure_is_logged_with_its_reason(monkeypatch, caplog):
    """Property 4. Silent degradation is how a paper book quietly stops being a
    rehearsal for the real one."""
    _armed(monkeypatch, _Venue(_Result(ok=False, error="qty below minimum")))
    asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=0.0001))
    assert any("qty below minimum" in r.message for r in caplog.records)


def test_nothing_is_placed_when_inactive(monkeypatch):
    v = _Venue(_Result())
    monkeypatch.setattr(pt, "get_venue", lambda: v)
    monkeypatch.setenv(pt.ENV_VAR, "false")
    assert asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=1.0)) is None
    assert v.orders == []


def test_a_close_is_reduce_only(monkeypatch):
    """Without it a close is just an opposite-side order, and any surplus over the
    live size OPENS a position the other way — on the testnet account, which then
    drifts from the paper book."""
    v = _Venue(_Result())
    _armed(monkeypatch, v)
    asyncio.run(pt.place(symbol="XRP/USDT", side="sell", qty=10.0, reduce_only=True))
    assert v.orders[0]["reduce_only"] is True


def test_a_leverage_refusal_does_not_abort_the_mirror(monkeypatch):
    """DELIBERATELY more permissive than the live path, which ABORTS on a leverage
    refusal because filling at a leverage we know is wrong is trading on a false
    number. On testnet the position is play money and a less-faithful mirror
    still beats no mirror."""
    class _NoLev(_Venue):
        async def ensure_leverage(self, symbol, leverage):
            raise RuntimeError("leverage not settable")

    v = _NoLev(_Result())
    _armed(monkeypatch, v)
    assert asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=1.0, leverage=10)) is not None


# ---------------------------------------------------------------------------
# The monitor routes a mirrored position's orders to the RIGHT venue
# ---------------------------------------------------------------------------

def test_a_mirrored_paper_position_is_venue_backed(monkeypatch):
    """The old guard was `pos.tab != "real"`, and it stopped being the right
    question: a mirrored paper position genuinely exists at the testnet, so it
    needs a stop there. Leaving the old guard would open a real testnet position
    with NO stop — the exact gap the resting stop exists to close, and unfaithful
    in the one direction that matters."""
    from backend.agents.position_monitor import _venue_backed

    class P:
        tab = "paper"

    _creds(monkeypatch)
    monkeypatch.setenv(pt.ENV_VAR, "false")
    assert _venue_backed(P()) is False
    monkeypatch.setenv(pt.ENV_VAR, "true")
    assert _venue_backed(P()) is True


def test_a_real_position_is_always_venue_backed(monkeypatch):
    from backend.agents.position_monitor import _venue_backed

    class P:
        tab = "real"

    monkeypatch.setenv(pt.ENV_VAR, "false")
    assert _venue_backed(P()) is True


def test_a_paper_position_routes_to_the_testnet_client(monkeypatch):
    """Sending a mirrored position's stop to the MAINNET client would place live
    orders against a position that does not exist there — the single worst thing
    this file could do."""
    from backend.agents.position_monitor import _venue_for

    sentinel = object()
    monkeypatch.setattr(pt, "get_venue", lambda: sentinel)

    class P:
        tab = "paper"

    assert _venue_for(P()) is sentinel


def test_the_tick_loop_survives_a_broken_toggle(monkeypatch):
    """`_venue_backed` runs on the stop-enforcement path. An exception there would
    stop every stop in the book from being checked."""
    from backend.agents.position_monitor import _venue_backed

    def _boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr(pt, "active", _boom)

    class P:
        tab = "paper"

    assert _venue_backed(P()) is False
