"""The 2026-11-03 re-analysis (docs/DIAGNOSIS.md, "reanalysed").

Two losing trades (a DOGE and an LTC short) on a session the operator believed
was XRP-only. Each finding was checked against the code, and against real
Binance candles where only data could decide:

  * trades in coins the operator did not select     CONFIRMED -> rotation opt-in
  * portfolio_risk forced the -0.5R reduce at 10x   CONFIRMED BUG -> margin-based
      (measured: an unconditional -0.5R cut is -0.011R/trade vs holding, 5,826 trades)
  * relative strength vs BTC ignored                MEASURED -> the REVERSE gate:
      chasing strength loses (-0.41R, 559 trades); against-strength is not stable
  * no hard loss limit under RUN_UNTIL_TARGET       CONFIRMED -> one that it cannot disable
  * execution_submissions.outcome always NULL       CONFIRMED -> terminal outcomes
  * tar_id column held the run id                   CONFIRMED -> run_id column
  * executed trades had no decisions row            CONFIRMED -> graph decisions
  * RLS disabled                                    CONFIRMED -> owner-scoped RLS
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from tests.test_monitoring_graph import _all_findings, _enriched

from backend.graphs.nodes.monitoring import decide_position, monitor_portfolio_risk
from backend.graphs.state import PortfolioStateSnapshot


# ---------------------------------------------------------------------------
# portfolio_risk: margin, not notional
# ---------------------------------------------------------------------------

def _ten_x_book(equity=4.11, notional=41.1):
    """The operator's book: the whole account as margin at 10x."""
    return PortfolioStateSnapshot(
        tab="paper", equity=equity, cash=0.0,
        open_positions=[{"symbol": "BTC/USDT", "qty": notional / 100.0,
                         "avgCost": 100.0, "marginLocked": notional / 10.0}],
    )


def test_a_10x_position_no_longer_pins_portfolio_risk_at_0_8():
    """The DOGE/LTC case: notional ~1,000% of equity, margin ~100%."""
    st = _enriched(price=98.9)
    st["portfolio_state"] = _ten_x_book()
    finding = monitor_portfolio_risk(st)["monitor_findings"][0]
    assert finding.concern < 0.5, finding.evidence


def test_a_10x_loser_at_minus_half_R_is_not_reduced_on_its_own_size():
    """Both live losses closed on 'binding constraint is portfolio_risk at 0.80'."""
    st = _enriched(price=98.9)            # about -0.55R
    st["portfolio_state"] = _ten_x_book()
    st = _all_findings(st)
    decision = decide_position(st)["position_decision"]
    assert decision.action != "REDUCE", decision.reason


def test_genuine_over_exposure_still_concerns():
    """200% of equity in MARGIN (nothing locked recorded -> 1x) still binds."""
    st = _enriched(price=98.9)
    st["portfolio_state"] = PortfolioStateSnapshot(
        tab="paper", equity=1_000.0, cash=100.0,
        open_positions=[{"symbol": "BTC/USDT", "qty": 20.0, "avgCost": 100.0}],
    )
    assert monitor_portfolio_risk(st)["monitor_findings"][0].concern >= 0.8


def test_the_positions_own_pnl_is_evidence_not_concern():
    st = _enriched(price=90.0)            # deep underwater
    st["portfolio_state"] = PortfolioStateSnapshot(
        tab="paper", equity=100.0, cash=100.0, open_positions=[],
    )
    finding = monitor_portfolio_risk(st)["monitor_findings"][0]
    assert finding.concern == 0.0
    assert any("of equity, unrealised" in e for e in finding.evidence)


# ---------------------------------------------------------------------------
# Relative strength: refuse CHASING, never prefer against
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, rel):
        self.relative_strength_pct = rel
        self.benchmark_symbol = "BTC/USDT"


@pytest.mark.parametrize("direction,rel,blocked", [
    ("LONG", +1.5, True),     # buying the coin that already ran ahead
    ("SHORT", -1.5, True),    # selling the coin that already fell behind
    ("LONG", -1.5, False),    # against strength: allowed (not preferred)
    ("SHORT", +0.69, False),  # the DOGE short: within the threshold
    ("SHORT", +1.5, False),
    ("LONG", +0.9, False),
    ("LONG", None, False),    # unmeasured never blocks
])
def test_only_chasing_is_refused(direction, rel, blocked, monkeypatch):
    from backend.algorithms.market_context import CHASE_THRESHOLD_VAR, chases_relative_strength

    monkeypatch.delenv(CHASE_THRESHOLD_VAR, raising=False)
    assert (chases_relative_strength(direction, _Ctx(rel)) is not None) is blocked


