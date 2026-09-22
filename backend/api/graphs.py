"""LangGraph API — the missing link between the reasoning layer and the UI.

    Recommended_Technology_Stack.md, layers 1-4:
        Frontend         Next.js + React + Tailwind
        Backend APIs     FastAPI
        Agent Services   Python
        AI Orchestration LangGraph

WHY THIS ROUTER EXISTS
----------------------
Those four layers were each built and each worked, but the last one had **no API
surface at all**. A final audit of the wiring found:

    L2 FastAPI  <-> L3 Agents      43 endpoints, fine
    L3 Agents   <-> L4 LangGraph   shared bus and state, fine
    L1 Next.js  <-> L2 FastAPI     ONE WebSocket (agent events)
    L4 LangGraph -> L2 -> L1       NOTHING

So every decision the seven graphs produced — the thesis, the specialist panel, the
Supervisor's ten answers, the risk verdict, the monitoring decision, the
meta-learning report — was computed, traced to disk, and unreachable from the
dashboard. The reasoning layer was invisible to the layer whose job is showing it.

Spec Section 39.5 asks for exactly this and says why it matters more here than in
most agent applications:

    "'the AI is currently in multi_agent_analysis, 4 of 6 specialists reporting' is
     exactly the kind of live visibility that makes a 24/7 autonomous system
     trustworthy to watch, versus a black box that occasionally reports a trade
     after the fact."

And Section 1's architecture diagram puts USER/UI at the top of the Agent OS, not
beside it.

EVERYTHING HERE IS READ-ONLY EXCEPT ONE ROUTE
---------------------------------------------
`POST /run/{symbol}` starts a reasoning run and is the only state-changing route, so
it carries `require_write_auth`. It produces a `TradeDecision` and an inert
`ExecutionPlan` — it does NOT execute: `GRAPH_EXECUTION_ENABLED` still gates
submission, and the Risk Gateway still gates the plan. A graph run costs market data
and possibly model tokens, which is why it is gated at all.

Nothing here can place an order. This module is under `api/`, not `graphs/`, so the
import ban does not apply to it by file location — but it imports no execution symbol
either, and `test_the_graph_api_cannot_reach_an_order_call` asserts that.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from backend.core.auth import require_write_auth, auth_required, ENV_VAR
import os
import hmac

logger = logging.getLogger(__name__)

router = APIRouter()

# The seven graphs of spec Section 35, with the module that owns each. Declared as
# data so `/graphs` cannot drift from what actually exists — the endpoint imports
# each one and reports the failure rather than listing a graph that does not load.
SECTION_35_GRAPHS = [
    ("1", "Market Intelligence", "backend.graphs.market_state", "market_state_config"),
    ("2", "Trade Decision", "backend.graphs.analysis", "analysis_config"),
    # Section 12 puts execution OUTSIDE LangGraph, so Graph 3 has no config to report.
    ("3", "Execution", "backend.graphs.execution_graph", None),
    ("4", "Position Monitoring", "backend.graphs.monitoring", "monitoring_config"),
    ("5", "Reflection", "backend.graphs.reflection_graph", None),
    ("6", "Research", "backend.graphs.research_graph", None),
    ("7", "Learning", "backend.graphs.learning_graph", None),
]


@router.get("")
async def list_graphs() -> Dict[str, Any]:
    """The seven graphs, their nodes, and which are LangGraph graphs at all.

    Two of the seven deliberately are not (Execution and, in practice, the pure
    pipelines), and reporting that plainly is more useful than a list that implies
    seven compiled graphs exist.
    """
    import importlib

    out: List[Dict[str, Any]] = []
    for number, name, module, config_fn in SECTION_35_GRAPHS:
        entry: Dict[str, Any] = {"graph": number, "name": name, "module": module}
        try:
            mod = importlib.import_module(module)
            entry["available"] = True
            if config_fn and hasattr(mod, config_fn):
                cfg = getattr(mod, config_fn)()
                entry["isLangGraph"] = True
                entry["nodeCount"] = len(cfg.nodes)
                entry["nodes"] = cfg.nodes
                entry["entryNode"] = cfg.entry
                entry["conditionalEdges"] = [ce.source for ce in cfg.conditional_edges]
            else:
                entry["isLangGraph"] = False
                entry["note"] = (
                    "deterministic module, not a compiled graph — spec Section 12 puts "
                    "execution outside LangGraph, and a pipeline with no branching, "
                    "parallelism or checkpointing gains nothing from being one"
                )
        except Exception as exc:  # noqa: BLE001
            # Reported, not hidden. A graph that cannot load is exactly what an
            # operator needs to see here.
            entry["available"] = False
            entry["error"] = str(exc)
        out.append(entry)

    return {
        "graphs": out,
        "note": (
            "spec Section 35: multiple purpose-built graphs, not one giant graph"
        ),
    }


@router.get("/nodes")
async def list_nodes() -> Dict[str, Any]:
    """Every registered node and its contract.

    This is the explainability surface for Rule 0: it shows, per node, what it may
    read, what it may WRITE, and whether it is permitted to call a model at all.
    """
    from backend.graphs.registry import all_contracts

    # Configs are called so their nodes are registered — the registry is populated
    # lazily by whichever graph was built first.
    _warm_registry()

    contracts = all_contracts()
    return {
        "nodes": [
            {
                "name": c.name,
                "purpose": c.purpose,
                "phase": c.phase,
                "reads": list(c.reads),
                "writes": list(c.writes),
                "deterministic": c.deterministic,
                "mayCallLlm": c.may_call_llm,
            }
            for c in sorted(contracts, key=lambda c: (c.phase or 0, c.name))
        ],
        "total": len(contracts),
        "llmNodes": [c.name for c in contracts if c.may_call_llm],
        "contractMeaning": (
            "`writes` is ENFORCED: a node writing a field outside its contract raises "
            "NodeContractViolation. `mayCallLlm` is mutually exclusive with "
            "deterministic — and no LLM node may write a deterministic-only field such "
            "as risk_assessment, confidence or decision"
        ),
    }


@router.get("/runs")
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    graph: Optional[str] = Query(
        None,
        description="Only runs of this graph, e.g. 'trade_analysis'. Omit for all.",
    ),
    symbol: Optional[str] = Query(
        None,
        description="Only runs on this instrument, e.g. 'BTC/USDT'. Omit for all.",
    ),
) -> Dict[str, Any]:
    """Recent graph runs from the trace store (spec Section 39.7).

    Tracing is observability, not recovery — 39.7 is explicit about that, and the
    durable state lives in the checkpointer instead.

    WHY `graph` EXISTS
    ------------------
    The monitoring graph runs once per tick per open position, so it OUTNUMBERS
    every other graph by orders of magnitude. A caller asking for "the last run"
    to seed a Trade Decision pipeline view got a `position_monitoring` trace every
    single time — twelve nodes with names that do not appear in Graph 2 at all, so
    the view stayed empty and looked exactly like a system that had never run.

    Filtering server-side rather than over-fetching and filtering in the browser:
    the caller would otherwise have to guess how many runs to request to be sure
    of finding one, and the honest answer is that there is no such number.

    WHY `symbol` EXISTS
    -------------------
    The same reasoning, one level down. "The last `trade_analysis` run" is not
    necessarily the last run on the instrument the operator is watching — the
    agent analyses whatever it was triggered on. A session running on SOL/USDT
    therefore seeded its pipeline view from a BTC/USDT run the moment anything
    triggered one, and the diagram showed a different coin than the trade being
    executed with nothing on screen saying so.
    """
    from backend.graphs.tracing import list_recent_runs

    # Over-fetch before filtering so `limit` means "give me this many of the graph
    # I asked for", not "look at this many and keep whatever matches".
    if graph or symbol:
        pool = list_recent_runs(limit=200)
        runs = [
            r for r in pool
            if (not graph or r.get("graph") == graph)
            and (not symbol or r.get("symbol") == symbol)
        ][:limit]
    else:
        runs = list_recent_runs(limit=limit)

    return {
        "runs": runs,
        "count": len(runs),
        "graphFilter": graph,
        "symbolFilter": symbol,
        "tracingMeaning": (
            "spec Section 39.7: tracing tells you what a run DID — every node, every "
            "unavailable input, every error. It is not a substitute for the "
            "checkpointer's durable state"
        ),
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    """One run's full trace."""
    from backend.graphs.tracing import list_recent_runs

    for run in list_recent_runs(limit=500):
        if str(run.get("runId") or run.get("run_id")) == run_id:
            return run
    raise HTTPException(status_code=404, detail=f"no trace for run {run_id}")


