"""Batch 6: the tail, and most of it is one value reaching the wrong reader.

`trade_id` stopped meaning the TAR and two readers went on using it as one.
Reconciliation's loop woke up for the mirrored book and then compared nothing.
The order watcher connected to an unrelated account. A stop-side check pointed
at a field that does not exist while the file it deferred to pointed back. The
backtest's holding floor moved with the DATA instead of the live cadence. A
streamed run accumulated nine specialists with a flat `dict.update`.

Every one of them produces a plausible answer rather than an error, which is
why a green suite sat on top of all six.
"""

from __future__ import annotations

import inspect
import os

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# #14 - the TAR stopped travelling with the close
# ---------------------------------------------------------------------------

def test_the_closed_event_declares_its_tar_separately_from_the_trade_row():
    """F88 repointed `trade_id` at the CLOSING ROW's id, for a good reason --
    `reflections.trade_id` is a foreign key into `trades` and a `manual-<hex>`
    TAR is not in that table. But two readers were using `trade_id` AS the TAR
    and were silently handed a different identifier."""
    from backend.models.events import PositionClosedEvent

    assert "tar_id" in PositionClosedEvent.model_fields

    event = PositionClosedEvent(
        trade_id="row-1", tar_id="tar-1", symbol="SOL/USDT", side="buy",
        tab="paper", entry_price=100.0, exit_price=101.0, quantity=1.0,
        realized_pnl=1.0, exit_reason="take-profit",
    )
    assert event.tar_id == "tar-1" and event.trade_id == "row-1", (
        "declared but does not round-trip -- the TarApprovedEvent footgun again"
    )


def test_the_monitor_publishes_the_tar_alongside_the_row_id():
    import backend.agents.position_monitor as pm

    src = inspect.getsource(pm.PositionMonitorAgent._close)
    assert "tar_id=pos.tar_id" in src


def test_the_telegram_close_joins_the_approval_on_the_tar():
    """The cache is keyed by tar_id at TAR_APPROVED. Popping `trade_id` missed
    on every close since F88, so the P&L line lost its "% of margin" -- the one
    figure that says whether the result was worth the capital -- and the cached
    entry was never dropped."""
    import backend.services.telegram_notifier as tn

    # RAW, not `code_only`: the key is a STRING LITERAL, and `code_only` strips
    # those. The sixth time a source-scanning test here has had to choose
    # between the two, and the first where stripping is the wrong choice.
    src = inspect.getsource(tn.TelegramNotifier._on_close)
    assert 'getattr(event, "tar_id"' in src, (
        "the approval cache is still keyed on the trade row id"
    )


async def _none():
    return None


@pytest.mark.asyncio
async def test_the_margin_percentage_survives_a_close():
    """Driven, because the symptom was a MISSING LINE rather than a wrong one,
    and a missing line is exactly what a source scan cannot see."""
    import backend.services.telegram_notifier as tn

    notifier = tn.TelegramNotifier()
    sent: list = []

    async def _capture(tab, text):
        sent.append(text)

    notifier._send = _capture                      # type: ignore[assignment]
    notifier._win_rate = lambda tab: _none()       # type: ignore[assignment]
    notifier._balance = lambda tab: _none()        # type: ignore[assignment]

    class Approval:
        tar_id = "tar-9"
        approved_leverage = 10
        stop_loss = 99.0
        take_profit = 110.0
        strategy = "Breakout"
        entry_context = None
        run_id = None

    notifier._remember_approval(Approval())

    class Closed:
        tab = "paper"
        symbol = "SOL/USDT"
        realized_pnl = 50.0
        exit_reason = "take-profit"
        entry_price = 100.0
        exit_price = 105.0
        quantity = 10.0
        side = "buy"
        held_seconds = 600.0
        strategy = "Breakout"
        trade_id = "row-9"      # NOT the tar id, which is the whole point
        tar_id = "tar-9"

    await notifier._on_close(Closed())
    assert sent, "no close message was produced"
    assert "% of margin" in sent[0], (
        "the approval was not found, so the P&L line lost the figure that says "
        "whether the result was worth the capital"
    )


def test_the_reflection_receipt_carries_the_tar_not_the_row():
    """`assess_execution_quality` looks the receipt's `tar_id` up in
    `execution_quality.tar_id`. Since F88 it was given a trade row id, so the
    join could never match and every reflection graded its own execution
    'unavailable' -- which reads as 'no reference price was recorded'."""
    import backend.agents.reflection_agent as ra

    src = inspect.getsource(ra.ReflectionAgent._reflect_on_close)
    assert 'getattr(event, "tar_id", None) or event.trade_id' in src


