"""RUN_UNTIL_TARGET: losses never stop the agent; each trade's stop still does.

The operator: "i don't need any stop after loss i need it to run until the
target amount is reach". Every brake that reacts to a LOSS is measured and
logged but does not act; the per-trade stop-loss, pause and emergency stop are
untouched. Each test drives the brake in BOTH positions, so the switch is shown
to be what changes the behaviour.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("RUN_UNTIL_TARGET", "true")


def test_default_is_off_so_a_fresh_deployment_keeps_its_brakes(monkeypatch):
    from backend.services.loss_brakes import run_until_target

    monkeypatch.delenv("RUN_UNTIL_TARGET", raising=False)
    assert run_until_target() is False
    monkeypatch.setenv("RUN_UNTIL_TARGET", "true")
    assert run_until_target() is True


def _bad_day_ledger():
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return [{"timestamp": f"{today}T01:00:00", "pnl": -400.0}]


def test_the_daily_loss_limit_rejects_when_off_and_warns_when_on(monkeypatch):
    from backend.core.risk_manager import check_daily_loss

    monkeypatch.delenv("RUN_UNTIL_TARGET", raising=False)
    assert check_daily_loss(1000.0, _bad_day_ledger()).status == "reject"

    monkeypatch.setenv("RUN_UNTIL_TARGET", "true")
    check = check_daily_loss(1000.0, _bad_day_ledger())
    assert check.status == "warn"
    assert "would have stopped entries" in check.detail   # still MEASURED


@pytest.mark.asyncio
async def test_the_drawdown_killswitch_does_not_halt_when_on(monkeypatch, on):
    from backend.agents.ceo_agent import CEOAgent
    from backend.core import system_state

    system_state.exit_observation_mode("test reset")
    agent = CEOAgent()

    equity = iter([100.0, 50.0])  # a 50% drawdown, past any limit

    async def _read(tab):
        return next(equity)

    monkeypatch.setattr(agent, "_read_equity", _read)
    await agent.evaluate_mandate("test")        # sets the high-water mark
    out = await agent.evaluate_mandate("test")  # the breach
    assert out.get("haltSkipped") is True
    assert system_state.is_in_observation_mode() is False


@pytest.mark.asyncio
async def test_the_drawdown_killswitch_still_halts_when_off(monkeypatch):
    from backend.agents.ceo_agent import CEOAgent
    from backend.core import system_state

    monkeypatch.delenv("RUN_UNTIL_TARGET", raising=False)
    system_state.exit_observation_mode("test reset")
    agent = CEOAgent()
    equity = iter([100.0, 50.0])

    async def _read(tab):
        return next(equity)

    monkeypatch.setattr(agent, "_read_equity", _read)
    monkeypatch.setattr(agent, "_open_positions", lambda tab: _none())
    await agent.evaluate_mandate("test")
    out = await agent.evaluate_mandate("test")
    try:
        assert out.get("halted") is True
        assert system_state.is_in_observation_mode() is True
    finally:
        system_state.exit_observation_mode("test cleanup")


async def _none():
    return []


def test_the_reentry_cooldown_is_off_when_on(monkeypatch):
    from backend.services import reentry_cooldown as rc

    rc.record_close("XRP/USDT", "sell", "stop-loss", -0.3, at=1000.0)
    monkeypatch.delenv("RUN_UNTIL_TARGET", raising=False)
    assert rc.refusal("XRP/USDT", "SHORT", now=1001.0) is not None
    monkeypatch.setenv("RUN_UNTIL_TARGET", "true")
    assert rc.refusal("XRP/USDT", "SHORT", now=1001.0) is None


def test_the_session_floor_and_coherence_refusal_consult_the_switch():
    import backend.services.trading_session as ts

    loop = inspect.getsource(ts._run_session)
    floor = loop[loop.index("if equity <= session.floor_equity:"):]
    assert "run_until_target()" in floor[:600]
    assert "_finish(session, \"floored\"" in floor[:900], "the floor still ends a session when off"

    start = inspect.getsource(ts.start_session)
    assert "_run_until_target()" in start


def test_only_the_drawdown_killswitch_writes_observation_mode():
    """Releasing a latched Observation Mode under the switch is safe ONLY because
    the drawdown killswitch is its sole writer. If a second writer ever appears,
    this fails and the release must be revisited."""
    import pathlib

    writers = [
        str(p) for p in pathlib.Path("backend").rglob("*.py")
        if "enter_observation_mode(" in p.read_text(encoding="utf-8")
        and p.name != "system_state.py"
    ]
    assert [w.replace("\\", "/") for w in writers] == ["backend/agents/ceo_agent.py"]


def test_the_per_trade_stop_and_the_operators_hand_are_untouched(on):
    """Invariant 3, plus pause and emergency stop: none of them read the switch."""
    import backend.core.risk_manager as rm
    import backend.core.system_state as ss

    assert "run_until_target" not in inspect.getsource(rm.compute_stop_loss_take_profit)
    assert "run_until_target" not in inspect.getsource(ss)
    ss.pause("test")
    try:
        assert ss.may_open_new_position() is False
    finally:
        ss.resume("test")
