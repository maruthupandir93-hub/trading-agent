"""The venue adapter: the parameter matrix, sizing filters, and the refusals.

WHY THE PARAMETER TESTS ARE THE IMPORTANT ONES
----------------------------------------------
Every test here is offline and none of them place an order. That is deliberate and
it is also the limit of what can honestly be tested without spending real money:
Binance mainnet order placement cannot be verified without placing a real order.

So what IS pinned is the thing that decides whether a real order is accepted, and
whether a "close" closes or opens:

    Binance one-way : reduceOnly, no positionSide
    Binance hedge   : positionSide, and NO reduceOnly (Binance rejects it)
    Bybit one-way   : positionIdx 0 + reduceOnly
    Bybit hedge     : positionIdx 1/2, acting on the leg OPPOSITE the order side

Getting the hedge-mode close backwards does not error — it opens a second position
on the other leg. That is the failure this file exists to prevent.
"""

from __future__ import annotations

import pytest

from backend.services.venue import (
    SUPPORTED,
    OrderResult,
    Venue,
    configured_venue,
    reset_venue,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # No credentials: every private path must refuse rather than reach a network.
    for var in ("BINANCE_API_KEY", "BINANCE_SECRET", "BYBIT_API_KEY", "BYBIT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("USE_TESTNET", "true")
    reset_venue()
    yield
    reset_venue()


def venue(venue_id: str) -> Venue:
    return Venue(venue_id, testnet=True)


# ---------------------------------------------------------------------------
# Venue selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SUPPORTED)
def test_each_supported_venue_can_be_built(name, monkeypatch):
    monkeypatch.setenv("EXCHANGE_ID", name)
    assert configured_venue() == name
    v = venue(name)
    assert v.id == name


def test_an_unknown_venue_falls_back_to_binance_rather_than_crashing_at_the_first_order(monkeypatch):
    # A typo would otherwise surface as an AttributeError from inside ccxt at the
    # moment an order is placed, which is the worst possible time to find it.
    monkeypatch.setenv("EXCHANGE_ID", "kraken-futures-typo")
    assert configured_venue() == "binance"


def test_the_two_venues_read_different_credentials(monkeypatch):
    """Different exchanges are different accounts holding different money.

    A shared key pair would authenticate against whichever venue was configured —
    and a key valid on the WRONG venue trades the wrong account.
    """
    monkeypatch.setenv("BINANCE_API_KEY", "bn-key")
    monkeypatch.setenv("BINANCE_SECRET", "bn-secret")

    assert venue("binance").has_credentials() is True
    assert venue("bybit").has_credentials() is False


# ---------------------------------------------------------------------------
# THE PARAMETER MATRIX
# ---------------------------------------------------------------------------

class TestBinanceParams:
    def test_one_way_entry_sends_no_position_side(self):
        p = venue("binance")._order_params(
            side="buy", reduce_only=False, hedge=False, client_order_id="k1"
        )
        assert p == {"clientOrderId": "k1"}

    def test_one_way_close_sends_reduce_only(self):
        # Without this a close is just an opposite-side order, and any surplus over
        # the live size OPENS a position the other way.
        p = venue("binance")._order_params(
            side="sell", reduce_only=True, hedge=False, client_order_id=None
        )
        assert p == {"reduceOnly": True}

    def test_hedge_mode_sends_position_side_and_NEVER_reduce_only(self):
        # Binance rejects reduceOnly in hedge mode: "Parameter reduceOnly sent when
        # not required". Sending both defensively fails every close.
        p = venue("binance")._order_params(
            side="sell", reduce_only=True, hedge=True, client_order_id=None
        )
        assert p == {"positionSide": "LONG"}
        assert "reduceOnly" not in p

    def test_hedge_mode_closes_the_leg_opposite_the_order_side(self):
        # Selling closes the LONG leg; buying closes the SHORT leg. Backwards here
        # does not error — it opens a second position on the other leg.
        v = venue("binance")
        assert v._order_params(side="sell", reduce_only=True, hedge=True, client_order_id=None)["positionSide"] == "LONG"
        assert v._order_params(side="buy", reduce_only=True, hedge=True, client_order_id=None)["positionSide"] == "SHORT"

    def test_hedge_mode_entry_opens_the_leg_matching_the_order_side(self):
        v = venue("binance")
        assert v._order_params(side="buy", reduce_only=False, hedge=True, client_order_id=None)["positionSide"] == "LONG"
        assert v._order_params(side="sell", reduce_only=False, hedge=True, client_order_id=None)["positionSide"] == "SHORT"


class TestBybitParams:
    def test_every_order_carries_the_linear_category(self):
        # Bybit v5 is split by category; without it the call can land on the wrong
        # product entirely.
        p = venue("bybit")._order_params(
            side="buy", reduce_only=False, hedge=False, client_order_id=None
        )
        assert p["category"] == "linear"

    def test_the_idempotency_key_is_orderLinkId_not_clientOrderId(self):
        # Bybit ignores `clientOrderId`, so a retry would produce a DUPLICATE FILL
        # rather than being rejected as a repeat.
        p = venue("bybit")._order_params(
            side="buy", reduce_only=False, hedge=False, client_order_id="k1"
        )
        assert p["orderLinkId"] == "k1"
        assert "clientOrderId" not in p

    def test_one_way_uses_position_index_zero_and_reduce_only(self):
        p = venue("bybit")._order_params(
            side="sell", reduce_only=True, hedge=False, client_order_id=None
        )
        assert p["positionIdx"] == 0
        assert p["reduceOnly"] is True

    def test_hedge_mode_closes_the_leg_opposite_the_order_side(self):
        v = venue("bybit")
        # positionIdx 1 is the long leg, 2 the short leg.
        assert v._order_params(side="sell", reduce_only=True, hedge=True, client_order_id=None)["positionIdx"] == 1
        assert v._order_params(side="buy", reduce_only=True, hedge=True, client_order_id=None)["positionIdx"] == 2

    def test_hedge_mode_entry_opens_the_leg_matching_the_order_side(self):
        v = venue("bybit")
        assert v._order_params(side="buy", reduce_only=False, hedge=True, client_order_id=None)["positionIdx"] == 1
        assert v._order_params(side="sell", reduce_only=False, hedge=True, client_order_id=None)["positionIdx"] == 2


# ---------------------------------------------------------------------------
# Sizing against the venue's filters
# ---------------------------------------------------------------------------

@pytest.fixture
def sized(monkeypatch):
    """A venue with one market whose filters are known, and no network."""
    v = venue("binance")
    market = {
        "symbol": "BTC/USDT",
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 100.0}},
        "precision": {"amount": 3},
    }
    v._markets = {"BTC/USDT": market}
    monkeypatch.setattr(
        v.public, "amount_to_precision", lambda symbol, amount: f"{float(amount):.3f}"
    )
    return v