# ---------------------------------------------------------------------------
# #16 - the mirrored book had no observer at all
# ---------------------------------------------------------------------------

class _Monitor:
    def __init__(self, rows):
        self._rows = rows

    def snapshot_open(self):
        return list(self._rows)


@pytest.mark.asyncio
async def test_reconciliation_compares_the_mirrored_paper_book(monkeypatch):
    """F7 made the LOOP wake up for a mirrored book. `reconcile()` still
    filtered to `tab == "real"` and still asked the MAINNET venue, so on a
    mirror-only deployment it compared an empty list against an account that
    has never heard of these positions -- the comparison that would have caught
    F1 on its first minute still did not happen."""
    import backend.services.reconciliation as rec
    from backend.services import paper_testnet

    async def _flat(symbol):
        return True

    monkeypatch.setattr(paper_testnet, "active", lambda: True)
    monkeypatch.setattr(paper_testnet, "venue_flat", _flat)

    out = await rec._reconcile_mirror(_Monitor([{
        "symbol": "XRP/USDT", "tab": "paper", "qty": 79.5,
        "protection": "venue", "stopLoss": 1.39,
    }]))

    assert len(out) == 1
    assert out[0].kind == "missing_at_venue"
    assert out[0].severity == "critical"
    assert out[0].symbol == "XRP/USDT"


@pytest.mark.asyncio
async def test_an_unreadable_mirror_is_not_a_phantom(monkeypatch):
    """None means "could not ask", NOT "the venue holds nothing". This codebase
    has now paid for that distinction four times."""
    import backend.services.reconciliation as rec
    from backend.services import paper_testnet

    async def _unknown(symbol):
        return None

    monkeypatch.setattr(paper_testnet, "active", lambda: True)
    monkeypatch.setattr(paper_testnet, "venue_flat", _unknown)

    rows = [{"symbol": "XRP/USDT", "tab": "paper", "protection": "venue"}]
    assert await rec._reconcile_mirror(_Monitor(rows)) == []


@pytest.mark.asyncio
async def test_a_paper_position_with_no_venue_order_is_not_compared(monkeypatch):
    """A paper position opened while the mirror was OFF has no counterpart at
    the demo venue. Asking about it would report a phantom for a position that
    was never supposed to be there."""
    import backend.services.reconciliation as rec
    from backend.services import paper_testnet

    monkeypatch.setattr(paper_testnet, "active", lambda: True)

    rows = [{"symbol": "XRP/USDT", "tab": "paper", "protection": "in-process"}]
    assert await rec._reconcile_mirror(_Monitor(rows)) == []


def test_the_order_stream_refuses_the_binance_demo_rather_than_watching_mainnet():
    """`venue_backed_book_exists()` is true for two different reasons and
    `_build_client` handled one. With the mirror on and LIVE_TRADING off it
    built a client for the CONFIGURED venue with MAINNET credentials and
    watched an account whose orders have nothing to do with this book --
    reporting itself "connected" the whole time.

    ccxt hard-refuses sandbox mode for binance futures, which is why
    `services/binance_testnet` is a direct REST client with one hardcoded host.
    There is no socket to connect to, and connecting to mainnet instead is the
    one outcome worse than not connecting.
    """
    import backend.services.order_stream as os_mod

    src = code_only(inspect.getsource(os_mod.OrderStream._build_client))
    assert "paper_testnet" in src, "the mirror's venue is still not consulted"
    assert "LIVE_TRADING" in src


# ---------------------------------------------------------------------------
# #22 - a check that pointed at a field that does not exist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side,entry,stop,faulty", [
    ("buy", 100.0, 90.0, False),
    ("buy", 100.0, 110.0, True),
    ("sell", 100.0, 110.0, False),
    ("sell", 100.0, 90.0, True),
    ("buy", 100.0, None, False),     # no stop is a different fault, not this one
])
def test_the_stop_side_is_actually_checked(side, entry, stop, faulty):
    from backend.agents.position_monitor import stop_side_fault

    assert (stop_side_fault(side, entry, stop) is not None) is faulty


