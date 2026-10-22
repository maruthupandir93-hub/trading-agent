"""Batch 2 of the 2026-10-21 review: the P0 close path and five more.

The one that moves real money is first. The rest share a theme the earlier
batches did too — a control that reports success while doing nothing, or a
safety latch that fires far wider than its author intended.
"""

from __future__ import annotations

import inspect

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# P0 — disabling live mode must not strand a real position
# ---------------------------------------------------------------------------

def _agent(**kw):
    from backend.agents.execution_agent import ExecutionAgent

    return ExecutionAgent(**kw)


def test_a_REAL_close_still_reaches_the_venue_when_live_trading_is_off(monkeypatch):
    """The P0. Turning live trading off is what an operator does when they want
    LESS exposure; it must never be the thing that strands a position.

        a REAL position is open, real money at the exchange
        the operator turns LIVE_TRADING off
        -> simulation_mode becomes True
        -> the stop fires and the close took the SIMULATED branch
        -> the local book recorded a close and a realised P&L
        -> THE EXCHANGE POSITION WAS STILL OPEN

    Invariant 4: exits are never blocked, "for real money more, not less".
    """
    monkeypatch.setenv("LIVE_TRADING", "false")
    agent = _agent()

    assert agent.simulation_mode is True
    assert agent.routes_to_venue("real") is False, "entries must still be blocked"
    assert agent.close_routes_to_venue("real") is True, (
        "a real position's close must reach the venue where the money is"
    )


def test_a_PAPER_close_never_reaches_a_venue(monkeypatch):
    """The opposite failure, and equally real: a reduce-only order on mainnet
    trims the operator's REAL position in the same symbol if they hold one."""
    for live in ("true", "false"):
        monkeypatch.setenv("LIVE_TRADING", live)
        assert _agent().close_routes_to_venue("paper") is False


def test_the_backtest_override_still_stops_every_close():
    """The one thing that may: it has no exchange behind it at all."""
    assert _agent(simulation_mode=True).close_routes_to_venue("real") is False


def test_the_close_path_does_not_consult_the_entry_predicate():
    import backend.agents.execution_agent as ea

    src = code_only(inspect.getsource(ea.ExecutionAgent.close_position))
    assert "close_routes_to_venue" in src
    assert "self.routes_to_venue" not in src, (
        "the close path still uses the ENTRY predicate, which strands real "
        "positions whenever LIVE_TRADING is turned off"
    )


# ---------------------------------------------------------------------------
# The daily loss limit must be reachable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("leverage", [1, 3, 5, 10])
def test_the_daily_loss_limit_fires_before_the_drawdown_halt(monkeypatch, leverage):
    """It scaled to 50% at 10x while the halt sits at 10-30%, so it could never
    fire. The operator believed they had two brakes and had one.

    A daily limit is the SOFTER, EARLIER brake — "stop for today" against the
    mandate's "stop until a human intervenes" — so it only means anything if it
    is reached first.
    """
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")

    from backend.agents.ceo_agent import max_drawdown_fraction
    from backend.core.risk_manager import (
        DAILY_LIMIT_SHARE_OF_DRAWDOWN, MAX_DAILY_LOSS_PCT,
    )

    halt_pct = max_drawdown_fraction() * 100.0
    effective = min(MAX_DAILY_LOSS_PCT * leverage, halt_pct * DAILY_LIMIT_SHARE_OF_DRAWDOWN)

    assert effective < halt_pct, (
        f"at {leverage}x the daily limit is {effective:.1f}% against a "
        f"{halt_pct:.0f}% halt — it can never fire"
    )


def test_at_1x_the_daily_limit_is_byte_for_byte_the_original(monkeypatch):
    """The cap is a ceiling, never a raise."""
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")

    from backend.agents.ceo_agent import max_drawdown_fraction
    from backend.core.risk_manager import (
        DAILY_LIMIT_SHARE_OF_DRAWDOWN, MAX_DAILY_LOSS_PCT,
    )

    effective = min(
        MAX_DAILY_LOSS_PCT * 1,
        max_drawdown_fraction() * 100.0 * DAILY_LIMIT_SHARE_OF_DRAWDOWN,
    )
    assert effective == MAX_DAILY_LOSS_PCT


