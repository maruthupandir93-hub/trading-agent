"""TRADE_LOSS_ANALYSIS_2026-11-12.md — the changes that were made, pinned.

  * issue 8: a stop that had been moved to entry closed as `stop-loss`, and the
    reflection model concluded the stop had been PLACED at entry -> honest
    labels, and the trade's path (placed stop, moved stop, MFE/MAE) reaches the
    closing row and the reflection
  * issue 6: the break-even move was a constant nothing could switch off ->
    `GRAPH_BREAKEVEN_AT_R`, read at call time (default unchanged, see the
    measurement in the constant's comment)
  * issue 3: no way to stop a strategy originating entries ->
    `DISABLED_ENTRY_STRATEGIES`, an operator control read at call time
  * issue 8: an empty model answer lost a real lesson -> one retry, that case only
"""

from __future__ import annotations

import pytest

from backend.agents.position_monitor import PositionMonitorAgent, _Tracked


def _pos(**over):
    kw = dict(tar_id="t1", symbol="XRP/USDT", side="sell", tab="paper", qty=10.0,
              entry_price=1.1052, stop_loss=1.1217, take_profit=1.0722,
              initial_risk=0.0165, peak_price=1.0839, worst_price=1.1054)
    kw.update(over)
    pos = _Tracked(**kw)
    # A stop away from where it was placed has been MOVED by `tighten_stop`.
    d = 1.0 if pos.side == "buy" else -1.0
    if pos.entry_price is not None and pos.initial_risk and pos.stop_loss is not None:
        placed = pos.entry_price - d * pos.initial_risk
        pos.stop_moved = abs(pos.stop_loss - placed) > 1e-9
    return pos


# ---------------------------------------------------------------------------
# Exit labels say what the stop WAS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side,entry,stop,label", [
    ("sell", 1.1052, 1.1217, "stop-loss"),        # the protective stop, loss side
    ("sell", 1.1052, 1.1052, "breakeven-stop"),   # live trade 12
    ("sell", 1.1052, 1.1051, "breakeven-stop"),   # tick rounding stays break-even
    ("sell", 1.1052, 1.0900, "trailing-stop"),    # locked profit on a short
    ("buy", 100.0, 97.5, "stop-loss"),
    ("buy", 100.0, 100.0, "breakeven-stop"),
    ("buy", 100.0, 102.0, "trailing-stop"),
])
def test_the_stop_label_reflects_where_the_stop_was(side, entry, stop, label):
    risk = 0.0165 if side == "sell" else 2.5
    pos = _pos(side=side, entry_price=entry, stop_loss=stop, initial_risk=risk)
    assert PositionMonitorAgent._stop_exit_label(pos) == label


def test_an_unknown_entry_keeps_the_label_that_claims_least():
    assert PositionMonitorAgent._stop_exit_label(_pos(entry_price=None)) == "stop-loss"


def test_the_path_note_states_the_placed_stop_and_the_move():
    """What the reflection needed for trade 12: the stop was PLACED at 1.1217
    and MOVED to entry after the trade reached +1.29R."""
    monitor = PositionMonitorAgent()
    note = monitor._path_note(_pos(stop_loss=1.1052))
    assert "PLACED at 1.1217" in note
    assert "MOVED to 1.1052" in note
    assert "best point reached +1.29R" in note


def test_an_unmoved_stop_is_not_described_as_moved():
    note = PositionMonitorAgent()._path_note(_pos())
    assert "PLACED at 1.1217" in note and "MOVED" not in note


def test_no_path_note_is_invented_without_a_measured_risk():
    assert PositionMonitorAgent()._path_note(_pos(initial_risk=None)) is None


@pytest.mark.asyncio
async def test_a_failed_close_does_not_keep_or_double_the_path_note():
    class _Exec:
        async def close_position(self, **kw):
            return None            # the close did not fill

    monitor = PositionMonitorAgent()
    monitor._execution = _Exec()
    pos = _pos(stop_loss=1.1052)
    monitor._open[pos.tar_id] = pos
    await monitor._close(pos, 1.1052, "breakeven-stop")
    assert pos.tar_id in monitor._open
    assert pos.exit_evidence is None


# ---------------------------------------------------------------------------
# The break-even move is a setting
# ---------------------------------------------------------------------------