def test_neither_request_model_declares_the_field_its_validator_reads():
    """THE EVIDENCE THAT THE OLD CHECK COULD NEVER RUN. Both validators read
    `limitPrice` / `price` and neither model has either, so `entry` was always
    None and both returned on their first line -- while each one's comment said
    the OTHER layer would catch it."""
    from backend.api.operator_exchange import PlaceOrderRequest
    from backend.api.operator_trade import PaperTradeRequest

    for model in (PlaceOrderRequest, PaperTradeRequest):
        fields = set(model.model_fields)
        assert "limitPrice" not in fields and "price" not in fields, (
            f"{model.__name__} gained a price field -- the validator may now be "
            f"live, and this test's premise needs rechecking"
        )


@pytest.mark.asyncio
async def test_a_manual_position_with_an_unusable_stop_is_tracked_but_not_protected():
    """It RECORDS rather than refuses, and the reason is the same one a refused
    resting stop follows: by the time an exchange-routed manual trade reaches
    the monitor the position is already open, and refusing to track it would
    leave it open AND unwatched."""
    import backend.agents.position_monitor as pm

    monitor = pm.PositionMonitorAgent()

    async def _no_persist():
        return None

    monitor.persist_watch_list = _no_persist      # type: ignore[assignment]

    tar = await monitor.track_manual_position(
        symbol="SOL/USDT", side="buy", qty=1.0,
        entry_price=100.0, stop_loss=110.0, tab="paper",
    )
    assert tar in monitor._open
    assert monitor._open[tar].protection == pm.PROTECTION_UNPROTECTED


@pytest.mark.asyncio
async def test_a_sound_manual_stop_is_not_flagged():
    import backend.agents.position_monitor as pm

    monitor = pm.PositionMonitorAgent()

    async def _no_persist():
        return None

    monitor.persist_watch_list = _no_persist      # type: ignore[assignment]

    tar = await monitor.track_manual_position(
        symbol="SOL/USDT", side="buy", qty=1.0,
        entry_price=100.0, stop_loss=95.0, tab="paper",
    )
    assert monitor._open[tar].protection != pm.PROTECTION_UNPROTECTED


def test_the_paper_route_refuses_before_it_books():
    """The one caller that can still refuse at zero cost: it knows the fill
    price before it writes anything."""
    import backend.api.operator_trade as ot

    src = code_only(inspect.getsource(ot))
    assert "stop_side_fault" in src


# ---------------------------------------------------------------------------
# #29 - the backtest's holding floor moved with the DATA
# ---------------------------------------------------------------------------

def test_the_holding_floor_does_not_depend_on_the_candles_a_study_used():
    """It took a `bar_minutes` argument, so on 1h candles the floor became
    8 x 60 = 480 minutes while the live monitor applied 8 x 15 = 120 -- four
    times the horizon, inside the one function written because "a backtest
    applying a different bound from production is precisely the Issue E defect
    this area was fixed for"."""
    from backend.algorithms.strategy_profiles import effective_holding_minutes

    assert "bar_minutes" not in inspect.signature(effective_holding_minutes).parameters


def test_the_floor_is_the_live_cadence():
    from backend.algorithms.strategy_profiles import (
        ANALYSIS_BAR_MINUTES, MIN_HOLDING_BARS, effective_holding_minutes,
    )

    assert effective_holding_minutes("Scalping") == MIN_HOLDING_BARS * ANALYSIS_BAR_MINUTES


def test_the_backtest_asks_for_the_live_bound_without_its_own_interval():
    import backend.core.strategy_backtest as bt

    assert "effective_holding_minutes(name)" in inspect.getsource(bt)


# ---------------------------------------------------------------------------
# #30 - a streamed run lost eight specialists out of nine
# ---------------------------------------------------------------------------

def test_the_reducers_are_discovered_from_the_state_itself():
    """Derived rather than listed, so a field that gains or loses a reducer
    cannot leave a second copy stale. And `get_type_hints`, not
    `__annotations__`: with `from __future__ import annotations` the raw dict
    holds STRINGS, so a scan of it finds no `Annotated` at all and returns an
    empty map -- a lookup that always misses, which is the very bug being
    fixed."""
    from backend.graphs.state import state_reducers

    found = state_reducers()
    assert "specialist_findings" in found
    assert "unavailable" in found
    assert "errors" in found


def test_parallel_specialists_all_survive_the_merge():
    """The nine specialists run in ONE superstep and each writes
    `specialist_findings`. A flat `dict.update` kept whichever the loop saw
    last, so the `done` summary reported one specialist out of nine."""
    from backend.graphs.state import SpecialistFinding, merge_delta

    acc: dict = {}
    for name in ("market", "orderflow", "liquidity", "news"):
        merge_delta(acc, {"specialist_findings": [
            SpecialistFinding(
                specialist=name, role="directional", available=True, evidence=[],
            ),
        ]})

    assert len(acc["specialist_findings"]) == 4, (
        "the merge still discards every finding but the last"
    )


