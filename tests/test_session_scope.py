"""One position at a time, and only the coin the operator's session named.

WHY THESE GATES EXIST — FROM THE LIVE LEDGER, 2026-09-12 to 2026-09-16
----------------------------------------------------------------------
The operator started no session. The agent traded anyway, for five days:

    3,766 fills          up to 3 symbols held at once      33 opens in one hour
    2,253 closed trades  win rate 66.8%                    NET +4.15
    fees paid 288.05     gross edge ~292                   BTC: 856 closes, -35.48

Two separate problems, one gate each.

CONCURRENCY. `agents/portfolio_agent.MAX_OPEN_POSITIONS = 3` looks like a limit
and is not — it counts entries in `api/agents._tasks`, a different registry that
the graph path never writes to. Nothing enforced concurrency on the path that
actually trades; the observed ceiling of three was just three watched symbols
holding one position each. Three positions sized against one capital pool is
three times the exposure the session's allocation describes.

SESSION SCOPE. `GRAPH_EXECUTION_ENABLED=true` subscribes execution to every plan
the trigger layer produces, and a session is only one of the things that can
drive a run. So the coin, capital fraction, leverage, daily target and target
equity an operator sets when starting a session governed NOTHING — the trigger
path opened whatever it liked, including 856 closes on BTC, which is on the
untradeable list.

WHAT MUST REMAIN TRUE REGARDLESS
--------------------------------
Invariant 4. Neither gate may ever block a CLOSE. Holding a position must not
make it harder to exit, and "no session is running" must not trap an operator in
something they already hold. `gate()` returns from the EXIT branch before either
check is consulted, and that is asserted here rather than assumed.
"""

from __future__ import annotations

import pytest

from backend.graphs.nodes.risk_gateway import (
    gate,
    max_concurrent_positions,
    session_only_trading,
)
from backend.graphs.state import (
    MarketSnapshot,
    PortfolioStateSnapshot,
    TechnicalAnalysis,
    TradeDecision,
    TradeThesis,
    new_state,
)
from backend.graphs.triggers import TriggerReason


def _candles(n: int = 60, price: float = 100.0):
    return [
        {"time": i, "open": price, "high": price + 1, "low": price - 1,
         "close": price, "volume": 1000.0}
        for i in range(n)
    ]


class _FakeSession:
    id = "sess-1"
    symbol = "SOL/USDT"
    start_equity = 1000.0
    target_equity = 1200.0


def _state(symbol="SOL/USDT", action="TRADE", positions=None, equity=10_000.0):
    st = new_state(
        run_id="scope-test", symbol=symbol,
        trigger=TriggerReason(kind="manual", symbol=symbol, detail="t"),
        started_at=0.0,
    )
    st.update(
        decision=TradeDecision(action=action, direction="LONG", probability=0.6),
        trade_thesis=TradeThesis(direction="LONG", strategy="Trend",
                                 entry_price=100.0, stop_loss=98.0, take_profit=104.0),
        technical_analysis=TechnicalAnalysis(atr=1.3),
        market_data=MarketSnapshot(symbol=symbol, price=100.0,
                                   candles={"15m": _candles()}),
        portfolio_state=PortfolioStateSnapshot(tab="paper", equity=equity,
                                               cash=equity,
                                               open_positions=positions or []),
    )
    return st


def _held(symbol="ETH/USDT", qty=1.0):
    return [{"symbol": symbol, "qty": qty, "avgCost": 100.0, "marginLocked": 100.0}]


def _reasons(out):
    ra = out.get("risk_assessment")
    return " ".join(ra.rejection_reasons or []) if ra else ""


def _checks(out):
    ra = out.get("risk_assessment")
    return (ra.checks or {}) if ra else {}


# ---------------------------------------------------------------------------
# Configuration is read at CALL time
# ---------------------------------------------------------------------------

def test_the_defaults_are_one_position_and_session_only(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENT_POSITIONS", raising=False)
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    assert max_concurrent_positions() == 1
    assert session_only_trading() is True


def test_both_settings_are_read_at_call_time_not_frozen_at_import(monkeypatch):
    """The `simulation_mode` bug class: an operator changes the setting, is told
    it worked, and the running agent keeps the old behaviour until a restart."""
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "4")
    monkeypatch.setenv("SESSION_ONLY_TRADING", "false")
    assert max_concurrent_positions() == 4
    assert session_only_trading() is False


