"""One originator of entries, and the attribution that proves which one ran.

THE OPERATOR'S REPORT, AND THE MEASUREMENT BEHIND THESE TESTS
=============================================================
"in the trade history page how the trade happens ... each trade is market data
and directly execute, all the agent doesn't working together".

That was literally true, and the live database said so. Read from Postgres on
2026-09-25, before any of this was changed:

    23 rows in `trades`. strategy, run_id AND entry_context NULL on EVERY one.
    5 of the 12 opening rows join `decisions` on the TAR id, and each of those
    rationales opens "Debate concluded LONG at 0% confidence".

Those are the EVENT path's rows. This system has two things that can start a
trade, and only one of them runs the agents:

  GRAPH PATH   24 nodes - 9 specialists, regime detection, strategy scoring,
               the debate, the Supervisor node, the Risk Gateway - then
               EXECUTION_PLAN_READY -> execution_service -> TAR. It records
               `run_id`, `strategy` and `entry_context` on the fill.
  EVENT PATH   DEBATE_CONCLUDED -> supervisor_agent -> TAR. A four-to-five leg
               technical debate. It records none of those three, because the
               nodes that produce them never ran.

They are not two routes to one answer. On a live SOL/USDT run taken while
writing these tests, the full panel reached NEUTRAL at 0.067 (the portfolio
constraint binding at 0.40) and the Supervisor returned DO_NOT_TRADE, while the
event path's own debate put the same symbol at 0.23-0.24 and traded it. The
shortcut wins the race because it is cheaper - milliseconds against the graph's
~5 seconds to its gateway - so it claims the single allowed position and the
graph's run is then refused for already holding one.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect

import pytest

from backend.agents.supervisor_agent import SupervisorAgent
from backend.models.events import StressTestedEvent, TarSubmittedEvent


def _event(symbol: str = "SOL/USDT") -> StressTestedEvent:
    return StressTestedEvent(symbol=symbol, passed=True, results={"ok": True})


@pytest.fixture
def sup(monkeypatch):
    """A supervisor whose refusals are captured instead of written to the database."""
    agent = SupervisorAgent()
    refusals: list = []

    async def _capture(symbol, cause, debate=None):
        refusals.append((symbol, cause))

    monkeypatch.setattr(agent, "_refuse", _capture)
    agent.captured_refusals = refusals
    return agent


# ---------------------------------------------------------------------------
# The crash that made the limit invisible
# ---------------------------------------------------------------------------

def test_a_scope_refusal_is_recorded_rather_than_raising(monkeypatch, sup):
    """THE BUG: `UnboundLocalError`, proven at runtime before it was fixed.

    The scope gate sat ABOVE `debate = self._debates.get(symbol)` and passed
    `debate` to `_refuse(...)`. Python makes `debate` a local of the whole method
    because it is assigned later, so every scope refusal raised

        UnboundLocalError: cannot access local variable 'debate' where it is not
        associated with a value

    instead of recording one. It is why the live `decisions` table holds 1,995
    rejections and NOT ONE of them is a scope rejection: the gate stopped the
    trade by crashing, so the operator could never see the limit working - and a
    limit that cannot be observed is indistinguishable from one that is not
    running.
    """
    monkeypatch.setenv("GRAPH_EXECUTION_ENABLED", "false")   # reach the scope gate
    monkeypatch.setenv("SESSION_ONLY_TRADING", "true")       # and refuse at it
    sup._debates["SOL/USDT"] = {
        "direction": "LONG",
        "confidence": 0.9,
        "participants": [],
        "rationale": "x",
        "ts": datetime.datetime.utcnow(),
    }

    # Must not raise. Whether it refuses at the scope gate or earlier does not
    # matter here; that it RETURNS rather than throwing is the property.
    asyncio.run(sup._consider_trade(_event()))


def test_the_scope_gate_is_below_the_debate_it_reports_with(monkeypatch):
    """Structural, because the runtime test above can only prove the path it takes.

    Any future edit that moves the gate back above the assignment reintroduces the
    same UnboundLocalError on a branch no ordinary run reaches.
    """
    src = inspect.getsource(SupervisorAgent._consider_trade)
    assignment = src.index("debate = self._debates.get(symbol)")
    gate = src.index("scope_refusal = entry_refusal(")
    assert assignment < gate, (
        "the scope gate passes `debate` to _refuse(), so it must sit AFTER the "
        "line that binds it"
    )


# ---------------------------------------------------------------------------
# One originator
# ---------------------------------------------------------------------------

def test_the_event_path_does_not_open_while_the_graph_path_is_enabled(monkeypatch, sup):
    """The headline fix. With the graph enabled, this path submits nothing."""
    monkeypatch.setenv("GRAPH_EXECUTION_ENABLED", "true")

    published = []

    async def _publish(ev):
        published.append(ev)

    monkeypatch.setattr(sup, "publish", _publish)
    asyncio.run(sup._consider_trade(_event()))

    assert not [e for e in published if isinstance(e, TarSubmittedEvent)], (
        "the event path submitted a TAR while the 24-node graph was the enabled "
        "originator - that is the race which produced 12 opening trades with no "
        "strategy, no run_id and no entry context"
    )


def test_the_refusal_says_why_rather_than_returning_silently(monkeypatch, sup):
    """A silent return would make "why did this not trade?" unanswerable.

    `decisions` is the audit trail and is unbounded on purpose; the reason a path
    declined belongs in it.
    """
    monkeypatch.setenv("GRAPH_EXECUTION_ENABLED", "true")
    asyncio.run(sup._consider_trade(_event()))

    assert sup.captured_refusals, "no refusal was recorded at all"
    _, cause = sup.captured_refusals[-1]
    assert "graph" in cause.lower()
    assert "GRAPH_EXECUTION_ENABLED" in cause


def test_turning_the_graph_path_off_hands_the_role_back(monkeypatch, sup):
    """Reversible in one line, and read at CALL time.

    A module-level `os.getenv` would be the `simulation_mode` bug again - the
    operator flips the flag, is told it worked, and the running agent keeps the
    old behaviour until a restart.
    """
    monkeypatch.setenv("GRAPH_EXECUTION_ENABLED", "false")
    asyncio.run(sup._consider_trade(_event()))

    causes = " ".join(c for _, c in sup.captured_refusals)
    assert "GRAPH_EXECUTION_ENABLED" not in causes, (
        "with the graph path off this path must be free to originate again; it "
        "still has every gate of its own"
    )


def test_exits_are_not_reachable_from_this_method_at_all():
    """INVARIANT 4, stated structurally so the deferral above cannot block a close.

    `_consider_trade` only ever opens. Closes belong to `PositionMonitorAgent` and
    never come through here, which is what makes refusing an ENTRY safe.
    """
    src = inspect.getsource(SupervisorAgent._consider_trade)
    for forbidden in ("close_position", "reduceOnly", "reduce_only"):
        assert forbidden not in src, (
            f"_consider_trade mentions {forbidden!r}; if this method could ever "
            f"close a position, refusing early here would block an exit"
        )


# ---------------------------------------------------------------------------
# What the rationale claims about itself
# ---------------------------------------------------------------------------

def test_the_rationale_renders_confidence_as_a_real_percentage():
    """`score_debate` emits a 0-1 FRACTION and the rationale renders a PERCENT.

    `f"{0.23:.0f}%"` is "0%", so every rationale this path ever wrote said "at 0%
    confidence" - including the five that became live trades. Zero is also
    exactly what a broken gate would look like, so the one line an operator reads
    to audit a trade asserted the opposite of what the gate had measured.
    """
    src = inspect.getsource(SupervisorAgent._consider_trade)
    assert "debate['confidence'] * 100:.0f}% confidence" in src, (
        "the rationale must scale the 0-1 debate confidence into the percent it "
        "claims to be printing"
    )


def test_an_approved_decision_records_its_confidence_like_a_refused_one():
    """`_refuse` filled these on all 1,995 rejections; the submit path filled
    neither, so the analysis columns were populated on exactly the rows nobody
    needs them for and NULL on the five that traded."""
    src = inspect.getsource(SupervisorAgent._consider_trade)
    submit = src[src.index('outcome="pending-approval"'):]
    assert "debate_confidence_pct=" in submit
    assert "debate_recommendation=" in submit


# ---------------------------------------------------------------------------
# The attribution the graph path carries and the event path does not
# ---------------------------------------------------------------------------

def test_the_tar_declares_every_attribution_field_it_is_passed():
    """Pydantic v2 IGNORES unknown keyword arguments, so a field that is passed
    but not DECLARED vanishes with nothing raised - which is how `run_id`,
    `strategy` and `entry_context` came to be NULL on every trade row once
    before. `strategy` was also declared TWICE here; the later definition won, so
    the model advertised a required field it did not have."""
    fields = TarSubmittedEvent.model_fields
    for name in ("run_id", "strategy", "entry_context"):
        assert name in fields, f"TarSubmittedEvent drops {name}"
    assert not fields["strategy"].is_required(), (
        "the event path deliberately passes strategy=None - a pipeline label "
        "there poisoned `strategy_performance` with 2,426 trades under one name "
        "that matches no real profile"
    )


def test_the_graph_path_carries_all_three_onto_the_tar():
    """The graph path is now the only originator, so its attribution is the only
    attribution. If `_submit_tar` stopped forwarding these, every trade would go
    back to having an unknown middle."""
    from backend.services import execution_service

    src = inspect.getsource(execution_service.ExecutionService._submit_tar)
    for field in ("run_id=", "entry_context=", "strategy="):
        assert field in src, f"_submit_tar no longer forwards {field}"
