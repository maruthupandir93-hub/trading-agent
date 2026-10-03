from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from backend.services import binance_testnet as bt


@pytest.mark.parametrize("qty", [.3, .6, 79.6])
def test_valid_lots_do_not_lose_a_step(qty):
    assert float(bt._quantise(qty, {"step": .1, "minQty": .1, "quantityPrecision": 1})) == qty


async def test_persistent_clock_error_stops_after_one_retry(monkeypatch):
    monkeypatch.setattr(bt, "_creds", lambda: ("dummy", "dummy"))
    offset = AsyncMock(return_value=0)
    monkeypatch.setattr(bt, "_server_offset", offset)
    response = NS(status_code=400, json=lambda: {"code": -1021, "msg": "clock"})
    client = NS(request=AsyncMock(return_value=response))
    ok, _ = await bt._signed(client, "GET", "/fapi/v2/account")
    assert not ok and client.request.await_count == 2
    assert sum(c.kwargs.get("force", False) for c in offset.await_args_list) == 1


async def test_non_json_and_signed_url_errors_are_sanitized(monkeypatch):
    monkeypatch.setattr(bt, "_creds", lambda: ("dummy", "dummy"))
    monkeypatch.setattr(bt, "_server_offset", AsyncMock(return_value=0))
    client = NS(request=AsyncMock(side_effect=TimeoutError("signature=SECRET")))
    ok, error = await bt._signed(client, "POST", "/fapi/v1/order")
    assert not ok and "SECRET" not in error and client.request.await_count == 1


async def test_wrong_balance_shape_is_unavailable(monkeypatch):
    monkeypatch.setattr(bt, "_signed", AsyncMock(return_value=(True, {"unexpected": 1})))
    assert await bt.free_usdt() is None


async def test_protection_uses_algo_api_and_namespaced_cancellation(monkeypatch):
    rules = {"step": .1, "minQty": .1, "quantityPrecision": 1, "tick": .0001, "pricePrecision": 4}
    monkeypatch.setattr(bt, "_instrument_rules", AsyncMock(return_value=rules))
    signed = AsyncMock(return_value=(True, {"algoId": 123}))
    monkeypatch.setattr(bt, "_signed", signed)
    result = await bt.place_stop_loss(symbol="XRP/USDT", side="sell", qty=.3,
                                      stop_price=1.41234, client_order_id="demo-sl")
    assert result.ok and result.order_id == "algo:123"
    args = signed.await_args.args
    assert args[1:3] == ("POST", "/fapi/v1/algoOrder")
    assert args[3]["algoType"] == "CONDITIONAL"
    assert args[3]["triggerPrice"] == "1.4123"
    assert args[3]["quantity"] == "0.3"
    assert args[3]["reduceOnly"] == "true"
    assert args[3]["clientAlgoId"] == "demo-sl"
    assert await bt.cancel_order(result.order_id, "XRP/USDT")
    assert signed.await_args.args[1:] == ("DELETE", "/fapi/v1/algoOrder", {"algoId": "123"})


async def test_lost_market_ack_queries_client_id_without_resending(monkeypatch):
    monkeypatch.setattr(bt, "_instrument_rules", AsyncMock(return_value={"step": .1, "minQty": .1}))
    signed = AsyncMock(side_effect=[(False, "execution status unknown"),
        (True, {"orderId": 321, "executedQty": "0.3", "avgPrice": "1.5"})])
    monkeypatch.setattr(bt, "_signed", signed)
    result = await bt.market_order(symbol="XRP/USDT", side="buy", qty=.3, client_order_id="stable")
    assert result.ok and result.filled_qty == .3
    assert [c.args[1] for c in signed.await_args_list] == ["POST", "GET"]
    assert signed.await_args.args[3]["origClientOrderId"] == "stable"


async def test_missing_rules_does_not_send_an_order(monkeypatch):
    monkeypatch.setattr(bt, "_instrument_rules", AsyncMock(return_value={}))
    signed = AsyncMock()
    monkeypatch.setattr(bt, "_signed", signed)
    assert not (await bt.market_order(symbol="XRP/USDT", side="buy", qty=1)).ok
    signed.assert_not_called()


def test_expired_demo_quote_is_not_used(monkeypatch):
    monkeypatch.setattr(bt, "_demo_prices", {"XRPUSDT": (1.5, 0)})
    assert bt.cached_price("XRP/USDT") == 0


