"""CEO AI — top of spec Section 4's chain of command.

    CEO AI -> CIO AI -> CRO AI -> Research -> Supervisor -> ...

The CEO and CIO were the only two links in that chain with no implementation.

WHAT THIS AGENT IS FOR — AND WHAT IT DELIBERATELY IS NOT
--------------------------------------------------------
It would be easy to add a "CEO AI" that produces strategy commentary and
changes nothing. That is ceremony, and this codebase does not need another
layer that reads well and does no work.

Instead the CEO owns the one authority that genuinely belongs at the top and
was genuinely missing: **the mandate to stop trading altogether.** Spec
Section 18 requires it —

    "Drawdown Killswitch: If portfolio equity drops 10% from the monthly
     high-water mark, the CRO automatically transitions the system to
     Observation Mode (close all trades, halt new entries)."

— and `agents/cro_agent.py` had this gap named in a comment because the CRO
evaluates one TAR at a time and has no view of equity over time. Tracking a
high-water mark across trades is a firm-level judgement, so it lives here.

WHY NOT IN THE CRO
------------------
The CRO is per-trade and stateless: it answers "may this trade proceed?".
The killswitch is per-account and stateful: it answers "should we be trading
at all?". Putting a running equity series inside a per-trade validator would
make each risk decision depend on hidden accumulated state, which is both
harder to test and easy to get wrong after a restart.

WHY IT ACTS ON `system_state` RATHER THAN VETOING TRADES
-------------------------------------------------------
Observation Mode is enforced through `may_open_new_position()`, which every
gate in the system already calls. Halting there stops new entries everywhere
at once, including the task-based `trading_agent` path that never goes through
the CRO. A CEO that published a "please stop" event would only be honoured by
agents that happened to subscribe.

Exits are never blocked (CLAUDE.md invariant 4). "Close all trades" from the
spec is deliberately NOT automated here: firing a market close on every
position during a drawdown is itself a large, slippage-bearing trade executed
in the worst conditions, and the drawdown is evidence the system's judgement
is currently unreliable. The CEO halts and reports what is still open; closing
is the operator's call.
"""

import os
import datetime
import logging
from typing import Any, Dict, List, Optional

from backend.core.agent_base import BaseAgent
from backend.core.config import settings
from backend.core.system_state import (
    enter_observation_mode,
    is_in_observation_mode,
)
from backend.models.events import BaseEvent, EventType, PositionClosedEvent
from backend.services.portfolio_store import get_portfolio

logger = logging.getLogger(__name__)

# Spec Section 18's threshold, and the DEFAULT rather than the law.
#
# THIS LIMIT AND THE PER-TRADE RISK MUST BE COMPATIBLE, AND AT 10x THEY WERE
# NOT. Measured on the operator's live $2.00 session:
#
#     average stop-out        $0.0531  = 2.66% of the account
#     drawdown limit          10% from the monthly high-water mark
#     losses needed to trip   3.8  -> FOUR consecutive stop-outs
#
# The payoff is a fixed 2:1, so break-even is a ~36% win rate, and four losses
# in a row at that rate happens about 1 in 6 — routine variance, not a bad run.
# The session took exactly four Breakout losses (-0.2123, -10.6%) and halted on
# the fourth. The killswitch did its job; the job was impossible.
#
# A limit that is certain to fire within the first handful of trades stops
# being a disaster brake and becomes a scheduled outage. The operator has to be
# able to set one that matches the risk they chose, so this is read from the
# env at CALL time — the `simulation_mode` rule, because a frozen-at-import
# constant means the operator changes it, is told it worked, and the running
# agent keeps halting on the old number until a restart.
MAX_DRAWDOWN_FROM_HIGH_WATER_MARK = 0.10
DRAWDOWN_LIMIT_VAR = "MAX_DRAWDOWN_FROM_HWM"


