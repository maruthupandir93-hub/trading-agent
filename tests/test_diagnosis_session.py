"""Wave 2 of the 2026-10-15 diagnosis: what stopped the agent running 24/7.

F21, F30, F41, F55, F56, F57, F58, F59, F65, and F2's sizing arithmetic. F54's
own tests live in `test_trading_session.py`, beside the one they replaced.

The shared shape of these: a value is carried to the decision point and then
not used, or a transient failure is treated as a settled fact. Both read as
correct code.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest


# ---------------------------------------------------------------------------
# F21 · the rotation must be able to trade every coin it scans
# ---------------------------------------------------------------------------

class _Session:
    def __init__(self, symbol, extras=()):
        self.symbol = symbol
        self._extras = list(extras)

    def scan_list(self):
        return [self.symbol, *self._extras]


def test_the_gate_admits_every_scanned_symbol_not_only_the_first():
    """`risk_gateway` compared `session.symbol` with the symbol under analysis,
    so with SESSION_ONLY_TRADING on every ROTATED coin ran all 24 nodes and was
    then refused -- four of every five decisions, and their share of a 40/min
    LLM budget, spent reaching a refusal settled before the run began."""
    from backend.graphs.nodes.risk_gateway import _symbol_in_session

    session = _Session("XRP/USDT", ["SOL/USDT", "DOGE/USDT"])
    assert _symbol_in_session(session, "XRP/USDT") is True
    assert _symbol_in_session(session, "SOL/USDT") is True
    assert _symbol_in_session(session, "DOGE/USDT") is True
    assert _symbol_in_session(session, "ADA/USDT") is False, (
        "a coin the session does not scan must still be refused"
    )


def test_a_session_from_before_watch_symbols_behaves_exactly_as_before():
    """A restored row with no `scan_list` must not crash and must not widen
    scope -- the single-symbol behaviour is the correct fallback."""
    from backend.graphs.nodes.risk_gateway import _session_symbols, _symbol_in_session

    class _Old:
        symbol = "XRP/USDT"

    old = _Old()
    assert _session_symbols(old) == ["XRP/USDT"]
    assert _symbol_in_session(old, "XRP/USDT") is True
    assert _symbol_in_session(old, "SOL/USDT") is False


def test_the_three_scope_checks_agree():
    """`risk_gateway`, `trade_scope` and `analysis` all answer "is this symbol
    in scope?". If one keeps the narrower test it silently becomes the real
    limit while another looks like the one in force."""
    from backend.graphs import analysis
    from backend.services import trade_scope

    assert "scan_list()" in inspect.getsource(trade_scope)
    assert "scan_list()" in inspect.getsource(analysis.subscribe_to_triggers)


# ---------------------------------------------------------------------------
# F55 · observation mode must stop the session deciding
# ---------------------------------------------------------------------------

def test_the_session_loop_checks_all_three_halt_states():
    """`may_open_new_position()` is false for pause, emergency stop AND
    observation mode; the loop tested only the first two. After the drawdown
    kill switch latched, the session ran a full 24-node graph every 30 seconds
    indefinitely -- full LLM spend, every result refused at the gateway -- while
    the panel showed "running"."""
    from backend.services import trading_session as ts

    src = inspect.getsource(ts._run_session)
    assert "is_emergency_stopped()" in src
    assert "is_system_paused()" in src
    assert "is_in_observation_mode()" in src, (
        "the third halt state must stop the session spending a graph run per cycle"
    )
    # It CONTINUES rather than finishing: observation mode is releasable, and
    # ending the session would end it for the positions still being managed.
    idx = src.index("is_in_observation_mode()")
    assert "continue" in src[idx:idx + 1200]
    assert "_finish(" not in src[idx:idx + 600]


def test_the_repeated_notes_are_throttled():
    """The log is 50 entries and the poll is every 12 seconds, so a steady
    state evicts the whole decision history in about ten minutes. The frozen
    session's log was fifty identical copies of "already holding a position"
    with the answer to "why did it not trade?" pushed out hours earlier."""
    from backend.services import trading_session as ts

    session = type("S", (), {"id": "t1", "log": []})()
    ts._throttled_notes.clear()

    for _ in range(5):
        ts._note_throttled(session, "holding", "already holding a position")
    assert len(session.log) == 1, "a repeated state must be written once"

    ts._note_throttled(session, "holding", "a DIFFERENT message")
    assert len(session.log) == 2, "a changed message must still be written"

    ts._clear_throttle(session, "holding")
    ts._note_throttled(session, "holding", "a DIFFERENT message")
    assert len(session.log) == 3, "clearing must let the next occurrence through"


# ---------------------------------------------------------------------------
# F56 · re-funding the book must re-anchor the drawdown mark
# ---------------------------------------------------------------------------

def test_setting_paper_cash_rearms_the_high_water_mark():
    """The CEO measures drawdown from a MONTHLY high-water mark. If it last saw
    a $10,000 book and the operator funds a $3 session, the first close computes
    a 99.97% drawdown and observation mode latches on a book that has lost
    nothing. Re-funding is exactly the event that invalidates the old mark."""
    from backend.services import trading_session as ts

    src = inspect.getsource(ts.set_paper_starting_amount)
    assert "rearm_high_water_mark" in src
    # And it must not be able to fail the funding it follows.
    assert "except Exception" in src


def test_resetting_the_paper_book_rearms_it_too():
    from backend.api import admin

    assert "rearm_high_water_mark" in inspect.getsource(admin.reset_paper)


# ---------------------------------------------------------------------------
# F57 · a failed READ must never become an empty WRITE
# ---------------------------------------------------------------------------

def test_an_unreadable_watch_list_blocks_the_next_save():
    """`load_watch_list` returned [] on a read error, and `save_watch_list` is a
    DELETE + INSERT of everything held -- which is nothing. One Supabase blip at
    boot forgot every real position AND the resting-stop ids that are the only
    handle on them."""
    from backend.services import position_store as ps

    original = ps._load_failed
    try:
        ps._load_failed = True
        assert asyncio.run(ps.save_watch_list([{"tar_id": "x"}])) is False
    finally:
        ps._load_failed = original


def test_an_unreadable_portfolio_blocks_the_next_persist():
    from backend.services import portfolio_store as pfs

    original = pfs._load_failed
    try:
        pfs._load_failed = True
        assert asyncio.run(pfs._persist()) is False
    finally:
        pfs._load_failed = original


def test_a_successful_read_clears_the_latch():
    """Otherwise one transient failure disables persistence for the life of the
    process, which trades a wiped book for a book that is never saved."""
    from backend.services import portfolio_store as pfs
    from backend.services import position_store as ps

    for module in (pfs, ps):
        src = inspect.getsource(module)
        assert "_load_failed = False" in src, f"{module.__name__} must clear its latch"


# ---------------------------------------------------------------------------
# F59 · one ordinary stop-out must not halt the day
# ---------------------------------------------------------------------------

def test_the_daily_loss_limit_scales_with_leverage_in_broker_mode():
    """A flat 5% was written for 1x. Broker-style sizing makes a stop-out cost
    ~leverage x stop% -- about 8% at 10x -- so the FIRST loss of the day
    exceeded the whole day's allowance and entries were hard-rejected until
    00:00 UTC. `MAX_DAILY_LOSS_PCT`'s own comment states the principle this
    broke: it sits above the per-trade limit on purpose."""
    import datetime as dt

    from backend.core.risk_manager import MAX_DAILY_LOSS_PCT, check_daily_loss

    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    # One ordinary stop-out: 8% of a 100 book.
    ledger = [{"timestamp": f"{today}T10:00:00", "pnl": -8.0}]

    at_1x = check_daily_loss(100.0, ledger, leverage=1.0)
    assert at_1x.status == "reject", "unchanged at 1x: 8% is over the flat 5%"

    at_10x = check_daily_loss(100.0, ledger, leverage=10.0)
    assert at_10x.status != "reject", (
        "at 10x a single in-rules stop-out must not halt the rest of the day"
    )
    assert MAX_DAILY_LOSS_PCT == 5.0, "the 1x policy itself is unchanged"


def test_a_genuinely_bad_day_still_halts_at_leverage():
    """The limit scales; it does not disappear."""
    import datetime as dt

    from backend.core.risk_manager import check_daily_loss

    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    ledger = [{"timestamp": f"{today}T10:00:00", "pnl": -60.0}]
    assert check_daily_loss(100.0, ledger, leverage=10.0).status == "reject"


# ---------------------------------------------------------------------------
# F2 · the gateway and the book must agree about what is affordable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("leverage", [1, 3, 10])
def test_the_sized_margin_plus_its_fee_fits_in_cash(leverage):
    """`apply_paper_fill` refuses when `margin + fee > free_cash`, and its fee
    is `notional x taker` = `margin x leverage x taker`. The gateway reserved
    `cash x taker x 1.5`, which covers that only at 1x -- so at 3x and 10x it
    sized a position the book then declined, while a REAL demo order already
    stood behind it."""
    from backend.services.fees import taker_rate

    cash = 2.8315
    t = taker_rate()
    margin = cash / (1.0 + leverage * t * 1.5)
    assert margin + margin * leverage * t <= cash, (
        f"at {leverage}x the sized margin plus its fee must fit in cash"
    )


def test_the_gateway_sizes_with_the_leverage_aware_reserve():
    from backend.graphs.nodes import risk_gateway

    src = inspect.getsource(risk_gateway.gate)
    assert "affordable_margin" in src
    assert "1.0 + _fee_per_margin" in src, (
        "the reserve must be solved against notional, not a fraction of cash"
    )


# ---------------------------------------------------------------------------
# F65 · approved-but-unfilled TARs must not accumulate forever
# ---------------------------------------------------------------------------

def test_a_stale_pending_approval_is_dropped():
    """`_pending` was cleared only by a FILL or `clear_all`, so every TAR
    refused after approval -- below the venue minimum, unfundable, or
    interrupted by a restart -- stayed forever, was re-persisted on every write
    and re-restored on every boot."""
    from backend.agents.position_monitor import (
        PositionMonitorAgent, _PENDING_TTL_S, get_position_monitor,
    )

    mon = get_position_monitor()
    mon._pending.clear()
    mon._pending["fresh"] = {"symbol": "SOL/USDT", "approved_at": time.time()}
    mon._pending["stale"] = {"symbol": "XRP/USDT",
                             "approved_at": time.time() - _PENDING_TTL_S - 1}

    mon._expire_stale_pending()

    assert "fresh" in mon._pending
    assert "stale" not in mon._pending
    mon._pending.clear()


def test_a_pending_row_with_no_timestamp_is_kept():
    """A row persisted before `approved_at` existed has no age to judge. Kept
    rather than dropped: losing a legitimate pending approval loses its stop,
    and the TTL will catch it once it is rewritten."""
    from backend.agents.position_monitor import get_position_monitor

    mon = get_position_monitor()
    mon._pending.clear()
    mon._pending["old"] = {"symbol": "XRP/USDT"}
    mon._expire_stale_pending()
    assert "old" in mon._pending

    # AND THE HALF THIS TEST USED TO MISS. Its reasoning was "the TTL will
    # catch it once it is rewritten" — a RESTORED row is never rewritten, it
    # was re-persisted undated on every boot, so it was IMMORTAL and
    # `may_open_new_position` counted it against the single allowed slot. The
    # row is now DATED on the first pass, so it survives the moment that
    # matters and still expires on schedule.
    assert mon._pending["old"]["approved_at"] > 0, (
        "an undated pending row is still undatable, so it can never expire"
    )
    mon._pending.clear()


# ---------------------------------------------------------------------------
# F30 · the session's book must follow LIVE_TRADING
# ---------------------------------------------------------------------------

def test_the_session_rereads_its_book_every_cycle():
    """`tab` was read once at startup, so turning live trading on mid-session
    left the loop measuring the PAPER book while the graph traded the REAL one:
    the floor never fired on real losses and the fill count never advanced."""
    from backend.services import trading_session as ts

    src = inspect.getsource(ts._run_session)
    assert "cycle_tab = _tab_for_session()" in src
    assert src.count("_tab_for_session()") >= 2, "once at startup is not enough"


# ---------------------------------------------------------------------------
# F41 · a refused start must leave the book alone
# ---------------------------------------------------------------------------

def test_the_refusals_run_before_the_cash_is_rewritten():
    """A second Start click with $2 set cash 10,000 -> 2 and THEN raised "a
    session is already running" -- and the session that was running ended
    "floored" against a book it never agreed to.

    STRENGTHENED: THE WRITE NOW COMES AFTER *EVERY* REFUSAL, NOT JUST THESE TWO.

    This asserted that the write lands before `current_equity`, because the
    session has to measure the amount the operator chose rather than the
    book's previous cash. That ordering had a cost the original fix did not
    close: the TARGET and FLOOR refusals run after `current_equity`, so a Start
    with a target below the new amount still rewrote cash 10,000 -> 2 and
    re-anchored the drawdown mark before refusing.

    The equity is now DERIVED instead of measured-after-writing, which is exact
    rather than an estimate: `set_paper_starting_amount` refuses while any
    paper position is open, so on success the book is flat and its equity is
    the amount. That frees the write to be the last statement that can change
    anything, after the last statement that can refuse.
    """
    from backend.services import trading_session as ts

    src = inspect.getsource(ts.start_session)
    already = src.index("is already running on")
    ceiling = src.index("is outside the 1x-")
    funds = src.index("await set_paper_starting_amount(")
    assert already < funds, "the already-running refusal must precede the write"
    assert ceiling < funds, "the leverage refusal must precede the write"

    # EVERY other refusal too, which is what this test did not cover before.
    for marker, name in (
        ("there would be nothing for the session to do", "target-below-equity"),
        ("_venue_minimum_refusal", "venue-minimum"),
    ):
        assert src.index(marker) < funds, f"the {name} refusal runs AFTER the write"

    # And nothing is left measuring the book after it was written, because the
    # amount IS the equity on a flat book.
    assert "_assert_paper_amount_settable" in src