def test_the_chase_gate_is_on_by_default_and_can_be_turned_off(monkeypatch):
    from backend.algorithms.market_context import CHASE_GATE_VAR, chase_gate_enabled

    monkeypatch.delenv(CHASE_GATE_VAR, raising=False)
    assert chase_gate_enabled() is True
    monkeypatch.setenv(CHASE_GATE_VAR, "false")
    assert chase_gate_enabled() is False


def test_the_gateway_applies_it_after_the_exit_branch():
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    call = "chase = chases_relative_strength(decision.direction, market_context)"
    assert call in src
    assert src.index('if decision.action == "EXIT":') < src.index(call)


def test_the_measurement_is_committed():
    src = pathlib.Path("scripts/measure_relative_strength.py").read_text(encoding="utf-8")
    assert "from backend.algorithms.market_context import percent_change" in src
    assert "NON_OVERLAPPING = True" in src


# ---------------------------------------------------------------------------
# The hard account loss limit
# ---------------------------------------------------------------------------

def test_the_hard_stop_fires_at_half_the_start_by_default(monkeypatch):
    from backend.services.loss_brakes import HARD_STOP_VAR, hard_stop_breached

    monkeypatch.delenv(HARD_STOP_VAR, raising=False)
    assert hard_stop_breached(5.0, 2.6) is None
    assert "HARD ACCOUNT LOSS LIMIT" in hard_stop_breached(5.0, 2.5)


def test_it_cannot_be_disabled_only_moved(monkeypatch):
    from backend.services.loss_brakes import HARD_STOP_VAR, hard_stop_fraction

    for raw, expect in (("0", 0.10), ("1", 0.95), ("5", 0.95), ("junk", 0.50), ("0.8", 0.8)):
        monkeypatch.setenv(HARD_STOP_VAR, raw)
        assert hard_stop_fraction() == pytest.approx(expect), raw


def test_run_until_target_does_not_switch_it_off(monkeypatch):
    from backend.services.loss_brakes import hard_stop_breached

    monkeypatch.setenv("RUN_UNTIL_TARGET", "true")
    assert hard_stop_breached(5.0, 2.0) is not None


def test_the_session_checks_it_before_the_run_until_target_floor():
    import backend.services.trading_session as ts

    loop = inspect.getsource(ts._run_session)
    assert loop.index("hard_stop_breached(session.start_equity, equity)") < loop.index(
        "if equity <= session.floor_equity:")


# ---------------------------------------------------------------------------
# Submission outcomes, the run_id column, and graph decision rows
# ---------------------------------------------------------------------------

def test_the_claim_writes_the_run_id_to_its_own_column():
    import backend.services.execution_service as es

    src = inspect.getsource(es._claim_basis_durably)
    assert "(basis, symbol, intent, run_id, outcome)" in src


def test_every_open_terminal_path_records_an_outcome():
    import backend.services.execution_service as es

    src = inspect.getsource(es.ExecutionService._handle) + inspect.getsource(
        es.ExecutionService._submit_tar)
    for outcome in ('outcome="refused"', 'outcome="dry-run"', 'outcome="submitted"'):
        assert outcome in src, outcome


def test_the_decision_row_is_written_before_the_tar_is_published():
    import backend.services.execution_service as es

    src = inspect.getsource(es.ExecutionService._submit_tar)
    assert src.index("await record_graph_decision(event, tar, size)") < src.index(
        'publish("TAR_SUBMITTED", tar)')


def test_the_cro_records_a_veto_and_the_executor_records_fill_or_not():
    import backend.agents.cro_agent as cro
    import backend.agents.execution_agent as ex

    assert 'outcome="rejected-by-risk"' in inspect.getsource(cro.CROAgent._reject)
    handle = inspect.getsource(ex.ExecutionAgent.handle_event)
    assert 'outcome="not-filled"' in handle and "finally:" in handle
    assert 'outcome="filled"' in inspect.getsource(ex.ExecutionAgent._execute_tar)


