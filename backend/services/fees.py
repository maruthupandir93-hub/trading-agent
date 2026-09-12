"""Trading fees — the cost that was missing from every P&L figure in this system.

WHY THIS MODULE EXISTS
======================
Nothing anywhere subtracted a fee. `portfolio_store` computed
`realized_pnl = (price - avgCost) * qty`, the closing trade row stored that
number, `strategy_performance` aggregated it, and the backtest reported
expectancy in R without a cost model. Every profit figure this system has ever
produced was GROSS.

That is not a rounding error at this system's trade size. Measured against its
own risk model and its own backtest:

    SOL 15m ATR%                          ~0.43%
    stop = 2.5 x ATR, so 1R               ~1.075% of notional
    Binance USDⓈ-M taker, both sides       0.10% of notional
    -> fees per round trip                ~0.093R

    Backtested edge      gross      net of fees
      Scalping          +0.160R      +0.067R     58% of the edge is fee
      Breakout          +0.142R      +0.049R     65%
      Momentum          +0.125R      +0.032R     74%
      Swing / Trend     -0.007R      -0.100R     break-even -> clearly losing

So the three strategies this system ranks as profitable are majority fee, and
two of them flip sign. Worse, `strategy_performance` feeds the 0.2 track-record
weight in `graphs/nodes/opportunity._score_one` from these same gross numbers —
the learning loop was learning from inflated results and preferring whatever
traded most.

MEASURED AND MODELLED FEES ARE DISTINGUISHED, ALWAYS
====================================================
`FeeResult.measured` says which one a number is, and it is persisted alongside
the cost (`trades.fee_measured`).

This is invariant 6, applied to a cost rather than a price. A REAL fill has a
real fee and the venue reports it. A PAPER fill has no venue and therefore no
fee to report — modelling one is the only honest option, but presenting that
model as though the exchange had confirmed it would make paper P&L look exactly
as authoritative as real P&L when it is strictly an estimate. A reader must be
able to tell which they are holding.

The model is also deliberately PESSIMISTIC where it is uncertain: the taker rate
is assumed on every fill. Every order this system places is a market order
(`Venue.market_order`, and the resting stop/TP are stop-MARKET), so taker is
correct rather than conservative — but if a maker path is ever added, assuming
taker keeps the estimate on the side that cannot flatter the account.

WHY A FEE IS NEVER FETCHED ON THE TRADING PATH
==============================================
Binance and Bybit both return the commission on the FILL for a market order in
the create-order response often enough to use, but not always. The obvious fix —
call `fetch_my_trades` when it is missing — puts a second authenticated HTTP
round trip between the fill and the position reaching the stop-loss watch list.

That is the wrong trade. An unwatched position is a safety problem; an
unmeasured fee is an accounting problem, and the modelled value is accurate to
the venue's published rate. So this module never performs I/O: it reads what the
order response already carried, and models the rest.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Binance USDⓈ-M and Bybit linear perpetual TAKER fee, as a fraction of notional.
# 0.05% is the standard tier on both venues; a VIP tier or a BNB/BIT discount
# makes the real cost LOWER, so this errs toward over-stating the cost, which is
# the only direction an estimate may err in a P&L figure.
#
# Configurable because it is a property of the operator's account, not of the
# code. Read at CALL time, not frozen at import: the `simulation_mode` bug is the
# standing example of what a module-level snapshot of a settable value costs.
_DEFAULT_TAKER_RATE = 0.0005


def taker_rate() -> float:
    """The taker fee as a fraction of notional. Read at call time."""
    raw = os.getenv("FEE_TAKER_RATE")
    if raw is None or raw == "":
        return _DEFAULT_TAKER_RATE
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "FEE_TAKER_RATE=%r is not a number; using the default %.4f.",
            raw, _DEFAULT_TAKER_RATE,
        )
        return _DEFAULT_TAKER_RATE
    # A negative rate is a rebate and a rate above 1% is a typo. Both would
    # silently corrupt every P&L figure downstream, so neither is accepted.
    if not (0.0 <= rate <= 0.01):
        logger.warning(
            "FEE_TAKER_RATE=%s is outside the plausible 0-1%% band; using the "
            "default %.4f instead.", rate, _DEFAULT_TAKER_RATE,
        )
        return _DEFAULT_TAKER_RATE
    return rate


@dataclass(frozen=True)
class FeeResult:
    """One fill's fee, and whether the venue actually said so.

    `cost` is always a POSITIVE number of quote currency (USDT). It is a cost;
    the caller subtracts it. Returning a signed value would invite the sign to be
    applied twice, which on a fee is the difference between paying it and being
    paid it.
    """

    cost: float
    measured: bool
    detail: str


def modelled_fee(notional: float) -> FeeResult:
    """The fee a taker fill of this size would cost, from the configured rate.

    Used for every paper fill (no venue exists to report one) and as the fallback
    when a real order response carried no usable fee.
    """
    rate = taker_rate()
    cost = abs(float(notional or 0.0)) * rate
    return FeeResult(
        cost=cost,
        measured=False,
        detail=f"modelled at the {rate:.4%} taker rate on {abs(notional or 0.0):.8g} notional",
    )


def fee_from_order(raw: Optional[Dict[str, Any]], quote: str = "USDT") -> Optional[FeeResult]:
    """The fee the venue reported on this order, or None if it reported none.

    ccxt normalises this to either `fee` (`{cost, currency, rate}`) or `fees`
    (a list of the same). Both are checked, and `fees` is SUMMED rather than
    first-taken: a fill that crossed several price levels can be billed as
    several entries, and reading only the first would under-count the cost.

    A NON-QUOTE CURRENCY IS REFUSED RATHER THAN CONVERTED. Binance can bill
    commission in BNB when the discount is enabled, and Bybit in BIT. Converting
    it would need a price for that asset at fill time, which this module has no
    business fetching — and a wrong conversion is worse than an honest model,
    because it would be labelled `measured`. The caller falls back to
    `modelled_fee`, which is the right answer expressed as an estimate.

    Returns None (not a zero FeeResult) when nothing was reported, so the caller
    can tell "the venue said zero" from "the venue said nothing".
    """
    if not raw:
        return None

    entries = []
    single = raw.get("fee")
    if isinstance(single, dict):
        entries.append(single)
    listed = raw.get("fees")
    if isinstance(listed, list):
        entries.extend(e for e in listed if isinstance(e, dict))
    if not entries:
        return None

    total = 0.0
    seen_any = False
    for entry in entries:
        cost = entry.get("cost")
        if cost is None:
            continue
        currency = (entry.get("currency") or quote or "").upper()
        if currency and quote and currency != quote.upper():
            logger.info(
                "Venue billed this fill in %s, not %s. Not treating it as a measured "
                "fee — converting would need a price this module must not fetch.",
                currency, quote,
            )
            return None
        try:
            total += abs(float(cost))
        except (TypeError, ValueError):
            continue
        seen_any = True

    if not seen_any:
        return None
    return FeeResult(
        cost=total,
        measured=True,
        detail=f"reported by the venue ({total:.8g} {quote})",
    )


def resolve_fee(
    raw: Optional[Dict[str, Any]],
    *,
    qty: float,
    price: float,
    quote: str = "USDT",
) -> FeeResult:
    """The best fee figure available for one fill: the venue's, else the model's.

    This is the single entry point every fill path should use, so that "did we
    ask the venue first?" is answered in one place rather than at each call site.
    """
    reported = fee_from_order(raw, quote=quote)
    if reported is not None:
        return reported
    return modelled_fee(abs(float(qty or 0.0)) * abs(float(price or 0.0)))


def round_trip_fee(entry_fee: float, exit_fee: float) -> float:
    """Total cost of a completed round trip. Trivial, and named for the call sites.

    Exists so `realized_net = gross - round_trip_fee(...)` reads as what it is at
    every close, instead of an unexplained two-term subtraction that a later edit
    might mistake for a single-sided cost.
    """
    return abs(float(entry_fee or 0.0)) + abs(float(exit_fee or 0.0))