def max_drawdown_fraction() -> float:
    """The drawdown limit, as a fraction. Read at call time.

    Bounded to (0, 1): a limit of 0 would halt on the first tick that is not a
    new high, and one above 1 can never fire, so both are refused in favour of
    the default rather than silently disabling the brake.
    """
    raw = (os.getenv(DRAWDOWN_LIMIT_VAR) or "").strip()
    if not raw:
        return MAX_DRAWDOWN_FROM_HIGH_WATER_MARK
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %.2f.",
                       DRAWDOWN_LIMIT_VAR, raw, MAX_DRAWDOWN_FROM_HIGH_WATER_MARK)
        return MAX_DRAWDOWN_FROM_HIGH_WATER_MARK
    if not 0.0 < value < 1.0:
        logger.warning("%s=%r is out of range (0,1); using %.2f.",
                       DRAWDOWN_LIMIT_VAR, raw, MAX_DRAWDOWN_FROM_HIGH_WATER_MARK)
        return MAX_DRAWDOWN_FROM_HIGH_WATER_MARK
    return value

# The high-water mark resets monthly per the spec ("monthly high-water mark").
# Without a reset, a single early peak would gate the account forever; with too
# frequent a reset, a slow bleed never trips the limit because the mark keeps
# following equity down.
HWM_WINDOW = "month"


