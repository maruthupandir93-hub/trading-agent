"""Is this risk configuration survivable, or does it halt itself on routine variance?

WHY THIS EXISTS — THE OPERATOR'S RUN, AS MEASURED
--------------------------------------------------
The live session took four losses in five trades and stopped. `ceo_agent`
already records the arithmetic:

    average stop-out        $0.0531  = 2.66% of the account
    drawdown limit          10% from the monthly high-water mark
    losses needed to trip   3.8  ->  FOUR consecutive stop-outs

The payoff is a fixed 2:1, so break-even is a ~36% win rate, which makes the
per-trade loss probability about 0.64. Four losses in a row at that rate is

    0.64 ** 4 = 0.168   — about one session in six

That is ROUTINE VARIANCE, not a broken strategy and not a bad run. The killswitch
fired correctly; the configuration made firing inevitable. A limit certain to
trip inside the first handful of trades has stopped being a disaster brake and
become a scheduled outage — and, worse, it halts the account long before enough
trades exist to measure whether the edge is real.

WHAT THIS MODULE DOES, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------------
It compares two numbers the operator has already chosen — the loss a stop-out
costs, and the drawdown the killswitch allows — and refuses a session whose own
limits cannot survive an ordinary losing streak.

It does NOT change either number. Both are the operator's decision and
`MAX_PORTFOLIO_VAR_FRACTION` is deliberately a `.env` setting rather than a UI
control, because raising it raises the loss on every stop-out by the same
multiple. Silently sizing down would be the `simulation_mode` failure in a new
place: the operator chooses one risk and the system quietly runs another.

So it REFUSES AND EXPLAINS, naming every lever and what each costs. A refusal
the operator can act on at the moment they choose the setting is worth far more
than a halt four losses later, which is what they get today.

IT IS ARITHMETIC OVER CONFIGURED VALUES — no model, no market data, no I/O, so
it is deterministic and cheap enough to run inside an HTTP request.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# HOW MANY CONSECUTIVE STOP-OUTS A CONFIGURATION MUST SURVIVE.
#
# Derived, not picked. At the 2:1 payoff this system places, break-even is a
# 33.3% win rate and the measured rate has run near it, so the per-trade loss
# probability is ~0.64 and a streak of k has probability 0.64**k:
#
#     k=4   16.8%   about 1 session in 6      <- what halted the live account
#     k=6    6.9%   about 1 in 15
#     k=8    2.8%   about 1 in 36
#    k=10    1.2%   about 1 in 87
#
# Eight is the point where a halt means "something is genuinely wrong" rather
# than "the coin came up tails a few times". Below it the brake fires on noise,
# and an alert that fires on noise teaches an operator to ignore it — the same
# argument the reconciliation alert is built around.
MIN_LOSSES_BEFORE_HALT = 8
MIN_LOSSES_VAR = "MIN_LOSSES_BEFORE_HALT"


def min_losses_before_halt() -> int:
    """Read at CALL time, never frozen at import.

    The `simulation_mode` rule: a frozen constant means the operator changes the
    setting, is told it worked, and the running process keeps enforcing the old
    number until a restart.
    """
    raw = os.getenv(MIN_LOSSES_VAR)
    if not raw:
        return MIN_LOSSES_BEFORE_HALT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %d.", MIN_LOSSES_VAR, raw, MIN_LOSSES_BEFORE_HALT)
        return MIN_LOSSES_BEFORE_HALT
    if value < 1:
        logger.warning("%s=%r is below 1; using %d.", MIN_LOSSES_VAR, raw, MIN_LOSSES_BEFORE_HALT)
        return MIN_LOSSES_BEFORE_HALT
    return value


SAFE = "SAFE"
UNSAFE = "UNSAFE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RiskCoherence:
    """The verdict, with every number that produced it.

    `loss_fraction_per_stop` is None when it cannot be derived — and that is NOT
    a refusal. "Could not measure" and "measured and it is unsafe" are different
    facts, and this codebase has paid for conflating them three times
    (`fetch_usdt_perpetual_prices`, `open_positions()`, `venue_flat()`). An
    unmeasurable configuration passes with its reason stated; only a measured
    and self-defeating one is refused.
    """

    ok: bool
    loss_fraction_per_stop: Optional[float]
    drawdown_limit: float
    losses_to_trip: Optional[float]
    required: int
    reason: str
    # SAFE | UNSAFE | UNKNOWN — the review's Issue 5.
    #
    # `ok` alone collapses two very different situations into one answer, and
    # the collapse is right for one caller and wrong for another. "I measured
    # the configuration and it survives eight losses" and "I could not
    # establish the risk budget at all" are both `ok=True` here, which is
    # correct for REPORTING and for paper research — an unmeasurable setting
    # is not evidence of an unsafe one, and refusing on it would halt the
    # system whenever an env var was being changed.
    #
    # It is NOT sufficient for authorising a LIVE entry. There, "I do not know
    # what a stop-out costs" is a reason to decline: the whole point of the
    # check is that the operator has chosen a coherent risk policy, and an
    # unknown policy has not been chosen.
    #
    # So the three states are carried separately and `ok` keeps its existing
    # meaning. `live_ok` is the stricter predicate; nothing silently changes
    # behaviour for the paper path.
    verdict: str = "UNKNOWN"

    @property
    def live_ok(self) -> bool:
        """May a REAL-money session start on this configuration?

        Only a measured, coherent one. UNKNOWN is refused here and allowed by
        `ok` — see `verdict`.
        """
        return self.verdict == SAFE


def assess(
    *,
    var_fraction: float,
    drawdown_limit: float,
    stop_slippage_multiplier: float = 1.5,
) -> RiskCoherence:
    """Can this configuration survive `min_losses_before_halt()` stop-outs?

    THE LOSS PER STOP-OUT IS THE VaR BUDGET ITSELF, and that is the whole point
    of the derivation. The Risk Gateway sizes a position so that the CRO's VaR
    check binds: `implied_var = notional x adverse_move <= equity x var_fraction`.
    Measured live, two of three trades used 99.5% of that budget. So a stop-out
    costs approximately `var_fraction` of equity, by construction, whatever the
    leverage or the instrument.

    That is also why raising `MAX_PORTFOLIO_VAR_FRACTION` to clear a venue
    minimum is not free: it moves this number by the same multiple.

    The slippage multiplier is carried for the same reason the CRO applies it —
    a stop gaps through its trigger — but it is NOT applied here, because the
    VaR budget is already expressed post-slippage. It is accepted and reported
    so a caller cannot silently double-count it.
    """
    required = min_losses_before_halt()

    if not var_fraction or var_fraction <= 0 or not drawdown_limit or drawdown_limit <= 0:
        return RiskCoherence(
            ok=True,
            loss_fraction_per_stop=None,
            drawdown_limit=drawdown_limit or 0.0,
            losses_to_trip=None,
            required=required,
            verdict=UNKNOWN,
            reason=(
                "risk coherence could not be measured: the VaR fraction or the drawdown "
                "limit is unset or non-positive, so how many losses would trip the "
                "killswitch is unknown rather than unsafe. Paper research may proceed; "
                "a REAL-money session may not, because an unknown risk budget is not a "
                "chosen one"
            ),
        )

    loss_per_stop = float(var_fraction)
    losses_to_trip = float(drawdown_limit) / loss_per_stop

    if losses_to_trip >= required:
        return RiskCoherence(
            ok=True,
            loss_fraction_per_stop=loss_per_stop,
            drawdown_limit=float(drawdown_limit),
            losses_to_trip=losses_to_trip,
            required=required,
            verdict=SAFE,
            reason=(
                f"a stop-out costs about {loss_per_stop * 100:.1f}% of equity and the "
                f"drawdown killswitch allows {drawdown_limit * 100:.0f}%, so it takes "
                f"{losses_to_trip:.1f} consecutive losses to halt — at or above the "
                f"{required} this configuration must survive"
            ),
        )

    return RiskCoherence(
        ok=False,
        loss_fraction_per_stop=loss_per_stop,
        drawdown_limit=float(drawdown_limit),
        losses_to_trip=losses_to_trip,
        required=required,
        verdict=UNSAFE,
        reason=(
            f"This configuration halts itself on ordinary variance. A stop-out costs "
            f"about {loss_per_stop * 100:.1f}% of equity (the VaR budget the gateway "
            f"sizes to), and the drawdown killswitch fires at {drawdown_limit * 100:.0f}%, "
            f"so only {losses_to_trip:.1f} consecutive losses halt the account. At this "
            f"system's 2:1 payoff the break-even win rate is 33%, which makes a streak "
            f"of {required} routine — the session would stop long before enough trades "
            f"exist to tell a good strategy from a bad one. "
            f"Three ways to fix it, and they are not equivalent: "
            f"(1) lower MAX_PORTFOLIO_VAR_FRACTION to "
            f"{drawdown_limit / required:.3f} or below, which makes every loss smaller "
            f"and is the only one that reduces risk; "
            f"(2) raise MAX_DRAWDOWN_FROM_HWM to at least "
            f"{loss_per_stop * required:.2f}, which accepts a deeper drawdown rather "
            f"than avoiding one; "
            f"(3) fund the account further, which lets a smaller VaR fraction still "
            f"clear the venue's minimum order size. "
            f"Set {MIN_LOSSES_VAR} to override this check deliberately."
        ),
    )


def assess_current() -> RiskCoherence:
    """Assess the configuration this process is actually running.

    Reads both numbers from the modules that OWN them rather than re-reading
    the environment here. A pre-check that re-derives its inputs is how
    `lib/riskManager.ts` and `core/risk_manager.py` drifted apart on the ATR
    multipliers, and how a venue pre-check starts disagreeing with the gate it
    stands in for.
    """
    from backend.agents.ceo_agent import max_drawdown_fraction
    from backend.agents.cro_agent import max_portfolio_var_fraction

    return assess(
        var_fraction=max_portfolio_var_fraction(),
        drawdown_limit=max_drawdown_fraction(),
    )
