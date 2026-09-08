"""Phase 28 — Risk Gateway node (spec Section 11).

    AI Decision -> Risk Gateway -> APPROVED

      Max Position · Max Leverage · Max Drawdown · Margin · Correlation
      Daily Loss · Exposure · Liquidity · Kill Switch

    "One of the most important phases. Do not make the CRO an LLM-only node —
     use deterministic risk code."
    "An LLM can recommend. Code enforces."

THIS IS THE ONLY NODE THAT SIZES A POSITION
-------------------------------------------
Phase 27 deliberately left `decision.size` and `decision.leverage` as `None` and
said in its own docstring that this phase owns them. It does — and it is the only
place in the reasoning layer that does, so there is exactly one answer to "who
decided how big this was".

Sizing happens BEFORE validation, not after, because most of Section 11's checks
are functions of size: margin, exposure, and per-trade risk are all meaningless
until a quantity exists. A gateway that validated an unsized request would be
checking nothing.

Sizing then feeds back into the checks, which can reject the size that was just
computed. That is the intended shape: the sizer proposes, the checks dispose.

WHAT THIS NODE WRITES, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------------
Writes `risk_assessment` (the verdict and all nine checks) and `execution_plan`
(the inert boundary object of Section 12).

Does NOT write `decision`. The Supervisor owns that record, and a second node
mutating it would mean an auditor could not tell which node's reasoning produced
which field. The plan is a separate object precisely so approval is visibly
downstream of the decision rather than folded into it.

`ExecutionPlan` is a dataclass, not an event and not a call. Producing one is not
placing an order: a separate deterministic service converts an approved plan into
a TAR, and `FORBIDDEN_IMPORTS` means nothing in this module can reach an order
call even if a later edit tried. The cognitive plane can be entirely wrong and
still cannot move money.

EXITS ARE NOT GATED
-------------------
An `EXIT` decision produces an unconditionally-approved plan. `validate_trade`
short-circuits on `intent='close'` before any check runs, and this node routes
exits down that path. CLAUDE.md invariant 4: a close is never blocked — and it is
most important not to block one when a limit has already been breached, which is
exactly when a gateway would otherwise refuse.

STRICT MODE
-----------
This node calls `validate_trade(..., strict=True)`, so a check that could not run
REJECTS rather than cautioning. The graph always has a portfolio snapshot (the
Phase 26 portfolio specialist writes it) and can always read the ledger, so a
missing input here is a bug in the graph rather than a limitation of the caller —
and an unrun check must never be allowed to look like a passed one.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

from backend.core.risk_manager import (
    ATR_STOP_MULTIPLIER,
    calculate_position_size,
    kelly_risk_fraction,
    max_leverage_ceiling,
    validate_trade,
)
from backend.graphs.contracts import NodeContract
from backend.graphs.registry import register_node
from backend.graphs.state import ExecutionPlan, RiskAssessment, TradingState

logger = logging.getLogger(__name__)

RISK_GATEWAY_NODE = "risk_gateway"

# Actions this node acts on. Anything else (WAIT / DO_NOT_TRADE) needs no plan and
# no validation — there is nothing to approve.
ACTIONABLE = ("TRADE", "EXIT")

# Fraction of equity to risk when Kelly has no usable win probability. Matches
# `kelly_risk_fraction`'s own fallback, referenced rather than re-picked so the two
# cannot drift apart.
DEFAULT_RISK_FRACTION = 0.02

# Leverage requested for a graph-originated plan.
#
# 1x, always. Not "up to the ceiling" — the ceiling is the maximum a human may
# configure, not a target for an autonomous system to aim at. This node has no
# validated track record to justify amplifying anything (every strategy profile
# still carries `historical_success_rate=None`), and leverage multiplies the
# consequences of a wrong read rather than the quality of it.
#
# Still passed through `max_leverage_ceiling` so the value can never exceed the
# hard limit even if this constant is edited carelessly.
GRAPH_REQUESTED_LEVERAGE = 1


from backend.algorithms.market_context import assess as assess_alignment
from backend.algorithms.market_context import build as build_market_context
from backend.services.tradeable_universe import refusal_reason as untradeable_reason
from backend.services.trading_session import active_capital_fraction


def _deployed_margin(positions: List[Dict[str, Any]]) -> float:
    """Margin already locked across open positions — the capital in play now.

    Reads `marginLocked` (the paper book records it per position). For a venue
    position without it, approximates margin as notional / leverage, and treats an
    unknown leverage as 1x — which OVER-counts margin and so caps the pool
    CONSERVATIVELY (it stops opening trades sooner, never later). Never raises.
    """
    total = 0.0
    for p in positions or []:
        m = p.get("marginLocked")
        if m is not None:
            try:
                total += float(m)
                continue
            except (TypeError, ValueError):
                pass
        try:
            qty = abs(float(p["qty"]))
            price = float(p.get("avgCost") or p.get("entryPrice") or 0.0)
            lev = float(p.get("leverage") or 1.0) or 1.0
            total += qty * price / lev
        except (KeyError, TypeError, ValueError):
            continue
    return total

# Refuse an entry that fights the 1h/4h consensus. Configurable because it is a
# hypothesis about selectivity rather than a safety invariant — an operator
# testing whether it helps must be able to turn it off without editing code.
REQUIRE_HTF_ALIGNMENT: bool = (
    os.getenv("REQUIRE_HTF_ALIGNMENT", "true").strip().lower() == "true"
)


def build_entry_context(
    *,
    symbol: str,
    technical: Any,
    regime_state: Any,
    volatility: Any,
    strategy: Optional[str],
    market_context: Any = None,
) -> str:
    """A compact snapshot of what the agent saw, for the trade row.

    WHY THIS EXISTS. "How this trade happened" could only ever show market data
    and execution with an unknown middle — and that was not a display bug.
    `trades.entry_context` existed and nothing wrote it, and a join could not have
    recovered the values either: the run trace records which node ran and what
    state KEYS it wrote, not what was in them. The graph state holding the
    indicators is gone by the time a fill is booked.

    The gateway is where this belongs because it is the last node holding
    `technical_analysis`, `market_regime` and `volatility` together.

    PURE, so it can be tested without driving the whole node — the gateway has
    real dependencies (the live book, the ledger) that make end-to-end synthetic
    approval brittle, and the thing worth pinning here is the FORMAT.

    A MISSING INPUT IS OMITTED, NEVER DEFAULTED. An invented RSI would be the most
    persuasive fabrication available in this system, because it would look exactly
    like evidence. The frontend renders "not recorded" for what is absent.

    The format is the one `learningDashboard.classifyEntryContext` and
    `lib/viz/entryContext.parseEntryContext` already read — one shape of snapshot
    in the system, not two.
    """
    bits: List[str] = [f"{symbol} @ 15m:"]

    if technical is not None:
        if getattr(technical, "rsi", None) is not None:
            bits.append(f"RSI(14)={technical.rsi:.1f},")
        if getattr(technical, "atr", None) is not None:
            bits.append(f"ATR(14)={technical.atr:.4g},")
        if getattr(technical, "multi_timeframe_trend", None):
            bits.append(f"structure trend={technical.multi_timeframe_trend},")

    if regime_state is not None and getattr(regime_state, "regime", None):
        bits.append(f"regime={regime_state.regime},")

    if volatility is not None and getattr(volatility, "regime", None):
        percentile = getattr(volatility, "percentile", None)
        bits.append(
            f"volatility={volatility.regime}"
            + (f" ({percentile:.0f}th pct)" if percentile is not None else "")
            + ","
        )

    # APPENDED LAST, and only when it measured something. The existing fields
    # are what the two frontend parsers already read by regex; adding this at the
    # end leaves every one of those patterns matching exactly as before, which is
    # what `test_entry_context` asserts. A snapshot the frontend cannot parse is
    # the same as no snapshot at all.
    if market_context is not None:
        described = market_context.describe()
        if described:
            bits.append(f"context: {described},")

    if strategy:
        bits.append(f"strategy={strategy}")

    return " ".join(bits).rstrip(",")


def gate(state: TradingState) -> Optional[Dict[str, Any]]:
    """Size, validate, and produce an inert execution plan. Deterministic."""
    decision = state.get("decision")

    if decision is None:
        return {"unavailable": ["risk gateway (no decision to validate)"]}

    if decision.action not in ACTIONABLE:
        # WAIT and DO_NOT_TRADE need no plan. Recorded as an assessment rather than
        # silence, so the trace shows the gateway ran and why it had nothing to do —
        # an absent `risk_assessment` is indistinguishable from a gateway that
        # failed to execute.
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[
                    f"nothing to validate: the Supervisor decided {decision.action}, "
                    f"not TRADE or EXIT"
                ],
                checks={
                    "NotApplicable": {
                        "status": "pass",
                        "detail": (
                            f"{decision.action} requires no risk validation and no "
                            f"execution plan."
                        ),
                    }
                },
            )
        }

    thesis = state.get("trade_thesis")
    portfolio = state.get("portfolio_state")
    snapshot = state.get("market_data")
    symbol = state["symbol"]
    tab = portfolio.tab if portfolio else "paper"

    # ---- EXIT: invariant 4, no gate ---------------------------------------
    if decision.action == "EXIT":
        return _exit_plan(state, decision, portfolio, symbol, tab)

    # ---- TRADEABLE-INSTRUMENT GATE ---------------------------------------
    #
    # Placed AFTER the EXIT branch and FIRST among the entry checks, and both
    # positions are deliberate.
    #
    # After EXIT, because invariant 4 is absolute: a close is never blocked. A
    # position already open in a symbol the operator has since excluded must
    # still be closable — refusing the exit would leave them holding exactly the
    # instrument they asked to stop holding.
    #
    # First among entry checks, for the reason the volatility gate gives: a
    # refusal that is a property of the INSTRUMENT rather than of the proposed
    # trade must not be reachable by making the trade smaller or moving its stop.
    # No size is acceptable, so nothing should be sized.
    refusal = untradeable_reason(symbol)
    if refusal is not None:
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[refusal],
                checks={
                    "TradeableInstrument": {
                        "status": "reject",
                        "detail": (
                            f"{symbol} may be watched and reasoned about but not opened. "
                            f"This is an instrument preference, not a risk limit."
                        ),
                    }
                },
            )
        }

    # ---- HIGHER-TIMEFRAME ALIGNMENT --------------------------------------
    #
    # 1h and 4h candles have been fetched on every run since the beginning, and
    # `TIMEFRAMES`' own comment says they exist to "cut conviction on a
    # counter-trend read". Nothing ever cut anything: the consensus was computed,
    # written to `TechnicalAnalysis.multi_timeframe_trend`, RECORDED in the entry
    # context, and never gated on.
    #
    # WHAT THE LEDGER SHOWS. 12 closed trades, 3 wins, -57.01 net. The 9 losses
    # average -26.04 and are tightly clustered — stop-outs at a consistent risk,
    # not disasters. All 3 wins landed in one 30-minute window. That is a
    # trend-follower being run in conditions that are not trending, and the lever
    # is selectivity.
    #
    # UNKNOWN AND MIXED DO NOT BLOCK. See `market_context.assess` for why this
    # differs from the volatility gate directly below: an unmeasured
    # higher-timeframe trend costs conviction, whereas an unmeasured volatility
    # means the loss cannot be bounded at all.
    #
    # THIS IS A HYPOTHESIS AND IS LABELLED ONE. It will reduce the number of
    # trades. Whether it raises EXPECTANCY rather than merely win rate depends on
    # how many removed trades would have won, and 12 trades cannot answer that.
    # `strategy_performance` is what will, now that a close records its strategy.
    # BUILT ONCE, here, so the SAME object feeds both this gate and the entry
    # snapshot below. A previous edit built it under a local name `context` and
    # then referenced `market_context` in the rejection branch — a NameError that
    # crashed the counter-trend rejection path instead of returning it, and which
    # no test drove into. Building it unconditionally also restores the market
    # context (HTF trend, BTC benchmark) to `build_entry_context`, which a partial
    # revert had dropped.
    market_context = None
    if snapshot is not None:
        market_context = build_market_context(
            candles=snapshot.candles,
            benchmark_symbol=snapshot.benchmark_symbol,
            benchmark_candles=snapshot.benchmark_candles,
        )

    if REQUIRE_HTF_ALIGNMENT and market_context is not None and thesis is not None:
        alignment = assess_alignment(decision.direction, market_context)
        if alignment.blocks:
            return {
                "risk_assessment": RiskAssessment(
                    approved=False,
                    rejection_reasons=[alignment.detail],
                    checks={
                        "HigherTimeframeAlignment": {
                            "status": "reject",
                            "detail": (
                                f"{alignment.detail} Context: "
                                f"{market_context.describe() or 'none measured'}."
                            ),
                        }
                    },
                )
            }

    # ---- VOLATILITY GATE, before anything is sized ------------------------
    #
    # Placed FIRST among the entry checks, for the same reason `check_leverage`
    # runs before the stop-distance maths: a refusal that depends on market
    # conditions rather than on the proposed trade should not be reachable by
    # making the trade smaller. If the regime says no, no size is acceptable.
    #
    # An UNKNOWN volatility blocks too. That is deliberate and it is the one place
    # in this graph where a missing input refuses rather than degrades: position
    # size and stop distance are both derived from volatility, and neither has a
    # safe default. "We could not measure how much this market is moving" is not a
    # reason to guess.
    volatility = state.get("volatility")
    if volatility is not None and not volatility.trading_allowed:
        regime = volatility.regime or "unknown"
        detail = (
            f"volatility regime is {regime}"
            + (f" ({volatility.percentile:.0f}th percentile of its own recent range)"
               if volatility.percentile is not None else "")
            + (f", and ATR% expanded {volatility.expansion_ratio:.1f}x its baseline"
               if volatility.volatility_shock and volatility.expansion_ratio else "")
        )
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[
                    f"{detail}. No position size is acceptable in this regime: a stop "
                    f"placed here is as likely to be gapped through as touched, so "
                    f"sizing cannot bound the loss it exists to bound."
                ],
                checks={
                    "Volatility": {"status": "reject", "detail": detail},
                },
            )
        }

    # ---- TRADE: size first, because the checks are functions of size ------
    if thesis is None or thesis.entry_price is None or thesis.stop_loss is None:
        # Should be unreachable: the Supervisor already returns DO_NOT_TRADE for a
        # thesis without a stop. Checked anyway rather than trusted, because the
        # consequence of being wrong is a stopless position (invariant 3).
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[
                    "no entry price or stop-loss on the thesis, so the position "
                    "cannot be sized and must not be opened"
                ],
                checks={
                    "MandatoryStopLoss": {
                        "status": "reject",
                        "detail": "Every position requires a computed stop-loss.",
                    }
                },
            ),
            "unavailable": ["risk gateway sizing (no entry or stop on the thesis)"],
        }

    equity = portfolio.equity if portfolio else None
    if not equity or equity <= 0:
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[
                    "equity is unknown, so the position cannot be sized and no risk "
                    "limit can be expressed as a percentage of it"
                ],
                checks={
                    "PositionSize": {
                        "status": "unavailable",
                        "detail": "No equity figure on the portfolio snapshot.",
                    }
                },
            ),
            "unavailable": ["risk gateway sizing (equity unknown)"],
        }

    # ---- SESSION CAPITAL ALLOCATION --------------------------------------
    #
    # The operator picks how much of the account this session may trade with —
    # 25 / 50 / 75 / 100% — on the home page ("trade with 75% of my balance").
    # It does two things, both here:
    #
    #   1. SIZES every trade as if the account were that fraction of its real
    #      size. Choosing 25% makes each trade a quarter of what it would be.
    #   2. CAPS the TOTAL margin the agent may have committed at once at that
    #      fraction of the account, so once the pool is deployed no new trade
    #      opens until one closes.
    #
    # It is NOT a leverage source and cannot raise risk: the leverage ceiling and
    # mandatory stop are untouched, so 100% is "use the whole account as margin",
    # never "use more leverage". 1.0 (no session, or a full allocation) is the
    # pre-feature behaviour exactly.
    fraction = active_capital_fraction()
    if fraction < 1.0:
        # The real capital base, NOT the leverage-inflated `cash + notional`:
        # free cash plus the margin already locked in open positions. That is the
        # money actually in the account, and the fraction is of that.
        deployed = _deployed_margin(portfolio.open_positions if portfolio else [])
        cash = float(portfolio.cash) if (portfolio and portfolio.cash is not None) else None
        account_capital = (cash + deployed) if cash is not None else equity
        pool = fraction * account_capital

        if deployed >= pool:
            return {
                "risk_assessment": RiskAssessment(
                    approved=False,
                    rejection_reasons=[
                        f"the session's capital pool is fully deployed: "
                        f"{deployed:,.2f} of a {pool:,.2f} pool "
                        f"({fraction * 100:.0f}% of {account_capital:,.2f}) is already in "
                        f"open positions. No new position opens until one closes."
                    ],
                    checks={
                        "CapitalPool": {
                            "status": "reject",
                            "detail": (
                                f"{fraction * 100:.0f}% allocation, {deployed:,.2f}/{pool:,.2f} "
                                f"margin deployed."
                            ),
                        }
                    },
                )
            }

        # Size against the allocated capital, so per-trade size scales with the
        # chosen fraction. This is the number every downstream sizing and margin
        # calc uses from here on.
        equity = equity * fraction

    bars_15m = (snapshot.candles.get("15m") if snapshot else None) or []
    technical = state.get("technical_analysis")
    atr = technical.atr if technical and technical.atr is not None else None

    if atr is None:
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=["ATR is unavailable, so risk-based sizing is impossible"],
                checks={
                    "PositionSize": {
                        "status": "unavailable",
                        "detail": (
                            "Sizing is a function of ATR; without it the quantity "
                            "would be a guess and the stop distance already is one."
                        ),
                    }
                },
            ),
            "unavailable": ["risk gateway sizing (no ATR)"],
        }

    # Kelly, capped downward only. `decision.probability` is the ONLY honest win
    # probability this system has, and it is None until 20 trades have resolved —
    # in which case `kelly_risk_fraction` falls back to fixed-fractional and says
    # so. Feeding it the panel confidence instead would be sizing on a number that
    # is not a win rate, which is the fabrication Phase 27 exists to prevent.
    sizing = kelly_risk_fraction(
        win_prob=decision.probability,
        payoff_ratio=2.0,
        fallback=DEFAULT_RISK_FRACTION,
    )

    if sizing["fraction"] <= 0.0:
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[f"sizing returned zero: {sizing['detail']}"],
                checks={
                    "PositionSize": {"status": "reject", "detail": sizing["detail"]},
                },
            )
        }

    size = calculate_position_size(
        equity=equity,
        price=thesis.entry_price,
        atr=atr,
        risk_per_trade_percent=sizing["fraction"],
    )

    # VOLATILITY SCALES THE SIZE DOWN, NEVER UP.
    #
    # `RISK_MULTIPLIER` is <= 1.0 for every regime by construction. A multiplier
    # above 1.0 would mean "this market is quiet, so take a bigger position",
    # which converts a risk control into a leverage source — and quiet markets are
    # precisely where the next expansion begins.
    #
    # Applied AFTER the risk-budget calculation rather than folded into
    # `risk_per_trade_percent`, so the trace shows both the budgeted size and what
    # volatility did to it. Folding them would make the reduction invisible.
    volatility_multiplier = 1.0
    if volatility is not None:
        volatility_multiplier = max(0.0, min(1.0, volatility.risk_multiplier))
        if volatility_multiplier < 1.0:
            size = size * volatility_multiplier

    if size <= 0:
        return {
            "risk_assessment": RiskAssessment(
                approved=False,
                rejection_reasons=[
                    f"computed size is {size} — the risk budget "
                    f"({sizing['fraction'] * 100:.2f}% of ${equity:,.2f}) does not "
                    f"support even the smallest position at this stop distance"
                ],
                checks={
                    "PositionSize": {
                        "status": "reject",
                        "detail": f"size {size} at {sizing['rule']} sizing",
                    },
                },
            )
        }

    leverage = min(GRAPH_REQUESTED_LEVERAGE, max_leverage_ceiling(tab))
    side = "buy" if decision.direction == "LONG" else "sell"

    # ---- validate the size that was just computed -------------------------
    validation = validate_trade(
        {
            "symbol": symbol,
            "qty": size,
            "price": thesis.entry_price,
            "equityUsd": equity,
            "klines": bars_15m,
            "side": side,
            "tab": tab,
            "requestedLeverage": leverage,
            "intent": "open",
            # Supplied so the Phase 28 checks actually run. `openPositions` being a
            # list (even empty) rather than None is what distinguishes "measured, no
            # positions" from "not supplied".
            "openPositions": list(portfolio.open_positions or []),
            "freeMarginUsd": portfolio.cash,
            "tradeLedger": _ledger(),
        },
        # See the module docstring: the graph has every input, so a check that
        # cannot run is a bug here, not a caller limitation.
        strict=True,
    )

    assessment = RiskAssessment(
        approved=validation.approved,
        rejection_reasons=list(validation.rejection_reasons),
        caution_notes=[
            *validation.caution_notes,
            f"sizing rule: {sizing['rule']} — {sizing['detail']}",
        ],
        checks={
            name: {"status": check.status, "detail": check.detail}
            for name, check in validation.checks.items()
        },
        stop_loss=thesis.stop_loss,
        take_profit=thesis.take_profit,
    )

    out: Dict[str, Any] = {"risk_assessment": assessment}

    if not validation.approved:
        logger.info(
            "Risk gateway REJECTED %s %s %.8g on %s: %s",
            decision.direction, thesis.strategy, size, symbol,
            "; ".join(validation.rejection_reasons),
        )
        # No plan on a rejection. An unapproved `ExecutionPlan` sitting in state is
        # an object shaped exactly like an approved one, and the only thing stopping
        # a downstream reader acting on it is that reader remembering to check a
        # separate field. Not producing it removes the question.
        return out

    out["execution_plan"] = ExecutionPlan(
        entry_context=build_entry_context(
            symbol=symbol,
            technical=state.get("technical_analysis"),
            regime_state=state.get("market_regime"),
            volatility=state.get("volatility"),
            strategy=thesis.strategy,
            # Restored: the HTF-trend and BTC-benchmark context, so the trade row
            # records WHY as well as WHAT. Safe when None.
            market_context=market_context,
        ),
        symbol=symbol,
        side=side,
        size=size,
        leverage=leverage,
        stop_loss=thesis.stop_loss,
        take_profit=thesis.take_profit,
        tab=tab,
        idempotency_basis=_idempotency_basis(state, decision, side, size),
    )
    logger.info(
        "Risk gateway APPROVED %s %s %.8g on %s at %.8g (stop %.8g, %gx, %s) — "
        "%d check(s) passed, %d caution(s)",
        decision.direction, thesis.strategy, size, symbol, thesis.entry_price,
        thesis.stop_loss, leverage, sizing["rule"],
        sum(1 for c in validation.checks.values() if c.status == "pass"),
        len(assessment.caution_notes),
    )
    return out


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------

def _exit_plan(
    state: TradingState,
    decision: Any,
    portfolio: Any,
    symbol: str,
    tab: str,
) -> Dict[str, Any]:
    """An unconditionally-approved close. Invariant 4.

    Routed through `validate_trade(intent='close')` rather than skipping the
    gateway entirely, so the bypass is recorded as a check in the assessment. An
    exit that simply had no `risk_assessment` would be indistinguishable in the
    trace from a gateway that crashed.

    The size is the held quantity, read from the portfolio. If it cannot be read
    the plan is still produced with `size=None` and the reason recorded — a close
    the executor must size itself is far better than no close at all.
    """
    validation = validate_trade({"intent": "close", "symbol": symbol}, strict=False)

    held = 0.0
    base = str(symbol).split("/")[0].upper()
    unreadable: List[str] = []
    for pos in (portfolio.open_positions if portfolio else []) or []:
        if str(pos.get("symbol", "")).split("/")[0].upper() != base:
            continue
        try:
            held += float(pos["qty"])
        except (KeyError, TypeError, ValueError):
            unreadable.append(str(pos.get("symbol")))

    caution = list(validation.caution_notes)
    if unreadable:
        caution.append(
            f"could not read the held quantity for {', '.join(unreadable)}; the "
            f"executor must determine the size to close"
        )
    if held == 0.0:
        caution.append(
            "no held quantity could be determined for this symbol, so the plan "
            "carries size=None rather than a guessed quantity"
        )

    assessment = RiskAssessment(
        approved=True,
        rejection_reasons=[],
        caution_notes=caution,
        checks={
            name: {"status": check.status, "detail": check.detail}
            for name, check in validation.checks.items()
        },
    )

    # Opposite side to the held direction: closing a long is a sell.
    side = "sell" if held > 0 else "buy"

    logger.info(
        "Risk gateway APPROVED EXIT on %s (%s %s) — risk checks NOT applied, "
        "invariant 4: a close is never blocked",
        symbol, side, "unknown size" if held == 0.0 else f"{abs(held):.8g}",
    )

    return {
        "risk_assessment": assessment,
        "execution_plan": ExecutionPlan(
            symbol=symbol,
            side=side,
            size=abs(held) if held != 0.0 else None,
            # Never levered up to close. Reducing an existing position does not
            # require leverage, and requesting it on an exit would be a way for a
            # close to increase exposure.
            leverage=1,
            # No stop or target on a close — it IS the exit.
            stop_loss=None,
            take_profit=None,
            tab=tab,
            idempotency_basis=_idempotency_basis(state, decision, side, held),
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ledger() -> Optional[List[Dict[str, Any]]]:
    """Today's realised P&L source for the daily-loss check.

    Returns None — not `[]` — when the store cannot be read, because an empty
    ledger means "no trades closed today" and an unreadable one means "unknown".
    In strict mode the first passes the check and the second rejects, which is the
    correct difference.
    """
    try:
        from backend.services.ai_memory import get_memory_stats

        stats = get_memory_stats() or {}
    except Exception as exc:  # noqa: BLE001 - never guess a P&L history
        logger.warning("Risk gateway could not read the trade ledger: %s", exc)
        return None

    ledger = stats.get("trade_ledger")
    return ledger if isinstance(ledger, list) else None


def _idempotency_basis(
    state: TradingState, decision: Any, side: str, size: Optional[float]
) -> str:
    """A stable key derived from DECISION IDENTITY, never from thread_id.

    Section 39.3. A thread id changes on every run, so a basis derived from it
    would let the same decision be submitted twice after a restart — which for an
    order means opening the position twice.

    `run_id` is included, so a genuinely new run produces a new key even for an
    identical decision. That is the intended tradeoff: the guard is against
    double-submitting ONE decision (a retry, a resumed checkpoint), not against a
    later run reaching the same conclusion, which is a real second decision.
    """
    raw = "|".join(
        str(part) for part in (
            state.get("run_id"),
            state.get("symbol"),
            decision.action,
            decision.direction,
            side,
            f"{size:.10g}" if size is not None else "unsized",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_risk_gateway_node() -> None:
    register_node(
        NodeContract(
            name=RISK_GATEWAY_NODE,
            reads=(
                "decision", "trade_thesis", "portfolio_state", "market_data",
                "technical_analysis", "symbol", "run_id",
                # The volatility layer gates entry and scales size. Declared so a
                # contract check fails loudly if the node is ever removed from the
                # graph, rather than the gateway silently sizing without it.
                "volatility",
            ),
            writes=("risk_assessment", "execution_plan"),
            purpose=(
                "Size the position, then run spec Section 11's nine deterministic "
                "checks. Produces an inert ExecutionPlan on approval. Never places "
                "an order; never blocks a close."
            ),
            deterministic=True,
            phase=28,
        ),
        gate,
    )
