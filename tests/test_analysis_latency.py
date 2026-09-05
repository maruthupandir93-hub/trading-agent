"""The execution plan must reach the bus BEFORE the explanation nodes run.

WHY THIS IS A CORRECTNESS TEST AND NOT A BENCHMARK
==================================================
`run_analysis_graph` used to `await graph.ainvoke(...)` and publish the plan
afterwards, so the order sat idle until all 24 nodes had finished. Measured on a
live BTC/USDT run, 19.6s wall:

    trade_thesis_narrative   7.56s   LLM
    external_consultation    2.38s   LLM
    data_validation          2.27s   upstream fetches
    memory_loader            0.76s
    the other 20 nodes       0.04s   combined

Ten of those seconds were spent generating prose ABOUT a trade that was already
decided. Both LLM nodes run after `risk_gateway`, and the plan is now published
the moment the gateway's output appears in the stream.

THAT IS ONLY SAFE BECAUSE OF WHAT THOSE NODES WRITE, so this file asserts it
rather than trusting it. If a future change let either of them touch
`execution_plan`, `risk_assessment` or `decision`, the published plan could be
stale by the time the graph ended — the system would have submitted one trade and
recorded another. The contracts are the guard; these tests are what keep them.

PUBLISHED EXACTLY ONCE is the other half. `stream_mode="values"` yields the whole
accumulated state after every superstep, so the plan appears in every chunk from
the gateway onward — without the caller's flag, one decision would submit a
dozen identical trades.
"""

from __future__ import annotations

import inspect

import pytest

from backend.graphs import analysis
from backend.graphs.registry import get_contract


# Nodes that run after the Risk Gateway and exist to EXPLAIN, not to decide.
EXPLANATION_NODES = ("trade_thesis_narrative", "external_consultation")

# State keys that determine the trade. If an explanation node could write any of
# these, publishing before it ran would be unsound.
DECISION_KEYS = ("execution_plan", "risk_assessment", "decision", "trade_thesis")


@pytest.mark.parametrize("node_name", EXPLANATION_NODES)
def test_an_explanation_node_cannot_write_anything_that_decides_the_trade(node_name):
    """The property that makes the early publish safe.

    Asserted against the registered `NodeContract`, which `wrap_node` enforces at
    runtime — so this is not merely a claim about intent.
    """
    # Registration happens when a graph is CONFIGURED, not on import — the
    # register_* functions are called from the graph builders. Building the
    # analysis config is the cheapest way to populate the registry the way the
    # running system does.
    analysis.analysis_config()

    contract = get_contract(node_name)
    assert contract is not None, f"{node_name} is not registered"

    overlap = set(contract.writes) & set(DECISION_KEYS)
    assert not overlap, (
        f"{node_name} declares it writes {sorted(overlap)}, which decides the trade. "
        f"The execution plan is published before this node runs, so the submitted "
        f"trade would not match the recorded one."
    )


def test_the_plan_is_published_from_the_stream_not_after_the_whole_graph():
    """A regression guard on the shape of the runner.

    Reverting to `await graph.ainvoke(...)` restores the ten-second delay silently
    — nothing fails, trades are simply slower. Read from the source because the
    cost is structural, not observable from one call.
    """
    # Comments stripped: this function's own docstring EXPLAINS the `ainvoke`
    # it replaced, and matching against prose would fail on the explanation
    # rather than on the code.
    code = "\n".join(
        line
        for line in inspect.getsource(analysis.run_analysis_graph).splitlines()
        if not line.strip().startswith("#")
    )

    assert "astream" in code, (
        "the analysis graph must be STREAMED so the execution plan can be published "
        "as soon as the Risk Gateway produces it; `ainvoke` waits for the LLM "
        "narrative nodes and delays every trade by ~10s"
    )
    assert "graph.ainvoke" not in code


def test_the_plan_is_published_exactly_once():
    """`stream_mode="values"` re-offers the plan in every later chunk.

    Without the caller's `published` flag, one approved decision becomes a dozen
    identical submissions.
    """
    source = inspect.getsource(analysis.run_analysis_graph)
    assert "published" in source
    assert "if not published and chunk.get(\"execution_plan\") is not None:" in source


def test_publish_plan_reports_whether_it_published():
    """The caller cannot guarantee once-only delivery from a function that
    returns None on both success and refusal."""
    source = inspect.getsource(analysis._publish_plan)
    assert "-> bool" in inspect.getsource(analysis._publish_plan).splitlines()[0]
    assert "return True" in source
    assert source.count("return False") >= 3, (
        "every refusal path — no plan, not approved, bus failure — must report "
        "False so the post-stream retry gets its chance"
    )


@pytest.mark.asyncio
async def test_a_plan_without_an_approval_is_never_published(monkeypatch):
    """The invariant that predates this change and must survive it.

    Publishing early means the guard runs earlier too, so it is worth re-checking
    that an unapproved plan still cannot reach the bus.
    """
    from backend.graphs.state import ExecutionPlan, RiskAssessment

    published = []

    class _Bus:
        async def publish(self, topic, event):
            published.append((topic, event))

    monkeypatch.setattr("backend.core.message_bus.get_message_bus", lambda: _Bus())

    plan = ExecutionPlan(symbol="BTC/USDT", side="buy", tab="paper", size=0.1, leverage=2)

    # Rejected.
    assert await analysis._publish_plan({
        "symbol": "BTC/USDT",
        "execution_plan": plan,
        "risk_assessment": RiskAssessment(approved=False),
    }) is False

    # No assessment at all.
    assert await analysis._publish_plan({
        "symbol": "BTC/USDT", "execution_plan": plan, "risk_assessment": None,
    }) is False

    assert published == [], "an unapproved plan reached the execution boundary"
