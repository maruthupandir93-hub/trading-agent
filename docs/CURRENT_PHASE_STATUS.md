# Current Phase Status

> **This file had drifted badly and was actively misleading.** It listed
> phases 41–50 as "unimplemented" and 23/31/32/33/34 as "pending migration",
> long after every one of those modules had been written and wired into
> `backend/main.py`'s lifespan. Anyone reading it would have rebuilt work that
> already existed — which is exactly what the spec's own Master Prompt forbids
> ("Do NOT rebuild existing systems. Reuse existing modules.").
>
> It is now written as a map to the file that implements each phase, so a wrong
> entry is falsifiable by opening the path. When you finish a phase, edit the row
> rather than adding a note somewhere else.

## LangGraph reasoning layer

Four graph *configs* are assembled through `graphs/builder.py` from the
declarative registry, with contracts validated at registration:

| Graph | Config | Subscribed? | Checkpointed? |
|---|---|---|---|
| 1 — Market Intelligence | `graphs/market_state.py::market_state_config` | no — contained in Graph 2 | no |
| 2 — Trading Opportunity | `graphs/opportunity.py::opportunity_config` | no — contained in Graph 2 (full) | no |
| 2 — Trade Analysis (full) | `graphs/analysis.py::analysis_config` | **yes**, to `TRIGGER_FIRED` | no |
| 4 — Position Monitoring | `graphs/monitoring.py::monitoring_config` | via `workers/position_worker` | **yes** |

Only `analysis_config` is subscribed. It already runs all nine market-state and
opportunity nodes, so subscribing the other two would execute them several times
per trigger for one usable result — see the comment in `main.py`.

Graphs 5, 6 and 7 exist as modules but are not `GraphConfig`s built through the
registry: `reflection_graph.py`, `research_graph.py`, `learning_graph.py`.
`execution_graph.py` is **deliberately not a graph at all** — order routing
inside the cognitive plane is the one thing Rule 0 exists to prevent; the
filename is historical.

## Implemented

| Phase | What | Where |
|---|---|---|
| 23 | LangGraph foundation | `graphs/builder.py`, `registry.py`, `contracts.py`, `runtime.py`, `state.py` |
| 24 | Market state | `graphs/market_state.py`, `graphs/nodes/market.py` |
| 25 | Opportunity | `graphs/opportunity.py`, `graphs/nodes/opportunity.py` |
| 26 | Specialists + debate | `graphs/analysis.py`, `graphs/nodes/specialists.py` |
| 29 | Execution service | `services/execution_service.py` (gated by `GRAPH_EXECUTION_ENABLED`) |
| 30 | Position monitoring | `graphs/monitoring.py`, `workers/position_worker.py` |
| 31 | Event triggers | `graphs/triggers.py`, `workers/trigger_worker.py` |
| 32 | Trading memory | `services/working_memory.py`, `semantic_memory.py`, `procedural_memory.py`, `risk_memory.py` |
| 33 | Trade reflection | `graphs/reflection_graph.py`, `agents/reflection_agent.py` |
| 34 | Learning system | `graphs/learning_graph.py`, `agents/hypothesis_agent.py` |
| 35 | Trading style intelligence | `algorithms/trading_styles.py` |
| 36 | Strategy selection | `graphs/strategy_selection_graph.py`, `algorithms/strategy_profiles.py` |
| 37 | Bayesian decision engine | `algorithms/bayesian_engine.py` (in the Supervisor) |
| 38 | Market regime intelligence | `agents/regime_agent.py` — 10 states |
| 39 | Dynamic thresholding | `algorithms/dynamic_thresholding.py` |
| 40 | Adaptive risk / sizing | `core/risk_manager.py::calculate_dynamic_risk` |
| 41 | Execution intelligence | `graphs/execution_graph.py`, `algorithms/execution.py` |
| 42 | Cross-exchange | `services/exchange_provider.py`, `algorithms/portfolio.py` |
| 43 | Market graph | `algorithms/market_graph.py`, `core/knowledge_graph.py` |
| 44 | Institutional footprint | `algorithms/footprint.py` |
| 45 | Research agent | `graphs/research_graph.py`, `agents/research_agent.py` |
| 46 | Simulation lab | `agents/simulation_agent.py`, `core/backtest_engine.py` |
| 47 | Multi-agent debate | `algorithms/debate.py`, `agents/debate_agent.py` |
| 48 | External AI consultation | `services/ai_consultation.py` |
| 49 | Curiosity engine | `workers/curiosity_worker.py` |
| 50 | Meta-learning | `graphs/learning_graph.py` — the six self-questions |

Polymarket (feed, registry, store, worker, stream, validation, 7 endpoints) is
implemented and **off by default**: `POLYMARKET_ENABLED=false`. Turning it on
changes every confidence number the system produces — `core/config.py` explains
why, and that is an operator decision, not a side effect of installing a
dependency.

## Known open gaps

These are real, currently true, and each is documented at its source rather than
only here.

- **No LLM adapter is wired.** `llm/provider.py` has the interface, the tiering
  and the fail-closed contract, but `NullProvider` is the default and
  `OPENAI_API_KEY` is empty. Every LLM node refuses honestly rather than
  reasoning. This is the single thing blocking the judgment-dependent phases
  from doing anything beyond their deterministic parts.
- **No resting stop orders at the exchange.** Stops are enforced in-process by
  `PositionMonitorAgent`. The watch list now survives a restart
  (`monitored_positions`), so the outage window is the length of the restart
  rather than forever — but nothing enforces a stop while the process is down.
  `agents/execution_agent.py` logs this at fill time.
- **Memory / Reflection data cannot reach `components/Supervisor.tsx`** — a
  provider-tree ordering constraint, see `docs/07_MEMORY_SYSTEM.md`.
