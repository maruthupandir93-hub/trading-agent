import pytest
from unittest.mock import AsyncMock
from tests.test_loss_audit_regressions import lifecycle

@pytest.mark.parametrize("pattern", ["winners", "losers", "alternating"])
async def test_repeated_partial_close_retries_accounting_and_events(monkeypatch,lifecycle,pattern):
    from backend.services import paper_testnet as pt,demo_accounting
    from backend.agents import position_monitor as pm
    from backend.services.fees import modelled_fee
    from backend.core.message_bus import get_message_bus
    ps,executor,monitor=lifecycle
    monkeypatch.setattr(pt,"active",lambda:True)
    monkeypatch.setattr(pt,"venue_choice",lambda:"binance")
    monkeypatch.setattr(pm,"_venue_backed",lambda pos:False)
    monkeypatch.setattr(demo_accounting,"closed_round_trip",AsyncMock(return_value=None))
    closed=[]
    get_message_bus().subscribe("POSITION_CLOSED",lambda event:closed.append(event))
    expected_cash=1000.
    client_ids=[]
    for i in range(40):
        side="buy" if i%2==0 else "sell"
        win=pattern=="winners" or (pattern=="alternating" and i%3==0)
        exit_price=100.+(2 if win else -2)*(1 if side=="buy" else -1)
        await ps.apply_paper_fill(symbol="XRP/USDT",side=side,qty=1.,price=100.,leverage=1.,reduce_only=False)
        await monitor.track_manual_position(symbol="XRP/USDT",side=side,qty=1.,entry_price=100.,stop_loss=90. if side=="buy" else 110.,take_profit=120. if side=="buy" else 80.,tab="paper")
        pos=next(iter(monitor._open.values()))
        pos.entry_fee=modelled_fee(100.).cost
        place=AsyncMock(side_effect=[None,dict(price=exit_price,filled_qty=.25,order_id=f"{i}-a"),dict(price=exit_price,filled_qty=.75,order_id=f"{i}-b")])
        monkeypatch.setattr(pt,"place",place)
        assert await monitor.close_tracked("XRP/USDT","audit",price=exit_price,tab="paper") is None
        assert monitor.snapshot_open()[0]["qty"]==1.
        assert await monitor.close_tracked("XRP/USDT","audit",price=exit_price,tab="paper") is None
        assert monitor.snapshot_open()[0]["qty"]==.75
        assert await monitor.close_tracked("XRP/USDT","audit",price=exit_price,tab="paper")==exit_price
        assert not monitor.snapshot_open()
        assert not ps._portfolio["paper"]["positions"]
        assert [c.kwargs["qty"] for c in place.call_args_list]==[1.,1.,.75]
        ids=[c.kwargs["client_order_id"] for c in place.call_args_list]
        assert ids[0]==ids[1] and ids[1]!=ids[2]
        assert not set(ids).intersection(client_ids)
        client_ids.extend(ids)
        expected_cash+=(2 if win else -2)-modelled_fee(100.).cost-modelled_fee(exit_price).cost
        assert ps._portfolio["paper"]["cash"]==pytest.approx(expected_cash)
        assert len(closed)==i+1
    assert monitor._persist_closed_trade.await_count==80
    booked=sum(call.args[2] for call in monitor._persist_closed_trade.call_args_list)
    assert booked==pytest.approx(expected_cash-1000.)
