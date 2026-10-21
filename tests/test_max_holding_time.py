"""A position must not outlive the strategy that chose it.

THE CONFIRMED FINDING (docs/DIAGNOSIS.md, P1 "position duration does not
consistently match the strategy label"): a recorded `Scalping` trade stayed
open for over an hour, other positions lasted ~9 hours, and the monitored paper
position had been open more than 14 hours.

It was not a mislabelling. `position_monitor` had exactly three exits -- the
stop, the target and the partial scale-out -- and NO concept of time, while
every strategy profile had carried `expected_holding_time` as prose since
Section 11.3 with nothing reading it. The same shape this project has recorded
a dozen times: a value declared where it is consumed and never produced.

Why it is a loss source and not just untidy: past its horizon, a position is no
longer the trade the signal justified. The setup that selected it has expired,
it is paying funding nobody budgeted for, and it is exposed to a market that
nothing analysed. A `Scalping` profile whose own risk note says it "needs a high
win rate to survive costs" held for 14 hours is pure cost.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest

from backend.core.message_bus import MessageBus
from backend.models.events import OrderFilledEvent, TarApprovedEvent


@pytest.fixture
def bus(monkeypatch):
    private = MessageBus()
    import backend.core.message_bus as mb

    monkeypatch.setattr(mb, "get_message_bus", lambda: private)
    for module in ("backend.core.agent_base", "backend.agents.position_monitor"):
        try:
            mod = __import__(module, fromlist=["get_message_bus"])
            if hasattr(mod, "get_message_bus"):
                monkeypatch.setattr(mod, "get_message_bus", lambda: private)
        except (ImportError, AttributeError):
            pass
    return private


class FakeExecution:
    def __init__(self):
        self.closes = []

    async def close_position(self, **kw):
        self.closes.append(kw)
        return kw.get("observed_price", 100.0)


@pytest.fixture
def monitor(bus, monkeypatch):
    # PINNED, not inherited. A test that reads its own enablement from a default
    # is really a test of the default -- the lesson `test_partial_tp` paid for.
    monkeypatch.setenv("PROFIT_TARGET_PCT", "0")
    monkeypatch.setenv("PARTIAL_TP_FRACTION", "0")
    monkeypatch.setenv("TRAILING_STOP_R", "0")

    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    agent.attach_execution(FakeExecution())
    return agent


def _open(monitor, *, strategy, entry=100.0, stop=97.5, side="buy", qty=1.0):
    """Open through the REAL event path.

    `direction` is the uppercase Literal both producers emit. Seven test files
    once built it lowercase and were silently exercising the SELL branch; the
    event is a Literal now and this would be a loud ValidationError.
    """
    tar_id = uuid.uuid4()
    asyncio.run(monitor.handle_event(TarApprovedEvent(
        tar_id=tar_id, symbol="SOL/USDT",
        direction="LONG" if side == "buy" else "SHORT",
        approved_size=qty, approved_leverage=3, cro_rationale="ok",
        stop_loss=stop, tab="paper", strategy=strategy,
        take_profit=(entry + 5000) if side == "buy" else (entry - 5000),
    )))
    asyncio.run(monitor.handle_event(OrderFilledEvent(
        tar_id=tar_id, exchange="x", order_id=str(uuid.uuid4()), symbol="SOL/USDT",
        side=side, tab="paper", fill_price=entry, fill_quantity=qty,
        slippage_bps=0.0, fee=0.0,
    )))
    return monitor._open[str(tar_id)]


def _age(pos, minutes):
    """Backdate the position. Naive UTC, matching the whole module."""
    pos.opened_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)


def _tick(monitor, price=100.0):
    asyncio.run(monitor._check_price("SOL/USDT", price))


# ---------------------------------------------------------------------------
# The bound exists at all
# ---------------------------------------------------------------------------

def test_every_profile_declares_an_enforceable_bound():
    """Prose alone is what let a 14-hour scalp happen."""
    from backend.algorithms.strategy_profiles import STRATEGY_PROFILES

    for profile in STRATEGY_PROFILES:
        assert profile.expected_holding_time, profile.agent
        bound = profile.max_holding_minutes
        assert isinstance(bound, int) and bound > 0, (
            f"{profile.agent} declares '{profile.expected_holding_time}' and "
            f"carries max_holding_minutes={bound!r}, so nothing can enforce it"
        )


def test_a_scalp_is_bounded_in_minutes_and_a_swing_in_days():
    """The bound must follow the strategy's own horizon, not one global number.

    A single constant would either cut trend-followers off at a scalp's horizon
    (destroying the 2:1 payoff that makes them viable) or let a scalp run for
    days, which is the bug.
    """
    from backend.algorithms.strategy_profiles import get_profile

    assert get_profile("Scalping").max_holding_minutes <= 60
    assert get_profile("Arbitrage").max_holding_minutes <= 60
    assert get_profile("Swing").max_holding_minutes >= 1440
    assert get_profile("Trend").max_holding_minutes >= 720


# ---------------------------------------------------------------------------
# The monitor enforces it
# ---------------------------------------------------------------------------

def test_a_scalp_held_past_its_horizon_is_closed_on_time(monitor):
    """The live symptom, reproduced: a Scalping position open for over an hour."""
    pos = _open(monitor, strategy="Scalping")
    # Scalping DECLARES 30 minutes; the EFFECTIVE bound is floored at
    # MIN_HOLDING_BARS of the analysis timeframe (8 x 15m = 120 min). See
    # `test_the_bound_is_floored_so_the_clock_stays_a_backstop` for why.
    _age(pos, 180)

    _tick(monitor, 100.0)              # neither stop nor target is anywhere near

    assert not monitor.snapshot_open(), "a scalp 3x past its horizon stayed open"
    assert monitor._execution.closes, "nothing was actually closed"
    assert monitor._execution.closes[-1]["reason"] == "max-holding-time"


def test_a_position_inside_its_horizon_is_left_alone(monitor):
    """The bound is a backstop, never a second profit target.

    Cutting a position at its MEDIAN expected hold would exit winners early,
    which is precisely how a 2:1 payoff is destroyed.
    """
    pos = _open(monitor, strategy="Trend")
    _age(pos, 60)                      # the Trend bound is 2 days

    _tick(monitor, 100.0)

    assert monitor.snapshot_open(), "a trend trade one hour in was closed"


def test_the_stop_still_wins_when_both_apply(monitor):
    """A tick that hits the stop must close as `stop-loss`.

    Re-labelling it would corrupt the attribution the learning loop reads --
    `strategy_performance` would be told the clock took a trade out when the
    market did, and the stop distance it measures would never have fired.
    """
    pos = _open(monitor, strategy="Scalping", entry=100.0, stop=97.5)
    _age(pos, 600)                     # far past its bound AND the stop is hit

    _tick(monitor, 97.0)

    assert monitor._execution.closes[-1]["reason"] == "stop-loss"


def test_a_manual_position_has_no_horizon_imposed_on_it(monitor):
    """A human's click was not chosen by an algorithm.

    Manual positions close with `strategy` NULL by design, and inventing a
    horizon for one would close the operator's own trade on a rule they never
    chose. The absence must mean "no bound", never a default.
    """
    pos = _open(monitor, strategy=None)
    _age(pos, 60 * 24 * 30)            # a month

    _tick(monitor, 100.0)

    assert monitor.snapshot_open(), "a manual position was closed on a rule it never opted into"


def test_an_unknown_strategy_is_not_given_a_guessed_bound(monitor):
    """A strategy with no profile is unbounded rather than bounded by a guess."""
    pos = _open(monitor, strategy="SomethingNobodyWrote")
    _age(pos, 60 * 24 * 30)

    _tick(monitor, 100.0)

    assert monitor.snapshot_open()


def test_the_time_exit_is_a_close_and_never_a_halt(monitor):
    """Invariant 4: an exit may never be blocked, and this only ever reduces risk.

    Asserted structurally as well as behaviourally -- it must go through the
    same ungated `_close` the stop uses, not a new path that a pause could gate.
    """
    from backend.agents.position_monitor import PositionMonitorAgent
    from tests.sourceutil import code_only

    # ASSERTED ON STRUCTURE, NOT ON THE STRING. `code_only` strips string
    # literals -- that is the whole reason it exists -- so the reason text is
    # not visible here and the behavioural tests above cover it instead. What
    # this adds is that the bound is read and the ungated `_close` is what acts
    # on it, rather than a new path a pause could gate.
    import inspect

    src = code_only(inspect.getsource(PositionMonitorAgent._check_price))
    assert "_max_holding_minutes" in src, "the bound is never read"
    assert "_held_minutes" in src, "the age is never measured"
    assert "_close" in src, "the time exit must act through _close"
    # And nothing in this node may consult a halt before exiting (invariant 4).
    for forbidden in ("is_paused", "emergency_stop", "may_open_new_position"):
        assert forbidden not in src, (
            f"_check_price consults {forbidden}; an exit may never be gated"
        )


def test_the_bound_is_floored_so_the_clock_stays_a_backstop(monitor):
    """A rule that fires on almost every trade is not a backstop.

    MEASURED on 5 instruments x 1000 real 15m candles with the RAW profile
    bounds: Arbitrage ended 98% of its trades on the clock and Scalping 95%,
    because their stated horizons ("Seconds", "Seconds to minutes") are one and
    two bars of a 15-minute chart. A position cut after one bar never had the
    chance to reach either level, so the clock had become the PRIMARY exit.

    NOT AN EXPECTANCY CLAIM, and that distinction is the point. Pooled over 850
    trades the floor moves the edge by at most 0.016R while
    `evidence.required_gap(850)` puts the smallest detectable gap at 0.0988R --
    six times larger. An earlier single-symbol run showed the time exit
    improving 7 of 11 strategies and a four-symbol run showed it harming them;
    both were the same noise, read in the direction the reader was hoping for.

    The original complaint is still fixed: eight bars of a 15m chart is two
    hours, and the live positions that prompted this ran nine and fourteen.
    """
    from backend.algorithms.strategy_profiles import (
        MIN_HOLDING_BARS, ANALYSIS_BAR_MINUTES, effective_holding_minutes, get_profile,
    )

    floor = MIN_HOLDING_BARS * ANALYSIS_BAR_MINUTES
    assert get_profile("Scalping").max_holding_minutes < floor
    assert effective_holding_minutes("Scalping") == floor

    # Behaviourally: inside the floor it is left alone...
    pos = _open(monitor, strategy="Scalping")
    _age(pos, floor - 30)
    _tick(monitor, 100.0)
    assert monitor.snapshot_open(), "closed before the floored bound"

    # ...and past it, it closes.
    _age(pos, floor + 30)
    _tick(monitor, 100.0)
    assert not monitor.snapshot_open()


def test_a_declared_bound_above_the_floor_is_used_unchanged(monitor):
    """The floor raises short bounds; it must not lower long ones."""
    from backend.algorithms.strategy_profiles import effective_holding_minutes, get_profile

    for agent in ("Trend", "Swing", "Breakout", "Range"):
        declared = get_profile(agent).max_holding_minutes
        assert effective_holding_minutes(agent) == declared, agent


def test_the_backtest_and_the_monitor_apply_THE_SAME_bound():
    """Issue E in one assertion.

    A backtest applying a different bound from production measures a strategy
    production does not run — and it would be invisible, because both would
    look reasonable in isolation. One function, called by both.
    """
    import inspect
    from tests.sourceutil import code_only
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.core import strategy_backtest as bt

    monitor_src = code_only(inspect.getsource(PositionMonitorAgent._max_holding_minutes))
    backtest_src = code_only(inspect.getsource(bt.backtest_strategy))
    assert "effective_holding_minutes" in monitor_src
    assert "effective_holding_minutes" in backtest_src
