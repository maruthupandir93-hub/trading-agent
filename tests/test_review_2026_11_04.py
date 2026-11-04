"""The 2026-11-04 re-analysis (docs/DIAGNOSIS.md).

Measured before anything was changed, and three proposals were REJECTED by the
measurement (scripts in `scripts/`, outputs in `backtests/2026-11-04/`):

  * I-2 nearer target for range strategies   worse or equal in 3/3 windows
  * I-3 range-edge location rule             edge entries WORSE in 3/3 windows
  * I-4 "one timeframe + Ranging" consensus  sign flips across windows -> unchanged

What WAS fixed, and is pinned here:
  * I-1 the CRO approval claimed VaR was "within" a limit it had skipped
  * I-5 graph refusals never reached `decisions`; abstention read the wrong path
"""

from __future__ import annotations

import inspect
import pathlib

import pytest


class _Conn:
    def __init__(self, sink, rows=None):
        self.sink, self.rows = sink, rows or []

    async def execute(self, sql, *args):
        self.sink.append((sql, args))

    async def fetch(self, sql, *args):
        return self.rows


class _Pool:
    def __init__(self, sink, rows=None):
        self.conn = _Conn(sink, rows)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# I-1: the approval record says when the VaR check did not run
# ---------------------------------------------------------------------------

def test_the_approval_says_SKIPPED_when_the_allocation_governs():
    import backend.agents.cro_agent as cro

    src = inspect.getsource(cro.CROAgent._process_tar)
    assert "VaR check SKIPPED" in src
    assert "if implied_var > var_limit and allocation_governs:" in src
    # The "within" sentence now lives only in the branch where the check ran.
    within = src.index("within ${var_limit:.2f}")
    assert src.rfind("else:", 0, within) > src.index("VaR check SKIPPED")


# ---------------------------------------------------------------------------
# I-5: graph refusals reach `decisions`, throttled, and only for a setup
# ---------------------------------------------------------------------------

@pytest.fixture
def sink(monkeypatch):
    import backend.core.db as db

    rows = []
    monkeypatch.setattr(db, "get_db_pool", lambda: _Pool(rows))
    return rows


@pytest.mark.asyncio
async def test_a_refused_setup_is_written_with_its_side_and_reasons(sink):
    from backend.services.execution_service import GRAPH_REFUSAL_ORIGIN, record_graph_refusal

    ok = await record_graph_refusal(
        symbol="LTC/USDT", direction="SHORT", price=54.05, action="TRADE_REFUSED",
        rationale="TRADE SHORT via Breakout - REFUSED", reasons=["chasing 4.00 ATR"],
        run_id="r1", now=1000.0)
    assert ok and len(sink) == 1
    args = sink[0][1]
    assert args[2:6] == ("LTC/USDT", "sell", "paper", GRAPH_REFUSAL_ORIGIN)
    assert "chasing 4.00 ATR" in args[-1]
    assert "[graph run r1]" in args[-2]


@pytest.mark.asyncio
async def test_no_setup_means_no_row_rather_than_an_invented_side(sink):
    from backend.services.execution_service import record_graph_refusal

    assert await record_graph_refusal(symbol="XRP/USDT", direction=None, price=None,
                                      action="DO_NOT_TRADE", rationale="x") is False
    assert sink == []


@pytest.mark.asyncio
async def test_it_is_throttled_per_symbol_but_a_new_verdict_is_written(sink):
    from backend.services.execution_service import record_graph_refusal

    kw = dict(symbol="SOL/USDT", price=93.0, rationale="DO NOT TRADE")
    assert await record_graph_refusal(direction="SHORT", action="DO_NOT_TRADE", now=0.0, **kw)
    assert not await record_graph_refusal(direction="SHORT", action="DO_NOT_TRADE", now=30.0, **kw)
    assert await record_graph_refusal(direction="SHORT", action="WAIT", now=60.0, **kw)
    assert await record_graph_refusal(direction="SHORT", action="WAIT", now=700.0, **kw)
    assert len(sink) == 3


@pytest.mark.asyncio
async def test_the_session_records_a_refused_trade(monkeypatch):
    import backend.graphs.analysis as analysis
    import backend.services.execution_service as es
    import backend.services.trading_session as ts

    seen = []

    async def _fake_run(symbol, trigger, *a, **kw):
        return {"ok": True, "runId": "r9",
                "decision": {"action": "TRADE", "rationale": "TRADE SHORT"},
                "thesis": {"direction": "SHORT", "entryPrice": 54.0},
                "risk": {"approved": False, "rejectionReasons": ["chasing"]},
                "executionPlan": None}

    async def _rec(**kw):
        seen.append(kw)
        return True

    monkeypatch.setattr(analysis, "run_analysis_graph", _fake_run)
    monkeypatch.setattr(es, "record_graph_refusal", _rec)
    session = ts.TradingSession(
        id="s1", symbol="LTC/USDT", leverage=1, start_equity=5.0, target_equity=10.0,
        floor_equity=2.5, status="running", started_at=0.0)
    await ts._decide_once(session, "LTC/USDT")
    assert seen and seen[0]["action"] == "TRADE_REFUSED"
    assert seen[0]["direction"] == "SHORT"
    assert seen[0]["reasons"] == ["chasing"]
    assert seen[0]["run_id"] == "r9"