# ---------------------------------------------------------------------------
# The graph stream socket must fail closed
# ---------------------------------------------------------------------------

def test_the_graph_stream_socket_fails_closed_with_no_key():
    """It starts 24-node graph runs, which spend market data and the 40/min
    model budget. `if auth_required():` skipped the whole check when the key
    was unset — the F6 shape, missed here because a WebSocket does not use the
    shared HTTP dependency."""
    import backend.api.graphs as graphs

    src = code_only(inspect.getsource(graphs.stream_graph_progress))
    assert "open_writes_allowed" in src, (
        "the socket does not consult the fail-closed rule, so it is open "
        "whenever TRADES_API_KEY is unset"
    )
    # And the old fail-open guard is gone.
    assert "if auth_required ( ) :" not in src


# ---------------------------------------------------------------------------
# A refused session must not have moved money
# ---------------------------------------------------------------------------

def test_the_paper_book_is_written_only_after_every_refusal():
    """A refusal must leave the system exactly as it found it.

    F41 moved the write below the already-running and leverage checks and that
    was not far enough: the TARGET and FLOOR checks still ran after it, so a
    Start with a target below the new amount rewrote cash 10,000 -> 2,
    re-anchored the drawdown mark, and only then refused.
    """
    import backend.services.trading_session as ts

    src = inspect.getsource(ts.start_session)
    write_at = src.index("await set_paper_starting_amount(")
    # Every refusal that can still fire must appear BEFORE the write.
    for marker in ("target", "floor", "_venue_minimum_refusal"):
        idx = src.find(marker)
        assert idx != -1 and idx < write_at, (
            f"the {marker!r} refusal runs AFTER the book is written"
        )


def test_the_dry_run_and_the_real_setter_refuse_the_same_things():
    """Two copies of one rule, kept in sync deliberately — the arrangement the
    ATR multipliers and the watchlist seed already use."""
    import backend.services.trading_session as ts

    dry = inspect.getsource(ts._assert_paper_amount_settable)
    real = inspect.getsource(ts.set_paper_starting_amount)
    for phrase in ("must be a positive number", "paper positions are open"):
        assert phrase in dry and phrase in real


# ---------------------------------------------------------------------------
# A partial sell must not become a full close
# ---------------------------------------------------------------------------

def test_a_partial_sell_of_a_watched_position_is_refused():
    """`close_tracked` closes the WHOLE position and never read `req.qty`, so
    selling 1 of 155 closed all 155. Doing the opposite of a trading
    instruction is worse than declining it."""
    import backend.api.operator_trade as ot

    src = code_only(inspect.getsource(ot))
    assert "held" in src and "close_tracked" in src
    raw = inspect.getsource(ot)
    assert "Refusing to sell" in raw, "a partial sell is still escalated to a full close"


# ---------------------------------------------------------------------------
# One write failure must not blank every store
# ---------------------------------------------------------------------------

def test_the_json_fallback_degrades_per_file_and_still_reads_the_disk():
    """One failed write latched process-wide and `readJson` consulted it BEFORE
    touching the disk, so every store returned the in-memory copy — empty for
    anything this process had not itself written — while the files were fine.
    On the trade log that reads as "you have no trades"."""
    import pathlib

    src = pathlib.Path("lib/jsonFallback.server.ts").read_text(encoding="utf-8")

    assert "const unwritable = new Map<string, string>()" in src, (
        "the degrade latch is still process-wide"
    )
    # The read path must reach fs.readFile without a latch check in front.
    read_fn = src[src.index("export async function readJson"):]
    read_fn = read_fn[: read_fn.index("export async function writeJson")]
    assert "fs.readFile" in read_fn
    assert "unwritable.has" not in read_fn, (
        "readJson still short-circuits on the write latch, so a write failure "
        "blinds reads of files that are perfectly readable"
    )


def test_the_collaboration_store_has_the_same_fix():
    """A separate store that carried an identical copy of the bug."""
    import pathlib

    src = pathlib.Path("lib/collaborationStore.server.ts").read_text(encoding="utf-8")
    read_fn = src[src.index("async function readAll"):]
    read_fn = read_fn[: read_fn.index("async function writeAll")]
    assert "fs.readFile" in read_fn
    assert "if (readOnlyReason !== null) return" not in read_fn
