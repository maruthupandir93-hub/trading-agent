"""Ranking well is not the same as having been shown to make money.

THE REVIEW'S ISSUES 12 AND 14, WHICH ARE ONE QUESTION
------------------------------------------------------
    Issue 14: *"A strategy score combines signal strength, multi-timeframe
    alignment, volatility suitability, and historical performance... That is
    reasonable for ranking candidates, but it means a strategy can rank well
    without having demonstrated positive net expectancy. Keep 'best available
    candidate' separate from 'approved for real money'."*

    Issue 12: *"approximately 76% of Scalping trades and 73% of Arbitrage
    trades are closed by the clock... a strategy whose holding period is
    dominated by the time exit is not necessarily being tested as the strategy
    it claims to be. Do not rank a scalping strategy as the best one until it
    has been evaluated using data and fill assumptions appropriate for
    scalping."*

`opportunity._rate` answers "which of these is the most promising right now",
and it answers it well. Nothing anywhere answered "has this strategy been
shown to make money net of costs", and the two questions were being settled by
the same number — a 0.2 track-record weight inside a score whose other 0.8 is
current-conditions fit.

WHAT APPROVAL REQUIRES, AND WHY EACH ONE
-----------------------------------------
    LIVE SAMPLE        `MIN_SAMPLE` (20) real closed trades. The same floor
                       `strategy_performance` already uses to let a record
                       steer selection, for the same reason: one lucky run
                       would otherwise entrench a bad strategy, which is how
                       naive adaptive systems destroy themselves.
    NET EXPECTANCY     positive AFTER costs. The gross figure is the one that
                       flatters: on this system's stop distance the round trip
                       is ~0.09R against measured edges of 0.13-0.16R, and
                       funding is charged on top of that.
    TIMEFRAME COHERENCE  the clock must not be ending most trades. A strategy
                       the time exit dominates is not running its own logic.

A BACKTEST CAN NEVER APPROVE, and that is the load-bearing rule.
`strategy_priors` reads a committed backtest and says so in four places: it is
IN-SAMPLE, gross of the costs measured since, and taken over a mostly trending
window. CLAUDE.md's rule is "a backtest INFORMS; it does not deploy", and this
module is where that sentence stops being advice. The review asks for
out-of-sample validation before promotion; a live closed trade IS out of
sample, by construction, and nothing else here is.

THE GATE IS OFF BY DEFAULT, AND THAT IS A DELIBERATE, STATED CHOICE.
No strategy in this system has twenty closed real trades, so switching it on
stops real trading completely until one does. That is a legitimate policy —
arguably the right one — and it is the OPERATOR's to choose, not a default
this module imposes while they are not looking. What runs unconditionally is
the RECORD: every real entry's evidence states whether the strategy that chose
it had demonstrated an edge, so the question is answerable afterwards even on
a deployment that never turns the gate on.

Deterministic arithmetic over closed trades and a committed file. No model is
consulted, and it cannot invent, edit or disable a strategy — it answers one
question about one. That is the same invariant-5 argument
`strategy_performance` and `strategy_priors` both make, and a test asserts the
module contains no model call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

APPROVED = "APPROVED"
CANDIDATE = "CANDIDATE"
REFUSED = "REFUSED"

GATE_VAR = "REQUIRE_STRATEGY_APPROVAL"

# Share of closed trades the CLOCK may end before the strategy stops being
# measured as itself. Half, because at that point the holding bound is the
# primary exit and the strategy's own entry and exit logic is deciding a
# minority of outcomes -- which is Issue 12's "not necessarily being tested as
# the strategy it claims to be", stated as a number.
MAX_TIME_EXIT_SHARE = 0.5


def gate_enabled() -> bool:
    """Is approval REQUIRED for a real entry? Read at call time.

    Call time, never frozen at import: the `simulation_mode` rule. An operator
    who turns this on expects the next trade attempt to see it, not the next
    restart.
    """
    raw = (os.getenv(GATE_VAR) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class StrategyApproval:
    """Whether a strategy has been SHOWN to make money, with the arithmetic."""

    strategy: str
    status: str                       # APPROVED | CANDIDATE | REFUSED
    live_trades: int
    required_trades: int
    # PER TRADE, IN THE ACCOUNT'S CURRENCY, and already cost-adjusted: it is
    # derived from `trades.pnl`, which is the REALISED P&L of a real fill and
    # therefore has the venue's own fees and slippage inside it. That is a
    # stronger cost model than any backtest here can offer, and the reason a
    # live record is the only thing that can approve.
    net_expectancy: Optional[float]
    time_exit_share: Optional[float]
    reason: str

    @property
    def approved_for_real_money(self) -> bool:
        return self.status == APPROVED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "status": self.status,
            "liveTrades": self.live_trades,
            "requiredTrades": self.required_trades,
            "netExpectancy": self.net_expectancy,
            "timeExitShare": self.time_exit_share,
            "reason": self.reason,
            "approvedForRealMoney": self.approved_for_real_money,
            "note": (
                "CANDIDATE is the normal state and is NOT a fault: it means this "
                "strategy may be the best available choice right now and has not yet "
                "demonstrated a net edge over enough real closed trades to be called "
                "proven. Ranking well and having been shown to make money are "
                "different claims."
            ),
        }


def _live_record_cached(strategy: str) -> Optional[Dict[str, Any]]:
    """The cached record, with NO database access. None when nothing is cached.

    The Risk Gateway is a graph node and `validate_market_data` is the single
    fetch point (Section 39.4): a node that queried the ledger mid-run would
    not be replay-safe, because a resumed checkpoint would reason over a
    different set of closed trades than the original decision.

    None is "not cached yet", never "no record" — which lands on CANDIDATE, not
    on a refusal.
    """
    try:
        from backend.services.strategy_performance import cached_performance

        rows = cached_performance()
        if rows is None:
            return None
        row = rows.get(strategy) or {}
        return row if isinstance(row, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Strategy approval could not read the cached record: %s", exc)
        return None


async def _live_record(strategy: str) -> Dict[str, Any]:
    """What this account's own closed trades say. Never raises.

    Through `performance()`, which is CACHED, so the entry path does not issue
    a query per decision. A database that cannot be read returns None there and
    {} here -- which lands on CANDIDATE below, not on a refusal: a bookkeeping
    outage is not evidence that a strategy loses money.
    """
    try:
        from backend.services.strategy_performance import performance

        rows = await performance() or {}
        row = rows.get(strategy) or {}
        return row if isinstance(row, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Strategy approval could not read the live record: %s", exc)
        return {}


def _min_sample() -> int:
    try:
        from backend.services.strategy_performance import MIN_SAMPLE

        return int(MIN_SAMPLE)
    except Exception:  # noqa: BLE001
        return 20


def assess_cached(
    strategy: Optional[str], *, time_exit_share: Optional[float] = None,
) -> StrategyApproval:
    """`assess` without any I/O, for a graph node. See `_live_record_cached`."""
    row = _live_record_cached((strategy or "").strip())
    return _verdict(strategy, row or {}, time_exit_share, record_available=row is not None)


async def assess(
    strategy: Optional[str], *, time_exit_share: Optional[float] = None,
) -> StrategyApproval:
    """Has `strategy` been shown to make money net of costs on this account?

    `time_exit_share` is supplied by a caller that has a backtest result to
    hand (Issue 12). It is NOT fetched here: this module runs on the entry path
    and a function that reached for a backtest would be doing I/O in the middle
    of a trading decision, which is the replay-safety rule Section 39.4 keeps.
    """
    return _verdict(
        strategy, await _live_record((strategy or "").strip()), time_exit_share,
    )


def _verdict(
    strategy: Optional[str],
    row: Dict[str, Any],
    time_exit_share: Optional[float],
    *,
    # False when the caller could not read the record at all (nothing cached in
    # this process yet). Distinct from an EMPTY record, which means the ledger
    # was read and this strategy has closed nothing -- the True/False/None rule
    # again, and the reason the two produce different wording below.
    record_available: bool = True,
) -> StrategyApproval:
    """The arithmetic, with the record already in hand.

    Separated so the sync and async front doors cannot disagree about what
    approval MEANS — two copies of a rule is how `lib/riskManager.ts` and
    `core/risk_manager.py` drifted apart on the ATR multipliers.
    """
    required = _min_sample()
    name = (strategy or "").strip()

    if not name:
        # A manual position closes with `strategy` NULL by design, and the
        # event path writes none either. Absence is not a refusal -- inventing
        # an attribution would poison the measurement this reads.
        return StrategyApproval(
            strategy="", status=CANDIDATE, live_trades=0, required_trades=required,
            net_expectancy=None, time_exit_share=time_exit_share,
            reason=(
                "no strategy is attributed to this trade, so there is nothing to "
                "hold a track record against. This is normal for a manual position "
                "and for the event path, and is not evidence either way"
            ),
        )

    if time_exit_share is not None and time_exit_share > MAX_TIME_EXIT_SHARE:
        # ISSUE 12. Measured on 5 instruments x 1000 real 15m candles: Scalping
        # 76%, Arbitrage 73%, even after the MIN_HOLDING_BARS floor. A longer
        # bound does not fix it -- raising it further would just stop the
        # strategy being a scalp. The mismatch is between the strategy's stated
        # horizon and the timeframe the system analyses on.
        return StrategyApproval(
            strategy=name, status=REFUSED, live_trades=0, required_trades=required,
            net_expectancy=None, time_exit_share=time_exit_share,
            reason=(
                f"the clock ends {time_exit_share * 100:.0f}% of this strategy's "
                f"trades, above the {MAX_TIME_EXIT_SHARE * 100:.0f}% at which its own "
                f"entry and exit logic stops deciding most outcomes. It is not being "
                f"measured as the strategy it claims to be, so its expectancy is not "
                f"evidence about it. The remedy is a timeframe that fits its stated "
                f"horizon, not a longer holding bound"
            ),
        )

    if not record_available:
        # AFTER the timeframe check above, because that one is a property of
        # the STRATEGY and holds whether or not a record can be read.
        return StrategyApproval(
            strategy=name, status=CANDIDATE, live_trades=0,
            required_trades=required, net_expectancy=None,
            time_exit_share=time_exit_share,
            reason=(
                "no strategy record is cached in this process yet, so whether this "
                "strategy has a demonstrated edge could not be established here. "
                "Could not measure is not measured and unprofitable"
            ),
        )

    trades = int(row.get("sampleSize") or 0)
    expectancy = row.get("expectancy")
    try:
        expectancy = float(expectancy) if expectancy is not None else None
    except (TypeError, ValueError):
        expectancy = None

    if trades < required:
        return StrategyApproval(
            strategy=name, status=CANDIDATE, live_trades=trades,
            required_trades=required, net_expectancy=expectancy,
            time_exit_share=time_exit_share,
            reason=(
                f"{trades} real closed trade(s) against the {required} this account "
                f"requires before a record steers anything. A committed backtest may "
                f"inform the CHOICE between strategies and can never approve one: it "
                f"is in-sample, gross of the costs measured since, and taken over a "
                f"mostly trending window"
            ),
        )

    if expectancy is None:
        return StrategyApproval(
            strategy=name, status=CANDIDATE, live_trades=trades,
            required_trades=required, net_expectancy=None,
            time_exit_share=time_exit_share,
            reason=(
                f"{trades} closed trade(s) are recorded but their net expectancy "
                f"could not be derived, so there is no cost-adjusted edge to approve. "
                f"Unmeasured is not unprofitable"
            ),
        )

    if expectancy <= 0:
        return StrategyApproval(
            strategy=name, status=REFUSED, live_trades=trades,
            required_trades=required, net_expectancy=expectancy,
            time_exit_share=time_exit_share,
            reason=(
                f"measured over {trades} real closed trades, this strategy returns "
                f"{expectancy:+.2f} per trade after costs. A strategy that loses "
                f"money on this account's own fills is not approved for real money "
                f"however well it ranks on current conditions"
            ),
        )

    return StrategyApproval(
        strategy=name, status=APPROVED, live_trades=trades,
        required_trades=required, net_expectancy=expectancy,
        time_exit_share=time_exit_share,
        reason=(
            f"{expectancy:+.2f} per trade after costs over {trades} real closed "
            f"trades, at or above the {required} this account requires. This is an "
            f"out-of-sample result by construction -- these are fills, not a "
            f"simulation"
        ),
    )
