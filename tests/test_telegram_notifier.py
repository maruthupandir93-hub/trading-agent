"""Telegram entry/close alerts — routing, content, and never blocking the bus.

The operator asked for exactly two messages: an ENTRY on every agent fill, and a
CLOSE carrying realised P&L + current win rate + current total balance — with
paper and real on SEPARATE channels. This pins that, plus the two properties that
keep it safe: a missing chat id is skipped rather than cross-posted, and a send
never raises into or blocks the trading bus.
"""

from __future__ import annotations

import uuid

import pytest

from backend.services.telegram_notifier import TelegramNotifier, reset_telegram_notifier


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID_PAPER", "TELEGRAM_CHAT_ID_REAL"):
        monkeypatch.delenv(var, raising=False)
    reset_telegram_notifier()
    yield
    reset_telegram_notifier()


def _entry(tab="paper", side="buy"):
    from backend.models.events import OrderFilledEvent

    return OrderFilledEvent(
        tar_id=uuid.uuid4(), exchange="binance", order_id="o1", symbol="SOL/USDT",
        side=side, tab=tab, fill_price=100.5, fill_quantity=29.58,
        slippage_bps=0.0, fee=0.0,
    )


def _close(tab="paper", pnl=-25.44):
    from backend.models.events import PositionClosedEvent

    return PositionClosedEvent(
        trade_id="t1", symbol="SOL/USDT", side="sell", tab=tab,
        entry_price=100.5, exit_price=99.9, quantity=29.58, realized_pnl=pnl,
        exit_reason="stop-loss",
    )


def _configured(monkeypatch, *, paper=True, real=True):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    if paper:
        monkeypatch.setenv("TELEGRAM_CHAT_ID_PAPER", "-100PAPER")
    if real:
        monkeypatch.setenv("TELEGRAM_CHAT_ID_REAL", "-100REAL")
    return TelegramNotifier()


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


def test_disabled_without_a_token():
    assert TelegramNotifier().enabled is False


def test_disabled_with_a_token_but_no_chats(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    assert TelegramNotifier().enabled is False


def test_paper_only_is_a_valid_configuration(monkeypatch):
    n = _configured(monkeypatch, paper=True, real=False)
    assert n.enabled is True
    assert n.status()["channels"] == {"paper": True, "real": False}


def test_status_never_leaks_the_token(monkeypatch):
    n = _configured(monkeypatch)
    import json

    assert "123:ABC" not in json.dumps(n.status())


# ---------------------------------------------------------------------------
# Routing and content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_entry_message_goes_to_the_tab_channel(monkeypatch):
    n = _configured(monkeypatch)
    sent: list = []

    async def _fake_send(tab, text):
        sent.append((tab, text))

    monkeypatch.setattr(n, "_send", _fake_send)

    await n._on_entry(_entry(tab="paper", side="buy"))
    assert len(sent) == 1
    tab, text = sent[0]
    assert tab == "paper"
    assert "ENTRY" in text and "PAPER" in text
    assert "SOL/USDT" in text and "BUY" in text and "LONG" in text


@pytest.mark.asyncio
async def test_a_sell_entry_reads_as_short(monkeypatch):
    n = _configured(monkeypatch)
    sent: list = []
    monkeypatch.setattr(n, "_send", lambda tab, text: sent.append((tab, text)) or _noop())
    await n._on_entry(_entry(tab="real", side="sell"))
    assert sent[0][0] == "real"
    assert "SHORT" in sent[0][1]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_a_close_message_carries_pnl_winrate_and_balance(monkeypatch):
    n = _configured(monkeypatch)
    sent: list = []

    async def _fake_send(tab, text):
        sent.append((tab, text))

    monkeypatch.setattr(n, "_send", _fake_send)
    # Stub the two data reads so the test is offline and deterministic.
    monkeypatch.setattr(n, "_win_rate", lambda tab: _val((3, 9)))
    monkeypatch.setattr(n, "_balance", lambda tab: _val(9876.12))

    await n._on_close(_close(tab="paper", pnl=-25.44))
    tab, text = sent[0]
    assert tab == "paper"
    assert "CLOSE" in text
    assert "-25.44" in text                 # realised P&L
    assert "33%" in text and "3/9" in text   # current win rate
    assert "9,876.12" in text                # current total balance


async def _val(v):
    return v


@pytest.mark.asyncio
async def test_the_close_still_sends_when_winrate_or_balance_is_unreadable(monkeypatch):
    """The P&L line must arrive even if the DB/balance read fails — a missing
    number is omitted, never faked, and never suppresses the whole message."""
    n = _configured(monkeypatch)
    sent: list = []
    monkeypatch.setattr(n, "_send", lambda tab, text: sent.append(text) or _noop())
    monkeypatch.setattr(n, "_win_rate", lambda tab: _val(None))
    monkeypatch.setattr(n, "_balance", lambda tab: _val(None))

    await n._on_close(_close(pnl=12.5))
    assert sent
    assert "+12.50" in sent[0]
    assert "Win rate" not in sent[0]        # omitted, not faked
    assert "Total balance" not in sent[0]


# ---------------------------------------------------------------------------
# The two safety properties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tab_with_no_chat_is_SKIPPED_not_cross_posted(monkeypatch):
    """The point of two channels: a real fill must never land in the paper channel
    just because the real one is not set up yet."""
    n = _configured(monkeypatch, paper=True, real=False)  # no real chat
    posted: list = []

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json):
            posted.append(json["chat_id"])
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    await n._send("real", "hello")          # no real chat -> nothing sent
    assert posted == []
    await n._send("paper", "hello")         # paper chat set -> sent
    assert posted == ["-100PAPER"]


@pytest.mark.asyncio
async def test_a_failing_send_never_raises(monkeypatch):
    n = _configured(monkeypatch)

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    # _safe wraps it; must swallow.
    await n._safe(n._send("paper", "hi"))


@pytest.mark.asyncio
async def test_handle_event_returns_immediately_when_disabled():
    """No token configured: handling an event is a no-op, not an error."""
    n = TelegramNotifier()
    await n.handle_event(_entry())   # must not raise, must not send


@pytest.mark.asyncio
async def test_handle_event_schedules_and_does_not_block(monkeypatch):
    """Fire-and-forget: handle_event returns without awaiting the send, so a slow
    Telegram endpoint cannot delay the trading bus."""
    import asyncio

    n = _configured(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_entry(event):
        started.set()
        await release.wait()          # would hang forever if awaited inline

    monkeypatch.setattr(n, "_on_entry", _slow_entry)

    await n.handle_event(_entry())    # returns immediately despite the slow send
    await asyncio.sleep(0)            # let the scheduled task start
    assert started.is_set()
    release.set()