@pytest.mark.parametrize("bad", ["0", "-3", "not-a-number", ""])
def test_a_broken_concurrency_setting_floors_at_one_rather_than_halting(bad, monkeypatch):
    """Zero would block every entry forever — a config typo silently becoming a halt."""
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", bad)
    assert max_concurrent_positions() == 1


# ---------------------------------------------------------------------------
# One position at a time
# ---------------------------------------------------------------------------

def test_a_second_position_is_refused_while_one_is_open(monkeypatch):
    monkeypatch.setenv("SESSION_ONLY_TRADING", "false")
    monkeypatch.delenv("MAX_CONCURRENT_POSITIONS", raising=False)

    out = gate(_state(positions=_held("ETH/USDT")))
    assert out["risk_assessment"].approved is False
    assert "OnePositionAtATime" in _checks(out)
    assert "already holding 1 position" in _reasons(out)


def test_the_limit_is_raisable_for_an_operator_who_wants_concurrency(monkeypatch):
    monkeypatch.setenv("SESSION_ONLY_TRADING", "false")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "3")

    out = gate(_state(positions=_held("ETH/USDT")))
    assert "OnePositionAtATime" not in _checks(out)


def test_a_zero_quantity_row_is_not_a_held_position(monkeypatch):
    """A flat row left in the book must not permanently block every new entry."""
    monkeypatch.setenv("SESSION_ONLY_TRADING", "false")
    monkeypatch.delenv("MAX_CONCURRENT_POSITIONS", raising=False)

    out = gate(_state(positions=_held("ETH/USDT", qty=0.0)))
    assert "OnePositionAtATime" not in _checks(out)


# ---------------------------------------------------------------------------
# Session scope
# ---------------------------------------------------------------------------

def test_no_session_means_no_new_position(monkeypatch):
    """The five-day finding, in one assertion."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr("backend.graphs.nodes.risk_gateway.active_session", lambda: None)

    out = gate(_state())
    assert out["risk_assessment"].approved is False
    assert "SessionScope" in _checks(out)
    assert "no trading session is running" in _reasons(out)


def test_a_session_does_not_authorise_a_different_coin(monkeypatch):
    """A session trades ONE instrument. Opening another spends its capital on
    something the operator did not choose."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_session", lambda: _FakeSession()
    )

    out = gate(_state(symbol="ETH/USDT"))
    assert out["risk_assessment"].approved is False
    assert "SessionScope" in _checks(out)
    assert "SOL/USDT" in _reasons(out) and "ETH/USDT" in _reasons(out)


def test_the_session_symbol_passes_the_scope_gate(monkeypatch):
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_session", lambda: _FakeSession()
    )

    out = gate(_state(symbol="SOL/USDT"))
    assert "SessionScope" not in _checks(out)


def test_the_perpetual_spelling_is_the_same_instrument(monkeypatch):
    """`SOL/USDT` and `SOL/USDT:USDT` are one market. A scope keyed on one
    spelling would be bypassed by whichever hop resolved the symbol first —
    the bug `reconciliation._compare` already hit once."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_session", lambda: _FakeSession()
    )

    out = gate(_state(symbol="SOL/USDT:USDT"))
    assert "SessionScope" not in _checks(out)


def test_turning_session_only_off_restores_autonomous_entries(monkeypatch):
    monkeypatch.setenv("SESSION_ONLY_TRADING", "false")
    monkeypatch.setattr("backend.graphs.nodes.risk_gateway.active_session", lambda: None)

    out = gate(_state())
    assert "SessionScope" not in _checks(out)


# ---------------------------------------------------------------------------
# Invariant 4 — a close is NEVER blocked by either gate
# ---------------------------------------------------------------------------

def test_an_exit_is_approved_with_no_session_running(monkeypatch):
    """"No session" must not trap an operator in a position they already hold."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr("backend.graphs.nodes.risk_gateway.active_session", lambda: None)

    out = gate(_state(action="EXIT", positions=_held("SOL/USDT")))
    assert out["risk_assessment"].approved is True
    assert "SessionScope" not in _checks(out)


