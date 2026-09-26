"""Real and paper must behave identically — and two places they did not.

The operator's question: "verify with all the real and paper trade all the
changes are working is same as real and paper".

Most of this system is already tab-agnostic: the graph, the gates, the sizing and
the exit rules all read the same constants whichever book is trading, and the
only real-only branches in `position_monitor` are the ones that place orders at a
venue (a paper fill has no venue order behind it to protect). Two things were
NOT, and both were invisible from the paper side.

1. THE VENUE TAKE-PROFIT SAT AT A LEVEL THE MONITOR NO LONGER USES
   `pos.take_profit` is the Risk Gateway's 5x-ATR target. Since PROFIT_TARGET_PCT
   became the default exit, `_check_price` closes at a fixed PERCENTAGE instead,
   which is much nearer. Measured on a live 3x SOL/USDT short: the percentage
   target was a 0.667% move to 120.56 while the ATR target sat at 116.96, 5.4x
   further away.

   While the process is alive both books behave the same, because the in-process
   monitor fires first. The divergence is the window it is NOT alive — which is
   the only reason the resting order exists. A real trade reaching its target
   during a deploy would ride straight through it; the paper book would book the
   win. Covered by `test_resting_stop.py`.

2. THE LEDGER DID NOT RECORD WHICH BOOK A TRADE CAME FROM
   Nothing wrote `ai_memory`'s ledger until the close path was connected, so its
   readers were all reading an empty list and none could be wrong. Turning the
   writer on turned them on — and two are tab-sensitive:

     * `risk_gateway._ledger()` feeds the DAILY-LOSS check, so a bad day on paper
       would have counted against the real book's limit and halted real trading.
     * `supervisor_agent._measured_win_rate` feeds KELLY SIZING. Paper fills are
       simulated against an observed price with no real slippage and no partial
       fills, so a paper win rate is optimistic relative to a real one — and an
       optimistic probability estimate driving real position size is Kelly at its
       most dangerous.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


# ---------------------------------------------------------------------------
# The books are separate
# ---------------------------------------------------------------------------

@pytest.fixture
def memory(tmp_path, monkeypatch):
    import backend.services.ai_memory as mem

    monkeypatch.setattr(mem, "MEMORY_FILE", str(tmp_path / "ai_memory.json"))
    return mem


def test_paper_and_real_outcomes_are_counted_separately(memory):
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", 50.0, tab="paper"))
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", -40.0, tab="paper"))
    asyncio.run(memory.record_closed_trade("SOL/USDT", "sell", 10.0, tab="real"))

    assert memory.stats_for_tab("paper")["total_trades"] == 2
    assert memory.stats_for_tab("paper")["win_rate"] == pytest.approx(50.0)
    assert memory.stats_for_tab("real")["total_trades"] == 1
    assert memory.stats_for_tab("real")["win_rate"] == pytest.approx(100.0)


def test_the_all_books_total_still_exists(memory):
    """Every pre-existing reader and the /api/memory surface read `global_stats`.
    Splitting the books must not blank them."""
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", 50.0, tab="paper"))
    asyncio.run(memory.record_closed_trade("SOL/USDT", "sell", 10.0, tab="real"))

    g = memory.get_memory_stats()["global_stats"]
    assert g["total_trades"] == 2
    assert g["total_pnl"] == pytest.approx(60.0)


def test_a_book_that_has_not_traded_reports_zero_not_the_other_book(memory):
    """The failure this prevents: asking for the REAL win rate and silently
    receiving the PAPER one. `measured_accuracy` refuses to report below its
    sample floor, so zeros read as "not measurable yet" — the honest answer."""
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", 50.0, tab="paper"))

    assert memory.stats_for_tab("real")["total_trades"] == 0
    assert memory.stats_for_tab("real")["win_rate"] == 0.0


def test_the_ledger_is_filtered_by_book(memory):
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", -40.0, tab="paper"))
    asyncio.run(memory.record_closed_trade("ETH/USDT", "sell", -10.0, tab="real"))

    assert len(memory.ledger_for_tab("paper")) == 1
    assert len(memory.ledger_for_tab("real")) == 1
    assert memory.ledger_for_tab("paper")[0]["symbol"] == "SOL/USDT"


def test_an_unlabelled_legacy_row_is_excluded_from_both(memory):
    """Rows written before the `tab` field existed carry no book.

    EXCLUDED rather than assumed: attributing an unlabelled loss to the real book
    could halt real trading on a paper result, and attributing it to paper could
    let a real daily-loss limit be exceeded. Neither guess is safe.
    """
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", -40.0, tab="paper"))
    raw = memory._load_memory()
    raw["trade_ledger"].append({"symbol": "OLD/USDT", "pnl": -999.0, "is_win": False})
    memory._save_memory(raw)

    assert memory.ledger_for_tab("paper") and memory.ledger_for_tab("real") == []
    assert all(e["symbol"] != "OLD/USDT" for e in memory.ledger_for_tab("paper"))


def test_an_unknown_book_name_does_not_create_a_third_ledger(memory):
    asyncio.run(memory.record_closed_trade("SOL/USDT", "buy", 1.0, tab="nonsense"))
    assert memory.stats_for_tab("paper")["total_trades"] == 1


# ---------------------------------------------------------------------------
# The readers ask for the right book
# ---------------------------------------------------------------------------

def test_the_daily_loss_gate_reads_the_book_it_is_gating():
    from backend.graphs.nodes import risk_gateway

    src = inspect.getsource(risk_gateway)
    assert '"tradeLedger": _ledger(tab)' in src, (
        "the daily-loss check must see only the traded book's outcomes — a bad "
        "day on paper must not halt real trading"
    )
    assert "ledger_for_tab" in inspect.getsource(risk_gateway._ledger)


def test_kelly_sizing_reads_the_book_it_is_sizing():
    from backend.agents.supervisor_agent import SupervisorAgent

    src = inspect.getsource(SupervisorAgent._measured_win_rate)
    assert "stats_for_tab" in src
    assert "execution_tab" in src
    assert "global_stats" not in src, (
        "sizing a REAL trade against a win rate inflated by frictionless paper "
        "fills is Kelly at its most dangerous"
    )


# ---------------------------------------------------------------------------
# Everything else is deliberately tab-agnostic
# ---------------------------------------------------------------------------

def test_the_exit_rules_are_the_same_numbers_for_both_books():
    """PROFIT_TARGET_PCT, the trail and the scale-out are module constants read on
    every tick for every position. There is no per-tab variant and there must not
    be one: "2% per trade" has to mean the same thing on both books, or paper
    stops being a rehearsal for real."""
    import backend.agents.position_monitor as pm

    src = inspect.getsource(pm)
    for const in ("PROFIT_TARGET_PCT", "TRAILING_STOP_R", "PARTIAL_TP_FRACTION"):
        assert f'{const}_PAPER' not in src and f'{const}_REAL' not in src


def test_the_only_real_only_branches_are_venue_orders():
    """`pos.tab != "real"` appears several times in the monitor, and every one of
    them guards an EXCHANGE call. A paper fill has no venue order behind it, so
    placing or cancelling one is meaningless — but nothing about the DECISION to
    exit may differ."""
    import backend.agents.position_monitor as pm

    src = inspect.getsource(pm)
    guarded = [
        line.strip() for line in src.splitlines()
        if 'pos.tab != "real"' in line
    ]
    assert guarded, "expected the real-only venue guards to still exist"

    # None of them may guard the close decision itself.
    for name in ("_close", "_check_price"):
        fn_src = inspect.getsource(getattr(pm.PositionMonitorAgent, name))
        assert 'tab != "real"' not in fn_src, (
            f"{name} branches on the book — the exit decision must be identical "
            f"on paper and real"
        )


def test_the_scope_rules_do_not_branch_on_the_book():
    """One position at a time, session scope and the tradeable universe are
    properties of the instrument and the session, not of which book is paying."""
    from backend.services import trade_scope

    src = inspect.getsource(trade_scope)
    assert '"real"' not in src and '"paper"' not in src