@pytest.mark.asyncio
async def test_a_refused_approval_is_recorded_as_not_filled(monkeypatch):
    """Driven, not read: an approved TAR the executor declines (no price)."""
    import uuid

    import backend.services.execution_service as es
    from backend.agents.execution_agent import ExecutionAgent
    from backend.models.events import TarApprovedEvent

    seen = []

    async def _rec(**kw):
        seen.append(kw)

    async def _resolve(*a, **kw):
        seen.append({"resolved": kw})

    monkeypatch.setattr(es, "record_submission_outcome", _rec)
    monkeypatch.setattr(es, "resolve_graph_decision", _resolve)
    agent = ExecutionAgent(simulation_mode=True)
    tar = TarApprovedEvent(tar_id=uuid.uuid4(), symbol="SOL/USDT", direction="LONG",
                           approved_size=1.0, approved_leverage=1, cro_rationale="test", stop_loss=90.0,
                           tab="paper")
    await agent.handle_event(tar)
    assert any(k.get("outcome") == "not-filled" for k in seen), seen
    assert any(k.get("resolved", {}).get("filled") is False for k in seen)
    assert agent._filled_tars == set()


@pytest.mark.asyncio
async def test_outcome_recording_without_a_database_is_silent():
    from backend.services.execution_service import record_submission_outcome

    await record_submission_outcome(basis="x", outcome="refused")   # must not raise


# ---------------------------------------------------------------------------
# Schema: RLS and security-invoker views, owner-scoped and idempotent
# ---------------------------------------------------------------------------

def test_the_schema_enables_rls_only_on_tables_this_role_owns():
    sql = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "tableowner = current_user" in sql
    assert "FORCE ROW LEVEL SECURITY" not in sql, "FORCE would lock out the app itself"
    assert "security_invoker = true" in sql
    assert "ADD COLUMN IF NOT EXISTS run_id" in sql


# ---------------------------------------------------------------------------
# The session trades the coin the operator selected
# ---------------------------------------------------------------------------

def test_the_panel_no_longer_pre_selects_extra_coins():
    src = pathlib.Path("components/operator/AutonomousSessionPanel.tsx").read_text(encoding="utf-8")
    assert "export const DEFAULT_ALSO_SCAN: string[] = [];" in src
    assert "useState<string[]>(DEFAULT_ALSO_SCAN)" in src
    assert "Will trade ANY of:" in src, "the coins that can be traded must be on screen"


# ---------------------------------------------------------------------------
# Found by running the REAL backend against a real Postgres (2026-11-03)
# ---------------------------------------------------------------------------

def test_a_no_decision_line_leads_with_this_cycles_reason_not_a_standing_note():
    from backend.graphs.nodes.opportunity import HISTORICAL_FROM_BACKTEST
    from backend.services.trading_session import _no_decision_reason

    real = "strategy selection (best score 0.25 for MeanReversion is below the 0.35 minimum)"
    out = _no_decision_reason({"unavailable": ["feed note", HISTORICAL_FROM_BACKTEST, real]})
    assert real in out and HISTORICAL_FROM_BACKTEST not in out
    # Only standing notes: still says something rather than a bare colon.
    assert _no_decision_reason({"unavailable": [HISTORICAL_FROM_BACKTEST]}).startswith("no decision:")


@pytest.mark.asyncio
async def test_a_trade_the_gateway_refused_says_so_in_the_session_log(monkeypatch):
    """The log showed 'TRADE SHORT via Grid: the panel agrees at 0.20' for a
    trade the Risk Gateway refused one node later — with the reason nowhere."""
    import backend.graphs.analysis as analysis
    import backend.services.trading_session as ts

    async def _fake_run(symbol, trigger, *a, **kw):
        return {"ok": True,
                "decision": {"action": "TRADE", "rationale": "TRADE SHORT via Breakout"},
                "risk": {"approved": False,
                         "rejectionReasons": ["chasing an extended move: 4.00 ATR"]},
                "executionPlan": None}

    monkeypatch.setattr(analysis, "run_analysis_graph", _fake_run)
    session = ts.TradingSession(
        id="s1", symbol="LTC/USDT", leverage=1, start_equity=5.0, target_equity=10.0,
        floor_equity=2.5, status="running", started_at=0.0)
    await ts._decide_once(session, "LTC/USDT")
    assert session.last_decision == "TRADE_REFUSED"
    assert "REFUSED by the Risk Gateway: chasing an extended move" in session.last_rationale
    assert session.plans_approved == 0


def test_the_wait_message_cannot_read_as_a_contradiction():
    import backend.graphs.nodes.supervisor as sup

    src = inspect.getsource(sup)
    assert "{verdict.confidence:.3f}, below the {_bar:.3f} minimum" in src
