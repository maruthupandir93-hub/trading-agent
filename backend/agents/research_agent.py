import logging
import json
import os
import asyncio
from datetime import datetime
from typing import Dict, Any, List

from backend.core.agent_os import AgentDescriptor, get_agent_os
from backend.agents.market_intelligence import run_multi_timeframe_analysis

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
REPORT_FILE = os.path.join(DATA_DIR, "research_report.json")

# Create data dir if it doesn't exist
os.makedirs(DATA_DIR, exist_ok=True)

TOP_N_BY_VOLUME = 5


async def scan_market() -> List[Dict[str, Any]]:
    """Top USDT pairs by 24h quote volume, with a multi-timeframe read on each.

    WHAT WAS WRONG WITH THE PREVIOUS VERSION
    ----------------------------------------
    Three separate ways to fail silently, all of which were observed:

    1. `if resp.status_code == 200:` with NO else. A 451 (Binance refuses some
       regions), a 429, or anything else fell straight through to `return []`
       without a single log line — indistinguishable from "the market has no
       interesting pairs today". This backend already has a whole module,
       `services/upstream`, written because that exact confusion cost real time;
       it was simply never used here.

    2. `logger.error(f"Error scanning market: {e}")`. Several httpx exceptions
       stringify to EMPTY, so the log line was literally
       "Error scanning market: " with nothing after it. The live log showed that
       twice. An error message that names no error is worse than none, because it
       looks like it was written on purpose.

    3. `[d for d in data if d['symbol'].endswith('USDT')]` assumes `data` is a
       list of dicts. When Binance returns an error OBJECT instead — which is
       what a rate limit or a WAF block produces — `d` is a string key and this
       raises `TypeError: string indices must be integers`, caught by the bare
       except above and logged as the empty message from (2).

    So this now goes through `fetch_json`, which is geo-block aware, retries only
    what retrying can fix, and returns a stated reason instead of raising; and the
    payload shape is checked before it is indexed.

    Returns `[]` on any failure, ALWAYS with a log line saying which one.
    """
    from backend.services.upstream import fetch_json

    result = await fetch_json(
        "https://api.binance.com/api/v3/ticker/24hr",
        label="Binance 24h tickers (research scan)",
    )
    if not result.ok:
        logger.error(
            "Market scan could not read the 24h ticker list: %s%s No setups are "
            "reported — this is a FEED failure, not a quiet market.",
            result.error,
            " (the exchange refused this region)" if result.geo_blocked else "",
        )
        return []

    data = result.data
    if not isinstance(data, list):
        # The shape check the old code lacked. An error object here used to raise
        # a TypeError that surfaced as an empty log message.
        logger.error(
            "Market scan expected a list of tickers and got %s: %.200s. No setups "
            "reported.",
            type(data).__name__,
            data,
        )
        return []

    usdt_pairs = []
    for row in data:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            continue
        try:
            volume = float(row["quoteVolume"])
            change = float(row["priceChangePercent"])
        except (KeyError, TypeError, ValueError):
            # A row missing its numbers is dropped, not defaulted to zero — a
            # zero volume would sort it to the bottom and quietly misreport it as
            # a real, very illiquid pair.
            continue
        usdt_pairs.append((volume, change, symbol))

    if not usdt_pairs:
        logger.warning(
            "Market scan read %d ticker(s) but none were usable USDT pairs with a "
            "volume and a 24h change. No setups reported.",
            len(data),
        )
        return []

    usdt_pairs.sort(reverse=True)

    results: List[Dict[str, Any]] = []
    for volume, pct_change, symbol in usdt_pairs[:TOP_N_BY_VOLUME]:
        try:
            mtf = await run_multi_timeframe_analysis(symbol)
        except Exception as exc:  # noqa: BLE001
            # One symbol's analysis failing must not empty the whole scan. Named,
            # because "4 results instead of 5" is otherwise invisible.
            logger.warning(
                "Multi-timeframe analysis failed for %s (%s: %s); it is omitted "
                "from this scan.", symbol, type(exc).__name__, exc,
            )
            continue

        # THE KEY IS `multi_tf_trend`. This read `"overall"`, which
        # `run_multi_timeframe_analysis` has never returned — it sets
        # `features["multi_tf_trend"]` — so `.get()` always took the "Mixed"
        # default, `setup` never left "None", and the Research Agent has never
        # discovered a single setup in its life. The log line it produced,
        # "Research Agent failed to find any setups", read as a quiet market.
        #
        # Defaulting to None rather than "Mixed" so a future rename fails loudly
        # here instead of silently reverting to that behaviour.
        trend = (mtf or {}).get("multi_tf_trend")
        if trend is None:
            logger.warning(
                "Multi-timeframe analysis for %s returned no 'multi_tf_trend' key "
                "(got %s). Treating the trend as unknown rather than Mixed.",
                symbol, sorted((mtf or {}).keys()),
            )
            trend = "Unknown"

        setup = "None"
        if trend == "Bullish" and pct_change > 0:
            setup = "Strong Long Setup"
        elif trend == "Bearish" and pct_change < 0:
            setup = "Strong Short Setup"

        results.append({
            "symbol": symbol,
            "change_24h": f"{pct_change:.2f}%",
            "volume": f"${volume:,.0f}",
            "mtf_trend": trend,
            "discovered_setup": setup,
        })

    return results


async def research_agent_tick(agent_id: str):
    """
    Wakes up periodically to discover new setups and write them to disk.
    """
    logger.info("Research Agent waking up to scan the market...")
    
    findings = await scan_market()
    
    if findings:
        report = {
            "last_updated": datetime.utcnow().isoformat(),
            "trending_coins": findings
        }
        
        with open(REPORT_FILE, "w") as f:
            json.dump(report, f, indent=4)
            
        logger.info(f"Research Agent discovered {len(findings)} trending setups. Saved to memory.")
        for f in findings:
            logger.info(f"  - {f['symbol']}: {f['mtf_trend']} trend | {f['discovered_setup']}")
    else:
        logger.warning("Research Agent failed to find any setups.")

def register_research_agent():
    descriptor = AgentDescriptor(
        id="research_agent_01",
        name="Autonomous Research Agent",
        version="1.0.0",
        description="Scans the market in the background for new setups.",
        capabilities=["research", "discovery"],
        dependencies=[],
        # Was "research", which is not a valid AgentCategory, so this agent
        # never appeared on the dashboard. Research feeds the learning
        # pipeline (spec Section 12), which is the closest valid category.
        category="learning",
        priority=5,
        tickIntervalMs=60000  # Runs every 60 seconds (for demo purposes)
    )
    get_agent_os().register(descriptor, research_agent_tick)
