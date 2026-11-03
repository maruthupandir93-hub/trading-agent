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


# ---------------------------------------------------------------------------
# The ONE loss limit RUN_UNTIL_TARGET cannot switch off (2026-11-03)
# ---------------------------------------------------------------------------
#
# An external review's P0: "if you deliberately want continuous operation, you
# should at least have a hard account-level maximum loss that this setting
# cannot override." It is right. With every brake off, a session that keeps
# losing trades until the account is below the venue's minimum order — at which
# point it can no longer reach ANY target, which defeats the operator's own
# instruction to run until the target.
#
# So: when equity has fallen this fraction below the session's START, the
# session ends. The operator chooses the fraction; they cannot remove it.
# Bounded to [0.10, 0.95] — below 0.10 it would end sessions on ordinary
# variance (the very thing RUN_UNTIL_TARGET exists to prevent), and a limit of
# 1.0 is not a limit.

HARD_STOP_VAR = "ACCOUNT_HARD_STOP_LOSS_FRACTION"
DEFAULT_HARD_STOP_FRACTION = 0.50
_HARD_STOP_MIN, _HARD_STOP_MAX = 0.10, 0.95


def hard_stop_fraction() -> float:
    """Read at call time; out-of-range or unparseable values are CLAMPED, never
    turned into "no limit"."""
    raw = (os.getenv(HARD_STOP_VAR) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_HARD_STOP_FRACTION
    except ValueError:
        logger.warning("%s=%r is not a number; using %.2f.", HARD_STOP_VAR, raw,
                       DEFAULT_HARD_STOP_FRACTION)
        value = DEFAULT_HARD_STOP_FRACTION
    return min(_HARD_STOP_MAX, max(_HARD_STOP_MIN, value))


def hard_stop_breached(start_equity: float, equity: float) -> "str | None":
    """Why the account has hit its hard loss limit, or None. Pure."""
    if not start_equity or start_equity <= 0 or equity is None:
        return None
    frac = hard_stop_fraction()
    line = start_equity * (1.0 - frac)
    if equity <= line:
        return (
            f"HARD ACCOUNT LOSS LIMIT: equity {equity:.2f} is {frac * 100:.0f}% or "
            f"more below the session start {start_equity:.2f} (line {line:.2f}). "
            f"This limit is not switched off by {RUN_UNTIL_TARGET_VAR}; "
            f"{HARD_STOP_VAR} moves it."
        )
    return None
