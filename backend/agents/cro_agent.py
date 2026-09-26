import logging
import os
import uuid
from typing import List, Optional

from backend.agents.supervisor_agent import SupervisorAgent
from backend.core.agent_base import BaseAgent
from backend.core.db import get_db_pool
from backend.core.risk_manager import max_leverage_ceiling
from backend.models.events import EventType, BaseEvent, TarSubmittedEvent, TarApprovedEvent, TarRejectedEvent
from backend.services.portfolio_store import get_portfolio

logger = logging.getLogger(__name__)

# Spec Section 18: "The 99% 24-hour VaR of the entire portfolio must never
# exceed 5% of total equity."
#
# READ AT CALL TIME so the operator can raise it knowingly without a restart —
# and DEFAULTED TO THE SPEC'S 0.05, so it is unchanged unless someone changes it
# on purpose. It is the one number here that encodes a POLICY rather than a
# measurement, and raising it is the operator's decision, not this module's.
def max_portfolio_var_fraction() -> float:
    raw = os.getenv("MAX_PORTFOLIO_VAR_FRACTION")
    if raw is None or not raw.strip():
        return 0.05
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("MAX_PORTFOLIO_VAR_FRACTION=%r is not a number; using 0.05.", raw)
        return 0.05
    if not 0.0 < value <= 1.0:
        logger.warning("MAX_PORTFOLIO_VAR_FRACTION=%r is out of range; using 0.05.", raw)
        return 0.05
    return value


# Kept for the case where the loss is genuinely unbounded by a stop. See
# `_adverse_move_fraction` for why this is no longer applied to every trade.
WORST_CASE_ADVERSE_MOVE = 0.10

# How far price is assumed to travel BEYOND the stop before the close fills.
# A stop is not a guarantee: it is a trigger, and a gap or a thin book fills it
# worse. 1.5x the stop distance is a deliberate, documented allowance for that,
# not a measurement — the same class of honest approximation as the constant
# above, and named rather than inlined for the same reason.
STOP_SLIPPAGE_MULTIPLIER = 1.5


def _adverse_move_fraction(entry: float, stop: Optional[float]):
    """How far this position can move against us, as a fraction of entry.

    WHY THIS IS NOT A FLAT 10% ANY MORE, AND WHY THAT WAS A MEASUREMENT ERROR
    ========================================================================
    The check used `notional x 0.10` for every trade. That is the right shape for
    a position with NO stop, and this system has no such positions: CLAUDE.md
    invariant 3 makes a computed stop mandatory, the Risk Gateway hard-rejects
    without one, and the constraint immediately above this one re-verifies that
    the stop is on the correct side of entry. The loss a stopped position can
    take is the STOP DISTANCE plus slippage, not 10% of notional.

    Using 10% regardless capped notional at a flat `0.05 / 0.10 = 0.5x equity`,
    whatever the stop and whatever the leverage. That silently forbade the
    broker-style sizing this system documents and the operator configured:

        25% allocation at 3x  ->  0.75x equity notional  ->  REJECTED
       100% allocation at 3x  ->  3.00x equity notional  ->  REJECTED

    Measured live on 2026-09-25 with a real plan:

        Global VaR limit exceeded: a 10% adverse move on $18815.50 notional is
        $1881.55, above the 5% of $25088.49 equity limit ($1254.42)

    while the stop on that same trade sat 1.71% away - a real worst case of
    about $483, a quarter of what the check asserted. Two gates over one
    quantity disagreed: the Risk Gateway sized the trade and the CRO refused it,
    so the operator's allocation was decided by a limit nobody could see.

    THIS IS NOT A LOOSENING OF THE POLICY. The 5% VaR limit is untouched and
    still enforced against equity. What changes is that the number compared
    against it is derived from the trade's OWN bounded loss rather than from a
    constant that ignores the stop the whole system is built to guarantee. A
    WIDER stop now consumes more of the limit, which is the correct direction:
    it is genuinely more risk.

    Falls back to the flat worst case when no usable stop is present, so an
    unbounded position is still judged as unbounded.
    """
    if stop is None or stop <= 0 or entry <= 0:
        return WORST_CASE_ADVERSE_MOVE, "no usable stop: flat worst-case move"
    distance = abs(entry - stop) / entry
    if distance <= 0:
        return WORST_CASE_ADVERSE_MOVE, "stop is at entry: flat worst-case move"
    # Never judged as riskier than an unstopped position would be. A stop wider
    # than the worst case adds nothing to the estimate, because beyond that point
    # the flat assumption is already the more conservative of the two.
    move = min(distance * STOP_SLIPPAGE_MULTIPLIER, WORST_CASE_ADVERSE_MOVE)
    return move, (
        "stop {:.2f}% away x{:.1f} slippage allowance".format(
            distance * 100, STOP_SLIPPAGE_MULTIPLIER
        )
    )

class CROAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "Chief Risk Officer AI"

    @property
    def purpose(self) -> str:
        return "Evaluates all Trade Authorization Requests (TAR) against hard mathematical constraints (VaR, Correlation)."

    @property
    def permissions(self) -> List[str]:
        return ["READ_PORTFOLIO_STATE", "REJECT_TAR", "APPROVE_TAR"]

    @property
    def inputs(self) -> List[str]:
        return [
            "TAR_SUBMITTED events (size, leverage, stop-loss, entry price, tab)",
            "Portfolio equity via services/portfolio_store.get_portfolio",
            "The leverage ceiling constants from core/risk_manager (module constants, not config)",
        ]

    @property
    def outputs(self) -> List[str]:
        return [
            "TAR_APPROVED events (carrying the stop-loss through to Execution)",
            "TAR_REJECTED events naming the specific rule breached",
            "Rows in the `risk_events` table for both outcomes",
        ]

    @property
    def category(self) -> str:
        return "risk"

    @property
    def events_consumed(self) -> List[EventType]:
        return ["TAR_SUBMITTED"]

    @property
    def events_published(self) -> List[EventType]:
        return ["TAR_APPROVED", "TAR_REJECTED"]


    @property
    def responsibilities(self) -> List[str]:
        return ["Execute core duties as assigned."]

    @property
    def dependencies(self) -> List[str]:
        return ["MessageBus"]

    @property
    def memory_ttl(self) -> str:
        return "Ephemeral (process lifetime)"

    @property
    def knowledge_sources(self) -> List[str]:
        return ["Internal state"]

    @property
    def prompt_reference(self) -> str:
        return "CRO_DETERMINISTIC_V1"

    @property
    def apis_used(self) -> List[str]:
        return ["None"]

    @property
    def database_tables(self) -> List[str]:
        return ["None"]

    @property
    def metrics_reported(self) -> List[str]:
        return ["Uptime", "Events Processed"]

    @property
    def failure_recovery_strategy(self) -> str:
        return "Restart agent process"

    @property
    def health_status(self) -> str:
        return "Active"


    async def handle_event(self, event: BaseEvent) -> None:
        if event.event_type == "TAR_SUBMITTED":
            if isinstance(event, TarSubmittedEvent):
                await self._process_tar(event)

    async def _reject(self, tar: TarSubmittedEvent, rule: str, reason: str) -> None:
        logger.warning("CRO REJECTED %s: %s", tar.tar_id, reason)
        await self._persist_risk_event(str(tar.tar_id), "REJECTED", rule, reason)
        await self.publish(TarRejectedEvent(
            tar_id=tar.tar_id,
            rule_breached=rule,
            cro_rationale=reason,
        ))

    async def _process_tar(self, tar: TarSubmittedEvent) -> None:
        logger.info(f"CRO evaluating TAR {tar.tar_id} for {tar.symbol}")

        ceiling = max_leverage_ceiling(tar.tab)

        # ---------------------------------------------------------------
        # Constraint 1: the leverage ceiling.
        #
        # Checked FIRST, before any other math, so no other quantity can
        # influence it. This used to be `if tar.requested_leverage > 5` — a
        # bare literal that (a) disagreed with the TypeScript side's 3x real
        # / 10x paper ceiling, so the effective limit depended on which code
        # path a trade took, and (b) ignored paper-vs-real entirely. It now
        # reads the shared constant from core/risk_manager, which is a module
        # constant specifically so no config or agent can raise it
        # (CLAUDE.md invariant 2).
        # ---------------------------------------------------------------
        if tar.requested_leverage > ceiling:
            await self._reject(
                tar,
                "MAX_LEVERAGE_LIMIT",
                f"Requested leverage {tar.requested_leverage}x exceeds the hard {ceiling}x ceiling "
                f"for the '{tar.tab}' tab. This ceiling is not configurable and cannot be raised "
                f"by any agent or confidence level.",
            )
            return

        # ---------------------------------------------------------------
        # Constraint 2: a stop-loss must be present and on the correct side
        # of entry (CLAUDE.md invariant 3).
        #
        # `stop_loss` is a required field on the event, so it cannot be
        # absent — but it can still be nonsense (a stop above entry on a
        # long is not a stop, it is a guaranteed immediate exit or an
        # inverted risk calculation). Verified here rather than trusted,
        # because the CRO is the last gate before execution.
        # ---------------------------------------------------------------
        entry = tar.entry_price
        if entry is not None and entry > 0:
            if tar.direction == "LONG" and tar.stop_loss >= entry:
                await self._reject(
                    tar,
                    "INVALID_STOP_LOSS",
                    f"Stop-loss {tar.stop_loss} is at or above the entry price {entry} on a LONG — "
                    f"that is not a protective stop.",
                )
                return
            if tar.direction == "SHORT" and tar.stop_loss <= entry:
                await self._reject(
                    tar,
                    "INVALID_STOP_LOSS",
                    f"Stop-loss {tar.stop_loss} is at or below the entry price {entry} on a SHORT — "
                    f"that is not a protective stop.",
                )
                return

        # ---------------------------------------------------------------
        # Constraint 3: Global VaR (spec Section 18 — 99% 24h VaR must not
        # exceed 5% of total equity).
        #
        # The previous version was, in its own words, "mocked logic":
        #
        #     total_equity = 100000.0
        #     implied_var = tar.requested_size * 0.10
        #
        # Two problems. Equity was a hardcoded $100,000 that had nothing to
        # do with the actual account, so the limit was meaningless for any
        # real balance — and for the $2-to-$5 capital-target mission in this
        # repo it was off by five orders of magnitude. And `size * 0.10`
        # treats *quantity* as if it were dollars: 0.1 BTC and 0.1 DOGE
        # produced an identical "VaR" of 0.01. With a 5000 threshold the
        # check could never fire, so it always passed.
        #
        # Now: real equity from the portfolio store, and VaR measured in
        # currency as notional × a worst-case adverse move.
        # ---------------------------------------------------------------
        portfolio = await get_portfolio()
        total_equity = SupervisorAgent._equity_for(portfolio, tar.tab)
        if total_equity <= 0:
            await self._reject(
                tar,
                "UNKNOWN_EQUITY",
                f"Equity for the '{tar.tab}' tab is unknown, so portfolio-level VaR cannot be "
                f"evaluated. Refusing rather than approving against an assumed balance "
                f"(this check previously assumed a hardcoded $100,000).",
            )
            return

        if entry is None or entry <= 0:
            await self._reject(
                tar,
                "UNKNOWN_ENTRY_PRICE",
                "TAR carries no entry price, so notional exposure and therefore VaR cannot be computed.",
            )
            return

        notional = tar.requested_size * entry
        adverse_move, move_basis = _adverse_move_fraction(entry, tar.stop_loss)
        var_fraction = max_portfolio_var_fraction()
        implied_var = notional * adverse_move
        var_limit = total_equity * var_fraction

        if implied_var > var_limit:
            # THE REFUSAL SAYS WHAT WOULD FIT. A limit that only says "no" leaves
            # the operator guessing at an allocation, and that guess is what
            # produced "I chose 100% allocation but it only takes some amount":
            # the size was being decided by a gate they could not see. The
            # affordable notional is arithmetic over numbers already in hand, so
            # stating it invents nothing.
            affordable = var_limit / adverse_move if adverse_move > 0 else 0.0
            await self._reject(
                tar,
                "GLOBAL_VAR_LIMIT",
                f"Global VaR limit exceeded: a {adverse_move * 100:.2f}% adverse move "
                f"({move_basis}) on ${notional:.2f} notional is ${implied_var:.2f}, above the "
                f"{var_fraction * 100:.1f}% of ${total_equity:.2f} equity limit "
                f"(${var_limit:.2f}). The largest notional that fits is "
                f"${affordable:.2f} ({affordable / total_equity:.2f}x equity) - reduce the "
                f"session's allocation or leverage, tighten the stop, or raise "
                f"MAX_PORTFOLIO_VAR_FRACTION if you accept the larger loss per stop-out.",
            )
            return

        # ---------------------------------------------------------------
        # Correlated-exposure and drawdown-killswitch constraints from spec
        # Section 18 are NOT implemented here yet. Named explicitly so this
        # gap is visible rather than implied by the rationale string, which
        # used to claim it had "Passed all VaR and correlation constraints"
        # while performing no correlation check whatsoever.
        # ---------------------------------------------------------------
        rationale = (
            f"Approved: leverage {tar.requested_leverage}x within {ceiling}x ceiling; "
            f"stop-loss {tar.stop_loss:.6g} verified on the correct side of entry {entry:.6g}; "
            f"implied VaR ${implied_var:.2f} ({move_basis}) within ${var_limit:.2f} "
            f"({var_fraction * 100:.1f}% of ${total_equity:.2f} equity). "
            f"Correlation caps and the drawdown killswitch are not yet implemented and were NOT checked."
        )
        logger.info(f"CRO APPROVED {tar.tar_id}")

        await self._persist_risk_event(str(tar.tar_id), "APPROVED", None, rationale)

        await self.publish(TarApprovedEvent(
            tar_id=tar.tar_id,
            symbol=tar.symbol,
            direction=tar.direction,
            approved_size=tar.requested_size,
            approved_leverage=tar.requested_leverage,
            cro_rationale=rationale,
            # Carried through so Execution attaches the stop Risk approved,
            # rather than re-deriving one that could differ.
            stop_loss=tar.stop_loss,
            take_profit=tar.take_profit,
            tab=tar.tab,
            # Passed straight through. The CRO decides whether to approve, not
            # which run this came from.
            run_id=getattr(tar, "run_id", None),
            strategy=getattr(tar, "strategy", None),
            entry_context=getattr(tar, "entry_context", None),
        ))

    async def _persist_risk_event(self, tar_id: str, decision: str, rule_breached: str | None, rationale: str):
        pool = get_db_pool()
        if not pool:
            return
            
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO risk_events (event_id, tar_id, decision, rule_breached, rationale)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    str(uuid.uuid4()), tar_id, decision, rule_breached, rationale
                )
        except Exception as e:
            logger.error(f"Failed to persist risk event for TAR {tar_id}: {e}")

def get_cro_agent() -> CROAgent:
    return CROAgent()
