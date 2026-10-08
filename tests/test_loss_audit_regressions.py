"""Regression cases derived from the October demo-loss audit."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.strategy_ensemble import breakout_agent
from backend.graphs.nodes.market import _closed_candles
from backend.services.fees import fee_from_order


def bars(volume=1001, close=101):
    return [dict(open=99, high=100, low=98, close=99, volume=1000)
            for _ in range(14)] + [dict(open=99, high=102, low=97, close=close, volume=volume)]


@pytest.mark.parametrize("volume", [0, 1, 1000, None, float("nan")])
def test_breakout_requires_positive_volume_above_baseline(volume):
    assert breakout_agent(bars(volume)) == "HOLD"


@pytest.mark.parametrize("close,signal", [(101, "BUY"), (97, "SELL"), (99, "HOLD")])
def test_confirmed_volume_breakout_both_directions(close, signal):
    assert breakout_agent(bars(close=close)) == signal


def test_unfinished_breakout_is_not_in_analysis_snapshot():
    series = bars()
    for b in series[:-1]:
        b["closeTime"] = 899999
    series[-1]["closeTime"] = 1799999
    completed = _closed_candles(series, 1200)
    assert len(completed) == 14
    assert breakout_agent(completed) == "HOLD"
    assert breakout_agent(_closed_candles(series, 1800)) == "BUY"


def test_fee_summary_does_not_duplicate_breakdown():
    result = fee_from_order({"fee": {"cost": 3, "currency": "USDT"},
                             "fees": [{"cost": 1, "currency": "USDT"},
                                      {"cost": 2, "currency": "USDT"}]})
    assert result.measured and result.cost == 3


async def test_reflection_joins_execution_by_trade_authorization_id(monkeypatch):
    from backend.graphs.nodes.reflection import assess_execution_quality
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=dict(
        score=93, slippage_bps=1, latency_ms=1000, fully_filled=True, notes=[])))
    class Pool:
        def acquire(self): return self
        async def __aenter__(self): return conn
        async def __aexit__(self, *args): pass
    monkeypatch.setattr("backend.core.db.get_db_pool", lambda: Pool())
    result = await assess_execution_quality({"closed_trade": {"trade_id": "trade-123", "pnl": -1}})
    assert "tar_id = $1" in conn.fetchrow.call_args.args[0]
    assert conn.fetchrow.call_args.args[1] == "trade-123"
    assert result["reflection"].execution_quality != "unavailable"


async def test_market_feeds_start_concurrently_and_filter_benchmark(monkeypatch):
    from backend.graphs.nodes import market
    started = set()
    all_started = asyncio.Event()
    async def fetch(symbol, tf, limit):
        started.add((symbol, tf))
        if len(started) == 6: all_started.set()
        await asyncio.wait_for(all_started.wait(), 1)
        return [dict(open=1, high=2, low=1, close=2, volume=1, closeTime=end)
                for end in [899999, 1799999]]
    monkeypatch.setattr(market, "fetch_klines", fetch)
    monkeypatch.setattr(market, "get_price", lambda _: 2)
    monkeypatch.setattr(market, "_fetch_specialist_feeds", AsyncMock(return_value=([], [], [], [], {})))
    result = await market.validate_market_data({"symbol": "XRP/USDT", "started_at": 1200})
    snapshot = result["market_data"]
    assert len(started) == 6
    assert all(len(v) == 1 for v in snapshot.candles.values())
    assert all(len(v) == 1 for v in snapshot.benchmark_candles.values())


def test_breakout_name_reaches_deterministic_reflection():
    from backend.graphs.nodes.reflection import classify_outcome
    result = classify_outcome({"closed_trade": {"pnl": -1, "strategies": ["Breakout"],
                                                 "exit_reason": "thesis-invalidated"}})
    assert any("Breakout failed" in s for s in result["reflection"].attribution)


@pytest.mark.parametrize("quantity,currency,expected", [(1, "USDT", True), (.25, "USDT", False), (1, "BNB", False)])
def test_demo_costs_require_full_quantity_and_quote_fees(quantity, currency, expected):
    from backend.services.demo_accounting import measured_leg
    rows = [dict(id=1, orderId=42, qty=str(quantity), commission="0.01",
                 commissionAsset=currency, realizedPnl="-0.1")]
    assert (measured_leg(rows, "42", 1) is not None) == expected


@pytest.fixture
def lifecycle(monkeypatch):
    from backend.core import message_bus
    from backend.services import portfolio_store as ps, execution_service as es
    from backend.agents.execution_agent import get_execution_agent
    from backend.agents.position_monitor import get_position_monitor
    monkeypatch.setattr(message_bus, "_bus", message_bus.MessageBus())
    es.reset_for_tests()
    monkeypatch.setattr(ps, "_portfolio", {"paper": {"cash": 1000., "positions": []}, "real": {"positions": []}})
    executor, monitor = get_execution_agent(), get_position_monitor()
    monitor.attach_execution(executor)
    monkeypatch.setattr(monitor, "_persist_closed_trade", AsyncMock())
    return ps, executor, monitor


async def test_partial_demo_exit_keeps_remainder_and_retries_exact_size(monkeypatch, lifecycle):
    from backend.services import paper_testnet as pt
    ps, executor, monitor = lifecycle
    await ps.apply_paper_fill(symbol="XRP/USDT", side="buy", qty=1., price=100., leverage=1., reduce_only=False)
    await monitor.track_manual_position(symbol="XRP/USDT", side="buy", qty=1., entry_price=100., stop_loss=90., take_profit=120., tab="paper")
    monkeypatch.setattr(pt, "active", lambda: True)
    place = AsyncMock(side_effect=[dict(price=101., filled_qty=.25, order_id="a"), dict(price=102., filled_qty=.75, order_id="b")])
    monkeypatch.setattr(pt, "place", place)
    assert await monitor.close_tracked("XRP/USDT", "stop-loss", price=101.) is None
    assert monitor.snapshot_open()[0]["qty"] == .75
    assert ps._portfolio["paper"]["positions"][0]["qty"] == .75
    assert await monitor.close_tracked("XRP/USDT", "stop-loss", price=102.) == 102.
    assert not monitor.snapshot_open()
    assert not ps._portfolio["paper"]["positions"]
    assert [c.kwargs["qty"] for c in place.call_args_list] == [1., .75]


async def test_measured_demo_costs_reach_cash_close_and_learning(monkeypatch, lifecycle):
    from backend.services import paper_testnet as pt, demo_accounting
    ps, executor, monitor = lifecycle
    await ps.apply_paper_fill(symbol="XRP/USDT", side="buy", qty=1., price=100., leverage=1., reduce_only=False)
    await monitor.track_manual_position(symbol="XRP/USDT", side="buy", qty=1., entry_price=100., stop_loss=90., take_profit=120., tab="paper")
    next(iter(monitor._open.values())).entry_fee = .05
    monkeypatch.setattr(pt, "active", lambda: True)
    monkeypatch.setattr(pt, "venue_choice", lambda: "binance")
    monkeypatch.setattr(pt, "place", AsyncMock(return_value=dict(price=110., filled_qty=1., order_id="exit")))
    monkeypatch.setattr(demo_accounting, "closed_round_trip", AsyncMock(return_value=dict(
        entry_order_id="entry", entry_fee=.04, exit_fee=.044, funding=0., realized=9.916)))
    assert await monitor.close_tracked("XRP/USDT", "audit", price=109.) == 110.
    assert ps._portfolio["paper"]["cash"] == pytest.approx(1009.916)
    call = monitor._persist_closed_trade.call_args
    assert call.args[2] == pytest.approx(9.916)
    assert call.args[1].order_id == "exit"
    assert call.kwargs["fee"].measured


@pytest.mark.parametrize("version,accepted", [(None, False), (1, False), (2, True)])
def test_breakout_prior_requires_current_signal_version(monkeypatch, tmp_path, version, accepted):
    import json
    from backend.services import strategy_priors as sp
    row = dict(strategy="Breakout", trades=40, wins=20, total_r=20)
    if version is not None:
        row["signal_version"] = version
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({"runs": [{"strategies": [row]}]}))
    monkeypatch.setattr(sp, "_newest_summary", lambda: str(path))
    sp.reset()
    try:
        assert ("Breakout" in sp.load(force=True)["strategies"]) == accepted
    finally:
        sp.reset()


async def test_session_counts_fast_fill_once_even_when_poll_never_sees_position(monkeypatch):
    from types import SimpleNamespace
    from backend.core import message_bus
    from backend.services import trading_session as ts
    bus = message_bus.MessageBus()
    monkeypatch.setattr(message_bus, "_bus", bus)
    session = ts.TradingSession(id="fast", symbol="XRP/USDT", leverage=1,
                                start_equity=1000., target_equity=2000., floor_equity=500.)
    monkeypatch.setattr(ts, "_sessions", {session.id: session})
    monkeypatch.setattr(ts, "_persist", lambda: None)
    monkeypatch.setattr(ts, "SESSION_POLL_S", 0)
    monkeypatch.setattr(ts, "_tab_for_session", lambda: "paper")
    monkeypatch.setattr(ts, "current_equity", AsyncMock(return_value=1000.))
    monkeypatch.setattr(ts, "_any_open_position", AsyncMock(return_value=False))
    monkeypatch.setattr(ts, "_daily_target_reached", lambda *args: False)
    monkeypatch.setattr("backend.core.system_state.is_emergency_stopped", lambda: False)
    monkeypatch.setattr("backend.core.system_state.is_system_paused", lambda: False)
    async def decide(*args):
        event = SimpleNamespace(tar_id="one", tab="paper", symbol="XRP/USDT", fill_quantity=1.)
        await bus.publish("ORDER_FILLED", event)
        await bus.publish("ORDER_FILLED", event)
        session.status = "stopped"
    monkeypatch.setattr(ts, "_decide_once", decide)
    await asyncio.wait_for(ts._run_session(session.id), 1)
    assert session.trades_opened == 1
    assert not bus._subscribers.get("ORDER_FILLED")


async def test_demo_accounting_reads_fills_and_signed_funding_only(monkeypatch):
    import datetime as dt
    from backend.services import demo_accounting as da
    conn = AsyncMock()
    conn.fetchrow.return_value = {"order_id": "entry"}
    class Pool:
        def acquire(self): return self
        async def __aenter__(self): return conn
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(da, "get_db_pool", lambda: Pool())
    calls = []
    async def signed(client, method, path, params):
        calls.append((method, path, params))
        if path.endswith("income"):
            return True, [{"symbol": "XRPUSDT", "asset": "USDT", "income": "-0.02"}]
        entry = params["orderId"] == "entry"
        return True, [dict(id=1 if entry else 2, orderId=params["orderId"], qty="1",
                           commission="0.01", commissionAsset="USDT", realizedPnl="0" if entry else "1")]
    monkeypatch.setattr(da.demo, "_signed", signed)
    result = await da.closed_round_trip(tar_id="tar", symbol="XRP/USDT", exit_order_id="exit",
                                       quantity=1., opened_at=dt.datetime(2026, 10, 5),
                                       closed_at=dt.datetime(2026, 10, 6))
    assert result["realized"] == pytest.approx(.96)
    assert result["funding"] == .02
    assert len(calls) == 3 and all(call[0] == "GET" for call in calls)


async def test_demo_microstructure_uses_futures_and_separate_cache(monkeypatch):
    from types import SimpleNamespace
    from backend.services import microstructure_feed as mf, binance_testnet as bt
    monkeypatch.setattr(mf, "_micro_cache", {})
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(ok=True, data={"bids": [[1, 2]], "asks": [[2, 2]]} if "depth?" in url else [])
    monkeypatch.setattr(mf, "fetch_json", fetch)
    monkeypatch.setattr(bt, "demo_data_active", lambda: False)
    await mf.fetch_microstructure("XRP/USDT")
    monkeypatch.setattr(bt, "demo_data_active", lambda: True)
    await mf.fetch_microstructure("XRP/USDT")
    assert len(calls) == 4
    assert all(url.startswith(bt.BASE_URL + "/fapi/v1/") for url in calls[2:])
    await mf.fetch_microstructure("XRP/USDT")
    assert len(calls) == 4


async def test_demo_routing_change_refused_before_config_write(monkeypatch):
    from fastapi import HTTPException
    from types import SimpleNamespace
    from backend.api.admin import set_testnet, TestnetRequest
    from backend.services import paper_testnet as pt
    monkeypatch.setattr(pt, "enabled", lambda: True)
    monkeypatch.setattr(pt, "venue_choice", lambda: "binance")
    monkeypatch.setattr("backend.services.portfolio_store.get_portfolio", AsyncMock(return_value={"paper": {"positions": [{"qty": 1}]}}))
    monkeypatch.setattr("backend.agents.position_monitor.get_position_monitor", lambda: SimpleNamespace(snapshot_open=lambda: []))
    with pytest.raises(HTTPException) as error:
        await set_testnet(TestnetRequest(enabled=False, verify=False))
    assert error.value.status_code == 409