class CEOAgent(BaseAgent):
    version = "1.0.0"
    priority = 1  # highest — evaluated before any other agent

    def __init__(self) -> None:
        self._high_water_mark: Optional[float] = None
        self._hwm_period: Optional[str] = None
        self._last_equity: Optional[float] = None
        super().__init__()

    @property
    def name(self) -> str:
        return "CEO AI"

    @property
    def purpose(self) -> str:
        return "Holds the firm-level mandate to trade, and halts the system into Observation Mode when equity falls more than 10% from the monthly high-water mark."

    @property
    def permissions(self) -> List[str]:
        # It can halt the firm but cannot size, approve, or place a trade.
        # Deliberately no TAR or order permissions: the ability to stop
        # everything should not come bundled with the ability to start it.
        return ["READ_PORTFOLIO", "HALT_TRADING", "SET_OPERATING_MODE"]

    @property
    def inputs(self) -> List[str]:
        return [
            "POSITION_CLOSED events (realized P&L, to update the equity series)",
            "Portfolio equity via services/portfolio_store.get_portfolio",
        ]

    @property
    def outputs(self) -> List[str]:
        return [
            "Observation Mode transitions via core/system_state.enter_observation_mode",
            "A decision record for every drawdown evaluation, including the ones that pass",
            "NO trade authorizations and NO orders — it can only stop, never start",
        ]

    @property
    def category(self) -> str:
        return "orchestration"

    @property
    def events_consumed(self) -> List[EventType]:
        return ["POSITION_CLOSED"]

    @property
    def events_published(self) -> List[EventType]:
        # Publishes nothing. Observation Mode is enforced through
        # may_open_new_position(), which every gate already calls — an event
        # would only be honoured by agents that happened to subscribe.
        return []

    @property
    def responsibilities(self) -> List[str]:
        return [
            "Track the monthly equity high-water mark.",
            "Transition the system to Observation Mode on a 10% drawdown breach.",
            "Report what remains open when it halts — it does not auto-close positions.",
        ]

    @property
    def dependencies(self) -> List[str]:
        return ["MessageBus", "PortfolioStore", "core/system_state"]

    @property
    def memory_ttl(self) -> str:
        return (
            "High-water mark held in-process for the current calendar month. NOT persisted — "
            "see the note in _current_period(); a restart re-establishes the mark from live "
            "equity, which is a real limitation, not a design choice."
        )

    @property
    def knowledge_sources(self) -> List[str]:
        return ["Portfolio equity", "Realized P&L from POSITION_CLOSED events"]

    @property
    def prompt_reference(self) -> str:
        return "CEO_DETERMINISTIC_V1"

    @property
    def apis_used(self) -> List[str]:
        return []

    @property
    def database_tables(self) -> List[str]:
        return []

    @property
    def metrics_reported(self) -> List[str]:
        return ["Current drawdown from HWM", "High-water mark", "Observation-mode transitions"]

    @property
    def failure_recovery_strategy(self) -> str:
        return (
            "Fails safe in the direction of halting. If equity cannot be read the drawdown check "
            "is skipped and logged as unevaluated — it does NOT clear an existing halt. A restart "
            "loses the in-process high-water mark, which is recorded as a known limitation rather "
            "than papered over."
        )

    @property
    def health_status(self) -> str:
        return "Active"

    # -----------------------------------------------------------------

    async def handle_event(self, event: BaseEvent) -> None:
        if isinstance(event, PositionClosedEvent):
            await self.evaluate_mandate(trigger=f"{event.symbol} closed at {event.realized_pnl:+.2f}")

    @staticmethod
    def _current_period() -> str:
        """Calendar month key for the high-water mark.

        Not persisted anywhere. That is a genuine limitation: after a restart
        the mark is re-established from current equity, so a drawdown that
        began before the restart is not detected. Fixing it properly needs an
        equity-history table, which is a schema change and therefore a
        separate piece of work — stated here rather than left for someone to
        discover during a drawdown.
        """
        now = datetime.datetime.utcnow()
        return f"{now.year}-{now.month:02d}"

    async def evaluate_mandate(self, trigger: str = "manual") -> Dict[str, Any]:
        """Update the high-water mark and halt if the drawdown limit is breached."""
        tab = settings.execution_tab
        equity = await self._read_equity(tab)

        if equity is None:
            # Unevaluated is not "passed". Importantly this does not clear an
            # existing halt.
            rationale = (
                f"Drawdown check NOT evaluated: equity for the '{tab}' tab is unknown. "
                f"An existing halt (if any) remains in force."
            )
            self.record_decision("unevaluated", rationale, {"trigger": trigger}, acted=False)
            logger.warning(rationale)
            return {"evaluated": False, "reason": rationale}

        period = self._current_period()
        if self._hwm_period != period:
            # New month: reset the mark to current equity per the spec's
            # "monthly high-water mark".
            self._hwm_period = period
            self._high_water_mark = equity
            logger.info("CEO: high-water mark reset for %s at %.2f", period, equity)

        if self._high_water_mark is None or equity > self._high_water_mark:
            self._high_water_mark = equity

        self._last_equity = equity
        hwm = self._high_water_mark
        drawdown = 0.0 if hwm <= 0 else max(0.0, (hwm - equity) / hwm)

        evidence = {
            "trigger": trigger,
            "tab": tab,
            "equity": round(equity, 2),
            "highWaterMark": round(hwm, 2),
            "drawdownPct": round(drawdown * 100, 3),
            "limitPct": max_drawdown_fraction() * 100,
            "period": period,
        }

        if drawdown > max_drawdown_fraction():
            reason = (
                f"Equity ${equity:.2f} is {drawdown * 100:.2f}% below the {period} high-water mark "
                f"of ${hwm:.2f}, exceeding the {max_drawdown_fraction() * 100:.0f}% "
                f"drawdown limit."
            )
            already = is_in_observation_mode()
            enter_observation_mode(reason)

            open_positions = await self._open_positions(tab)
            if open_positions:
                # Surfaced loudly: the spec's "close all trades" is not
                # automated here (see the module docstring), so the operator
                # needs to know exactly what risk is still on the book.
                logger.critical(
                    "CEO halted trading with %d position(s) still OPEN — these were NOT closed: %s",
                    len(open_positions),
                    open_positions,
                )

            self.record_decision(
                "halt",
                reason,
                {**evidence, "openPositionsNotClosed": open_positions},
                acted=not already,
            )
            return {
                "evaluated": True,
                "halted": True,
                "reason": reason,
                "openPositionsNotClosed": open_positions,
                **evidence,
            }

        rationale = (
            f"Mandate to trade upheld: drawdown {drawdown * 100:.2f}% is within the "
            f"{max_drawdown_fraction() * 100:.0f}% limit "
            f"(equity ${equity:.2f} vs HWM ${hwm:.2f})."
        )
        # Recorded even when it passes — a killswitch that only logs on the day
        # it fires gives no way to see how close the account has been running.
        self.record_decision("continue", rationale, evidence, acted=False)
        return {"evaluated": True, "halted": False, "reason": rationale, **evidence}

    @staticmethod
    async def _read_equity(tab: str) -> Optional[float]:
        """Cash plus marked positions, or None when unknowable."""
        from backend.agents.supervisor_agent import SupervisorAgent

        portfolio = await get_portfolio()
        equity = SupervisorAgent._equity_for(portfolio, tab)
        # _equity_for returns 0.0 for "unknown", which must not be read as a
        # wiped-out account — that would trip the killswitch on every startup
        # for the real tab, where no cash figure is declared.
        return None if equity <= 0 else equity

    @staticmethod
    async def _open_positions(tab: str) -> List[Dict[str, Any]]:
        portfolio = await get_portfolio()
        book = portfolio.get(tab) or {}
        return [
            {"symbol": p.get("symbol"), "qty": p.get("qty"), "avgCost": p.get("avgCost")}
            for p in book.get("positions", [])
        ]


