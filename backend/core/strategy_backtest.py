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

import os

from backend.services.fees import taker_rate


# SLIPPAGE, AS A FRACTION OF PRICE PER LEG.
#
# The backtest fills at exactly the stop or the target. A live market order
# does not: it crosses the spread, and a stop fills through its trigger in the
# move that triggered it. Reporting that as zero is the same flattery the
# testnet mirror exists to remove -- a simulated fill reports spread, slippage
# and partial fills all as zero, and every one of them appears on day one of
# real money.
#
# 2 bps per leg is deliberately conservative for the liquid USDT perpetuals
# this agent trades, and it is env-readable because it is a property of the
# VENUE and the instrument rather than of this code. It is NOT measured, and
# saying so matters: `execution_quality` records real slippage per fill, and
# once enough live fills exist this should be replaced by that measurement
# rather than left as an assumption that looks like one.
# WHAT THIS FILE STILL DOES NOT MODEL: FUNDING.
#
# Perpetual funding accrues per 8h interval on the notional, so costing it
# needs a funding rate per symbol per period that this module does not have and
# would have to invent. An invented funding number would be the most
# persuasive fabrication available here, because it would look exactly like a
# measured cost — invariant 6 forbids precisely that. The per-strategy holding
# bound bounds the exposure instead, and this absence is stated in the stored
# output rather than left for a reader to discover.
SLIPPAGE_RATE_PER_LEG = 0.0002
SLIPPAGE_VAR = "BACKTEST_SLIPPAGE_RATE"


def slippage_rate() -> float:
    """Read at call time, never frozen at import (the `simulation_mode` rule)."""
    raw = os.getenv(SLIPPAGE_VAR)
    if not raw:
        return SLIPPAGE_RATE_PER_LEG
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return SLIPPAGE_RATE_PER_LEG
    return value if value >= 0 else SLIPPAGE_RATE_PER_LEG


