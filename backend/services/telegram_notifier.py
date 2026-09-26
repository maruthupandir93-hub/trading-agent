"""Telegram alerts on entry and close — one channel for paper, one for real.

WHY THIS EXISTS
===============
Nobody watches a dashboard at 3am, and this system is meant to run 24/7 unattended.
The operator asked for two things, and only two:

  * on every ENTRY (the agent opens a position, buy or sell) — a message;
  * on every CLOSE — a message carrying the realised P&L, the CURRENT WIN RATE and
    the CURRENT TOTAL BALANCE.

Paper and real go to DIFFERENT channels, because they are different money and
mixing a paper fill into the channel you watch for real ones is exactly how a
paper result gets mistaken for a real one at a glance.

WHERE IT HOOKS IN, AND WHY THOSE TWO EVENTS
===========================================
`ORDER_FILLED` is published in exactly one place — `execution_agent`'s OPEN path.
`close_position` returns a fill price and publishes nothing; the CLOSE is announced
by `POSITION_CLOSED` from the monitor, which carries the realised P&L. So the two
events map cleanly onto "entry" and "close" with no open/close ambiguity to
disentangle. (Manual operator-panel trades do not flow through `ORDER_FILLED` —
a human clicking Buy already knows they did; this is for the autonomous agent the
operator is NOT watching.)

IT NEVER BLOCKS THE TRADING BUS, AND NEVER RAISES INTO IT
=========================================================
The bus delivers an event to every subscriber in turn, so a subscriber that
awaited a ~200ms Telegram POST would delay `POSITION_CLOSED` reaching the
reflection agent and everything behind it. So the send is FIRE-AND-FORGET: the
handler schedules the HTTP call and returns immediately, and the call has its own
timeout and swallows every error. A missed notification is a missed notification;
it must never slow or break a trade. This mirrors invariant 4's spirit — nothing
peripheral may interfere with the money path.

DISABLED BY DEFAULT, AND SILENT WHEN IT IS
==========================================
No bot token, or no chat id for a tab, means that tab simply is not notified. No
error, no retry, no noise. Configure with:

    TELEGRAM_BOT_TOKEN=123456:ABC...        (one bot, from @BotFather)
    TELEGRAM_CHAT_ID_PAPER=-1001234567890   (the paper channel/chat)
    TELEGRAM_CHAT_ID_REAL=-1009876543210    (the real channel/chat)

A tab with a configured chat is notified; a tab without one is skipped. So paper-
only is a valid setup (leave the real chat unset until you go live).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"
# A hard timeout on the notification call. Generous enough for a normal POST,
# short enough that a hung Telegram endpoint cannot pile up background tasks.
_SEND_TIMEOUT_S = 10.0


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


class TelegramNotifier:
    """Subscribes to ORDER_FILLED and POSITION_CLOSED; sends per-tab messages."""

    # WHY AN APPROVAL CACHE, AND WHY IT IS NOT ORDERING-DEPENDENT.
    #
    # `OrderFilledEvent` carries the fill and nothing about the trade's TERMS —
    # no leverage, no stop, no target, no strategy. Those live on
    # `TarApprovedEvent`, one hop earlier. So a message built from the fill alone
    # can only say "bought 155 SOL", which is the least useful half of what an
    # operator woken at 3am needs to know.
    #
    # The join is SAFE rather than lucky. `MessageBus.publish` queues a publish
    # made while a delivery is in flight instead of recursing into it, so
    # TAR_APPROVED reaches EVERY subscriber before ORDER_FILLED reaches any —
    # the same guarantee `position_monitor._pending` relies on, and the reason
    # that ordering fix is described in CLAUDE.md as load-bearing. This cache
    # therefore does not depend on the order agents are constructed in `main.py`.
    #
    # Bounded, and entries are dropped on close: a TAR that is approved and never
    # fills would otherwise sit here forever on a 24/7 process.
    _MAX_PENDING = 64

    def __init__(self) -> None:
        self._approved: Dict[str, Dict[str, Any]] = {}
        self._token = _env("TELEGRAM_BOT_TOKEN")
        # One chat id per tab. Routing is by the event's own `tab`, so a real fill
        # can never land in the paper channel or vice versa.
        self._chats: Dict[str, str] = {
            "paper": _env("TELEGRAM_CHAT_ID_PAPER"),
            "real": _env("TELEGRAM_CHAT_ID_REAL"),
        }

    # -- configuration ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when a token AND at least one tab's chat are configured."""
        return bool(self._token and any(self._chats.values()))

    def status(self) -> Dict[str, Any]:
        """For the monitoring API. Never returns the token."""
        return {
            "enabled": self.enabled,
            "tokenConfigured": bool(self._token),
            "channels": {tab: bool(chat) for tab, chat in self._chats.items()},
            "note": (
                "Sends an entry message on every agent ORDER_FILLED and a close "
                "message (with win rate and total balance) on every POSITION_CLOSED, "
                "routed to the paper or real channel by the trade's tab."
                if self.enabled else
                "Disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID_PAPER and/or "
                "TELEGRAM_CHAT_ID_REAL to enable. A tab with no chat id is not notified."
            ),
        }

    # -- the two events --------------------------------------------------

    async def handle_event(self, event: Any) -> None:
        """Fire-and-forget the right message. Never blocks, never raises.

        Scheduling the work as a task and returning is what keeps a slow Telegram
        endpoint off the trading bus's critical path.
        """
        if not self.enabled:
            return
        try:
            etype = getattr(event, "event_type", None)
            if etype == "TAR_APPROVED":
                # Recorded SYNCHRONOUSLY, not scheduled: the fill follows on the
                # same bus and a task that had not run yet would miss it. It is a
                # dict assignment, so it costs the bus nothing.
                self._remember_approval(event)
            elif etype == "ORDER_FILLED":
                asyncio.create_task(self._safe(self._on_entry(event)))
            elif etype == "POSITION_CLOSED":
                asyncio.create_task(self._safe(self._on_close(event)))
        except Exception as exc:  # noqa: BLE001 - a notifier must not break delivery
            logger.debug("Telegram notifier could not schedule a message: %s", exc)

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram notification failed (ignored): %s", exc)

    def _remember_approval(self, event: Any) -> None:
        """Keep the approved terms so the fill message can report them."""
        try:
            tar_id = str(getattr(event, "tar_id", "") or "")
            if not tar_id:
                return
            if len(self._approved) >= self._MAX_PENDING:
                self._approved.pop(next(iter(self._approved)), None)
            self._approved[tar_id] = {
                "leverage": getattr(event, "approved_leverage", None),
                "stop_loss": getattr(event, "stop_loss", None),
                "take_profit": getattr(event, "take_profit", None),
                "strategy": getattr(event, "strategy", None),
                "entry_context": getattr(event, "entry_context", None),
                "run_id": getattr(event, "run_id", None),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("Telegram notifier could not cache an approval: %s", exc)

    @staticmethod
    def _direction(side: str) -> str:
        return "LONG" if side == "buy" else "SHORT" if side == "sell" else str(side).upper()

    @staticmethod
    def _held(seconds: Optional[float]) -> Optional[str]:
        """Hold time in the largest unit that still reads naturally."""
        if seconds is None:
            return None
        s = float(seconds)
        if s < 90:
            return f"{s:.0f}s"
        if s < 5400:
            return f"{s / 60:.0f}m"
        if s < 172800:
            return f"{s / 3600:.1f}h"
        return f"{s / 86400:.1f}d"

    async def _on_entry(self, event: Any) -> None:
        tab = getattr(event, "tab", "paper")
        side = getattr(event, "side", "?")
        direction = self._direction(side)
        emoji = "\U0001F7E2" if side == "buy" else "\U0001F534"  # green / red circle
        symbol = getattr(event, "symbol", "?")
        qty = getattr(event, "fill_quantity", None)
        price = getattr(event, "fill_price", None)
        fee = getattr(event, "fee", None)
        slippage = getattr(event, "slippage_bps", None)
        terms = self._approved.get(str(getattr(event, "tar_id", "") or "")) or {}

        # DIRECTION LEADS THE MESSAGE. It used to be a parenthetical after the
        # raw side — "SELL (SHORT)" — and the close message did not state it at
        # all, so an operator scrolling a channel could not tell which way a
        # position was facing without reading entry and exit prices and doing the
        # subtraction.
        lines = [
            f"{emoji} <b>{direction} OPENED</b> · {tab.upper()}",
            f"<b>{symbol}</b>",
        ]

        if qty is not None and price is not None:
            notional = qty * price
            lines.append(f"Size: {qty:g} @ {price:g}")
            lev = terms.get("leverage")
            if lev:
                # Margin is what the position actually costs the account;
                # notional is what it controls. Showing only one of them is how
                # "10k at 5x" gets misread in either direction.
                lines.append(
                    f"Notional: {notional:,.2f} USDT  |  Margin: "
                    f"{notional / float(lev):,.2f} @ <b>{int(lev)}x</b>"
                )
            else:
                lines.append(f"Notional: {notional:,.2f} USDT")

        # WHAT PROTECTS THIS TRADE AND WHAT PAYS FOR IT, with the distances.
        # A bare price means nothing without knowing how far away it is.
        stop = terms.get("stop_loss")
        target = terms.get("take_profit")
        if price and stop:
            dist = abs(price - stop) / price * 100.0
            lines.append(f"Stop: {stop:g} ({dist:.2f}% away)")
        if price and target:
            dist = abs(target - price) / price * 100.0
            lines.append(f"Target: {target:g} ({dist:.2f}% away)")
        if price and stop and target:
            risk = abs(price - stop)
            reward = abs(target - price)
            if risk > 0:
                lines.append(f"Risk/reward: 1:{reward / risk:.1f}")

        if terms.get("strategy"):
            lines.append(f"Strategy: {terms['strategy']}")
        if fee is not None:
            extra = f"  |  slippage {slippage:.1f} bps" if slippage is not None else ""
            lines.append(f"Entry fee: {fee:,.2f} USDT{extra}")

        # THE REASONING, IN ONE LINE. This is the snapshot the Risk Gateway took
        # at decision time — RSI, ATR, structure, regime. It is what turns "the
        # bot bought SOL" into something an operator can agree or disagree with.
        context = terms.get("entry_context")
        if context:
            lines.append(f"\n<i>{str(context)[:220]}</i>")

        await self._send(tab, "\n".join(lines))

    async def _on_close(self, event: Any) -> None:
        tab = getattr(event, "tab", "paper")
        symbol = getattr(event, "symbol", "?")
        pnl = getattr(event, "realized_pnl", None)
        reason = getattr(event, "exit_reason", "closed")
        entry = getattr(event, "entry_price", None)
        exit_price = getattr(event, "exit_price", None)
        qty = getattr(event, "quantity", None)
        side = getattr(event, "side", "?")
        direction = self._direction(side)
        held = self._held(getattr(event, "held_seconds", None))
        strategy = getattr(event, "strategy", None)
        terms = self._approved.pop(str(getattr(event, "trade_id", "") or ""), {}) or {}

        won = pnl is not None and pnl > 0
        flat = pnl is not None and pnl == 0
        emoji = "\u2705" if won else ("\u26AA" if flat else "\U0001F53B")

        lines = [
            f"{emoji} <b>{direction} CLOSED</b> · {tab.upper()}",
            f"<b>{symbol}</b> — {reason}",
        ]
        if entry is not None and exit_price is not None:
            move = (exit_price - entry) / entry * 100.0 * (1.0 if side == "buy" else -1.0)
            lines.append(f"Entry {entry:g} → exit {exit_price:g} ({move:+.2f}%)")
        if held:
            lines.append(f"Held: {held}")

        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            line = f"P&amp;L: <b>{sign}{pnl:,.2f}</b> USDT"
            # AS A RETURN ON THE MARGIN, which is the number that answers "was
            # that worth it?". A +100 USDT win means something different on 1,000
            # of margin than on 20,000, and the raw figure alone cannot say which.
            lev = terms.get("leverage")
            if qty and entry and lev:
                margin = (qty * entry) / float(lev)
                if margin > 0:
                    line += f"  ({sign}{pnl / margin * 100.0:.2f}% of margin)"
            lines.append(line)

        if strategy:
            lines.append(f"Strategy: {strategy}")

        # THE TWO NUMBERS THE OPERATOR ASKED FOR AT CLOSE TIME.
        wr = await self._win_rate(tab)
        if wr is not None:
            wins, closed = wr
            rate = (wins / closed * 100.0) if closed else 0.0
            lines.append(f"Win rate: <b>{rate:.0f}%</b> ({wins}/{closed} on {tab})")

        balance = await self._balance(tab)
        if balance is not None:
            lines.append(f"Total balance: <b>{balance:,.2f}</b> USDT")

        await self._send(tab, "\n".join(lines))

    # -- data --------------------------------------------------------------

    async def _balance(self, tab: str) -> Optional[float]:
        """Current total equity for the tab — cash + marked positions (paper), or
        the exchange balance (real). None when it cannot be read, never a guess."""
        try:
            from backend.services.trading_session import current_equity

            return await current_equity(tab)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Telegram: could not read %s balance: %s", tab, exc)
            return None

    async def _win_rate(self, tab: str) -> Optional[tuple[int, int]]:
        """(wins, closed) over all closed trades for this tab. None if unreadable.

        A closed trade is a row carrying a pnl — the same rule the P&L dashboard
        uses. Counts across all strategies, which is the 'current win rate' the
        operator wants to see at a glance, not a per-strategy breakdown.
        """
        try:
            from backend.core.db import get_db_pool

            pool = get_db_pool()
            if pool is None:
                return None
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT count(pnl) AS closed, "
                    "count(*) FILTER (WHERE pnl > 0) AS wins "
                    "FROM trades WHERE tab = $1",
                    tab,
                )
            if row is None:
                return None
            return int(row["wins"] or 0), int(row["closed"] or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Telegram: could not read %s win rate: %s", tab, exc)
            return None

    # -- send --------------------------------------------------------------

    async def _send(self, tab: str, text: str) -> None:
        """POST one message to the tab's channel. Never raises.

        A tab with no configured chat is skipped silently — that is how paper-only
        operation works, and how a real fill is dropped rather than cross-posted to
        the paper channel before the real one is set up.
        """
        chat_id = self._chats.get(tab, "")
        if not self._token or not chat_id:
            return

        import httpx

        url = _API.format(token=self._token)
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                # Logged, not raised. The most common causes are a wrong chat id or
                # the bot not being a member of the channel — both worth a line, but
                # neither may interrupt trading.
                logger.warning(
                    "Telegram send to the %s channel failed (HTTP %d): %s. "
                    "Check the chat id and that the bot is a member of that channel.",
                    tab, resp.status_code, resp.text[:200],
                )

    async def send_test(self, tab: str = "paper") -> Dict[str, Any]:
        """Send a one-off test message, for a config check. Returns what happened."""
        if not self.enabled:
            return {"ok": False, "reason": "notifier is not enabled (missing token or chat id)"}
        if not self._chats.get(tab):
            return {"ok": False, "reason": f"no chat id configured for the {tab} channel"}
        try:
            await self._send(tab, f"✅ <b>TradingOS test</b> · {tab.upper()} channel is wired.")
            return {"ok": True, "tab": tab}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}


