"""The drawdown killswitch halted the account and nothing could release it.

WHAT HAPPENED, read from the operator's live session on 2026-10-08 after it had
run 120 hours and stopped opening positions:

    SESSION   $2.00 -> $25.00, floor $1.00, 10x, 100% allocation
    EQUITY    $1.7332
    LOG       50 of the last 50 entries, identical:
              "[XRP/USDT] DO_NOT_TRADE: No new position may be opened:
               observation mode (Equity $1.73 is 11.04% below the 2026-10
               high-water mark of $1.95, exceeding the 10% drawdown limit)"

The agent was not stuck, misconfigured or crashed. A safety control fired, and
then THREE separate gaps turned a brake into a permanent stop:

  1. THE LIMIT WAS UNREACHABLE TO BEGIN WITH. Measured from the same account:
     the average stop-out was $0.0531, which is 2.66% of $2.00. A 10% limit is
     therefore FOUR consecutive losses. The payoff is a fixed 2:1, so
     break-even is a ~36% win rate, and four straight losses at that rate is
     about one run in six. It took exactly four Breakout losses (-0.2123,
     -10.6%) and halted on the fourth. The killswitch did its job; the job was
     arithmetically impossible.

  2. NOTHING COULD CLEAR IT. `core/system_state.exit_observation_mode` has
     existed all along and its docstring calls leaving "a deliberate
     acknowledgement" — but no route called it. The only exit was restarting
     the process, which clears the in-memory mark as an undocumented side
     effect.

  3. IT WAS INVISIBLE. `GET /api/admin/status` reported
     `isPaused: false, emergencyStop: false` throughout — a dashboard showing
     green over a halted system, which is worse than no indicator because it
     argues against looking further.

And beneath all three, the DEADLOCK: the mark is the MONTH's peak, so an
account 11% below it is still 11% below it the moment it resumes. Equity climbs
only by trading; trading is what the halt forbids. Without re-anchoring, the
account cannot recover inside the month.
"""

from __future__ import annotations

import os

import pytest

from backend.agents import ceo_agent as ceo
from backend.core import system_state as st


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ceo.DRAWDOWN_LIMIT_VAR, raising=False)
    st.exit_observation_mode("test setup")
    # The CEO is a singleton now, so one test's high-water mark would otherwise
    # become the next one's starting peak — the same reason `conftest` resets
    # the monitor and the executor around every test.
    ceo.reset_ceo_agent()
    yield
    st.exit_observation_mode("test teardown")
    ceo.reset_ceo_agent()


def test_the_ceo_is_a_singleton():
    """IT WAS NOT, AND THAT WOULD HAVE MADE THE RESUME ROUTE A NO-OP.

    `get_ceo_agent` used to `return CEOAgent()` — a fresh, empty agent per call
    — while `main.py` builds one at startup and subscribes it to the bus. The
    HALT was never affected; the bus instance tracked the mark correctly. What
    broke was asking about it or acting on it from outside: the observation
    route would report `highWaterMark: null`, and `rearm_high_water_mark` would
    re-anchor a THROWAWAY object and return happily while the real agent kept
    its old mark and halted again on the next closed trade.

    A control that reports success while doing nothing is the `simulation_mode`
    failure, and this one sat on the operator's first attempt to recover.
    """
    a = ceo.get_ceo_agent()
    a._high_water_mark = 42.0
    assert ceo.get_ceo_agent() is a
    assert ceo.get_ceo_agent()._high_water_mark == 42.0

    ceo.reset_ceo_agent()
    assert ceo.get_ceo_agent()._high_water_mark is None


# ---------------------------------------------------------------------------
# 1. The limit is configurable, because 10% was not compatible with 10x
# ---------------------------------------------------------------------------

def test_the_limit_defaults_to_ten_percent():
    assert ceo.max_drawdown_fraction() == pytest.approx(0.10)
    assert ceo.MAX_DRAWDOWN_FROM_HIGH_WATER_MARK == 0.10


def test_the_limit_is_read_at_call_time(monkeypatch):
    """The `simulation_mode` rule. A frozen-at-import constant means the
    operator raises the limit, is told it worked, and the running agent keeps
    halting on the old number until a restart."""
    monkeypatch.setenv(ceo.DRAWDOWN_LIMIT_VAR, "0.25")
    assert ceo.max_drawdown_fraction() == pytest.approx(0.25)
    monkeypatch.setenv(ceo.DRAWDOWN_LIMIT_VAR, "0.40")
    assert ceo.max_drawdown_fraction() == pytest.approx(0.40)


@pytest.mark.parametrize("bad", ["nonsense", "0", "1", "1.5", "-0.1", ""])
def test_an_unusable_limit_falls_back_rather_than_disabling_the_brake(monkeypatch, bad):
    """0 would halt on the first tick that is not a new high; above 1 can never
    fire. Both are refused in favour of the default — a typo must not silently
    remove the brake, and must not silently weld it shut either."""
    monkeypatch.setenv(ceo.DRAWDOWN_LIMIT_VAR, bad)
    assert ceo.max_drawdown_fraction() == pytest.approx(0.10)