def bar_minutes(candles: List[Dict[str, Any]]) -> Optional[float]:
    """Minutes per candle, from the data rather than from a passed-in label.

    A holding bound is written in MINUTES and this series is indexed in BARS,
    so one cannot be applied to the other without it. Derived from the median
    gap between consecutive `openTime` values -- the median rather than the
    first gap, because a single missing candle would otherwise double the
    inferred interval and halve every bound.

    None when it cannot be derived, which is NOT the same as "no bound": the
    caller treats it as "the time exit could not be modelled" and says so,
    rather than silently applying a guessed interval.
    """
    stamps = []
    for c in candles:
        t = c.get("openTime")
        if isinstance(t, (int, float)) and t > 0:
            stamps.append(float(t))
    if len(stamps) < 3:
        return None
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]) if b > a)
    if not gaps:
        return None
    median = gaps[len(gaps) // 2]
    # Binance klines are milliseconds. A gap under 1000 would mean seconds,
    # which this project never requests, so treating it as ms is safe and a
    # wrong unit would be caught by the implausible-interval guard below.
    minutes = median / 60000.0
    return minutes if 0.1 <= minutes <= 10080 else None
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
    # HOW THE TRADES ENDED, so a reader can tell WHICH exit policy produced
    # this expectancy. Two runs with the same number can be different
    # strategies: one that mostly reaches its target and one that mostly runs
    # out of time are not the same thing, and the difference is invisible in
    # the expectancy alone.
    exits: Dict[str, int] = field(default_factory=dict)
    # Set when a requested model could NOT be applied -- e.g. a holding bound
    # was asked for but the candle interval was underivable. Stated rather than
    # silently ignored, so a run is never read as having modelled something it
    # did not.
    note: Optional[str] = None
    # Share of closed trades the CLOCK ended, derived so a reader does not have
    # to. A majority here means the strategy is not running its own logic on
    # this timeframe -- see `timeframe_warning`.
    time_exit_share: float = 0.0
    # Set when the clock is ending MOST trades, which means the strategy's
    # stated horizon is shorter than this candle interval can express. Measured
    # on 5 instruments x 1000 real 15m candles: Scalping 76%, Arbitrage 73%
    # even after the MIN_HOLDING_BARS floor. That is a TIMEFRAME MISMATCH, not
    # something a longer bound fixes -- raising it further would just stop the
    # strategy being a scalp. Reported rather than silently corrected, because
    # which strategies to run on which timeframe is the operator's decision.
    timeframe_warning: Optional[str] = None
    trade_log: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        """Without the full trade log, for a compact table."""
        d = asdict(self)
        d.pop("trade_log", None)
        from backend.core.strategy_versions import signal_version
        d["signal_version"] = signal_version(self.strategy)
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
    # THE LIVE EXIT POLICY. Defaults match the live defaults exactly, so a run
    # with no arguments measures what production does today and the stored
    # backtests stay comparable.
    max_holding_minutes: Optional[int] = None,
    trail_r: float = 0.0,
) -> StrategyResult:
    """Walk the candles, take every signal, simulate under the live risk model.

    `window` caps how much trailing history the signal function sees, matching the
    live system's fetch of 120 candles — a strategy that only ever sees 120 bars
    live must be measured on 120 bars, or the backtest tests a different function.
    """
    reward_per_r = target_mult / stop_mult  # +2.0 with the live 5.0/2.5

    # THE HOLDING BOUND, CONVERTED TO BARS ONCE.
    #
    # `max_holding_minutes` is live by default now, so a backtest that ignores
    # it measures a strategy production does not run. None when no bound was
    # given OR when the interval could not be derived -- and those are
    # different facts, so the second is reported rather than silently treated
    # as "no bound".
    minutes_per_bar = bar_minutes(candles)

    # THE SHARED FLOOR, AND IT IS NOT MEASURED FROM THESE CANDLES (#29).
    #
    # This passed `minutes_per_bar`, so the bound moved with whatever data the
    # study used: 8 x 60 = 480 minutes on 1h candles against the live monitor's
    # 8 x 15 = 120. That is the Issue E defect reintroduced inside the very
    # function written to prevent it, and invisible because both numbers look
    # reasonable in isolation.
    #
    # The bound is a property of the LIVE system's decision cadence. The candle
    # interval is used BELOW, to turn those minutes into a number of bars on
    # this particular series -- which is the only thing it is an input to.
    if max_holding_minutes is None:
        try:
            from backend.algorithms.strategy_profiles import effective_holding_minutes

            max_holding_minutes = effective_holding_minutes(name)
        except Exception:  # pragma: no cover - a missing profile is not fatal
            max_holding_minutes = None

    max_bars: Optional[int] = None
    time_exit_note: Optional[str] = None
    if max_holding_minutes:
        if minutes_per_bar:
            max_bars = max(1, int(round(max_holding_minutes / minutes_per_bar)))
        else:
            time_exit_note = (
                "a holding bound was requested but the candle interval could not "
                "be derived, so the time exit was NOT modelled"
            )

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

        risk_per_unit_price = stop_mult * atr

        def _r_at(level: float) -> float:
            """R realised at `level`, measured against the ORIGINAL risk.

            An untrailed stop is exactly -1.0, so this is byte-for-byte the old
            behaviour for every trade the trail does not touch.

            IT EXISTS BECAUSE HARDCODING -1.0 MADE THE TRAIL WORTHLESS. The
            trailed exit price was recorded correctly and the R was not, so a
            trade that trailed to +0.6R still booked a full -1R and every
            backtest reported the trail as having no effect. Found by the test
            that asserts a reversal exits better WITH the trail than without —
            the exit prices differed (97.5 vs 101.5) and `total_r` did not.

            The denominator stays the ORIGINAL risk on purpose: R is defined by
            what was staked at entry, so re-basing it on the trailed distance
            would make a tightened stop look like a bigger loss than it was.
            """
            if risk_per_unit_price <= 0:
                return 0.0
            moved = (level - entry) if long else (entry - level)
            return moved / risk_per_unit_price

        for j in range(i + 1, n):
            high = float(candles[j]["high"])
            low = float(candles[j]["low"])
            hit_stop = low <= stop if long else high >= stop
            hit_target = high >= target if long else low <= target

            # STOP AND TARGET FIRST, AND THE STOP WINS AN AMBIGUOUS BAR.
            # Checked before the trail and the clock because a bar that reached
            # a price level resolved there; letting a later rule claim it would
            # overstate the strategy.
            if hit_stop and hit_target:
                # Ambiguous bar — assume the STOP filled first. Conservative.
                outcome, exit_price, r = "stop", stop, _r_at(stop)
                exit_index = j
                break
            if hit_stop:
                outcome, exit_price, r = "stop", stop, _r_at(stop)
                exit_index = j
                break
            if hit_target:
                outcome, exit_price, r = "target", target, reward_per_r
                exit_index = j
                break

            # THE TRAIL, in R, the units the live rule is written in.
            #
            # Ratchets toward price and NEVER away from it, which is the one
            # rule `tighten_stop` enforces live: a stop that can loosen is not
            # a stop. Measured against the bar's favourable extreme, because
            # that is what the live monitor sees on the ticks inside the bar.
            if trail_r > 0 and risk_per_unit_price > 0:
                best = high if long else low
                gain_r = ((best - entry) if long else (entry - best)) / risk_per_unit_price
                if gain_r >= trail_r:
                    trailed = (
                        best - trail_r * risk_per_unit_price if long
                        else best + trail_r * risk_per_unit_price
                    )
                    stop = max(stop, trailed) if long else min(stop, trailed)

            # THE CLOCK, LAST. A position that reached neither level and is past
            # its strategy's horizon exits at the bar's close, which is the
            # honest fill for a decision made on that bar.
            if max_bars is not None and (j - i) >= max_bars:
                exit_price = float(candles[j]["close"])
                r = ((exit_price - entry) if long else (entry - exit_price)) / risk_per_unit_price
                outcome = "time"
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
        # Two legs each, and both scale with the entry price against the stop
        # distance for the reason the fee comment gives: a wider stop costs
        # proportionally less, so neither can be a flat constant.
        cost_rate = 2.0 * (taker_rate() + slippage_rate())
        fee_r = (cost_rate * entry / risk_per_unit) if risk_per_unit > 0 else 0.0

        trades.append(Trade(
            direction="long" if long else "short",
            entry_index=i, exit_index=exit_index,
            entry_price=entry, exit_price=exit_price,
            outcome=outcome, r_multiple=round(r, 4), fee_r=round(fee_r, 6),
        ))

        # Resume scanning AFTER the exit — one position at a time.
        i = exit_index + 1

    return _summarise(name, trades, note=time_exit_note)


