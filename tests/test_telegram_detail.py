"""What the alert says, not just where it goes.

The operator runs this agent unattended and reads its Telegram channel instead of
a dashboard. The messages carried the fill and almost nothing else: the ENTRY
said "SELL (SHORT) 155 @ 121.37", and the CLOSE DID NOT STATE THE DIRECTION AT
ALL — so scrolling a channel you could not tell which way a closed position had
been facing without reading entry and exit and doing the subtraction yourself.

Everything added here is a FACT ALREADY IN THE SYSTEM at the moment the message
is built, not a derivation of market data. Leverage, stop, target and strategy
live on `TarApprovedEvent`, one hop before the fill; hold time, direction and
realised P&L are on `PositionClosedEvent`.

THE JOIN IS SAFE, NOT LUCKY. `MessageBus.publish` queues a publish made while a
delivery is in flight rather than recursing into it, so TAR_APPROVED reaches
EVERY subscriber before ORDER_FILLED reaches any. That is the same guarantee
`position_monitor._pending` depends on, and it is why this cache does not depend
on the order agents happen to be constructed in `main.py`.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest

from backend.models.events import (
    OrderFilledEvent,
    PositionClosedEvent,
    TarApprovedEvent,
)
from backend.services.telegram_notifier import TelegramNotifier


def _notifier():
    n = TelegramNotifier()
    n._token = "stub"
    n._chats = {"paper": "-100", "real": "-200"}
    sent: list = []

    async def _send(tab, text):
        sent.append((tab, text))

    async def _wr(tab):
        return (6, 11)

    async def _bal(tab):
        return 25_073.71

    n._send = _send
    n._win_rate = _wr
    n._balance = _bal
    return n, sent


def _approve(n, tar, **kw):
    base = dict(
        tar_id=tar, symbol="SOL/USDT", direction="SHORT", approved_size=155.06,
        approved_leverage=3, cro_rationale="ok", stop_loss=123.4991,
        take_profit=116.9617, tab="paper", strategy="Range",
        entry_context="SOL/USDT @ 15m: RSI(14)=61.4, ATR(14)=0.872, regime=Range",
    )
    base.update(kw)
    asyncio.run(n.handle_event(TarApprovedEvent(**base)))


def _fill(n, tar, **kw):
    base = dict(
        tar_id=tar, exchange="x", order_id="o", symbol="SOL/USDT", side="sell",
        tab="paper", fill_price=121.37, fill_quantity=155.06, slippage_bps=1.2,
        fee=9.41,
    )
    base.update(kw)
    asyncio.run(n._on_entry(OrderFilledEvent(**base)))


def _close(n, tar, **kw):
    base = dict(
        trade_id=str(tar), symbol="SOL/USDT", side="sell", tab="paper",
        entry_price=121.37, exit_price=120.5609, quantity=155.06,
        realized_pnl=106.71, exit_reason="profit-target", held_seconds=4520.0,
        strategy="Range", strategies=["Range"],
    )
    base.update(kw)
    asyncio.run(n._on_close(PositionClosedEvent(**base)))


def _text(sent, i=0):
    return re.sub(r"</?[bi]>", "", sent[i][1])


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------

def test_the_entry_leads_with_the_direction():
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    assert _text(sent).splitlines()[0].startswith(("🔴", "🟢"))
    assert "SHORT OPENED" in _text(sent)


def test_the_close_states_the_direction_which_it_never_used_to():
    """The gap that prompted this: a CLOSE message named the symbol, the reason
    and the P&L, and never said whether the position had been long or short."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    _close(n, tar)
    assert "SHORT CLOSED" in _text(sent, 1)


def test_a_long_reads_as_long_on_both_messages():
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar, direction="LONG")
    _fill(n, tar, side="buy")
    _close(n, tar, side="buy", exit_price=122.5, realized_pnl=175.0)
    assert "LONG OPENED" in _text(sent, 0)
    assert "LONG CLOSED" in _text(sent, 1)


# ---------------------------------------------------------------------------
# The trade's terms
# ---------------------------------------------------------------------------