def test_the_breakeven_threshold_is_read_at_call_time(monkeypatch):
    from backend.graphs.nodes.monitoring import BREAKEVEN_VAR, breakeven_at_r

    monkeypatch.delenv(BREAKEVEN_VAR, raising=False)
    assert breakeven_at_r() == 1.0
    monkeypatch.setenv(BREAKEVEN_VAR, "0")
    assert breakeven_at_r() == 0.0
    monkeypatch.setenv(BREAKEVEN_VAR, "1.5")
    assert breakeven_at_r() == 1.5
    monkeypatch.setenv(BREAKEVEN_VAR, "junk")
    assert breakeven_at_r() == 1.0


def test_zero_switches_the_breakeven_move_off_and_default_keeps_it(monkeypatch):
    from tests.test_monitoring_graph import _enriched

    from backend.graphs.nodes.monitoring import BREAKEVEN_VAR, _trail_stop

    st = _enriched(price=102.4)                    # +1.2R on the fixture long
    position = st["monitored_position"]

    monkeypatch.delenv(BREAKEVEN_VAR, raising=False)
    proposed, note = _trail_stop(position)
    assert proposed == pytest.approx(100.0) and "break-even" in note

    monkeypatch.setenv(BREAKEVEN_VAR, "0")
    proposed, note = _trail_stop(position)
    assert proposed is None and "break-even move is off" in note


# ---------------------------------------------------------------------------
# The strategy deny-list
# ---------------------------------------------------------------------------

def test_the_deny_list_is_empty_by_default_and_read_at_call_time(monkeypatch):
    from backend.graphs.nodes.opportunity import (DISABLED_STRATEGIES_VAR,
                                                  disabled_entry_strategies)

    monkeypatch.delenv(DISABLED_STRATEGIES_VAR, raising=False)
    assert disabled_entry_strategies() == frozenset()
    monkeypatch.setenv(DISABLED_STRATEGIES_VAR, " Grid , range,")
    assert disabled_entry_strategies() == {"grid", "range"}


@pytest.mark.asyncio
async def test_a_denied_strategy_is_never_selected_and_says_why(monkeypatch):
    from tests.test_opportunity_graph import _full_state

    from backend.graphs.nodes.opportunity import (DISABLED_STRATEGIES_VAR,
                                                  enumerate_candidates, score_candidates)

    state = _full_state()
    state.update(enumerate_candidates(state))
    eligible = [c.name for c in state["candidate_strategies"] if c.eligible]
    assert eligible, "fixture must offer at least one eligible strategy"

    monkeypatch.setenv(DISABLED_STRATEGIES_VAR, ",".join(eligible))
    out = await score_candidates(state)
    assert out.get("selected_strategy") is None
    denied = [c for c in out["candidate_strategies"] if c.name in eligible]
    assert all(not c.eligible and "switched off by the operator" in c.gated_out_reason
               for c in denied)


# ---------------------------------------------------------------------------
# One retry on an empty model answer, and only then
# ---------------------------------------------------------------------------

def test_the_reflection_retries_only_an_empty_completion():
    import inspect

    import backend.graphs.nodes.reflection as r

    src = inspect.getsource(r)
    assert 'if not result.ok and "empty completion" in (result.error or ""):' in src
    assert src.count("await provider.complete(") == 2, "exactly one retry"


# ---------------------------------------------------------------------------
# The EMA20/EMA50 trend gate (issue 4) — refuses COUNTER only
# ---------------------------------------------------------------------------

def _series(start, step, n=120):
    return [{"close": start + step * i} for i in range(n)]


UP, DOWN = _series(100.0, 0.5), _series(200.0, -0.5)


@pytest.mark.parametrize("direction,h1,h4,verdict,blocks", [
    ("SHORT", UP, UP, "counter", True),        # against both
    ("LONG", DOWN, DOWN, "counter", True),
    ("LONG", UP, UP, "aligned", False),
    ("SHORT", DOWN, DOWN, "aligned", False),
    ("LONG", UP, DOWN, "mixed", False),        # one with, one against: kept
    ("LONG", UP[:30], UP[:30], "unknown", False),   # too few candles never blocks
])
def test_only_a_counter_trend_entry_is_refused(direction, h1, h4, verdict, blocks):
    from backend.algorithms.market_context import ema_alignment

    out = ema_alignment(direction, {"1h": h1, "4h": h4})
    assert (out.verdict, out.blocks) == (verdict, blocks)


def test_one_timeframe_against_and_the_other_flat_is_counter():
    from backend.algorithms.market_context import ema_alignment, ema_trend_of

    flat = [{"close": 100.0} for _ in range(120)]
    assert ema_trend_of(flat) == "Neutral"
    assert ema_alignment("LONG", {"1h": DOWN, "4h": flat}).blocks is True


def test_an_unmeasured_trend_is_None_not_neutral():
    from backend.algorithms.market_context import ema_trend_of

    assert ema_trend_of(UP[:50]) is None
    assert ema_trend_of([{"nope": 1}] * 120) is None


