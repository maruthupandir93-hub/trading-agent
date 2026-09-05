"""The three faults the Bybit testnet round trip found, pinned offline.

WHY THESE NEEDED A LIVE VENUE TO FIND
=====================================
`tests/test_venue.py` pins the parameter matrix and every refusal, and all of it
passed while the real-money path was broken in three separate places. The offline
suite could not see any of them because each one depends on what the VENUE holds
or what ccxt does with a parameter — neither of which a hand-built fixture
reproduces unless you already know the answer.

Building `scripts/bybit_testnet_roundtrip.py` surfaced all three. Now that the
answers are known they are cheap to pin here, and these tests are the guard: the
round trip needs testnet keys and a funded wallet, so it cannot run in CI.

  1. SYMBOL RESOLUTION. Every caller says "SOL/USDT", and that exact key is a
     SPOT market in ccxt's dict. `check_size` looked it up with `dict.get`, so a
     perpetual order was filtered against spot limits — measured live on Bybit
     testnet, spot minQty 0.001 against the perpetual's 0.1. Worse, the two
     venues DISAGREE: ccxt's binance honours `defaultType: future` inside
     `market()` and resolves the bare key to the swap, while bybit's returns
     spot. The same call placed a perpetual on one venue and a SPOT order on the
     other, while positionIdx, reduceOnly, the leverage call and the stop all
     described a perpetual.

  2. THE BYBIT STOP NEVER REACHED THE VENUE. `place_stop_loss` sent
     `triggerPrice`, which makes ccxt treat the order as a generic trigger order
     — and a generic trigger order requires an explicit `triggerDirection`. ccxt
     raised `ArgumentsRequired` before any request left the process, our handler
     caught it, and it was logged as "stop-loss order REJECTED" — which reads as
     the venue refusing a stop rather than as this process never having asked
     for one. The unified `stopLossPrice` makes ccxt derive the direction from
     the order side on Bybit and select STOP_MARKET on Binance.

  3. RECONCILIATION COMPARED TWO SPELLINGS OF THE SAME POSITION. The local book
     holds "SOL/USDT"; ccxt reports the same perpetual as "SOL/USDT:USDT".
     `_compare` keyed on the raw strings, so every real position was reported
     CRITICAL "missing at venue" AND the venue's own position as unknown — false
     alarms on exactly the alert that means a liquidation or a manual close.
"""

from __future__ import annotations

import math

import pytest

from backend.services.reconciliation import _compare
from backend.services.venue import Venue, _credentials, key_variable


