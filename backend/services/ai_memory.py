import json
import os
import logging
from typing import Dict, Any, List, Optional
from backend.agents.reflection_agent import analyze_mistake

logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "ai_memory.json")

def _load_memory() -> Dict[str, Any]:
    if not os.path.exists(MEMORY_FILE):
        return {
            "global_stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0
            },
            "assets": {},
            "successful_strategies": {},
            "mistakes": [],
            "trade_ledger": []
        }
    
    try:
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load AI Memory: {e}")
        return {}

def _save_memory(memory: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        with open(MEMORY_FILE, 'w') as f:
            json.dump(memory, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save AI Memory: {e}")

async def record_trade(symbol: str, side: str, pnl: float, reasons: List[str] = None, active_strategies: List[str] = None) -> None:
    """
    Level 5: AI Memory - Record a closed trade.
    """
    memory = _load_memory()
    if not memory:
        return
        
    is_win = pnl > 0
    
    # 1. Update Global Stats
    stats = memory["global_stats"]
    stats["total_trades"] += 1
    stats["total_pnl"] += pnl
    if is_win:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
        
    stats["win_rate"] = (stats["wins"] / stats["total_trades"]) * 100
    
    # 2. Update Asset Preferences
    if symbol not in memory["assets"]:
        memory["assets"][symbol] = {"trades": 0, "pnl": 0.0}
    memory["assets"][symbol]["trades"] += 1
    memory["assets"][symbol]["pnl"] += pnl
    
    # 3. Update Strategies
    if active_strategies and is_win:
        for strat in active_strategies:
            if strat not in memory["successful_strategies"]:
                memory["successful_strategies"][strat] = 0
            memory["successful_strategies"][strat] += 1
            
    # 4. Record Mistakes and Trigger Level 6 Reflection
    if not is_win:
        mistake = {
            "symbol": symbol,
            "side": side,
            "pnl": pnl,
            "strategies": active_strategies or [],
            "note": "Awaiting Reflection Agent analysis"
        }
        
        # Trigger Reflection Agent
        reflection = await analyze_mistake(mistake)
        mistake["note"] = reflection
        
        memory["mistakes"].append(mistake)
        
    # 5. Ledger
    import datetime
    memory["trade_ledger"].append({
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "symbol": symbol,
        "side": side,
        "pnl": pnl,
        "is_win": is_win,
        "strategies": active_strategies or []
    })
    
    # Keep ledger and mistakes from growing infinitely
    if len(memory["trade_ledger"]) > 1000:
        memory["trade_ledger"] = memory["trade_ledger"][-1000:]
    if len(memory["mistakes"]) > 100:
        memory["mistakes"] = memory["mistakes"][-100:]
        
    _save_memory(memory)
    logger.info(f"AI Memory updated. Win Rate: {stats['win_rate']:.1f}%. Total PnL: ${stats['total_pnl']:.2f}")

async def record_closed_trade(
    symbol: str,
    side: str,
    pnl: float,
    strategy: Optional[str] = None,
    tab: str = "paper",
) -> None:
    """Record ONE closed position's outcome. Stats and ledger only — no model call.

    WHY THIS EXISTS, AND WHAT IT FIXES
    ==================================
    `record_trade` above is the only writer of `global_stats`, and its ONLY caller
    is `agents/trading_agent.trading_agent_tick` — the legacy task-based path that
    the autonomous system does not use. The autonomous close path is
    `PositionMonitorAgent._close` -> POSITION_CLOSED, and nothing on it ever
    touched this file.

    Measured on the live system on 2026-09-25:

        backend/data/ai_memory.json   total_trades 0, wins 0, trade_ledger []
        Postgres `trades`             11 closed rows carrying a realised pnl

    Everything downstream of `global_stats` therefore reported "unmeasurable"
    forever, and read as an honest young system rather than a broken feed:

      * `algorithms/probability.measured_accuracy` needs 20 resolved trades and
        always saw 0, so `decision.probability` was permanently null and the live
        graph trace says "only 0 resolved trade(s), need 20".
      * `ConfidenceAgent` silently fell back to its prior on every call.
      * `supervisor_agent._measured_win_rate` returned None, so Kelly sizing used
        the fixed fraction rather than the measured edge.

    WHY IT DOES NOT CALL `analyze_mistake`, WHICH `record_trade` DOES
    ================================================================
    That is an LLM call, and this runs on the CLOSE path. Two reasons not to:
    a close must never wait on a model, and `ReflectionAgent` is ALREADY
    subscribed to POSITION_CLOSED and already produces the lesson — calling it
    here would reflect on every loss twice and spend two slots of the 40/min key
    budget to write the same analysis.

    EVERY ENTRY RECORDS ITS BOOK, AND THAT IS NOT COSMETIC.
    =======================================================
    Before this function existed nothing wrote the ledger at all, so every reader
    of it was reading an empty list and none of them could be wrong. Connecting
    the close path turned those readers ON — and two of them are tab-sensitive:

      * `risk_gateway._ledger()` feeds `validate_trade`'s DAILY-LOSS check. An
        unlabelled ledger means a bad day on PAPER counts against the real book's
        daily loss limit and halts real trading, and a bad day on REAL money
        halts paper testing. Neither is a measurement of the book being gated.
      * `supervisor_agent._measured_win_rate` feeds KELLY SIZING. Paper fills are
        simulated against an observed price with no real slippage and no partial
        fills, so a paper win rate is optimistic relative to a real one. Feeding
        it into the sizing of a REAL trade is an optimistic probability estimate
        driving real position size, which is Kelly at its most dangerous.

    So `global_stats` stays as the all-books total (every existing reader and the
    /api/memory surface keep working unchanged), `tab_stats` carries the split,
    and each ledger row is stamped with its tab so a caller can filter.

    NEVER RAISES. A bookkeeping failure must not propagate into the close path;
    the money has already moved by the time this is called.
    """
    try:
        memory = _load_memory()
        if not memory:
            return

        memory.setdefault("global_stats", {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0,
        })
        memory.setdefault("trade_ledger", [])
        memory.setdefault("successful_strategies", {})
        memory.setdefault("tab_stats", {})
        book = (tab or "paper").strip().lower()
        if book not in ("paper", "real"):
            book = "paper"
        memory["tab_stats"].setdefault(book, {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0,
        })

        # A pnl of exactly 0.0 is a REAL break-even close and counts as a trade —
        # the same rule `trades.pnl` follows, where absent means open and 0 means
        # closed flat. It is not a win.
        is_win = pnl > 0

        for stats in (memory["global_stats"], memory["tab_stats"][book]):
            stats["total_trades"] = (stats.get("total_trades") or 0) + 1
            stats["total_pnl"] = (stats.get("total_pnl") or 0.0) + float(pnl)
            if is_win:
                stats["wins"] = (stats.get("wins") or 0) + 1
            else:
                stats["losses"] = (stats.get("losses") or 0) + 1
            stats["win_rate"] = (stats["wins"] / stats["total_trades"]) * 100
        stats = memory["global_stats"]

        if strategy and is_win:
            memory["successful_strategies"][strategy] = (
                memory["successful_strategies"].get(strategy, 0) + 1
            )

        import datetime as _dt
        memory["trade_ledger"].append({
            "timestamp": _dt.datetime.utcnow().isoformat(),
            "symbol": symbol,
            "side": side,
            # WHICH BOOK. Readers that gate real money must not count paper
            # outcomes, and vice versa — see this function's docstring.
            "tab": book,
            "pnl": float(pnl),
            "is_win": is_win,
            # A manual or event-path close genuinely has no strategy profile
            # behind it, and crediting one would poison the measurement this
            # feeds — the same rule the closing trade row follows.
            "strategies": [strategy] if strategy else [],
        })
        # Bounded for the reason the whole file is bounded: this runs on a 24/7
        # system and an unbounded local journal is a disk incident.
        if len(memory["trade_ledger"]) > 1000:
            memory["trade_ledger"] = memory["trade_ledger"][-1000:]

        _save_memory(memory)
        per_tab = memory["tab_stats"][book]
        logger.info(
            "AI Memory: %s closed at %+.2f on the %s book. %d %s trade(s) at %.1f%% "
            "win rate; %d overall.",
            symbol, pnl, book, per_tab["total_trades"], book, per_tab["win_rate"],
            stats["total_trades"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not record closed trade in AI memory: %s", exc)


def stats_for_tab(tab: str) -> Dict[str, Any]:
    """Realised stats for ONE book, or zeros when that book has closed nothing.

    Zeros rather than the all-books total: a reader asking for the real book's
    win rate must not silently receive the paper book's. `measured_accuracy`
    already refuses to report a rate below its sample floor, so zeros read as
    "not measurable yet", which is the honest answer for a book that has not
    traded.
    """
    memory = _load_memory() or {}
    per_tab = (memory.get("tab_stats") or {}).get((tab or "paper").strip().lower())
    return per_tab or {
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0,
    }


def ledger_for_tab(tab: str) -> list:
    """The trade ledger for ONE book.

    Rows written before the `tab` field existed carry no book and are EXCLUDED
    rather than assumed: attributing an unlabelled loss to the real book could
    halt real trading on a paper result, and attributing it to paper could let a
    real daily-loss limit be exceeded. Neither guess is safe, and there are very
    few such rows.
    """
    memory = _load_memory() or {}
    book = (tab or "paper").strip().lower()
    return [e for e in (memory.get("trade_ledger") or []) if e.get("tab") == book]


def get_memory_stats() -> Dict[str, Any]:
    return _load_memory()

def generate_learning_report() -> Dict[str, Any]:
    """
    Level 17: Learning Dashboard
    Crunches historical trade data into actionable insights.
    """
    memory = _load_memory()
    ledger = memory.get("trade_ledger", [])
    
    if not ledger:
        return {"error": "Not enough data to generate learning report."}
        
    total_trades = len(ledger)
    wins = [t for t in ledger if t["is_win"]]
    losses = [t for t in ledger if not t["is_win"]]
    
    win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
    
    avg_win = sum(w["pnl"] for w in wins) / len(wins) if wins else 0
    avg_loss = sum(abs(l["pnl"]) for l in losses) / len(losses) if losses else 0
    
    # Expectancy = (Win % * Avg Win) - (Loss % * Avg Loss)
    expectancy = ((len(wins) / total_trades) * avg_win) - ((len(losses) / total_trades) * avg_loss) if total_trades > 0 else 0
    
    # Strategy Performance
    strategy_stats = {}
    for t in ledger:
        for s in t.get("strategies", []):
            if s not in strategy_stats:
                strategy_stats[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
            strategy_stats[s]["trades"] += 1
            strategy_stats[s]["pnl"] += t["pnl"]
            if t["is_win"]:
                strategy_stats[s]["wins"] += 1
                
    for s in strategy_stats:
        strategy_stats[s]["win_rate"] = (strategy_stats[s]["wins"] / strategy_stats[s]["trades"]) * 100
        
    # Sort strategies
    sorted_strats = sorted(strategy_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
    best_strategies = [{"name": s[0], **s[1]} for s in sorted_strats[:3]]
    worst_strategies = [{"name": s[0], **s[1]} for s in sorted_strats[-3:] if s[1]["pnl"] < 0]
    
    # Calculate Peak and Max Drawdown
    peak = 0
    current = 0
    max_drawdown = 0
    returns = []
    
    for t in ledger:
        current += t["pnl"]
        returns.append(t["pnl"]) # Simple nominal returns for proxy
        if current > peak:
            peak = current
        dd = peak - current
        if dd > max_drawdown:
            max_drawdown = dd
            
    # Calculate Sharpe / Sortino (Proxy using nominal returns, assuming risk_free=0)
    import numpy as np
    
    sharpe_ratio = 0
    sortino_ratio = 0
    if len(returns) > 1:
        mean_return = np.mean(returns)
        std_dev = np.std(returns)
        if std_dev > 0:
            sharpe_ratio = mean_return / std_dev
            
        downside_returns = [r for r in returns if r < 0]
        if len(downside_returns) > 0:
            downside_std = np.std(downside_returns)
            if downside_std > 0:
                sortino_ratio = mean_return / downside_std

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "expectancy_usd": expectancy,
        "average_win": avg_win,
        "average_loss": avg_loss,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": float(sharpe_ratio),
        "sortino_ratio": float(sortino_ratio),
        "best_strategies": best_strategies,
        "worst_strategies": worst_strategies
    }
