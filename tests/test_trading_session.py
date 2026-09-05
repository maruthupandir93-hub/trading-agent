"""Autonomous sessions: the target is a STOP CONDITION, never a sizing input.

WHY THIS FILE'S FIRST TEST IS THE MOST IMPORTANT ONE
----------------------------------------------------
CLAUDE.md's Primary objective section is unambiguous:

    "Do NOT optimize for a guaranteed return multiple (e.g. 'turn $X into $Y').
     That is a financial outcome, not an engineering requirement, and encoding it
     as a hard objective pushes the system toward unsafe risk-taking."

A session exists precisely to pursue "turn $X into $Y", so the line between the
safe and the unsafe version of that is thin and worth pinning in code: the target
may decide WHEN TO STOP and must never decide HOW MUCH TO TRADE.

If it ever reaches sizing, the system becomes a martingale — the further behind
schedule it falls, the harder it pushes, and the worse a losing run gets. These
tests assert the target never travels into the reasoning layer, by inspecting what
the session actually passes to the graph.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services import trading_session as ts


@pytest.fixture(autouse=True)
def clean_sessions():
    ts._reset_for_tests()
    yield
    ts._reset_for_tests()


@pytest.fixture
def paper_equity(monkeypatch):
    """A measurable paper book at 1000, and a stubbed graph that records its input."""
    calls: list[dict] = []

    async def fake_equity(tab):
        return 1000.0

    monkeypatch.setattr(ts, "current_equity", fake_equity)
    return calls


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

def test_the_target_never_reaches_the_analysis_graph(monkeypatch, paper_equity):
    """`_decide_once` must not pass the target into the reasoning layer.

    The graph receives a symbol and a TriggerReason. The reason is a human-readable
    string for the audit trail; nothing downstream parses a number out of it, and
    the session passes no target field at all. This asserts both: the call carries
    no numeric target argument, and the detail string says so explicitly.
    """
    captured: dict = {}

    async def fake_run(symbol, trigger, **kwargs):
        captured["symbol"] = symbol
        captured["trigger"] = trigger
        captured["kwargs"] = kwargs
        return {"ok": True, "decision": {"action": "DO_NOT_TRADE", "rationale": "no setup"}}

    monkeypatch.setattr("backend.graphs.analysis.run_analysis_graph", fake_run)

    session = ts.TradingSession(
        id="t1", symbol="BTC/USDT", leverage=2,
        start_equity=1000.0, target_equity=5000.0, floor_equity=500.0,
    )
    asyncio.run(ts._decide_once(session))

    assert captured["symbol"] == "BTC/USDT"
    assert captured["kwargs"] == {}, (
        "the session passed extra arguments to the graph; a target, a shortfall or a "
        "size hint reaching the reasoning layer is the martingale this forbids"
    )
    # The reason names the target only to say it was NOT used for sizing.
    assert "NOT used to size" in captured["trigger"].detail


def test_a_session_far_behind_its_target_decides_identically(monkeypatch, paper_equity):
    """Two sessions, same market, wildly different targets -> identical graph input.

    This is the property that makes the target safe. If the agent behaved
    differently when it was 5x behind versus 1.1x behind, the target would be
    influencing risk even without a numeric path into sizing.
    """
    seen: list = []

    async def fake_run(symbol, trigger, **kwargs):
        seen.append((symbol, kwargs))
        return {"ok": True, "decision": {"action": "DO_NOT_TRADE", "rationale": "x"}}

    monkeypatch.setattr("backend.graphs.analysis.run_analysis_graph", fake_run)

    modest = ts.TradingSession(id="a", symbol="BTC/USDT", leverage=2,
                               start_equity=1000.0, target_equity=1100.0, floor_equity=500.0)
    greedy = ts.TradingSession(id="b", symbol="BTC/USDT", leverage=2,
                               start_equity=1000.0, target_equity=50_000.0, floor_equity=500.0)

    asyncio.run(ts._decide_once(modest))
    asyncio.run(ts._decide_once(greedy))

    assert seen[0] == seen[1], (
        "a session further from its target sent different input to the graph — the "
        "target is influencing behaviour and must not"
    )


# ---------------------------------------------------------------------------
# Refusals at start
# ---------------------------------------------------------------------------

def test_a_target_at_or_below_current_equity_is_refused(monkeypatch, paper_equity):
    with pytest.raises(ValueError, match="not above the current equity"):
        asyncio.run(ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=1000.0))
    with pytest.raises(ValueError, match="not above the current equity"):
        asyncio.run(ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=10.0))


def test_leverage_above_the_hard_ceiling_is_refused(monkeypatch, paper_equity):
    """The ceiling is `core.risk_manager.max_leverage_ceiling`, not a local number.

    A session that could name its own leverage would be the cleanest possible way
    around CLAUDE.md invariant 2, which is why this reads the same function the
    Risk Gateway does.
    """
    with pytest.raises(ValueError, match="ceiling is not operator-configurable"):
        asyncio.run(ts.start_session(symbol="BTC/USDT", leverage=99, target_equity=2000.0))


def test_two_concurrent_sessions_are_refused(monkeypatch, paper_equity):
    """Two sessions on one book would each size against equity the other is using."""
    async def go():
        await ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=2000.0)
        with pytest.raises(ValueError, match="already running"):
            await ts.start_session(symbol="ETH/USDT", leverage=1, target_equity=2000.0)
        await ts.stop_all()

    asyncio.run(go())


def test_unmeasurable_equity_refuses_to_start(monkeypatch):
    """A session with no equity reading has no definition of done."""
    async def no_equity(tab):
        return None

    monkeypatch.setattr(ts, "current_equity", no_equity)
    with pytest.raises(ValueError, match="cannot be measured"):
        asyncio.run(ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=2000.0))


def test_a_floor_at_or_above_the_start_is_refused(monkeypatch, paper_equity):
    with pytest.raises(ValueError, match="must be below the starting equity"):
        asyncio.run(ts.start_session(
            symbol="BTC/USDT", leverage=1, target_equity=2000.0, floor_equity=1000.0,
        ))


def test_a_floor_is_always_set_even_when_not_supplied(monkeypatch, paper_equity):
    """'Trade until the target' with no floor means 'trade until zero'."""
    async def go():
        s = await ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=2000.0)
        assert s.floor_equity == pytest.approx(1000.0 * ts.DEFAULT_FLOOR_FRACTION)
        assert s.floor_equity > 0
        await ts.stop_all()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------

def test_stopping_a_session_does_not_close_positions(monkeypatch, paper_equity):
    """Flattening the book from a button labelled 'stop' would be a large,
    irreversible, slippage-bearing trade nobody asked for — the same reasoning
    `api/admin.emergency_stop` gives for not doing it either."""
    import backend.agents.execution_agent as ea

    closes: list = []

    async def spy_close(**kwargs):
        closes.append(kwargs)
        return 1.0

    async def go():
        s = await ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=2000.0)
        monkeypatch.setattr(ea.get_execution_agent(), "close_position", spy_close, raising=False)
        stopped = await ts.stop_session(s.id, "operator")
        assert stopped is not None and stopped.status == "stopped"
        assert closes == [], "stopping the session closed a position"

    asyncio.run(go())


def test_a_finished_session_is_no_longer_active(monkeypatch, paper_equity):
    async def go():
        s = await ts.start_session(symbol="BTC/USDT", leverage=1, target_equity=2000.0)
        assert ts.active_session() is not None
        await ts.stop_session(s.id)
        assert ts.active_session() is None
        assert s.finished_at is not None and s.stop_reason

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def test_an_interrupted_session_is_restored_stopped_not_resumed(monkeypatch, tmp_path):
    """Resuming real trading on process boot, unasked, would be the worst possible
    reading of a restart — the operator may have restarted to make it stop."""
    import json

    store = tmp_path / "trading_sessions.json"
    store.write_text(json.dumps([{
        "id": "old", "symbol": "BTC/USDT", "leverage": 2,
        "start_equity": 1000.0, "target_equity": 5000.0, "floor_equity": 500.0,
        "status": "running", "started_at": 1.0, "finished_at": None,
        "stop_reason": None, "cycles_run": 7, "trades_opened": 2,
        "last_cycle_at": None, "last_decision": None, "last_rationale": None, "log": [],
    }]), encoding="utf-8")

    monkeypatch.setattr(ts, "_STORE_PATH", str(store))
    interrupted = ts.restore()

    assert interrupted == 1
    restored = ts.get_session("old")
    assert restored is not None
    assert restored.status == "stopped"
    assert restored.active is False
    assert "NOT resumed" in (restored.stop_reason or "")


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------

def test_equity_is_none_when_a_position_cannot_be_priced(monkeypatch):
    """A stop condition compared against a partial figure is worse than no figure.

    Valuing an unpriceable position at cost would report a losing position as flat,
    and the session would keep trading against a number it had made up.
    """
    async def fake_portfolio():
        return {"paper": {"cash": 500.0, "positions": [{"symbol": "WAT/USDT", "qty": 1.0}]}}

    monkeypatch.setattr("backend.services.portfolio_store.get_portfolio", fake_portfolio)
    monkeypatch.setattr("backend.services.market_data.get_price", lambda s: 0.0)

    assert asyncio.run(ts.current_equity("paper")) is None


def test_equity_is_free_cash_plus_locked_margin_plus_unrealized(monkeypatch):
    """THIS TEST USED TO ASSERT THE BUG.

    It expected `cash + qty * price` — 500 + 2*100 = 700 — which is only correct
    at 1x leverage. The book deducts MARGIN from cash, so cash is FREE cash;
    adding the whole notional back double-counts the leveraged part. At 10x a
    7,000 position funded by 700 of margin reported 6,300 of equity that did not
    exist, and a session compares its target against this number.

    So: 500 free + 200 locked + 20 unrealized = 720.
    """
    async def fake_portfolio():
        return {
            "paper": {
                "cash": 500.0,
                "positions": [{
                    "symbol": "BTC/USDT", "qty": 2.0, "avgCost": 100.0,
                    "marginLocked": 200.0, "side": "buy",
                }],
            }
        }

    monkeypatch.setattr("backend.services.portfolio_store.get_portfolio", fake_portfolio)
    monkeypatch.setattr("backend.services.market_data.get_price", lambda s: 110.0)

    assert asyncio.run(ts.current_equity("paper")) == pytest.approx(720.0)


def test_equity_values_a_SHORT_in_the_right_direction(monkeypatch):
    """`qty * price` ignored direction, so a short moving AGAINST the operator
    read as equity going up — and a session would have reported progress toward
    its target while losing money."""
    async def fake_portfolio():
        return {
            "paper": {
                "cash": 500.0,
                "positions": [{
                    "symbol": "BTC/USDT", "qty": 2.0, "avgCost": 100.0,
                    "marginLocked": 200.0, "side": "sell",
                }],
            }
        }

    monkeypatch.setattr("backend.services.portfolio_store.get_portfolio", fake_portfolio)
    # Price UP is a LOSS on a short: 500 + 200 - 20 = 680.
    monkeypatch.setattr("backend.services.market_data.get_price", lambda s: 110.0)

    assert asyncio.run(ts.current_equity("paper")) == pytest.approx(680.0)


def test_equity_refuses_a_position_with_no_cost_basis(monkeypatch):
    """Unrealized P&L needs an entry price. Valuing it at notional instead — what
    the old formula did — reports a losing position as flat."""
    async def fake_portfolio():
        return {"paper": {"cash": 500.0, "positions": [{"symbol": "BTC/USDT", "qty": 2.0}]}}

    monkeypatch.setattr("backend.services.portfolio_store.get_portfolio", fake_portfolio)
    monkeypatch.setattr("backend.services.market_data.get_price", lambda s: 100.0)

    assert asyncio.run(ts.current_equity("paper")) is None