def venue(venue_id: str) -> Venue:
    return Venue(venue_id, testnet=True)


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for var in (
        "BINANCE_API_KEY", "BINANCE_SECRET", "BYBIT_API_KEY", "BYBIT_SECRET",
        "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET",
        "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. Symbol resolution
# ---------------------------------------------------------------------------
#
# The market shapes below are the ones ccxt really returns, trimmed. The
# minimums are the LIVE values read off Bybit testnet during the round trip —
# they are the whole reason this matters.

SPOT = {
    "symbol": "SOL/USDT", "type": "spot", "spot": True, "swap": False,
    "contract": False, "linear": None,
    "limits": {"amount": {"min": 0.001}, "cost": {"min": 1.0}},
}
PERP = {
    "symbol": "SOL/USDT:USDT", "type": "swap", "spot": False, "swap": True,
    "contract": True, "linear": True,
    "limits": {"amount": {"min": 0.1}, "cost": {"min": None}},
}


@pytest.fixture
def both_markets(monkeypatch):
    """A venue holding BOTH markets under the keys ccxt really uses."""
    v = venue("bybit")
    v._markets = {"SOL/USDT": dict(SPOT), "SOL/USDT:USDT": dict(PERP)}
    # TRUNCATES, because ccxt does. An earlier version of this fixture used
    # `f"{amount:.1f}"`, which ROUNDS — so 0.05 became 0.1, cleared the
    # perpetual's minimum, and the test asserting a refusal passed for the wrong
    # reason. A fixture that is kinder than the venue proves nothing.
    monkeypatch.setattr(
        v.public,
        "amount_to_precision",
        lambda symbol, amount: f"{math.floor(float(amount) / 0.1) * 0.1:.1f}",
    )
    return v


@pytest.mark.asyncio
async def test_the_bare_symbol_resolves_to_the_PERPETUAL_not_the_spot_market(both_markets):
    """The one that placed spot orders on Bybit with the agent's own symbol."""
    assert await both_markets.resolve_symbol("SOL/USDT") == "SOL/USDT:USDT"


@pytest.mark.asyncio
async def test_an_already_resolved_symbol_is_returned_unchanged(both_markets):
    assert await both_markets.resolve_symbol("SOL/USDT:USDT") == "SOL/USDT:USDT"


@pytest.mark.asyncio
async def test_a_spot_only_market_is_REFUSED_rather_than_traded(monkeypatch):
    """Never fall back to spot.

    A spot fill is a different instrument: no leverage, no reduce-only close, and
    no position to rest a stop against. Every risk figure this system computed
    would describe a leveraged perpetual that does not exist.
    """
    v = venue("bybit")
    v._markets = {"FOO/USDT": dict(SPOT, symbol="FOO/USDT")}
    assert await v.resolve_symbol("FOO/USDT") is None


@pytest.mark.asyncio
async def test_check_size_applies_the_PERPETUAL_filters(both_markets):
    """0.05 SOL clears spot's 0.001 minimum and cannot be expressed on the perp.

    Under the old lookup this returned ok=True and Bybit rejected the order,
    with our log saying only "order rejected". Verified live during the round
    trip: ccxt itself refuses it —
    "bybit amount of SOL/USDT:USDT must be greater than minimum amount
    precision of 0.1".
    """
    check = await both_markets.check_size("SOL/USDT", 0.05, 100.0)
    assert check.ok is False
    # THE ASSERTION THAT NAMES THE BUG: the filters reported are the
    # perpetual's. Spot's minimum is 0.001 and would have waved this through.
    assert check.min_qty == 0.1
    assert SPOT["limits"]["amount"]["min"] == 0.001


@pytest.mark.asyncio
async def test_a_size_that_clears_the_perpetual_minimum_passes(both_markets):
    check = await both_markets.check_size("SOL/USDT", 0.15, 100.0)
    assert check.ok is True


@pytest.mark.asyncio
async def test_the_order_is_placed_against_the_RESOLVED_symbol(both_markets, monkeypatch):
    """Resolution has to reach `create_order`, not just the size check.

    Resolving for the filters and then ordering the bare symbol would apply the
    right minimums to an order sent to the wrong market — the worst of both.
    """
    monkeypatch.setattr(both_markets, "_api_key", "k")
    monkeypatch.setattr(both_markets, "_secret", "s")
    monkeypatch.setattr(both_markets, "_hedge_mode", False)

    seen: dict = {}

    async def _create(symbol, type_, side, amount, price, params):
        seen.update(symbol=symbol, type=type_, side=side, amount=amount, params=params)
        return {"id": "1", "filled": amount, "average": 100.0}

    monkeypatch.setattr(both_markets.private, "create_order", _create)
    result = await both_markets.market_order(
        symbol="SOL/USDT", side="buy", qty=0.15, expected_price=100.0
    )

    assert result.ok is True
    assert seen["symbol"] == "SOL/USDT:USDT"


@pytest.mark.asyncio
async def test_a_symbol_with_no_perpetual_is_refused_before_any_network_call(monkeypatch):
    v = venue("bybit")
    v._markets = {"FOO/USDT": dict(SPOT, symbol="FOO/USDT")}
    monkeypatch.setattr(v, "_api_key", "k")
    monkeypatch.setattr(v, "_secret", "s")

    async def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("an order was sent for a symbol with no perpetual")

    monkeypatch.setattr(v.private, "create_order", _boom)
    result = await v.market_order(symbol="FOO/USDT", side="buy", qty=1.0)

    assert result.ok is False
    assert "no linear perpetual" in (result.error or "")


# ---------------------------------------------------------------------------
# 2. The stop parameters
# ---------------------------------------------------------------------------


@pytest.fixture
def stoppable(request, monkeypatch):
    """A credentialled venue that records the stop order it would send."""
    v = venue(request.param if hasattr(request, "param") else "bybit")
    key = "SOL/USDT:USDT"
    v._markets = {"SOL/USDT": dict(SPOT), key: dict(PERP)}
    monkeypatch.setattr(v, "_api_key", "k")
    monkeypatch.setattr(v, "_secret", "s")
    monkeypatch.setattr(v, "_hedge_mode", False)
    monkeypatch.setattr(v.public, "amount_to_precision", lambda s, a: f"{float(a):.1f}")
    monkeypatch.setattr(v.public, "price_to_precision", lambda s, p: f"{float(p):.2f}")

    sent: dict = {}

    async def _create(symbol, type_, side, amount, price, params):
        sent.update(symbol=symbol, type=type_, side=side, amount=amount, params=dict(params))
        return {"id": "stop-1"}

    monkeypatch.setattr(v.private, "create_order", _create)
    v._sent = sent  # type: ignore[attr-defined]
    return v


@pytest.mark.parametrize("stoppable", ["bybit", "binance"], indirect=True)
@pytest.mark.asyncio
async def test_the_stop_uses_the_unified_stopLossPrice_on_both_venues(stoppable):
    """ccxt turns this into STOP_MARKET on Binance and derives the trigger
    direction from the side on Bybit. It is the same intent on both."""
    result = await stoppable.place_stop_loss(
        symbol="SOL/USDT", side="sell", qty=0.15, stop_price=90.0
    )
    assert result.ok is True
    assert stoppable._sent["params"]["stopLossPrice"] == "90.00"


@pytest.mark.parametrize("stoppable", ["bybit"], indirect=True)
@pytest.mark.asyncio
async def test_the_bybit_stop_sends_NO_bare_triggerPrice(stoppable):
    """THE BUG. `triggerPrice` makes ccxt treat this as a generic trigger order,
    which then requires an explicit `triggerDirection` — and raises
    ArgumentsRequired without one, before any request leaves the process.

    Our handler caught that and logged "stop-loss order REJECTED", so the failure
    read as the venue refusing a stop rather than as never having asked for one.
    """
    await stoppable.place_stop_loss(
        symbol="SOL/USDT", side="sell", qty=0.15, stop_price=90.0
    )
    params = stoppable._sent["params"]
    assert "triggerPrice" not in params
    # And not `stopLoss` either: ccxt expects an OBJECT there, and a bare string
    # would attach a second position-level stop on top of this one.
    assert "stopLoss" not in params


@pytest.mark.parametrize("stoppable", ["bybit", "binance"], indirect=True)
@pytest.mark.asyncio
async def test_the_stop_triggers_on_the_MARK_price_on_both_venues(stoppable):
    """A last-price trigger can be moved by a thin book on a single print, which
    is exactly the move a stop exists to survive. This was previously set on
    Bybit only, leaving every Binance stop on last price."""
    await stoppable.place_stop_loss(
        symbol="SOL/USDT", side="sell", qty=0.15, stop_price=90.0
    )
    params = stoppable._sent["params"]
    if stoppable.id == "bybit":
        assert params["triggerBy"] == "MarkPrice"
    else:
        assert params["workingType"] == "MARK_PRICE"


@pytest.mark.parametrize("stoppable", ["bybit"], indirect=True)
@pytest.mark.asyncio
async def test_the_stop_is_placed_on_the_resolved_perpetual(stoppable):
    await stoppable.place_stop_loss(
        symbol="SOL/USDT", side="sell", qty=0.15, stop_price=90.0
    )
    assert stoppable._sent["symbol"] == "SOL/USDT:USDT"


# ---------------------------------------------------------------------------
# 3. The symbol form that reaches reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_positions_reports_the_symbol_in_the_agents_own_form(monkeypatch):
    v = venue("bybit")
    monkeypatch.setattr(v, "_api_key", "k")
    monkeypatch.setattr(v, "_secret", "s")

    async def _positions(symbols, params):
        return [{
            "symbol": "SOL/USDT:USDT", "side": "long", "contracts": 0.2,
            "entryPrice": 100.0, "leverage": 3, "unrealizedPnl": 1.0,
            "liquidationPrice": 70.0,
        }]

    monkeypatch.setattr(v.private, "fetch_positions", _positions)
    positions = await v.open_positions()

    assert positions is not None
    assert positions[0]["symbol"] == "SOL/USDT"
    # The raw form is kept alongside rather than discarded — it is what the
    # venue's own UI and API responses show.
    assert positions[0]["venueSymbol"] == "SOL/USDT:USDT"


def test_reconciliation_matches_the_two_spellings_of_one_position():
    """The false alarm this caused was on the CRITICAL alert.

    "missing_at_venue" is supposed to mean a liquidation, an ADL, or a close by
    hand. Firing it for every real position teaches an operator to ignore it —
    which is worse than not having the alert at all.
    """
    local = [{"symbol": "SOL/USDT", "qty": 0.2, "side": "buy"}]
    at_venue = [{"symbol": "SOL/USDT:USDT", "qty": 0.2, "side": "long"}]

    assert _compare(local, at_venue) == []


def test_a_genuine_discrepancy_still_reports_after_normalisation():
    """Normalising must not swallow the real signal it was hiding."""
    local = [{"symbol": "SOL/USDT", "qty": 0.2, "side": "buy"}]

    discrepancies = _compare(local, [])
    assert [d.kind for d in discrepancies] == ["missing_at_venue"]
    assert discrepancies[0].severity == "critical"


# ---------------------------------------------------------------------------
# The credential split that makes running the round trip safe
# ---------------------------------------------------------------------------


def test_testnet_reads_its_own_variables(monkeypatch):
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET", "test-secret")
    monkeypatch.setenv("BYBIT_API_KEY", "REAL-key")
    monkeypatch.setenv("BYBIT_SECRET", "REAL-secret")

    assert _credentials("bybit", testnet=True) == ("test-key", "test-secret")


def test_MAINNET_NEVER_READS_THE_TESTNET_VARIABLES(monkeypatch):
    """The direction that matters.

    A testnet key reaching a mainnet client merely fails closed. The reverse is
    the one worth preventing structurally: the whole point of the split is that
    verifying on testnet does not require pasting testnet keys over the mainnet
    ones and putting them back afterwards.
    """
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET", "test-secret")

    assert _credentials("bybit", testnet=False) == ("", "")


def test_testnet_falls_back_to_the_mainnet_variables(monkeypatch):
    """Backwards compatible: that was the arrangement before the split, and a
    mainnet key sent to a sandbox endpoint is refused — it fails closed."""
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_SECRET", "s")

    assert _credentials("bybit", testnet=True) == ("k", "s")


def test_the_named_variable_matches_the_network_in_force():
    """An error telling the operator to set the mainnet key while the process
    signs testnet requests sends them to set a key that is never read."""
    assert key_variable("bybit", testnet=True) == "BYBIT_TESTNET_API_KEY"
    assert key_variable("bybit", testnet=False) == "BYBIT_API_KEY"
    assert key_variable("binance", testnet=True) == "BINANCE_TESTNET_API_KEY"


def test_a_testnet_client_reports_the_testnet_variable(monkeypatch):
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET", "s")

    v = venue("bybit")
    assert v.has_credentials() is True
    assert v.key_variable == "BYBIT_TESTNET_API_KEY"