def test_the_ema_gate_is_on_by_default_and_can_be_turned_off(monkeypatch):
    from backend.algorithms.market_context import EMA_GATE_VAR, ema_gate_enabled

    monkeypatch.delenv(EMA_GATE_VAR, raising=False)
    assert ema_gate_enabled() is True
    monkeypatch.setenv(EMA_GATE_VAR, "false")
    assert ema_gate_enabled() is False


def test_the_gateway_applies_it_after_the_exit_branch_and_records_it():
    import inspect

    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    call = "ema_trend = ema_alignment(decision.direction, snapshot.candles)"
    assert call in src
    assert src.index('if decision.action == "EXIT":') < src.index(call)   # invariant 4
    assert "ema_trend=ema_trend," in src
    out = rg.build_entry_context(
        symbol="XRP/USDT", technical=None, regime_state=None, volatility=None,
        strategy="Range",
        ema_trend=type("A", (), {"verdict": "mixed"})())
    assert "ema_trend=mixed" in out and out.endswith("strategy=Range")


def test_the_refusal_is_reported_under_the_trend_gate_not_the_fee_gate():
    from backend.algorithms.market_context import ema_alignment
    from backend.services.abstention import classify

    detail = ema_alignment("SHORT", {"1h": UP, "4h": UP}).detail
    assert classify(f"counter to the EMA trend: {detail}") == "counter-trend"


def test_the_measurement_calls_the_real_function():
    import pathlib

    src = pathlib.Path("scripts/measure_ema_trend_gate.py").read_text(encoding="utf-8")
    assert "ema_alignment" in src and "assess as repo_assess" in src
    assert "HTF_BARS = 120" in src


def test_a_stop_PLACED_on_the_wrong_side_is_not_called_a_trailed_stop():
    """Found by the suite: a misconfigured long with its stop above entry was
    labelled `trailing-stop`. Nothing moved it, so it is a `stop-loss`."""
    pos = _Tracked(tar_id="t", symbol="BTC/USDT", side="buy", tab="paper", qty=1.0,
                   entry_price=60_000.0, stop_loss=62_000.0, initial_risk=2_000.0)
    assert PositionMonitorAgent._stop_exit_label(pos) == "stop-loss"


def test_tighten_stop_is_what_marks_a_stop_as_moved():
    monitor = PositionMonitorAgent()
    pos = _Tracked(tar_id="t", symbol="XRP/USDT", side="buy", tab="paper", qty=1.0,
                   entry_price=100.0, stop_loss=97.5, take_profit=105.0,
                   initial_risk=2.5, peak_price=103.0)
    monitor._open["t"] = pos
    assert PositionMonitorAgent._stop_exit_label(pos) == "stop-loss"
    import inspect

    assert "pos.stop_moved = True" in inspect.getsource(PositionMonitorAgent.tighten_stop)


def test_a_positions_open_time_is_exported_as_UTC_whatever_the_host_timezone():
    """Found running the real backend on a UTC+5:30 machine: `naive.timestamp()`
    reads a naive-UTC datetime as LOCAL time, so a position opened seconds ago
    was exported as opened 5.5 hours ago."""
    import datetime

    naive = datetime.datetime(2026, 11, 13, 16, 0, 0)
    expected = datetime.datetime(2026, 11, 13, 16, 0, 0,
                                 tzinfo=datetime.timezone.utc).timestamp()
    assert PositionMonitorAgent._epoch_seconds(naive) == expected
    assert PositionMonitorAgent._epoch_seconds(None) is None
    import inspect

    import backend.agents.position_monitor as pm
    from tests.sourceutil import code_only as strip_comments_and_strings

    code = strip_comments_and_strings(inspect.getsource(pm))
    assert "opened_at.timestamp()" not in code


def test_the_empty_answer_retry_asks_for_MORE_room_than_the_first_call():
    """Three real closes all lost their lesson: the retry reused the budget the
    model had just run out of."""
    import inspect

    import backend.graphs.nodes.reflection as r
    from backend.llm.provider import ModelTier, _TIER_MAX_TOKENS, request_budget

    assert r.RETRY_BUDGET_MULTIPLE >= 2
    assert "max_tokens=RETRY_BUDGET_MULTIPLE * request_budget(400)" in inspect.getsource(r)
    # ...and the bigger request still fits under the tier's cost cap.
    assert r.RETRY_BUDGET_MULTIPLE * request_budget(400) <= _TIER_MAX_TOKENS[ModelTier.REASONING]