def test_an_unreduced_key_is_replaced_exactly_as_langgraph_replaces_it():
    from backend.graphs.state import merge_delta

    acc = {"symbol": "SOL/USDT"}
    merge_delta(acc, {"symbol": "XRP/USDT"})
    assert acc["symbol"] == "XRP/USDT"


def test_the_stream_runner_uses_the_reducers():
    import backend.graphs.runtime as rt

    src = code_only(inspect.getsource(rt.stream_run))
    assert "merge_delta" in src
    assert "accumulated . update ( delta )" not in src.replace("  ", " ")


# ---------------------------------------------------------------------------
# #31 - the SECOND corruption was permanent
# ---------------------------------------------------------------------------

def test_a_repeat_corruption_still_clears_the_live_path(tmp_path, monkeypatch):
    """The quarantine used a FIXED `.corrupt` name and skipped the move when
    one existed, so a second corruption left the damaged file in place.
    `_load_memory` then returned {} forever and `record_closed_trade` begins
    `if not memory: return` -- so every close after that was silently dropped
    and the win rate, the Kelly input and the DAILY-LOSS CHECK stayed frozen at
    zero for the life of the deployment.
    """
    import backend.services.ai_memory as mem

    path = tmp_path / "ai_memory.json"
    monkeypatch.setattr(mem, "MEMORY_FILE", str(path))

    for i in range(3):
        monkeypatch.setattr(mem, "_quarantine_path", lambda i=i: f"{path}.corrupt.{i}")
        path.write_text("{ this is not json", encoding="utf-8")
        assert mem._load_memory() == {}, "a corrupt file must not be read as data"
        assert not path.exists(), (
            "the damaged file was left at the live path, so every subsequent "
            "load returns {} and nothing can ever write again"
        )
        # The next load now takes the does-not-exist branch and returns a
        # usable skeleton, which is what makes recovery automatic.
        assert mem._load_memory()["global_stats"]["total_trades"] == 0


def test_quarantine_copies_are_bounded(tmp_path, monkeypatch):
    """A process corrupting the file every minute must not fill a disk with
    copies of it."""
    import backend.services.ai_memory as mem

    path = tmp_path / "ai_memory.json"
    monkeypatch.setattr(mem, "MEMORY_FILE", str(path))

    for i in range(mem.MAX_QUARANTINE_COPIES + 4):
        # The clock has one-second resolution, so each incident's name is
        # forced rather than slept for.
        monkeypatch.setattr(mem, "_quarantine_path", lambda i=i: f"{path}.corrupt.{1000 + i}")
        path.write_text(f"not json {i}", encoding="utf-8")
        mem._load_memory()

    copies = [f for f in os.listdir(tmp_path) if ".corrupt" in f]
    assert len(copies) <= mem.MAX_QUARANTINE_COPIES


# ---------------------------------------------------------------------------
# Review Issue 5 - SAFE / UNSAFE / UNKNOWN
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var_fraction,drawdown,verdict", [
    (0.0375, 0.30, "SAFE"),
    (0.12, 0.30, "UNSAFE"),
    (0.0, 0.30, "UNKNOWN"),
    (0.05, 0.0, "UNKNOWN"),
])
def test_the_three_outcomes_are_distinct(var_fraction, drawdown, verdict):
    """`ok` alone collapsed "measured and coherent" with "could not establish
    the risk budget at all". That collapse is right for reporting and for paper
    research and wrong for authorising a live entry: the point of the check is
    that the operator CHOSE a coherent policy, and an unknown policy has not
    been chosen."""
    from backend.services.risk_coherence import assess

    out = assess(var_fraction=var_fraction, drawdown_limit=drawdown)
    assert out.verdict == verdict


def test_unknown_passes_paper_and_fails_live():
    from backend.services.risk_coherence import assess

    out = assess(var_fraction=0.0, drawdown_limit=0.30)
    assert out.ok is True, "an unmeasurable setting is not evidence of an unsafe one"
    assert out.live_ok is False, "an unknown risk budget is not a chosen one"


def test_a_real_session_requires_the_stricter_verdict():
    import backend.services.trading_session as ts

    src = code_only(inspect.getsource(ts.start_session))
    assert "live_ok" in src, "the real book still accepts an unmeasurable configuration"