async def test_demo_candles_do_not_fetch_mainnet(monkeypatch):
    from backend.services import market_data
    monkeypatch.setattr(bt, "demo_data_active", lambda: True)
    candles = [{"close": 1.5}]
    monkeypatch.setattr(bt, "fetch_klines", AsyncMock(return_value=candles))
    monkeypatch.setattr(market_data, "get_exchange_client", lambda: pytest.fail("mainnet client used"))
    assert await market_data.fetch_klines("XRP/USDT", "1m") == candles


@pytest.mark.parametrize("enabled,live,venue,expected", [
    (False, False, "binance", False), (True, False, "binance", True),
    (True, True, "binance", False), (True, False, "bybit", False),
])
def test_demo_data_requires_the_connected_paper_mode(monkeypatch, enabled, live, venue, expected):
    from backend.core.config import settings
    from backend.services import paper_testnet
    monkeypatch.setattr(settings, "_live_trading", live)
    monkeypatch.setattr(paper_testnet, "active", lambda: enabled)
    monkeypatch.setattr(paper_testnet, "venue_choice", lambda: venue)
    assert bt.demo_data_active() is expected


@pytest.mark.parametrize("payload", [[], {"symbols": "invalid"},
    {"symbols": [{"filters": [{"filterType": "LOT_SIZE", "stepSize": "bad"}]}]}])
async def test_malformed_filters_refuse_orders(monkeypatch, payload):
    monkeypatch.setattr(bt, "_rules_cache", {})
    monkeypatch.setattr(bt, "_get_json", AsyncMock(return_value=payload))
    assert await bt._instrument_rules(None, "XRP/USDT") == {}


async def test_invalid_trigger_does_not_contact_demo(monkeypatch):
    signed = AsyncMock()
    monkeypatch.setattr(bt, "_signed", signed)
    result = await bt.place_stop_loss(symbol="XRP/USDT", side="sell", qty=1, stop_price=float("nan"))
    assert not result.ok
    signed.assert_not_called()


async def test_demo_tick_reaches_the_agent_bus(monkeypatch):
    import asyncio
    from backend.services import live_market_data
    monkeypatch.setattr(bt, "demo_data_active", lambda: True)
    monkeypatch.setattr(bt, "fetch_ticker", AsyncMock(return_value={"last": 1.5, "baseVolume": 20}))
    monkeypatch.setattr(live_market_data, "_live_prices", {})
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    exchange = NS(watch_ticker=AsyncMock())
    bus = NS(publish=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await live_market_data._watch_ticker_loop(exchange, "XRP/USDT", bus)
    exchange.watch_ticker.assert_not_called()
    topic, event = bus.publish.await_args.args
    assert topic == "TICK_RECEIVED" and event.exchange == "binance_testnet" and event.price == 1.5


async def test_binance_mirror_fill_keeps_the_correct_venue_label(monkeypatch):
    import uuid
    from backend.agents import execution_agent
    from backend.models.events import TarApprovedEvent
    from backend.services import paper_testnet
    monkeypatch.setattr(execution_agent, "may_open_new_position", lambda: True)
    monkeypatch.setattr(paper_testnet, "active", lambda: True)
    monkeypatch.setattr(paper_testnet, "venue_choice", lambda: "binance")
    monkeypatch.setattr(paper_testnet, "place", AsyncMock(return_value={
        "order_id": "demo-fill", "price": 101., "filled_qty": 1.}))
    agent = execution_agent.ExecutionAgent(simulation_mode=True)
    agent._last_prices["XRP/USDT"] = 100.
    for name in dir(agent):
        if name.startswith("_persist"):
            monkeypatch.setattr(agent, name, AsyncMock())
    monkeypatch.setattr(agent, "_apply_paper_fill", AsyncMock(return_value={"realized": 0}))
    monkeypatch.setattr(agent, "publish", AsyncMock())
    approved = TarApprovedEvent(tar_id=uuid.uuid4(), symbol="XRP/USDT", direction="LONG",
        approved_size=1., approved_leverage=1, cro_rationale="test", stop_loss=90.,
        take_profit=120., tab="paper")
    await agent._execute_tar(approved)
    fills = [c.args[0] for c in agent.publish.await_args_list if c.args[0].event_type == "ORDER_FILLED"]
    assert len(fills) == 1 and fills[0].exchange == "binance_testnet"
    assert fills[0].order_id == "demo-fill" and fills[0].fill_price == 101.