@pytest.mark.asyncio
async def test_a_size_is_rounded_down_to_the_venues_step(sized):
    # An unrounded float is rejected outright, and the agent's log said only
    # "order rejected" with no hint that the number itself was the problem.
    check = await sized.check_size("BTC/USDT", 0.0123456, 70_000.0)
    assert check.ok is True
    assert check.qty == 0.012


@pytest.mark.asyncio
async def test_a_size_below_the_minimum_is_REFUSED_not_rounded_up(sized):
    """The one direction sizing must never move on its own.

    Bumping up to the venue's minimum would stake more than the Risk Gateway
    approved — the system would be taking a position size no gate ever cleared.
    """
    # 0.012 survives rounding at 3dp — so this exercises the MINIMUM check
    # specifically, not the rounds-to-zero one above it.
    sized._markets["BTC/USDT"]["limits"]["amount"]["min"] = 0.05
    check = await sized.check_size("BTC/USDT", 0.012, 70_000.0)
    assert check.ok is False
    assert "minimum quantity" in (check.reason or "")
    assert check.qty == 0.012


@pytest.mark.asyncio
async def test_a_notional_below_the_venues_minimum_is_refused(sized):
    # 0.001 BTC clears the minimum QUANTITY but at $50 is under the $100 minimum
    # notional — a different filter, and a separate rejection at the venue.
    check = await sized.check_size("BTC/USDT", 0.001, 50_000.0)
    assert check.ok is False
    assert "notional" in (check.reason or "")


@pytest.mark.asyncio
async def test_a_size_that_rounds_to_zero_is_refused_with_a_readable_reason(sized):
    check = await sized.check_size("BTC/USDT", 0.0000001, 70_000.0)
    assert check.ok is False
    assert "rounds to zero" in (check.reason or "")


@pytest.mark.asyncio
async def test_an_unknown_symbol_is_refused_rather_than_ordered(sized):
    check = await sized.check_size("DOGE/USDT", 100.0, 0.15)
    assert check.ok is False
    assert "not a market" in (check.reason or "")


# ---------------------------------------------------------------------------
# Refusals — every one of these is a path that must not reach the network
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_order_without_credentials_refuses_and_names_the_variables():
    result = await venue("bybit").market_order(symbol="BTC/USDT", side="buy", qty=1.0)
    assert result.ok is False
    assert "BYBIT_API_KEY" in (result.error or "")


