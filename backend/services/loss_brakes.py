"""RUN_UNTIL_TARGET: the operator's choice that losses never stop the agent.

THE OPERATOR'S INSTRUCTION (2026-11-02): *"i don't need any stop after loss i
need it to run until the target amount is reach"*.

Five mechanisms could stop or pause trading because of losses. With this switch
ON, every one of them keeps MEASURING and LOGGING what it would have done, and
none of them ACTS:

    session floor          trading_session   ends a session at 50% of its start
    drawdown killswitch    ceo_agent         Observation Mode at -MAX_DRAWDOWN_FROM_HWM
    daily loss limit       risk_manager      refuses entries after a bad day
    risk-coherence check   trading_session   refuses to START a self-halting config
    re-entry cooldown      reentry_cooldown  30 min same-side pause after a loss

WHAT IT DOES NOT TOUCH, BECAUSE THEY ARE NOT "STOPPING AFTER A LOSS":

  * The per-trade STOP-LOSS (invariant 3). It does not stop the agent; it
    bounds each trade. Without it one adverse move can lose the whole account
    and the session can never reach any target at all.
  * The operator's own PAUSE and EMERGENCY STOP. Those are the operator's
    hand, not an automatic brake.
  * The leverage ceiling, the liquidation cap, the venue minimum — properties
    of a single order, not reactions to a loss.
  * Exits (invariant 4) — never blocked by anything.

WHAT IT COSTS, STATED ONCE. With every brake off, a session that is losing
keeps trading until the account is too small for the venue's minimum order.
The brakes existed to end a losing run before that. The operator owns the
account and made this choice explicitly; `RUN_UNTIL_TARGET=false` restores
every brake, read at call time, no restart.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

RUN_UNTIL_TARGET_VAR = "RUN_UNTIL_TARGET"


def run_until_target() -> bool:
    """Read at call time. Default OFF in code; the operator turns it on in `.env`.

    Default off so a fresh deployment of this codebase keeps its brakes; the
    operator's own `.env` is where their choice lives.
    """
    raw = (os.getenv(RUN_UNTIL_TARGET_VAR) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def note(brake: str, would_have: str) -> str:
    """One consistent line for a brake that measured a breach and did not act."""
    return (
        f"{brake}: would have {would_have}, but {RUN_UNTIL_TARGET_VAR} is on — "
        f"the operator chose to keep trading until the target."
    )