def test_the_entry_reports_leverage_notional_and_margin():
    """Notional is what the position controls; margin is what it costs the
    account. Showing one without the other is how "10k at 5x" gets misread in
    either direction."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    text = _text(sent)
    assert "18,819.63" in text          # 155.06 x 121.37
    assert "6,273.21" in text           # /3
    assert "3x" in text


def test_the_entry_reports_the_stop_and_target_WITH_distances():
    """A bare price says nothing without knowing how far away it is."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    text = _text(sent)
    assert "Stop: 123.499 (1.75% away)" in text
    assert "Target: 116.962 (3.63% away)" in text
    assert "Risk/reward: 1:2.1" in text


def test_the_entry_carries_the_reasoning_snapshot():
    """The Risk Gateway's entry context is what turns "the bot sold SOL" into
    something the operator can agree or disagree with."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    assert "RSI(14)=61.4" in _text(sent)
    assert "Strategy: Range" in _text(sent)


def test_the_close_reports_the_return_on_margin_and_the_hold_time():
    """"+106.71" means something different on 1,000 of margin than on 20,000, and
    the raw figure alone cannot say which."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    _close(n, tar)
    text = _text(sent, 1)
    assert "+1.70% of margin" in text
    assert "Held: 75m" in text
    assert "(+0.67%)" in text            # the price move, signed for a short


def test_a_loss_reads_as_a_loss_in_both_figures():
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar, direction="LONG")
    _fill(n, tar, side="buy")
    _close(n, tar, side="buy", exit_price=119.0, realized_pnl=-370.0,
           exit_reason="stop-loss")
    text = _text(sent, 1)
    assert "-370.00" in text and "-5.90% of margin" in text
    assert "(-1.95%)" in text


def test_a_break_even_close_is_neither_a_win_nor_a_loss():
    """The operator spent weeks removing 0.00 exits; a green tick on one would
    misreport the thing they were trying to eliminate."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    _close(n, tar, exit_price=121.37, realized_pnl=0.0)
    first = _text(sent, 1).splitlines()[0]
    assert "✅" not in first and "🔻" not in first


# ---------------------------------------------------------------------------
# Degradation and safety
# ---------------------------------------------------------------------------

def test_a_fill_with_no_cached_approval_still_sends():
    """A restart between approval and fill loses the cache. The message must lose
    the TERMS, never the alert — and must not invent a leverage."""
    n, sent = _notifier()
    _fill(n, uuid.uuid4())
    text = _text(sent)
    assert "SHORT OPENED" in text and "SOL/USDT" in text
    assert "Margin" not in text and "Stop:" not in text


def test_the_win_rate_names_the_book_it_measured():
    """Paper and real go to different channels, and a rate that does not say
    which book it counted is one glance away from being read as the other."""
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    _close(n, tar)
    assert "on paper" in _text(sent, 1)


def test_a_real_trade_never_reaches_the_paper_channel():
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar, tab="real")
    _fill(n, tar, tab="real")
    assert sent[0][0] == "real"


def test_the_approval_cache_is_bounded():
    """A TAR that is approved and never fills would otherwise sit here forever on
    a 24/7 process."""
    n, _ = _notifier()
    for _ in range(n._MAX_PENDING + 20):
        _approve(n, uuid.uuid4())
    assert len(n._approved) <= n._MAX_PENDING


def test_a_closed_position_is_dropped_from_the_cache():
    n, sent = _notifier()
    tar = uuid.uuid4()
    _approve(n, tar)
    _fill(n, tar)
    assert str(tar) in n._approved
    _close(n, tar)
    assert str(tar) not in n._approved


def test_caching_an_approval_never_raises_into_the_bus():
    """A notifier must not be able to break event delivery — the same rule the
    fire-and-forget send follows."""
    n, _ = _notifier()

    class Broken:
        event_type = "TAR_APPROVED"

        def __getattr__(self, name):
            raise RuntimeError("boom")

    asyncio.run(n.handle_event(Broken()))
