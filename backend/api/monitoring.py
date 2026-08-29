"""Monitoring API (`/api/monitoring`) — is the autonomous loop actually running?

This is the endpoint an operator checks first. It answers three separate
questions that are easy to conflate:

  1. Is the AgentOS scheduler ticking, and is every registered agent alive?
  2. Is the REASONING layer able to reason — i.e. is an LLM configured?
  3. Are the autonomy gates open, and which ones?

The second and third were previously invisible here, which meant the honest
answer to "is my agent running autonomously?" could not be obtained from any
single endpoint. An operator would see `overall: healthy` while the reasoning
layer had no model and every graph run was skipping its narration, or while
`GRAPH_EXECUTION_ENABLED` was off and no plan could ever reach the exchange.

`overall` deliberately does NOT go unhealthy when no LLM is configured. The
system is designed to trade on deterministic nodes — only TWO nodes in the whole
graph may call a model (`trade_thesis_narrative` and `external_consultation`),
and both run after the decision and the risk gateway. So "no LLM" is a degraded
EXPLANATION capability, not a degraded trading capability. Conflating the two
would make the health signal useless: it would read red on a system that is
working exactly as designed.

The live split is reported in `reasoning` rather than restated here, so this
comment cannot drift from the registry the way a hardcoded count would.
"""

import os

from fastapi import APIRouter

from backend.core.agent_os import get_agent_os
from backend.graphs.registry import coverage as node_coverage
from backend.llm.provider import consultation_panel_status, provider_status

router = APIRouter()


def _flag(name: str) -> bool:
    """Read at call time, never captured at import — a flag toggled while running
    must be reported as it is now, not as it was at boot."""
    return (os.getenv(name) or "false").strip().lower() == "true"


@router.get("")
async def get_system_health():
    """Health of the Agent OS, the reasoning layer, and the autonomy gates."""
    os_kernel = get_agent_os()

    agents_health = [agent.health.model_dump() for agent in os_kernel.agents.values()]

    llm = provider_status()

    # The node registry fills when a graph config is FIRST BUILT, and
    # `analysis.subscribe_to_triggers` builds lazily on the first trigger — so
    # zero here means "no graph has run yet", not "no nodes exist". Reported with
    # that distinction rather than as a bare count, because a health page showing
    # `nodesRegistered: 0` reads as a broken reasoning layer when the truth is
    # that the market has simply been quiet.
    try:
        nodes = node_coverage()
    except Exception:
        nodes = {"total": 0, "deterministicCount": 0, "llmCount": 0}

    live_trading = _flag("LIVE_TRADING")
    graph_execution = _flag("GRAPH_EXECUTION_ENABLED")
    position_monitoring = _flag("POSITION_MONITORING_ENABLED")

    return {
        # Unchanged shape for existing callers.
        "overall": "healthy" if os_kernel.is_running else "degraded",
        "status": "ok",
        "scheduler_running": os_kernel.is_running,
        "agents": agents_health,
        "checks": [{"label": "FastAPI Core", "ok": True}],

        # ---- The reasoning layer -------------------------------------------
        #
        # `available: false` here is NOT an outage. It means LLM nodes report
        # themselves unavailable and the deterministic nodes carry the run —
        # which is the designed degraded mode, not a failure.
        "llm": llm,

        # The Phase 48 second-opinion panel, reported separately from the main
        # provider because they are configured separately and fail separately.
        # A working main provider with no panel is the normal state: the agent can
        # explain itself but will not seek an outside view.
        "consultationPanel": consultation_panel_status(),

        # The deterministic/LLM split, which Section 39.6 says is the number to
        # watch over time: this system's safety rests on decision-critical nodes
        # staying deterministic, and drift toward LLM nodes is the failure mode
        # the plan names as most likely.
        "reasoning": {
            "nodesRegistered": nodes.get("total", 0),
            "deterministicNodes": nodes.get("deterministicCount", 0),
            "llmNodes": nodes.get("llmCount", 0),
            # Named explicitly so 0 is not read as a fault.
            "graphBuilt": nodes.get("total", 0) > 0,
            "note": (
                "Decisions are deterministic by design. The LLM narrates a decision "
                "that has already been computed and provides second opinions when "
                "uncertainty is high — it never decides."
                if nodes.get("total", 0) > 0 else
                "No graph has run yet, so no nodes are registered. The analysis graph "
                "builds on the first market trigger — this is normal shortly after "
                "startup or during a quiet market, not a fault."
            ),
        },

        # ---- The autonomy gates -------------------------------------------
        #
        # All three reported together because they answer one question between
        # them and mean nothing apart. A reader who sees only LIVE_TRADING=false
        # may conclude nothing is happening, when the agent may be trading paper
        # autonomously and monitoring positions.
        "autonomy": {
            "liveTrading": live_trading,
            "graphExecutionEnabled": graph_execution,
            "positionMonitoringEnabled": position_monitoring,
            "tab": "real" if live_trading else "paper",
            "summary": _autonomy_summary(live_trading, graph_execution, position_monitoring),
        },
    }


def _autonomy_summary(live: bool, graph_execution: bool, monitoring: bool) -> str:
    """One sentence an operator can act on.

    Written out per combination rather than assembled from fragments, because the
    combinations mean genuinely different things and a generated sentence tends
    to obscure the one that matters most — graph execution off, which means the
    agent reasons and records but can never open a position.
    """
    if not graph_execution:
        return (
            "The agent analyses and records decisions but CANNOT open positions — "
            "GRAPH_EXECUTION_ENABLED is off. Closes are still routed regardless."
        )
    if not live:
        return (
            "Autonomous PAPER trading is active: the agent can open and close paper "
            "positions on its own. No real funds are at risk (LIVE_TRADING is off)."
            + ("" if monitoring else " Position-monitoring DECISIONS are off, though "
                                     "stop-loss enforcement always runs.")
        )
    return (
        "AUTONOMOUS LIVE TRADING IS ACTIVE — the agent can open and close positions "
        "with REAL funds, subject to the CRO's risk checks and the hard leverage "
        "ceiling."
        + ("" if monitoring else " Position-monitoring DECISIONS are off, though "
                                 "stop-loss enforcement always runs.")
    )
