"""A simulated close fills at the price the DECISION was made against.

THE BUG, FOUND BY EXERCISING THE LIVE SYSTEM RATHER THAN BY READING CODE
=======================================================================
`ExecutionAgent.close_position` filled a simulated close at
`self._last_prices[symbol]` — its own cache, fed by TICK_RECEIVED.
`PositionMonitorAgent._check_price` decides to close from the tick IT received.
Both agents subscribe to the same event, so whether the two prices agree depends
entirely on which subscriber the bus reaches first — which is the order the
agents were constructed in `main.py`.

CLAUDE.md already says of exactly this hazard: *"Do NOT fix a future instance of
this by reordering construction in main.py. That works until the next reorder and
no test can see it."* So this test exists to see it.

Measured, with the monitor constructed before the executor:

    monitor decided profit-target at 122.0587   (a +0.667% move on a long)
    close filled at 121.25 — the entry price, the executor's stale tick
    booked P&L -18.80 on a WINNING move, almost exactly the round-trip fee

A profit target that fires and books a loss the size of the fees is
indistinguishable from the scratch exits this system spent weeks removing — and
it would have been read as one.

REAL FILLS ARE DELIBERATELY UNAFFECTED. A live close is a market order and its
price comes back from the venue; `position_monitor._close` still computes P&L
from that actual fill rather than the trigger, because slippage is real there.
For a SIMULATED fill there is no separate reality to defer to: the observed price
at the moment of the decision IS the honest fill.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.agents.execution_agent import ExecutionAgent


@pytest.fixture
def agent(monkeypatch):
    a = ExecutionAgent(simulation_mode=True)

    # The paper book is exercised elsewhere; here we only care about the price.
    async def _noop(**_kwargs):
        return None

    monkeypatch.setattr(a, "_apply_paper_fill", _noop)
    return a


def _close(agent, **kw):
    base = dict(symbol="SOL/USDT", entry_side="buy", qty=1.0, tab="paper", reason="profit-target")
    base.update(kw)
    return asyncio.run(agent.close_position(**base))


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------

def test_the_observed_price_wins_over_a_stale_cache(agent):
    """The exact live failure: the cache is a beat behind the decision."""
    agent._last_prices["SOL/USDT"] = 121.25          # the stale entry tick
    fill = _close(agent, observed_price=122.0587)    # what the monitor saw
    assert fill == pytest.approx(122.0587)


def test_a_winning_move_no_longer_books_the_fee_as_a_loss(agent):
    """Stated in P&L terms, because that is how it presented.

    A long from 121.25 closed at the target must show a GROSS GAIN. Filling at
    the stale cache made gross exactly zero and the row booked -18.80 — the
    round-trip fee — on a trade that had moved 0.667% in its favour.
    """
    entry, target = 121.25, 122.0587
    agent._last_prices["SOL/USDT"] = entry
    fill = _close(agent, qty=155.0, observed_price=target)
    assert (fill - entry) * 155.0 > 0


def test_the_cache_is_still_used_when_no_price_is_supplied(agent):
    """Every pre-existing caller passes nothing and must keep working."""
    agent._last_prices["SOL/USDT"] = 119.5
    assert _close(agent) == pytest.approx(119.5)


@pytest.mark.parametrize("bad", [None, 0.0, -1.0])
def test_an_unusable_observed_price_falls_back_rather_than_filling_at_it(agent, bad):
    """A zero or negative price is not a price. Filling at it would book a
    fabricated P&L the size of the whole position."""
    agent._last_prices["SOL/USDT"] = 119.5
    assert _close(agent, observed_price=bad) == pytest.approx(119.5)


def test_with_neither_price_the_position_stays_open(agent, caplog):
    """Invariant 6: refuse rather than invent. Returning a made-up fill would
    close the position in the book against a price nobody observed."""
    agent._last_prices.clear()
    assert _close(agent) is None
    assert any("no observed price" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# The wiring, so the fix cannot be undone one hop away
# ---------------------------------------------------------------------------

def test_the_monitor_passes_the_trigger_price_on_a_full_close():
    from backend.agents.position_monitor import PositionMonitorAgent

    src = inspect.getsource(PositionMonitorAgent._close)
    assert "observed_price=trigger_price" in src, (
        "_close holds the price it decided on; not passing it reintroduces the "
        "dependency on subscriber construction order"
    )


def test_the_monitor_passes_the_trigger_price_on_a_scale_out():
    from backend.agents.position_monitor import PositionMonitorAgent

    src = inspect.getsource(PositionMonitorAgent._take_partial)
    assert "observed_price=price" in src


def test_a_real_close_does_not_consult_the_observed_price():
    """The venue's fill is the truth for real money, and slippage is real there.

    Asserted against the source because driving a live close needs credentials —
    what matters is that `observed_price` is read ONLY inside the simulation
    branch.
    """
    src = inspect.getsource(ExecutionAgent.close_position)
    body = src[src.index("if self.simulation_mode:"):]
    live = body[body.index("from backend.services.venue import get_venue"):]
    assert "observed_price" not in live, (
        "a live close must report the price the exchange actually filled at"
    )


def test_the_realised_pnl_still_comes_from_the_fill_not_the_trigger():
    """The rule this fix must NOT quietly reverse. On a real close the two
    genuinely differ, and the ledger has to record what happened."""
    from backend.agents.position_monitor import PositionMonitorAgent

    src = inspect.getsource(PositionMonitorAgent._close)
    assert "fill_price" in src.split("observed_price=trigger_price")[1], (
        "P&L must still be computed from the returned fill"
    )
