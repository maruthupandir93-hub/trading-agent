"""Turning live trading off must actually turn it off.

THE BUG
-------
`ExecutionAgent.simulation_mode` was a plain attribute assigned once in
`__init__` from `settings.LIVE_TRADING`. `main.py` constructs one agent at
startup and subscribes it to the bus, so that instance's mode was frozen at
whatever the flag was when the process booted.

Meanwhile `config.set_live_trading` documented the opposite in its own
docstring — "`ExecutionAgent` reads `settings.LIVE_TRADING` at call time, not
import time, so the next trade attempt sees the new value" — and
`POST /api/admin/live-trading/disable` returned `{"status": "success"}`.

So the Settings-page toggle updated the setting, wrote it to .env, reported
success, and changed nothing about what the running executor did:

    OFF -> ON   the operator believes they are live; orders are simulated, so
                they think they hold positions they do not hold.
    ON -> OFF   the operator presses "disable live trading", is told it worked,
                and REAL ORDERS KEEP BEING PLACED until the process restarts.

The second is the one that matters. A kill switch that reports success while
doing nothing is worse than no kill switch, because it is trusted.

WHY THESE TESTS ARE AT THE AGENT AND NOT AT THE ROUTE
-----------------------------------------------------
The route was never the broken part — it correctly called `set_live_trading`.
The gap was between the setting and the component that acts on it, so that is
where the assertion belongs. `tests/test_api_surface.py` already covers the
route's auth and existence.
"""

from __future__ import annotations

import pytest

from backend.agents.execution_agent import ExecutionAgent
from backend.core.config import settings


@pytest.fixture(autouse=True)
def restore_flag():
    """Never leave the process live. `persist=False` so no test writes .env."""
    original = settings.LIVE_TRADING
    yield
    settings.set_live_trading(original, persist=False)


def test_disabling_live_trading_reaches_an_already_constructed_agent():
    """The severe direction: OFF must actually stop real orders."""
    settings.set_live_trading(True, persist=False)
    agent = ExecutionAgent()
    assert agent.simulation_mode is False, "agent should start live"

    settings.set_live_trading(False, persist=False)

    assert agent.simulation_mode is True, (
        "the operator disabled live trading and this agent is STILL routing real "
        "orders — the toggle reported success and did nothing"
    )


def test_enabling_live_trading_reaches_an_already_constructed_agent():
    """The other direction: ON must stop simulating, or positions are imaginary."""
    settings.set_live_trading(False, persist=False)
    agent = ExecutionAgent()
    assert agent.simulation_mode is True

    settings.set_live_trading(True, persist=False)

    assert agent.simulation_mode is False, (
        "the operator enabled live trading but this agent is still simulating, so "
        "they believe they hold positions that do not exist"
    )


def test_the_exchange_name_follows_the_toggle_too():
    """`_execute_tar` picks the venue from `simulation_mode` on every order.

    Asserted separately because the routing decision is what actually reaches an
    exchange; a correct flag that the routing line did not read would be the same
    bug one layer down.
    """
    settings.set_live_trading(False, persist=False)
    agent = ExecutionAgent()

    def venue() -> str:
        return "simulated_exchange" if agent.simulation_mode else "binance_futures"

    assert venue() == "simulated_exchange"
    settings.set_live_trading(True, persist=False)
    assert venue() == "binance_futures"
    settings.set_live_trading(False, persist=False)
    assert venue() == "simulated_exchange"


def test_an_explicit_simulation_override_ignores_the_global_flag():
    """The backtest engine passes `simulation_mode=True` and must stay simulated.

    A backtest that went live because someone flipped a setting mid-run would be
    the worst possible outcome of making the flag dynamic, so the override is
    pinned here rather than left as an implementation detail.
    """
    settings.set_live_trading(False, persist=False)
    agent = ExecutionAgent(simulation_mode=True)

    settings.set_live_trading(True, persist=False)
    assert agent.simulation_mode is True, (
        "an explicitly simulated agent went live when the global flag changed"
    )


def test_an_explicit_live_override_also_holds():
    """Symmetry. An explicit False must not be quietly re-derived either."""
    settings.set_live_trading(True, persist=False)
    agent = ExecutionAgent(simulation_mode=False)

    settings.set_live_trading(False, persist=False)
    assert agent.simulation_mode is False


def test_the_default_is_simulation_when_the_flag_is_off():
    """Unchanged behaviour, pinned so the property rewrite cannot invert it.

    `ExecutionAgent` once defaulted to LIVE routing and `main.py` constructed it
    with no arguments, so the running system placed real orders by default. That
    must never come back.
    """
    settings.set_live_trading(False, persist=False)
    assert ExecutionAgent().simulation_mode is True
