// ---------------------------------------------------------------------
// Position Sizing Agent. Every function here is pure — no fetching, no
// side effects — so the Risk Manager (lib/riskManager.ts) can call these
// and compare the result against whatever size was actually requested.
// ---------------------------------------------------------------------

export type SizingResult = { qty: number; method: string; riskUsd: number };

// Fixed-fractional: risk a fixed % of account equity on this trade,
// sized off the ACTUAL stop distance (structural or ATR-based, from
// riskManager's computeStopLossTakeProfit) — the standard, most
// defensible sizing method when a real stop level exists.
export function fixedFractionalSize(equityUsd: number, riskPct: number, entryPrice: number, stopLoss: number): SizingResult | null {
  const stopDistance = Math.abs(entryPrice - stopLoss);
  if (stopDistance <= 0 || equityUsd <= 0) return null;
  const riskUsd = equityUsd * riskPct;
  const qty = riskUsd / stopDistance;
  return { qty, method: `fixed-fractional (${(riskPct * 100).toFixed(1)}% equity risk, stop distance $${stopDistance.toFixed(2)})`, riskUsd };
}

// Volatility-based: same idea, but sizes directly off ATR rather than a
// specific stop price — useful as a cross-check even when a structural
// stop exists, and as the primary method when one doesn't.
// THE DEFAULT MULTIPLE WAS 1.5 WHILE EVERY REAL STOP IS 2.5 (F45).
//
// `volatilityBasedSize` divides the risk budget by `atr * multiplier`, so a
// multiplier SMALLER than the stop actually placed produces a quantity LARGER
// than the budget allows: 2.5/1.5 = 1.67x the configured risk on every trade
// sized this way. Both callers — `riskManager`'s fallback and the TS backtest
// — relied on the default, so the backtest's reported risk per trade was not
// the risk it modelled.
//
// Imported from `riskManager` rather than restated, because that is where the
// TS/Python alignment is maintained and a third copy of this number is how it
// drifts again (the same reasoning as `lib/watchlistSeed.test.ts`).

// THE STOP THIS SYSTEM ACTUALLY PLACES, in ATR multiples.
//
// Must equal `backend/core/risk_manager.ATR_STOP_MULTIPLIER` and
// `lib/riskManager`'s ATR_FLOOR_MULTIPLIER / ATR_FALLBACK_MULTIPLIER. Three
// numbers that must agree; CLAUDE.md records the TS and Python copies drifting
// apart once already (1.2x/1.8x against 2.5x), which sized a tighter stop on
// the browser path than on the autonomous one.
//
// Declared HERE rather than imported from `riskManager`, because that module
// imports this one — the other direction would be a cycle.
export const ATR_STOP_MULTIPLIER = 2.5;

export function volatilityBasedSize(equityUsd: number, riskPct: number, entryPrice: number, atrValue: number, atrMultiplier: number = ATR_STOP_MULTIPLIER): SizingResult | null {
  if (atrValue <= 0 || equityUsd <= 0) return null;
  const stopDistance = atrValue * atrMultiplier;
  const riskUsd = equityUsd * riskPct;
  const qty = riskUsd / stopDistance;
  return { qty, method: `volatility-based (${(riskPct * 100).toFixed(1)}% equity risk, ${atrMultiplier}x ATR = $${stopDistance.toFixed(2)})`, riskUsd };
}

// Max exposure cap: regardless of what the risk-based sizing above says,
// never let a single position exceed this % of account equity — a
// backstop against a tiny stop distance implying an enormous position.
export function capToMaxExposure(qty: number, entryPrice: number, equityUsd: number, maxExposurePct: number = 0.25): { qty: number; capped: boolean } {
  const positionValue = qty * entryPrice;
  const maxValue = equityUsd * maxExposurePct;
  if (positionValue <= maxValue || equityUsd <= 0) return { qty, capped: false };
  return { qty: maxValue / entryPrice, capped: true };
}

// Kelly Criterion: f* = W - (1-W)/R, where W = win rate and R = average
// win / average loss. Deliberately returns null (not a guessed number)
// when there isn't enough real trade history to estimate W/R honestly —
// Kelly on a handful of trades is noise, not a real edge estimate, and a
// negative or absurd result (R<=0, or f*<=0) means "no edge detected,"
// not "size zero and try again."
const MIN_TRADES_FOR_KELLY = 20;
// Kelly's raw output is famously aggressive (full Kelly routinely implies
// >50% of equity on a single trade) — half-Kelly is the standard
// practitioner haircut, trading some growth rate for a much smaller
// drawdown, so that's what's returned as the usable fraction.
const KELLY_FRACTION_MULTIPLIER = 0.5;

export function kellyFraction(closedTradeCount: number, winRate: number, avgWinUsd: number, avgLossUsd: number): { fraction: number; raw: number } | null {
  if (closedTradeCount < MIN_TRADES_FOR_KELLY) return null; // not enough real history to estimate an edge
  if (avgLossUsd <= 0 || avgWinUsd <= 0) return null; // degenerate inputs — can't compute a payoff ratio
  const payoffRatio = avgWinUsd / avgLossUsd;
  const raw = winRate - (1 - winRate) / payoffRatio;
  if (raw <= 0) return null; // no positive edge detected — Kelly says don't size this up at all
  return { fraction: Math.min(raw * KELLY_FRACTION_MULTIPLIER, 1), raw };
}