@pytest.mark.asyncio
async def test_abstention_ignores_the_switched_off_event_paths_refusals(monkeypatch):
    import backend.core.db as db
    from backend.services import abstention

    rows = [
        {"outcome": "rejected", "rejection_reasons": '["entries are originated by the graph"]',
         "ts": 1, "origin_tag": "agent-plan"},
        {"outcome": "rejected", "rejection_reasons": '["chasing 4.00 ATR"]',
         "ts": 2, "origin_tag": "graph-analysis"},
        {"outcome": "approved-executed", "rejection_reasons": "[]",
         "ts": 3, "origin_tag": "agent-plan"},
    ]

    async def _pool():
        return _Pool([], rows)

    monkeypatch.setattr(db, "get_db_pool", _pool)
    monkeypatch.setenv("GRAPH_EXECUTION_ENABLED", "true")
    fetch = next(v for v in vars(abstention).values()
                 if inspect.iscoroutinefunction(v) and "FROM decisions" in inspect.getsource(v))
    got = await fetch(100)
    outcomes = [(r["outcome"], r["rejection_reasons"]) for r in got]
    assert ("rejected", ["entries are originated by the graph"]) not in outcomes
    assert ("rejected", ["chasing 4.00 ATR"]) in outcomes
    assert any(o == "approved-executed" for o, _ in outcomes)


# ---------------------------------------------------------------------------
# The rejected proposals stay rejected until re-measured
# ---------------------------------------------------------------------------

def test_the_measurements_that_rejected_three_proposals_are_committed():
    for script in ("measure_range_rules.py", "measure_htf_consensus.py",
                   "measure_strategy_net.py"):
        src = pathlib.Path("scripts", script).read_text(encoding="utf-8")
        assert "non-overlapping" in src.lower(), script


@pytest.mark.asyncio
async def test_a_supervisor_refusal_records_its_own_reason_not_the_gateway_placeholder(monkeypatch):
    """Found by the local end-to-end run: every DO_NOT_TRADE row carried the
    gateway's 'nothing to validate' placeholder, so abstention bucketed them all
    as one generic gate."""
    import backend.graphs.analysis as analysis
    import backend.services.execution_service as es
    import backend.services.trading_session as ts

    seen = []

    async def _fake_run(symbol, trigger, *a, **kw):
        return {"ok": True, "runId": "r2",
                "decision": {"action": "DO_NOT_TRADE",
                             "rationale": "DO NOT TRADE: setup SHORT, panel NEUTRAL at 0.05"},
                "thesis": {"direction": "SHORT", "entryPrice": 1.2},
                "risk": {"approved": False, "rejectionReasons": [
                    "nothing to validate: the Supervisor decided DO_NOT_TRADE"]},
                "executionPlan": None}

    async def _rec(**kw):
        seen.append(kw)
        return True

    monkeypatch.setattr(analysis, "run_analysis_graph", _fake_run)
    monkeypatch.setattr(es, "record_graph_refusal", _rec)
    session = ts.TradingSession(
        id="s2", symbol="XRP/USDT", leverage=1, start_equity=5.0, target_equity=10.0,
        floor_equity=2.5, status="running", started_at=0.0)
    await ts._decide_once(session, "XRP/USDT")
    assert seen[0]["reasons"] == ["DO NOT TRADE: setup SHORT, panel NEUTRAL at 0.05"]


@pytest.mark.parametrize("reason,gate", [
    # The real graph sentences, verbatim from the local end-to-end run.
    ("WAIT: the panel agrees with the SHORT setup but only at 0.169, below the "
     "0.180 minimum. Cause: the liquidity constraint reduced it a further 13%.",
     "confidence-floor"),
    ("no decision: strategy selection (best score 0.25 for MeanReversion is below "
     "the 0.35 minimum — no setup worth proposing)", "no-setup"),
    # Real venue refusals still land where they belong.
    ("quantity 0.05 is below the venue minimum 0.1. Refusing", "venue-minimum"),
    ("notional 4.27 is below XRP/USDT's minimum of 5.00", "venue-minimum"),
    ("4.5 is below XRP/USDT's minimum quantity of 5 on binance", "venue-minimum"),
])
def test_a_confidence_shortfall_is_not_filed_as_an_exchange_order_size(reason, gate):
    """`venue-minimum` used to be the bare word 'minimum', which filed every WAIT
    under an exchange refusal — invisible until graph refusals reached the table."""
    from backend.services.abstention import classify

    assert classify(reason) == gate