def test_the_arithmetic_that_made_ten_percent_unreachable():
    """Not a code test — a guard on the REASONING, so the next person to lower
    the limit sees what it costs. These are the operator's measured numbers.

    If this ever fails because the stop multiplier or the VaR budget changed,
    the limit needs revisiting with it: they are one decision, not two.
    """
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER

    payoff = ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER
    breakeven = ATR_STOP_MULTIPLIER / (ATR_STOP_MULTIPLIER + ATR_TARGET_MULTIPLIER)
    assert payoff == 2.0
    assert round(breakeven, 3) == 0.333

    per_trade_loss = 0.0531 / 2.00          # measured: $0.0531 on a $2.00 account
    losses_to_trip = 0.10 / per_trade_loss
    assert 3.0 < losses_to_trip < 4.5, (
        f"a 10% limit tolerates only {losses_to_trip:.1f} stop-outs at this risk"
    )

    # Four straight losses at break-even skill is routine, not a disaster.
    p_four = (1 - 0.358) ** 4
    assert p_four > 0.15, "four consecutive losses must be recognised as common"


# ---------------------------------------------------------------------------
# 2. There is a way out now
# ---------------------------------------------------------------------------

def test_exit_observation_mode_existed_but_no_route_called_it():
    """The function was never the gap. Pinned so the ROUTE is what must stay."""
    assert hasattr(st, "exit_observation_mode")

    import backend.api.admin as admin

    paths = {r.path for r in admin.router.routes}
    assert "/observation/resume" in paths, "an operator needs a way to acknowledge"
    assert "/observation" in paths, "and a way to see the arithmetic"


@pytest.mark.asyncio
async def test_resuming_clears_the_halt_AND_reanchors_the_mark():
    """BOTH HALVES, because doing only the first is a no-op.

    The mark is the month's peak. An account that halted 11% below it is still
    11% below it the instant it resumes, so the CEO re-evaluates on the next
    closed trade and halts again. Equity climbs only by trading and trading is
    what the halt forbids — a deadlock, not a safety property.
    """
    import backend.api.admin as admin

    agent = ceo.get_ceo_agent()
    agent._high_water_mark = 1.95
    agent._last_equity = 1.7332
    agent._hwm_period = agent._current_period()
    st.enter_observation_mode("test: 11.04% below the mark")
    assert st.is_in_observation_mode() is True

    out = await admin.resume_from_observation({})

    assert out["ok"] is True
    assert out["wasHalted"] is True
    assert st.is_in_observation_mode() is False
    # THE RE-ANCHOR IS THE PART THAT MATTERS.
    assert out["highWaterMark"] == pytest.approx(1.7332), (
        "without re-anchoring, the next evaluation halts again immediately"
    )
    assert agent._high_water_mark == pytest.approx(1.7332)


@pytest.mark.asyncio
async def test_resuming_is_safe_when_nothing_was_halted():
    """An operator clicking it on a healthy system must not break anything."""
    import backend.api.admin as admin

    agent = ceo.get_ceo_agent()
    agent._high_water_mark = 100.0
    agent._last_equity = 100.0
    assert st.is_in_observation_mode() is False

    out = await admin.resume_from_observation({})
    assert out["ok"] is True
    assert out["wasHalted"] is False
    assert st.is_in_observation_mode() is False


def test_rearming_refuses_to_invent_a_mark():
    """Invariant 6. With no measured equity there is no honest value to anchor
    to, so it returns None rather than a plausible number."""
    agent = ceo.get_ceo_agent()
    agent._last_equity = None
    agent._high_water_mark = 50.0
    assert ceo.rearm_high_water_mark(None) is None
    assert agent._high_water_mark == 50.0, "the old mark must be left alone"


# ---------------------------------------------------------------------------
# 3. It is visible now
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_status_route_reports_the_halt_that_is_actually_in_force():
    """IT REPORTED `isPaused: false, emergencyStop: false` WHILE HALTED.

    `may_open_new_position()` is false for all three states, so a status route
    naming two of them describes a different system from the one the gate
    reads — and the operator's dashboard showed green for days.
    """
    import backend.api.admin as admin

    st.enter_observation_mode("test: drawdown limit")
    out = await admin.get_status()

    assert out["observationMode"] is True
    assert "drawdown" in (out["observationReason"] or "").lower()
    # And the two it already reported are untouched.
    assert out["isPaused"] is False
    assert out["emergencyStop"] is False


@pytest.mark.asyncio
async def test_the_status_route_agrees_with_the_gate_in_every_state():
    """The property, rather than the three fields: if the status route says
    nothing is blocking, opening a position must actually be allowed."""
    import backend.api.admin as admin
    from backend.core.system_state import may_open_new_position

    out = await admin.get_status()
    blocked_by_status = (
        out["isPaused"] or out["emergencyStop"] or out["observationMode"]
    )
    assert bool(blocked_by_status) is not bool(may_open_new_position())

    st.enter_observation_mode("test")
    out2 = await admin.get_status()
    blocked2 = out2["isPaused"] or out2["emergencyStop"] or out2["observationMode"]
    assert bool(blocked2) is not bool(may_open_new_position())


@pytest.mark.asyncio
async def test_the_observation_route_shows_the_arithmetic_not_just_a_boolean():
    """An operator who sees "11.04% below a $1.95 mark against a 10% limit" can
    act. One who sees "halted" can only guess, which is what happened."""
    import backend.api.admin as admin

    agent = ceo.get_ceo_agent()
    agent._high_water_mark = 1.95
    agent._last_equity = 1.7332
    st.enter_observation_mode("test")

    out = await admin.get_observation()
    assert out["observationMode"] is True
    assert out["highWaterMark"] == pytest.approx(1.95)
    assert out["lastEquity"] == pytest.approx(1.7332)
    assert out["drawdownPct"] == pytest.approx(11.12, abs=0.2)
    assert out["limitPct"] == pytest.approx(10.0)
    assert out["limitVariable"] == ceo.DRAWDOWN_LIMIT_VAR
    # And it names the trade-off rather than only the number.
    assert "leverage" in out["note"].lower()