def test_an_exit_is_approved_while_at_the_position_limit(monkeypatch):
    """Holding the maximum must not make it harder to close — it is exactly when
    a gateway would otherwise refuse, and exactly when refusing is worst."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.delenv("MAX_CONCURRENT_POSITIONS", raising=False)
    monkeypatch.setattr("backend.graphs.nodes.risk_gateway.active_session", lambda: None)

    out = gate(_state(action="EXIT", positions=_held("SOL/USDT")))
    assert out["risk_assessment"].approved is True
    assert "OnePositionAtATime" not in _checks(out)


def test_an_exit_on_a_coin_the_session_did_not_name_is_still_approved(monkeypatch):
    """A position opened before the session started, or under an older config,
    must still be closable."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr(
        "backend.graphs.nodes.risk_gateway.active_session", lambda: _FakeSession()
    )

    out = gate(_state(symbol="BTC/USDT", action="EXIT", positions=_held("BTC/USDT")))
    assert out["risk_assessment"].approved is True


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_the_scope_gates_run_before_sizing(monkeypatch):
    """A refusal that is a property of the PORTFOLIO must not be reachable by
    making the trade smaller — the same reason the instrument gate is first."""
    monkeypatch.delenv("SESSION_ONLY_TRADING", raising=False)
    monkeypatch.setattr("backend.graphs.nodes.risk_gateway.active_session", lambda: None)

    # No ATR: sizing would refuse for its own reason if it were reached first.
    st = _state()
    st["technical_analysis"] = TechnicalAnalysis(atr=None)
    out = gate(st)
    assert "SessionScope" in _checks(out), "scope must be decided before sizing"


# ---------------------------------------------------------------------------
# BOTH execution paths must respect the scope, not just the graph one
# ---------------------------------------------------------------------------
#
# THE FAILURE THIS PREVENTS, MEASURED. Every gate in this project lived in
# `graphs/nodes/risk_gateway` — a node `agents/supervisor_agent` never touches.
# That was harmless while `dynamic_thresholding` demanded 0.60-0.99 confidence on
# a scale whose ceiling was 0.44 and the event path refused everything: 2,948
# decisions, zero trades. Rescaling those thresholds was correct, and it unblocked
# a path with no other gates:
#
#     4,080 fills in five days · 856 closes on BTC (untradeable)
#     3 symbols held at once   · no session ever started
#     every fill tagged strategy='Event-Driven Multi-Agent Pipeline'
#
# A limit only one of two execution paths respects is not a limit.

import ast
import pathlib


def test_the_event_driven_supervisor_consults_the_shared_scope():
    """Asserted against the source, because a behavioural test on this path needs
    a live bus, a debate, a price feed and a stress-test event — and would prove
    only that it refused THIS time."""
    src = pathlib.Path("backend/agents/supervisor_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_consider_trade"
    )
    called = {
        c.func.id for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "entry_refusal" in called, (
        "_consider_trade must consult services/trade_scope.entry_refusal before "
        "submitting a TAR — it is the only thing standing between this path and "
        "the 4,080 ungated fills it produced."
    )


def test_the_event_path_records_no_fabricated_strategy():
    """`strategy='Event-Driven Multi-Agent Pipeline'` is a PATH name, not a strategy.

    It landed in `trades.strategy` on every fill, and `strategy_performance`
    aggregates exactly that column — so 2,426 closes accumulated under one label
    matching none of the eleven real strategies, and every profile's
    `historical_success_rate` stayed None. The learning loop looked wired and was
    measuring a name.
    """
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS

    src = pathlib.Path("backend/agents/supervisor_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name != "TarSubmittedEvent":
            continue
        kw = {k.arg: k.value for k in node.keywords}
        assert "strategy" in kw, "TarSubmittedEvent must state a strategy explicitly"
        value = kw["strategy"]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            assert value.value in STRATEGY_FUNCTIONS, (
                f"strategy={value.value!r} is not one of the real strategy profiles "
                f"({sorted(STRATEGY_FUNCTIONS)}). A label that is not a strategy "
                f"poisons strategy_performance, which selects on this column."
            )


def test_the_scope_rules_have_exactly_one_definition():
    """They used to be defined inside the gateway, which is why only one path had
    them. A second copy is how the two paths diverge again."""
    gw = pathlib.Path("backend/graphs/nodes/risk_gateway.py").read_text(encoding="utf-8")
    defined_here = [
        n.name for n in ast.walk(ast.parse(gw))
        if isinstance(n, ast.FunctionDef)
        and n.name in ("max_concurrent_positions", "session_only_trading")
    ]
    assert not defined_here, (
        f"{defined_here} is defined in risk_gateway as well as services/trade_scope. "
        f"One definition, or the two execution paths drift apart again."
    )
