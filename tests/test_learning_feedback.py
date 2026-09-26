"""Closed trades reach the win-rate memory, and an advisory model cannot stall a run.

TWO INDEPENDENT FAULTS, BOTH FOUND BY READING THE LIVE SYSTEM RATHER THAN THE CODE.

1. NOTHING COUNTED A CLOSED TRADE
---------------------------------
`services/ai_memory.record_trade` is the only writer of `global_stats`, and its
only caller is `agents/trading_agent.trading_agent_tick` - the legacy task-based
path the autonomous system does not run. The autonomous close path is
`PositionMonitorAgent._close` -> POSITION_CLOSED, and it never touched the file.

Measured on 2026-09-25:

    backend/data/ai_memory.json   total_trades 0, wins 0, trade_ledger []
    Postgres `trades`             11 closed rows carrying a realised pnl

So three readers reported "unmeasurable" indefinitely, and each of them reads as
an honest young system rather than a broken feed:

    algorithms/probability.measured_accuracy   needs 20, always saw 0, so
                                               `decision.probability` was null on
                                               every run - confirmed verbatim in a
                                               live trace: "only 0 resolved
                                               trade(s), need 20"
    ConfidenceAgent                            fell back to its prior every call
    supervisor_agent._measured_win_rate        None, so Kelly used the fixed
                                               fraction instead of the measured edge

2. AN ADVISORY NODE COULD HOLD A RUN OPEN FOR FIVE MINUTES
----------------------------------------------------------
`ai_consultation` asks for `ModelTier.REASONING`, whose read timeout is 300s -
correct for a slow reasoning model on a path nothing waits on. But something does
wait: `external_consultation` is a node of the analysis graph, and
`run_analysis_graph` does not return until every node finishes, so
`trading_session._run_session` is blind for the duration.

On 2026-09-25 the configured consultation model stopped answering and a live
trace recorded `external_consultation 300,612.9ms` - the tier ceiling to the
millisecond, three times out of three. Direct curl against the same key showed
`openai/gpt-oss-20b` returning no HTTP status at all in 120s while
`nvidia/nemotron-3-super-120b-a12b` answered in 0.79s on the same endpoint. A
dead model, not a slow graph.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os

import pytest


# ---------------------------------------------------------------------------
# 1. Counting a close
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    """Point the memory store at a temp file so tests never touch the real one."""
    import backend.services.ai_memory as mem

    path = tmp_path / "ai_memory.json"
    monkeypatch.setattr(mem, "MEMORY_FILE", str(path))
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_closed_trade_is_counted(memory_file):
    from backend.services.ai_memory import record_closed_trade

    asyncio.run(record_closed_trade("SOL/USDT", "buy", 12.50, strategy="Grid"))

    stats = _read(memory_file)["global_stats"]
    assert stats["total_trades"] == 1
    assert stats["wins"] == 1
    assert stats["total_pnl"] == pytest.approx(12.50)
    assert stats["win_rate"] == pytest.approx(100.0)


def test_a_break_even_close_counts_as_a_trade_but_not_a_win(memory_file):
    """A pnl of exactly 0.0 is a REAL close - the same rule `trades.pnl` follows,
    where ABSENT means open and 0 means closed flat. Treating it as "no trade"
    would quietly drop the exact outcome this system spent weeks trying to
    eliminate, and so hide whether the fix worked."""
    from backend.services.ai_memory import record_closed_trade

    asyncio.run(record_closed_trade("SOL/USDT", "buy", 0.0))

    stats = _read(memory_file)["global_stats"]
    assert stats["total_trades"] == 1
    assert stats["wins"] == 0
    assert stats["losses"] == 1


def test_the_measured_win_rate_becomes_readable_once_the_sample_exists(memory_file):
    """The whole point: `measured_accuracy` must stop saying "0 resolved trades".

    20 is the floor - below it the rate is reported but must not steer anything,
    because a three-win streak reading as 100% is how a naive adaptive system
    entrenches a bad strategy.
    """
    from backend.algorithms.probability import measured_accuracy
    from backend.services.ai_memory import get_memory_stats, record_closed_trade

    for i in range(25):
        asyncio.run(record_closed_trade("SOL/USDT", "buy", 5.0 if i % 2 == 0 else -4.0))

    rate, note = measured_accuracy(get_memory_stats())
    assert rate is not None, f"still unmeasurable after 25 closes: {note}"
    assert "25 resolved trades" in note
    # 13 wins of 25 = 0.52, inside the [0.2, 0.9] sampling-artefact bounds.
    assert rate == pytest.approx(13 / 25)


def test_the_ledger_is_bounded(memory_file):
    """This runs on a 24/7 system. An unbounded local journal is the disk incident
    the operator already hit once with `graph_checkpoints.sqlite`."""
    from backend.services.ai_memory import record_closed_trade

    for _ in range(1010):
        asyncio.run(record_closed_trade("SOL/USDT", "buy", 1.0))

    assert len(_read(memory_file)["trade_ledger"]) == 1000


def test_recording_never_raises_into_the_close_path(monkeypatch, memory_file):
    """The money has already moved by the time this is called. A bookkeeping
    failure must not propagate into the close."""
    import backend.services.ai_memory as mem

    def _boom(_):
        raise OSError("disk full")

    monkeypatch.setattr(mem, "_save_memory", _boom)
    asyncio.run(mem.record_closed_trade("SOL/USDT", "buy", 1.0))   # must not raise


def test_the_close_is_counted_before_the_reflection_and_without_a_model_call():
    """Ordering matters: the reflection makes an LLM call that can fail, and the
    COUNT is a fact that must not depend on a model answering.

    And `record_closed_trade` must not call `analyze_mistake` the way
    `record_trade` does - `ReflectionAgent` is already the POSITION_CLOSED
    subscriber that writes the lesson, so doing it here too would reflect on every
    loss twice and spend two slots of the 40/min key budget on one analysis.
    """
    from backend.agents.reflection_agent import ReflectionAgent
    from backend.services import ai_memory

    handler = inspect.getsource(ReflectionAgent.handle_event)
    assert "record_closed_trade" in handler
    assert handler.index("record_closed_trade(") < handler.index("_reflect_on_close(")

    # Matched as a CALL, not as a mention: the function's own docstring
    # explains why it does not call `analyze_mistake`, so a bare substring
    # test matches the explanation and passes for the wrong reason.
    recorder = inspect.getsource(ai_memory.record_closed_trade)
    assert "analyze_mistake(" not in recorder
    assert "await analyze_mistake" not in recorder


# ---------------------------------------------------------------------------
# 2. The advisory deadline
# ---------------------------------------------------------------------------

def test_a_hanging_panel_member_does_not_hold_the_run_open(monkeypatch):
    """The measured failure: 300,612.9ms on an advisory node, three for three."""
    import backend.services.ai_consultation as ac

    monkeypatch.setattr(ac, "CONSULT_DEADLINE_S", 0.05)

    class Hanging:
        name = "hanging"
        available = True

        async def complete(self, **_kwargs):
            await asyncio.sleep(30)          # stands in for a dead endpoint
            raise AssertionError("should never be awaited to completion")

    async def run():
        return await ac.consult(
            question="Should this short be taken?",
            internal_view="SHORT on SOL/USDT at 0.23 confidence",
            providers=[Hanging()],
        )

    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        result = loop.run_until_complete(run())
        elapsed = loop.time() - started
    finally:
        loop.close()

    assert elapsed < 5.0, f"the advisory call held the run for {elapsed:.1f}s"
    assert result.opinions, "the timeout must be recorded, not silently dropped"
    assert result.opinions[0].error, "a timed-out opinion must carry its reason"
    assert result.opinions[0].stance is None, (
        "an unanswered second opinion must never read as agreement - that is the "
        "one thing worse than not consulting at all"
    )


def test_the_deadline_is_far_below_the_reasoning_tier_ceiling():
    """The bug was the tier's 300s applying to a node nothing may wait 300s for."""
    from backend.llm.provider import ModelTier, timeout_for
    from backend.services.ai_consultation import CONSULT_DEADLINE_S

    assert CONSULT_DEADLINE_S < timeout_for(ModelTier.REASONING) / 5


def test_the_consultation_stays_advisory():
    """Bounding it must not have turned it into something a gate reads.

    ASSERTED AGAINST THE NODE CONTRACT, NOT THE SOURCE. The first version of this
    test scanned for the string "risk_assessment" and failed - correctly, because
    the node READS the gateway verdict to phrase the question it asks. Reading is
    not the hazard; WRITING is. A node that reads a decision and writes only prose
    cannot alter the decision, and `NodeContract` is what enforces that.
    """
    from backend.graphs.nodes import consultation

    src = inspect.getsource(consultation)
    assert 'writes=("consultation", "llm_calls_made", "llm_tokens_used")' in src, (
        "the consultation node's contract must still write prose and counters "
        "only - never `execution_plan`, `risk_assessment` or `decision`"
    )