@pytest.mark.asyncio
async def test_a_failed_order_carries_no_fill_price():
    """`ok=False` must never be mistakable for a fill.

    `create_market_order` once fabricated a $60,000 fill on any exception and the
    caller wrote it into the trade log as real. A result with no price on it cannot
    be misread that way.
    """
    result = await venue("binance").market_order(symbol="BTC/USDT", side="buy", qty=1.0)
    assert result.ok is False
    assert result.average_price is None
    assert result.filled_qty is None


@pytest.mark.asyncio
async def test_leverage_reports_failure_without_credentials():
    # False must mean "do not place an order sized for a leverage the venue is not
    # applying" — so it cannot be optimistic.
    assert await venue("binance").ensure_leverage("BTC/USDT", 3) is False


@pytest.mark.asyncio
async def test_leverage_not_modified_is_treated_as_SUCCESS(monkeypatch):
    """Both venues error when the leverage is already the requested value.

    Bybit returns 110043 and Binance -4046. That is the desired state, and reading
    it as a failure would abort a correctly-configured trade.
    """
    v = venue("bybit")
    v._api_key, v._secret = "k", "s"

    async def already_set(*_a, **_k):
        raise RuntimeError("bybit {\"retCode\":110043,\"retMsg\":\"leverage not modified\"}")

    monkeypatch.setattr(v.private, "set_leverage", already_set)
    assert await v.ensure_leverage("BTC/USDT", 3) is True


@pytest.mark.asyncio
async def test_leverage_is_only_sent_once_per_symbol(monkeypatch):
    # Re-sending on every order is a wasted private call and, on Bybit, an error.
    v = venue("binance")
    v._api_key, v._secret = "k", "s"
    calls = []

    async def record(leverage, symbol, params):
        calls.append((leverage, symbol))

    monkeypatch.setattr(v.private, "set_leverage", record)
    assert await v.ensure_leverage("BTC/USDT", 3) is True
    assert await v.ensure_leverage("BTC/USDT", 3) is True
    assert len(calls) == 1

    # ...but a CHANGED leverage must be sent.
    assert await v.ensure_leverage("BTC/USDT", 5) is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_positions_returns_None_when_the_venue_cannot_be_asked():
    """None and [] are different and the difference is load-bearing.

    [] means "the venue reports nothing open", which would justify dropping a local
    position during reconciliation. "We could not reach the venue" must never do that.
    """
    assert await venue("binance").open_positions() is None


@pytest.mark.asyncio
async def test_a_flat_row_is_not_reported_as_an_open_position(monkeypatch):
    # Both venues return zero-contract rows for symbols the account has touched.
    v = venue("binance")
    v._api_key, v._secret = "k", "s"

    async def fake(symbols, params):
        return [
            {"symbol": "BTC/USDT", "contracts": 0, "side": None},
            {"symbol": "ETH/USDT", "contracts": 1.5, "side": "long", "entryPrice": 3000.0,
             "leverage": 3, "unrealizedPnl": 12.0, "liquidationPrice": 2000.0},
        ]

    monkeypatch.setattr(v.private, "fetch_positions", fake)
    positions = await v.open_positions()
    assert [p["symbol"] for p in positions] == ["ETH/USDT"]
    assert positions[0]["qty"] == 1.5


@pytest.mark.asyncio
async def test_balance_is_None_not_zero_when_unreadable():
    assert await venue("binance").free_usdt() is None


@pytest.mark.asyncio
async def test_cancelling_an_order_that_is_already_gone_is_success(monkeypatch):
    """A stop left resting after its position closes becomes an order to OPEN
    the opposite position. 'Already gone' is the desired end state."""
    v = venue("binance")

    async def unknown(*_a, **_k):
        raise RuntimeError("binance Unknown order sent.")

    monkeypatch.setattr(v.private, "cancel_order", unknown)
    assert await v.cancel_order("123", "BTC/USDT") is True


# ---------------------------------------------------------------------------
# The public/private split
# ---------------------------------------------------------------------------

def test_the_public_client_carries_no_credentials(monkeypatch):
    """Market data must not spend the API key's rate budget.

    That budget is what places orders; exhausting it on a price poll means the
    order that matters is the one that gets throttled.
    """
    monkeypatch.setenv("BINANCE_API_KEY", "bn-key")
    monkeypatch.setenv("BINANCE_SECRET", "bn-secret")
    v = venue("binance")

    assert not v.public.apiKey
    assert not v.public.secret
    assert v.private.apiKey == "bn-key"


def test_binance_defaults_to_futures_and_bybit_to_linear_swaps():
    # Without defaultType ccxt's binance defaults to SPOT — orders would go to a
    # different market than every log line in this system claims.
    assert venue("binance").private.options["defaultType"] == "future"
    assert venue("bybit").private.options["defaultType"] == "swap"
    assert venue("bybit").private.options["defaultSubType"] == "linear"
