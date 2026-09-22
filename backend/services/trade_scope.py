"""May we OPEN a position right now? One answer, for both execution paths.

WHY THIS MODULE EXISTS — AND IT IS A CORRECTION OF MY OWN EARLIER FIX
=====================================================================
This system has TWO paths that can submit a trade:

  1. THE GRAPH PATH   trigger -> 24-node analysis -> Supervisor node
                      -> `graphs/nodes/risk_gateway.gate()` -> execution service
  2. THE EVENT PATH   DEBATE_CONCLUDED / STRESS_TESTED
                      -> `agents/supervisor_agent._consider_trade` -> TAR -> CRO

Every gate written over the life of this project — the tradeable-instrument
blocklist, the higher-timeframe alignment check, the capital pool, and most
recently session scope and one-position-at-a-time — lives in the RISK GATEWAY,
which is on path 1 only. Path 2 has `validate_trade` and nothing else.

That did not matter while path 2 could not trade. `algorithms/dynamic_thresholding`
demanded 0.60-0.99 confidence on a scale whose observed ceiling was 0.44, so the
event-driven supervisor refused everything: 2,948 decisions, 100% rejected, zero
trades.

Rescaling those thresholds to the debate's real units was correct — an
unreachable gate is a bug, and it was reported as ordinary selectivity. But it
UNBLOCKED A PATH WITH NO OTHER GATES, and the ledger shows exactly that:

    before the rescale   0 trades
    after  the rescale   4,080 fills in five days, all tagged
                         strategy='Event-Driven Multi-Agent Pipeline'
                         856 closes on BTC/USDT, which is on the untradeable list
                         up to 3 symbols held at once
                         no session was ever started

So the gates are lifted out of the gateway into here, and both paths consult
them. A limit that only one of two execution paths respects is not a limit.

WHAT THIS DELIBERATELY DOES NOT COVER
=====================================
EXITS. Nothing in this module is ever consulted for a close. Invariant 4 is
absolute and is most important precisely when a limit has been breached — that is
exactly when a gateway would otherwise refuse to let someone out. Both callers
check this only on the entry branch, and their tests assert it.

SIZING. This answers "may we open at all?", never "how big?". A refusal here is a
property of the instrument, the session or the portfolio, and must not be
reachable by making the trade smaller — the same reason the tradeable-instrument
gate sits first among the gateway's entry checks.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def normalise_symbol(symbol) -> str:
    """Compare instruments across spellings: `SOL/USDT` vs `SOL/USDT:USDT`.

    The settle suffix is stripped for the reason `tradeable_universe` and
    `reconciliation` strip it — ccxt's unified perpetual form and the agent's own
    form are two spellings of one market, and a scope keyed on one spelling is
    bypassed by whichever hop resolved the symbol first. That exact bug made
    reconciliation report every real position as a phantom once already.
    """
    return str(symbol or "").split(":")[0].strip().upper()


def max_concurrent_positions() -> int:
    """How many positions may be open at once, across every instrument.

    ONE by default. The live ledger is the argument: 4,080 fills in five days, up
    to three symbols held simultaneously, and 2,426 closed trades that netted
    -219.13. Three concurrent positions each sized against the same capital pool
    is three times the exposure a session's allocation describes, and three times
    the fee bill for the same money.

    READ AT CALL TIME. A module-level `os.getenv` would be the `simulation_mode`
    bug again: the operator turns concurrency down, is told it worked, and the
    running agent keeps the old behaviour until a restart.

    Floored at 1 — a zero would block every entry forever, which is a config typo
    silently becoming a halt.
    """
    raw = os.getenv("MAX_CONCURRENT_POSITIONS")
    if raw is None or not raw.strip():
        return 1
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("MAX_CONCURRENT_POSITIONS=%r is not an integer; using 1.", raw)
        return 1


def session_only_trading() -> bool:
    """Whether an ENTRY requires an operator session.

    Defaults ON, and that is a deliberate change of behaviour. With it off the
    agent opened 4,080 positions over five days on instruments, sizes and
    leverages no session had chosen — while the operator believed starting a
    session was how trading began.
    """
    return (os.getenv("SESSION_ONLY_TRADING") or "true").strip().lower() == "true"


def entry_refusal(
    symbol: str,
    held_symbols: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Why this entry may not open, or None if it may. NEVER call this for an exit.

    `held_symbols` is what is open RIGHT NOW, from whichever book the caller
    trusts — the graph path reads `portfolio.open_positions`, the event path reads
    the position monitor's watch list. Passed in rather than fetched here so this
    module stays a pure decision function with no I/O and no opinion about which
    book is authoritative.

    Order matters and mirrors the gateway's: instrument first, then portfolio,
    then session. Each is a property of something other than the trade's size, so
    none of them may be reachable by proposing a smaller trade.
    """
    from backend.services.tradeable_universe import refusal_reason as untradeable_reason

    refusal = untradeable_reason(symbol)
    if refusal is not None:
        return refusal

    held = [h for h in (held_symbols or []) if h]
    limit = max_concurrent_positions()
    if len(held) >= limit:
        listed = ", ".join(str(h) for h in list(held)[:5]) or "unknown"
        return (
            f"already holding {len(held)} position(s) ({listed}) and the limit is "
            f"{limit}. No new position opens until one closes."
        )

    if session_only_trading():
        from backend.services.trading_session import active_session

        session = active_session()
        if session is None:
            return (
                "no trading session is running, and SESSION_ONLY_TRADING is on, so no "
                "new position may be opened. Start a session to trade."
            )
        if normalise_symbol(session.symbol) != normalise_symbol(symbol):
            return (
                f"the running session is on {session.symbol}, not {symbol}. A session "
                f"trades one instrument."
            )

    return None
