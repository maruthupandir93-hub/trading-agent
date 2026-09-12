"""A faithful, deterministic backtest of each strategy's OWN signal function.

WHY THIS EXISTS
===============
`core/backtest_engine.py` drives the whole Agent OS over historical ticks, but its
own comment says "For this PoC, we just track trade count" — it produces no
per-strategy P&L, so it cannot answer the one question that matters: which of the
eleven voting strategies actually makes money, and which loses it. Every profile
in `strategy_profiles.py` carries `historical_success_rate=None` because none has
ever been measured on this system's own logic.

This module measures it. For each strategy it walks historical candles bar by bar,
feeds the trailing window to that strategy's REAL signal function (the same
`STRATEGY_FUNCTIONS[name]` the live ensemble votes with), and simulates the trade
under the SAME risk model the live system uses — `ATR_STOP_MULTIPLIER` /
`ATR_TARGET_MULTIPLIER` imported from `core/risk_manager`, so the stop and target
are exactly what the autonomous agent would place (and, since 2026-09, what the
TypeScript path places too — they were aligned).

DETERMINISTIC AND PURE
======================
No network, no model, no randomness. Given the same candles it always produces the
same numbers — which is what makes a stored result reproducible and a regression
test possible. The network fetch lives in `scripts/run_backtests.py`; this is the
part that can be unit-tested offline with synthetic candles.

RESULTS ARE IN R, NOT DOLLARS
=============================
1R = the risk (the stop distance). A win at the 2:1 target is +2R, a stop-out is
-1R. Expressing outcomes in R makes them comparable across trades with different
ATRs and across symbols at different prices — a dollar figure would just measure
which symbol was more expensive. Expectancy in R is the number that says whether a
strategy has an edge: positive means it makes money per unit risked, negative
means it loses it, regardless of sizing.

HONEST LIMITATIONS, STATED NOT HIDDEN
=====================================
  * NO FEES OR SLIPPAGE. A gross-of-costs backtest. Scalping in particular looks
    better here than it can trade — its own profile says "Net P&L AFTER fees is
    the only meaningful number." Treat a thin positive expectancy as break-even.
  * STOP-BEFORE-TARGET on an ambiguous candle. When one bar's range covers both
    the stop and the target, this assumes the STOP filled first. Without intrabar
    data that is the conservative assumption, and it makes results a floor rather
    than a flatter.
  * SINGLE POSITION, LONG OR SHORT, NO PYRAMIDING. One trade at a time per
    strategy, which is what the live monitor enforces anyway.
  * IN-SAMPLE. This is measured on whatever window is passed. It is evidence, not
    proof, and `MIN_SAMPLE` in `strategy_performance` is the floor before any of
    it should steer selection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.services.fees import taker_rate
from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER

# The signal function contract: candles in, "BUY" | "SELL" | "HOLD" out. Exactly
# the shape of every function in `strategy_ensemble.STRATEGY_FUNCTIONS`.
SignalFn = Callable[[List[Dict[str, Any]]], str]

ATR_PERIOD = 14


@dataclass
class Trade:
    """One simulated round trip, in R."""
    direction: str          # "long" | "short"
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    outcome: str            # "target" | "stop" | "open_at_end"
    # GROSS, and deliberately so: +2.0 for a target hit, -1.0 for a stop. This is
    # the risk model's own definition and keeping it exact is what makes a trade
    # log readable — a target that reported +1.96 because of a fee would make
    # every outcome a slightly different number with no obvious meaning.
    r_multiple: float
    # The round trip's cost for THIS trade, in R. Scales inversely with stop
    # width: one R is `stop_mult * atr` of price while the fee is a fraction of
    # the entry price on each of two legs, so a wider stop costs proportionally
    # less. That relationship is why this is per-trade and not a constant.
    fee_r: float = 0.0


@dataclass
class StrategyResult:
    """Per-strategy performance over one candle series."""
    strategy: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    # THE number: average R per trade, NET OF FEES. >0 means an edge that
    # survives the cost of taking it, which is the only kind worth having.
    expectancy_r: float
    # The same figure before costs. Reported beside the net one rather than
    # instead of it, because the GAP is the interesting quantity: on this
    # system's stop distance fees are ~0.09R against edges of 0.13-0.16R, so a
    # strategy can look strong gross and be break-even net.
    payoff: Optional[float]  # avg win / avg loss magnitude
    total_r: float
    open_at_end: int
    # The same figure before costs. Reported beside the net one rather than
    # instead of it, because the GAP is the interesting quantity: on this
    # system's stop distance fees are ~0.09R against edges of 0.13-0.16R, so a
    # strategy can look strong gross and be break-even net.
    #
    # Defaulted (and so placed after every required field) purely for dataclass
    # ordering — an existing caller constructing a StrategyResult positionally
    # keeps working.
    gross_expectancy_r: float = 0.0
    fee_r_per_trade: float = 0.0
    trade_log: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        """Without the full trade log, for a compact table."""
        d = asdict(self)
        d.pop("trade_log", None)
        return d


def _atr(candles: List[Dict[str, Any]], end: int, period: int = ATR_PERIOD) -> Optional[float]:
    """ATR over the `period` bars ending at index `end`. Mean of true ranges, the
    same shape as `algorithms/indicators.calculate_atr`'s rolling mean.

    None when there is not enough history — the caller must not enter a trade it
    cannot bound the risk of (the live system's invariant 3 in miniature)."""
    if end < period:
        return None
    trs: List[float] = []
    for i in range(end - period + 1, end + 1):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(trs) / len(trs)
    return atr if atr > 0 else None


def backtest_strategy(
    candles: List[Dict[str, Any]],
    signal_fn: SignalFn,
    *,
    name: str = "strategy",
    stop_mult: float = ATR_STOP_MULTIPLIER,
    target_mult: float = ATR_TARGET_MULTIPLIER,
    atr_period: int = ATR_PERIOD,
    window: int = 120,
) -> StrategyResult:
    """Walk the candles, take every signal, simulate under the live risk model.

    `window` caps how much trailing history the signal function sees, matching the
    live system's fetch of 120 candles — a strategy that only ever sees 120 bars
    live must be measured on 120 bars, or the backtest tests a different function.
    """
    reward_per_r = target_mult / stop_mult  # +2.0 with the live 5.0/2.5

    trades: List[Trade] = []
    i = atr_period
    n = len(candles)

    while i < n:
        hist = candles[max(0, i - window + 1): i + 1]
        vote = signal_fn(hist)
        if vote not in ("BUY", "SELL"):
            i += 1
            continue

        atr = _atr(candles, i, atr_period)
        if atr is None:
            i += 1
            continue

        entry = float(candles[i]["close"])
        long = vote == "BUY"
        if long:
            stop = entry - stop_mult * atr
            target = entry + target_mult * atr
        else:
            stop = entry + stop_mult * atr
            target = entry - target_mult * atr

        # Walk forward until the stop or the target is touched.
        exit_index = n - 1
        exit_price = float(candles[-1]["close"])
        outcome = "open_at_end"
        r = 0.0

        for j in range(i + 1, n):
            high = float(candles[j]["high"])
            low = float(candles[j]["low"])
            hit_stop = low <= stop if long else high >= stop
            hit_target = high >= target if long else low <= target
            if hit_stop and hit_target:
                # Ambiguous bar — assume the STOP filled first. Conservative.
                outcome, exit_price, r = "stop", stop, -1.0
                exit_index = j
                break
            if hit_stop:
                outcome, exit_price, r = "stop", stop, -1.0
                exit_index = j
                break
            if hit_target:
                outcome, exit_price, r = "target", target, reward_per_r
                exit_index = j
                break

        if outcome == "open_at_end":
            # Marked to the last close, in R, so an unfinished trade is counted
            # honestly rather than dropped (dropping open trades flatters a
            # strategy that entered right before the data ended).
            r = ((exit_price - entry) if long else (entry - exit_price)) / (stop_mult * atr)

        # FEES, IN R. This backtest was GROSS, and its own output said so — but
        # the ranking it produced was then used as evidence about which
        # strategies work, and at this system's stop distance the round trip is
        # ~0.09R against edges of 0.13-0.16R. Gross, Scalping/Breakout/Momentum
        # lead and Swing/Trend look break-even; net, the leaders keep about a
        # third of their edge and the break-even pair are losers.
        #
        # Expressed in R rather than in currency so it stays comparable across
        # symbols and ATRs like every other number here. One R is `stop_mult *
        # atr` of price, and the fee is a fraction of the ENTRY PRICE on each of
        # two legs, so the cost in R is (2 * rate * entry) / (stop_mult * atr).
        # A wider stop therefore costs proportionally less, which is the real
        # relationship and the reason this cannot be a flat constant.
        risk_per_unit = stop_mult * atr
        fee_r = (2.0 * taker_rate() * entry / risk_per_unit) if risk_per_unit > 0 else 0.0

        trades.append(Trade(
            direction="long" if long else "short",
            entry_index=i, exit_index=exit_index,
            entry_price=entry, exit_price=exit_price,
            outcome=outcome, r_multiple=round(r, 4), fee_r=round(fee_r, 6),
        ))

        # Resume scanning AFTER the exit — one position at a time.
        i = exit_index + 1

    return _summarise(name, trades)


def _summarise(name: str, trades: List[Trade]) -> StrategyResult:
    closed = [t for t in trades if t.outcome in ("target", "stop")]
    wins = [t for t in closed if t.r_multiple > 0]
    losses = [t for t in closed if t.r_multiple <= 0]
    open_at_end = sum(1 for t in trades if t.outcome == "open_at_end")

    n_closed = len(closed)
    win_rate = (len(wins) / n_closed) if n_closed else 0.0
    avg_win = (sum(t.r_multiple for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.r_multiple for t in losses) / len(losses)) if losses else 0.0
    # Expectancy over CLOSED trades — the open-at-end mark is informational and
    # not counted in the edge estimate, the same way the live learning loop only
    # scores realised closes.
    gross_expectancy = (sum(t.r_multiple for t in closed) / n_closed) if n_closed else 0.0
    # NET OF FEES, and this is the headline figure. The backtest used to report
    # the gross number as the edge; at this system's stop distance a round trip
    # is ~0.09R against measured edges of 0.13-0.16R, so gross ranked three
    # strategies as profitable that keep about a third of that net and two as
    # break-even that are actually losing.
    avg_fee_r = (sum(t.fee_r for t in closed) / n_closed) if n_closed else 0.0
    expectancy = gross_expectancy - avg_fee_r
    payoff = (avg_win / abs(avg_loss)) if avg_loss else None
    total_r = sum(t.r_multiple - t.fee_r for t in closed)

    return StrategyResult(
        strategy=name,
        trades=n_closed,
        wins=len(wins),
        losses=len(losses),
        win_rate=round(win_rate, 4),
        avg_win_r=round(avg_win, 4),
        avg_loss_r=round(avg_loss, 4),
        expectancy_r=round(expectancy, 4),
        gross_expectancy_r=round(gross_expectancy, 4),
        fee_r_per_trade=round(avg_fee_r, 6),
        payoff=round(payoff, 4) if payoff is not None else None,
        total_r=round(total_r, 4),
        open_at_end=open_at_end,
        trade_log=[asdict(t) for t in trades],
    )