def _summarise(name: str, trades: List[Trade], note: Optional[str] = None) -> StrategyResult:
    # A TIME EXIT IS A REALISED CLOSE, AND EXCLUDING IT WOULD BE THE SAME BUG
    # THE LEDGER ALREADY HAD. `/api/stats` once reported `totalClosedTrades: 0`
    # on an account that had been trading, because the reconstruction demanded
    # a shape real closes did not have. A trade closed by the clock has a real
    # entry, a real exit and a real R; dropping it would measure only the
    # trades that reached a price level and flatter every strategy whose
    # positions go nowhere -- which is precisely the behaviour the holding
    # bound exists to penalise.
    closed = [t for t in trades if t.outcome in ("target", "stop", "time")]
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
    n_time = sum(1 for t in closed if t.outcome == "time")
    time_share = (n_time / n_closed) if n_closed else 0.0

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
        exits={
            outcome: sum(1 for t in trades if t.outcome == outcome)
            for outcome in ("target", "stop", "time", "open_at_end")
            if any(t.outcome == outcome for t in trades)
        },
        note=note,
        time_exit_share=round(time_share, 4),
        timeframe_warning=(
            f"{time_share:.0%} of closed trades were ended by the holding bound, not by "
            f"the stop or the target. {name}'s stated horizon is shorter than this candle "
            f"interval can express, so these results describe the clock more than the "
            f"strategy. Measure it on a finer timeframe or do not run it on this one."
            if time_share > 0.5 else None
        ),
        trade_log=[asdict(t) for t in trades],
    )