def rearm_high_water_mark(equity: Optional[float] = None) -> Optional[float]:
    """Re-anchor the high-water mark to current equity. Returns the new mark.

    WITHOUT THIS, LEAVING OBSERVATION MODE IS POINTLESS. The mark is the
    MONTH's peak, so an account that halted 11% below it is still 11% below it
    the instant it resumes — the CEO re-evaluates on the next closed trade and
    halts again. Equity can only climb by trading, and trading is what the halt
    forbids, so the account cannot recover inside the month. That is a
    DEADLOCK, not a safety property.

    Re-anchoring is the operator ACCEPTING the drawdown as the new baseline,
    which is exactly what `exit_observation_mode`'s docstring already calls "a
    deliberate acknowledgement". It is deliberately NOT automatic: a mark that
    followed equity down would never trip at all, which is the failure mode
    `HWM_WINDOW`'s comment warns about one screen above.
    """
    agent = get_ceo_agent()
    if equity is not None:
        agent._high_water_mark = float(equity)
    elif agent._last_equity is not None:
        agent._high_water_mark = agent._last_equity
    else:
        return None
    agent._hwm_period = agent._current_period()
    logger.warning(
        "CEO high-water mark RE-ANCHORED to %.2f by operator acknowledgement. "
        "The previous peak is no longer the reference for the drawdown limit.",
        agent._high_water_mark,
    )
    return agent._high_water_mark


_ceo_agent: Optional[CEOAgent] = None


def get_ceo_agent() -> CEOAgent:
    """The process-wide CEO. A SINGLETON, and it was not one.

    THE SAME BUG `get_position_monitor` AND `get_execution_agent` ALREADY HAD,
    missed on the third agent. This used to be `return CEOAgent()` — a new,
    EMPTY agent on every call — while `main.py` builds one at startup and
    subscribes it to the bus, and that instance is the only one accumulating
    `_high_water_mark` and `_last_equity`.

    The halt itself was never affected: the bus-subscribed instance tracked the
    mark correctly and entered observation mode exactly as designed. What broke
    was everything that tried to ASK about it or ACT on it from outside the bus:

      * `GET /api/admin/observation` would read a fresh agent and report
        `highWaterMark: null` — the arithmetic the operator needs, missing.
      * `rearm_high_water_mark()` would re-anchor a THROWAWAY object and return
        happily. The real agent would keep its old mark, re-evaluate on the next
        closed trade and halt again — so the resume route would have looked like
        it worked while the deadlock it exists to break stayed exactly in place.

    That second one is why this is fixed here rather than worked around: a
    control that reports success while doing nothing is the `simulation_mode`
    failure, and this one would have been reached on the operator's first
    attempt to recover a halted account.

    `reset_ceo_agent()` exists for tests, so one test's high-water mark cannot
    become the next one's starting peak.
    """
    global _ceo_agent
    if _ceo_agent is None:
        _ceo_agent = CEOAgent()
    return _ceo_agent


def reset_ceo_agent() -> None:
    """Drop the singleton. For tests only."""
    global _ceo_agent
    _ceo_agent = None