_notifier: Optional[TelegramNotifier] = None


def get_telegram_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


def reset_telegram_notifier() -> None:
    global _notifier
    _notifier = None


def telegram_status() -> Dict[str, Any]:
    return get_telegram_notifier().status()


def subscribe_telegram_notifier() -> bool:
    """Wire the notifier to the bus. Idempotent-ish; returns whether it subscribed.

    Called from `main.py`'s lifespan. Subscribes even when disabled is pointless,
    so it only subscribes when enabled — a disabled notifier on the bus is just a
    handler that returns immediately on every event.
    """
    notifier = get_telegram_notifier()
    if not notifier.enabled:
        logger.info(
            "Telegram notifier is OFF (set TELEGRAM_BOT_TOKEN and a TELEGRAM_CHAT_ID_* "
            "to enable entry/close alerts)."
        )
        return False

    from backend.core.message_bus import get_message_bus

    bus = get_message_bus()
    bus.subscribe("ORDER_FILLED", notifier.handle_event)
    bus.subscribe("POSITION_CLOSED", notifier.handle_event)
    # The approval carries the trade's TERMS — leverage, stop, target, strategy,
    # entry context — none of which are on the fill. See `_remember_approval`.
    bus.subscribe("TAR_APPROVED", notifier.handle_event)
    channels = [tab for tab, chat in notifier._chats.items() if chat]
    logger.info("Telegram notifier ON for channel(s): %s", ", ".join(channels))
    return True
