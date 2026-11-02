"""The 2026-11 forensic review of six XRP shorts (docs/DIAGNOSIS.md).

Each finding was checked against the source and, where only data could decide
it, against real Binance candles, before anything was changed:

  * 11-second same-side re-entry after a thesis exit   CONFIRMED -> cooldown
  * exit evidence not linked to the closing trade        CONFIRMED -> recorded
  * reflection entry_context_used always NULL            CONFIRMED -> recorded
  * "drawdown killswitch not implemented" in approvals   FALSE STATEMENT -> fixed
  * thesis-invalidated exits caused the losses           OVERTURNED by replay:
        held to stop/target the five would have lost -2.90R vs -2.37R taken,
        so the exit threshold is deliberately NOT loosened.
  * late entries                                         MEASURED -> gate ON
        (see entry_extension's docstring)
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# Same-side re-entry cooldown
# ---------------------------------------------------------------------------

@pytest.fixture
def cooldown(monkeypatch):
    from backend.services import reentry_cooldown as rc

    monkeypatch.delenv(rc.COOLDOWN_VAR, raising=False)
    rc.reset()
    yield rc
    rc.reset()


def test_the_measured_case_is_refused(cooldown):
    """SHORT closed thesis-invalidated, another SHORT 11 seconds later."""
    cooldown.record_close("XRP/USDT", "sell", "thesis-invalidated", -0.241, at=1000.0)
    why = cooldown.refusal("XRP/USDT", "SHORT", now=1011.0)
    assert why and "re-entry cooldown" in why and "XRP/USDT" in why


def test_the_opposite_side_is_never_refused(cooldown):
    """A thesis exit says the panel argues the OTHER way; acting on that is
    consistent with the exit."""
    cooldown.record_close("XRP/USDT", "sell", "thesis-invalidated", -0.241, at=1000.0)
    assert cooldown.refusal("XRP/USDT", "LONG", now=1011.0) is None


def test_other_symbols_and_winners_are_not_affected(cooldown):
    cooldown.record_close("XRP/USDT", "sell", "stop-loss", -0.3, at=1000.0)
    assert cooldown.refusal("SOL/USDT", "SHORT", now=1001.0) is None
    cooldown.record_close("SOL/USDT", "buy", "take-profit", +0.5, at=1000.0)
    assert cooldown.refusal("SOL/USDT", "LONG", now=1001.0) is None


def test_a_losing_stop_out_also_counts_and_the_window_expires(cooldown):
    cooldown.record_close("XRP/USDT", "buy", "stop-loss", -0.3, at=1000.0)
    assert cooldown.refusal("XRP/USDT", "LONG", now=1000.0 + 60) is not None
    assert cooldown.refusal("XRP/USDT", "LONG",
                            now=1000.0 + cooldown.DEFAULT_COOLDOWN_S + 1) is None


def test_the_perpetual_spelling_is_the_same_instrument(cooldown):
    cooldown.record_close("XRP/USDT:USDT", "sell", "stop-loss", -0.3, at=1000.0)
    assert cooldown.refusal("XRP/USDT", "SHORT", now=1001.0) is not None


def test_zero_disables_it_at_call_time(cooldown, monkeypatch):
    cooldown.record_close("XRP/USDT", "sell", "stop-loss", -0.3, at=1000.0)
    monkeypatch.setenv(cooldown.COOLDOWN_VAR, "0")
    assert cooldown.refusal("XRP/USDT", "SHORT", now=1001.0) is None


def test_both_entry_paths_consult_it_and_the_exit_branch_does_not():
    """A limit only one of two paths respects is not a limit (trade_scope)."""
    import backend.agents.supervisor_agent as sa
    import backend.graphs.nodes.risk_gateway as rg

    # Raw source: `code_only` strips the "EXIT" literal this ordering check needs.
    gate_src = inspect.getsource(rg.gate)
    call = "cooldown = reentry_refusal(symbol, thesis.direction)"
    assert call in gate_src
    # AFTER the EXIT branch — invariant 4.
    assert gate_src.index('if decision.action == "EXIT":') < gate_src.index(call)
    assert "scope_refusal = reentry_refusal(symbol, direction)" in inspect.getsource(
        sa.SupervisorAgent._consider_trade)


def test_the_monitor_records_full_closes():
    import backend.agents.position_monitor as pm

    assert "record_close(pos.symbol, pos.side, reason, realized)" in inspect.getsource(
        pm.PositionMonitorAgent._close)


# ---------------------------------------------------------------------------
# Exit evidence reaches the closing row, the event and the reflection
# ---------------------------------------------------------------------------

def test_the_close_event_DECLARES_exit_context():
    """Pydantic v2 drops an undeclared kwarg in silence (TarApprovedEvent)."""
    from backend.models.events import PositionClosedEvent

    assert "exit_context" in PositionClosedEvent.model_fields


def test_the_execution_service_passes_the_supervisors_rationale():
    import backend.services.execution_service as es

    src = inspect.getsource(es)
    assert "tab=event.tab, evidence=evidence," in src
    assert "(event.rationale or \"\").strip()" in src


@pytest.mark.asyncio
async def test_close_tracked_carries_the_evidence_into_the_close(monkeypatch):
    from backend.agents.position_monitor import PositionMonitorAgent, _Tracked

    monitor = PositionMonitorAgent()
    pos = _Tracked(tar_id="t1", symbol="XRP/USDT", side="sell", tab="paper",
                   qty=10.0, entry_price=1.2111, stop_loss=1.24, take_profit=1.15)
    monitor._open["t1"] = pos
    seen = {}

    async def _fake_close(p, price, reason):
        seen["evidence"] = p.exit_evidence
        seen["reason"] = reason
        monitor._open.pop(p.tar_id, None)
        return price

    monkeypatch.setattr(monitor, "_close", _fake_close)
    fill = await monitor.close_tracked(
        "XRP/USDT", "thesis-invalidated", price=1.2239, tab="paper",
        evidence="the specialist evidence now reads LONG at 0.21",
    )
    assert fill == pytest.approx(1.2239)
    assert seen == {"evidence": "the specialist evidence now reads LONG at 0.21",
                    "reason": "thesis-invalidated"}


@pytest.mark.asyncio
async def test_a_failed_close_does_not_leave_stale_evidence(monkeypatch):
    """Otherwise a later STOP-OUT of the same position would be recorded with
    a judgement that never caused it."""
    from backend.agents.position_monitor import PositionMonitorAgent, _Tracked

    monitor = PositionMonitorAgent()
    pos = _Tracked(tar_id="t1", symbol="XRP/USDT", side="sell", tab="paper",
                   qty=10.0, entry_price=1.2111, stop_loss=1.24, take_profit=1.15)
    monitor._open["t1"] = pos

    async def _failed(p, price, reason):
        return None  # the position stays tracked

    monkeypatch.setattr(monitor, "_close", _failed)
    assert await monitor.close_tracked("XRP/USDT", "thesis-invalidated",
                                       price=1.22, evidence="LONG at 0.2") is None
    assert pos.exit_evidence is None


def test_the_reflection_records_what_it_was_given():
    from backend.agents.reflection_agent import _exit_context_line
    from backend.models.events import PositionClosedEvent

    import backend.agents.reflection_agent as ra

    src = inspect.getsource(ra)
    assert 'entry_context_used=receipt.get("entry_context")' in src
    assert "exit_context_used=_exit_context_line(event)" in src
    assert '"Auto-generated reflection", lesson_source' not in src

    ev = PositionClosedEvent(
        trade_id="r1", symbol="XRP/USDT", side="sell", tab="paper",
        entry_price=1.2111, exit_price=1.2239, quantity=10, realized_pnl=-0.24,
        exit_reason="thesis-invalidated", held_seconds=1560,
        exit_context="panel reads LONG at 0.21",
    )
    line = _exit_context_line(ev)
    assert "thesis-invalidated" in line and "26.0 min" in line
    assert "panel reads LONG at 0.21" in line


# ---------------------------------------------------------------------------
# The approval record states the safeguards truthfully
# ---------------------------------------------------------------------------

def test_the_approval_no_longer_denies_the_killswitch(monkeypatch):
    from backend.agents.cro_agent import _portfolio_controls_note

    monkeypatch.delenv("MAX_CONCURRENT_POSITIONS", raising=False)
    note = _portfolio_controls_note()
    assert "not yet implemented" not in note
    assert "enforced by the CEO" in note
    assert "not applicable at MAX_CONCURRENT_POSITIONS=1" in note


def test_above_one_position_correlation_is_admitted_unchecked(monkeypatch):
    from backend.agents.cro_agent import _portfolio_controls_note

    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "3")
    assert "Correlation caps are NOT implemented" in _portfolio_controls_note()


def test_the_exit_threshold_was_not_loosened():
    """The replay showed the thesis exits SAVED 0.53R across the five losses.
    Raising this bar would have held them into the stop."""
    from backend.graphs.nodes.supervisor import MIN_CONFIDENCE_TO_EXIT

    assert MIN_CONFIDENCE_TO_EXIT == 0.15