@router.get("/meta-learning")
async def meta_learning() -> Dict[str, Any]:
    """Spec Section 33's six self-assessment questions.

    Read-only and writes nothing — CLAUDE.md invariant 5. A finding here becomes a
    change only by a human reading it and creating a hypothesis.
    """
    from backend.graphs.learning_graph import run_meta_learning

    return await run_meta_learning()


@router.get("/positions")
async def monitored_positions() -> Dict[str, Any]:
    """What the position monitor is currently watching (spec Section 13).

    Read through `snapshot_open()`, which returns copies — a caller holding a live
    `_Tracked` could otherwise assign `stop_loss` directly and bypass
    `tighten_stop`'s refusal to widen a stop.
    """
    from backend.agents.position_monitor import get_position_monitor

    monitor = get_position_monitor()
    positions = monitor.snapshot_open()
    return {
        "positions": positions,
        # `open_position_count` is a PROPERTY, not a method. Counted from the snapshot
        # instead so this endpoint cannot disagree with the list it just returned.
        "count": len(positions),
        "stopMeaning": (
            "stops are enforced by PositionMonitorAgent on every tick, and can only "
            "ever be TIGHTENED — widening would exceed the risk the position was sized "
            "against"
        ),
    }


@router.get("/volatility")
async def volatility_readings(
    limit: int = Query(50, ge=1, le=200),
    symbol: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Volatility readings produced since this process started. Newest first.

    IN-MEMORY, NOT A TABLE, AND IT SAYS SO IN THE RESPONSE. The volatility node
    runs on every analysis cycle and every monitoring tick per open position;
    persisting each one would add thousands of rows a day to a database the
    operator is deliberately keeping small, for a handful of readings that ever
    attach to a trade. The durable record is the frontend's capped file
    (`lib/volatilityHistoryStore.server.ts`), which folds these in as it polls.

    So a restart empties this, and only the last
    `volatility_journal.MAX_ENTRIES` readings are held. `bufferedTotal` and
    `retention` are returned so a caller can SEE that rather than mistake a short
    list for a quiet market.
    """
    from backend.services import volatility_journal

    entries = volatility_journal.recent(limit=limit, symbol=symbol)
    return {
        "readings": entries,
        "count": len(entries),
        "bufferedTotal": volatility_journal.size(),
        "retention": (
            f"in-memory ring of at most {volatility_journal.MAX_ENTRIES} readings, "
            f"cleared on restart; the durable record is the frontend's capped file"
        ),
        "meaning": (
            "regime is ranked against this instrument's OWN recent ATR% distribution "
            "when basis='percentile'; basis='absolute' is a thin-history fallback and "
            "is NOT comparable across instruments"
        ),
    }


@router.get("/reconciliation")
async def reconciliation_status(refresh: bool = Query(False)) -> Dict[str, Any]:
    """How the local book compares to the VENUE's, as of the last check.

    `refresh=true` forces a fresh private call. Off by default because this
    endpoint is polled by a dashboard and the balance/positions move only on a
    fill — re-asking the venue on every poll spends the API key's rate budget,
    and that budget is what places orders.

    A `venuePositions` of null means the venue could not be ASKED. It is not a
    report that the venue holds nothing, and no caller may treat it as one.
    """
    from backend.services import reconciliation

    report = await reconciliation.reconcile() if refresh else reconciliation.last_report()
    if report is None:
        return {
            "available": False,
            "reason": (
                "no reconciliation has run yet. The loop runs once a minute and only "
                "while LIVE_TRADING is on — there is no real book to compare otherwise."
            ),
        }
    return {"available": True, **report.as_dict()}


@router.get("/strategy-performance")
async def strategy_performance_summary() -> Dict[str, Any]:
    """Realised results per strategy — the feedback loop, made visible.

    Every profile used to carry `historical_success_rate=None` and scoring said so
    on every run. This is what closed that: deterministic arithmetic over closed
    trades, gated on a sample floor before it may influence selection.

    `usable: false` means the strategy has not closed enough trades for its win
    rate to steer anything yet. It is still reported — an operator should be able
    to see a young strategy's record without it moving the agent.
    """
    from backend.services import strategy_performance

    return await strategy_performance.summary()


@router.post("/run/{symbol:path}")
async def run_analysis(
    symbol: str,
    _auth: None = Depends(require_write_auth),
) -> Dict[str, Any]:
    """Start one Trade Decision run for a symbol (spec Section 35, Graph 2).

    THE ONLY STATE-CHANGING ROUTE HERE, and it still cannot trade. It produces a
    decision and — only if the Risk Gateway approves — an INERT `ExecutionPlan`.
    Submission is gated separately by `GRAPH_EXECUTION_ENABLED`, which defaults off.

    Auth-gated because a run costs market-data calls and possibly model tokens, not
    because it can move money.
    """
    from backend.graphs.analysis import run_analysis_graph
    from backend.graphs.state import TriggerReason

    result = await run_analysis_graph(
        symbol,
        TriggerReason(
            kind="manual",
            symbol=symbol,
            detail="operator requested a run via the API",
        ),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "graph failed"))
    return result


@router.websocket("/stream")
async def stream_graph_progress(websocket: WebSocket, api_key: str = None) -> None:
    """Live node-by-node progress (spec Section 39.5).

    Accepts `{"symbol": "BTC/USDT"}` and streams one message per node as the graph
    executes, then a final message with the summary.

    Messages carry COUNTS, not the state itself. The state holds candles, seven
    specialist findings and a portfolio snapshot; streaming it per node would push
    megabytes over the socket for a 20-node run, and a dashboard needs to know which
    stage is running, not to re-receive the market.

    Authenticated because it starts a graph run, which costs market data and model tokens.
    """
    if auth_required():
        required = os.getenv(ENV_VAR)
        if not required:
            pass # Shouldn't happen if auth_required is True, but safe fallback
        elif not api_key or not hmac.compare_digest(api_key, required):
            await websocket.close(code=1008)
            return

    await websocket.accept()
    try:
        request = await websocket.receive_json()
    except Exception:  # noqa: BLE001
        await websocket.close(code=1003)
        return

    symbol = str(request.get("symbol") or "").strip()
    if not symbol:
        await websocket.send_json({"error": "a 'symbol' is required"})
        await websocket.close(code=1003)
        return

    from backend.graphs.analysis import analysis_config, summarise_analysis
    from backend.graphs.builder import build_graph
    from backend.graphs.runtime import finish_run, start_run, stream_run
    from backend.graphs.state import TriggerReason

    state, ctx, _ = start_run(
        graph="trade_analysis",
        symbol=symbol,
        trigger=TriggerReason(kind="manual", symbol=symbol,
                              detail="streamed run requested by the dashboard"),
        thread_scope=f"{symbol}-stream",
    )

    final: Dict[str, Any] = {}
    try:
        graph = build_graph(analysis_config(), ctx)
        async for event in stream_run(graph, state):
            await websocket.send_json({"type": "node", **event})
        # `stream_run` yields updates; the accumulated state is what the runner
        # normally returns, so the summary is rebuilt from the graph's final read.
        final = await graph.ainvoke(state)
        await websocket.send_json({"type": "done", **summarise_analysis(final)})
    except WebSocketDisconnect:
        # The client went away mid-run. The run itself is unaffected and its trace is
        # still written — a disconnected dashboard must not abort reasoning.
        logger.info("Graph stream client disconnected for %s", symbol)
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("Graph stream failed for %s: %s", symbol, exc)
        try:
            await websocket.send_json({"type": "error", "error": str(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        finish_run(ctx, final or None, produces_decision=bool(final))
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


def _warm_registry() -> None:
    """Build each config once so the node registry is populated.

    The registry fills lazily as graphs are built, so `/nodes` on a fresh process
    would otherwise report an empty or partial list depending on what had run. Each
    `*_config()` is individually guarded because a failure in one graph must not hide
    the nodes of the others.
    """
    import importlib

    for _, _, module, config_fn in SECTION_35_GRAPHS:
        if not config_fn:
            continue
        try:
            getattr(importlib.import_module(module), config_fn)()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not warm %s.%s: %s", module, config_fn, exc)


@router.get("/order-stream")
async def order_stream_status() -> Dict[str, Any]:
    """What the venue has pushed about our orders, and whether we are listening.

    THE FIELD THAT MATTERS IS `connected`. Reconciliation compares books once a
    minute; this stream is what makes a resting stop firing at the venue visible
    in milliseconds instead. When `connected` is false that fresher signal is
    gone and the only thing watching venue order state is the once-a-minute poll
    — which is the pre-existing behaviour, degraded rather than broken, and worth
    surfacing rather than inferring from an empty list.

    `recent` is a bounded diagnostic ring, newest first. It is NOT a record of
    truth — `trades` is that — and nothing in the system reads back from it.
    """
    from backend.services.order_stream import get_order_stream

    return get_order_stream().snapshot()


@router.get("/excursion")
async def excursion_study(limit: int = Query(2000, ge=1, le=20000)) -> Dict[str, Any]:
    """Would a trailing stop beat the current exit rules? Answered from real fills.

    THIS ENDPOINT EXISTS BECAUSE THE QUESTION WAS UNANSWERABLE.

    Asked to replay a trail against five days of live trading, the honest answer
    was that it could not be done: a trailed stop sits at `peak - TRAILING_STOP_R`
    and `trades` recorded entry and exit but never the path between. 1,173 real
    positions and the data simply was not there.

    `mfe_r` and `mae_r` are now written on every close, so this computes the
    comparison directly rather than modelling it:

      * `trailWouldHaveBeaten` — closes whose MFE ran far enough that a trail
        would have exited ABOVE where they actually did.
      * `stopTooTight` — closes that were stopped out having first gone close to,
        but not through, the stop in the other direction... i.e. winners the stop
        converted into losers. High MAE on WINNING trades says the same thing from
        the other side: the stop was nearly hit on trades that went on to work.

    `sampleSize` is the number of closes carrying an excursion, NOT the number of
    closes. Rows written before this instrumentation have `mfe_r IS NULL` and are
    excluded rather than counted as zero — a zero MFE is a real and much rarer
    fact (a trade that never went a tick into profit) and conflating the two would
    understate every average here.
    """
    from backend.core.db import get_db_pool
    from backend.agents.position_monitor import TRAILING_STOP_R

    pool = get_db_pool()
    if pool is None:
        return {"available": False, "reason": "no database pool"}

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT symbol, pnl, mfe_r, mae_r, note
            FROM trades
            WHERE pnl IS NOT NULL AND mfe_r IS NOT NULL
            ORDER BY ts DESC LIMIT $1
            """,
            limit,
        )

    if not rows:
        return {
            "available": False,
            "reason": (
                "no closed trade carries an excursion yet. Rows written before this "
                "instrumentation have mfe_r NULL; the study becomes answerable as "
                "new trades close."
            ),
            "sampleSize": 0,
        }

    mfes = [float(r["mfe_r"]) for r in rows]
    maes = [float(r["mae_r"]) for r in rows if r["mae_r"] is not None]
    winners = [r for r in rows if float(r["pnl"]) > 0]
    losers = [r for r in rows if float(r["pnl"]) <= 0]

    # How often the peak ran far enough that a trail would have kept more than a
    # break-even exit. A trail only beats break-even when MFE exceeds the trail
    # distance — below that its stop sits at or under the entry.
    beat = [m for m in mfes if m > TRAILING_STOP_R]

    def _avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "available": True,
        "sampleSize": len(rows),
        "trailDistanceR": TRAILING_STOP_R,
        "mfe": {
            "mean": _avg(mfes),
            "max": round(max(mfes), 4),
            "onWinners": _avg([float(r["mfe_r"]) for r in winners]),
            "onLosers": _avg([float(r["mfe_r"]) for r in losers]),
        },
        "mae": {
            "mean": _avg(maes),
            "max": round(max(maes), 4) if maes else None,
            # THE STOP-WIDTH ANSWER. A high MAE on trades that still WON means the
            # stop was nearly hit on trades that went on to work — widen it and
            # those become wins rather than losses.
            "onWinners": _avg([float(r["mae_r"]) for r in winners if r["mae_r"] is not None]),
            "onLosers": _avg([float(r["mae_r"]) for r in losers if r["mae_r"] is not None]),
        },
        "trailWouldHaveBeatenBreakEven": {
            "count": len(beat),
            "fraction": round(len(beat) / len(rows), 4),
            "meanCaptureR": _avg([m - TRAILING_STOP_R for m in beat]),
        },
    }
