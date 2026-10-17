import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

def calculate_trade_probabilities(
    debate_result: Dict[str, Any],
    base_win_rate: Optional[float] = None,
    reward_risk_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """Expected value for a proposed trade, from the debate's conviction.

    TWO DEFAULTS MADE THIS GATE UNABLE TO REJECT (F84).

    The prior was 0.45 and the payoff 1.5:1, so at ZERO confidence:

        EV = 0.45 x 1.5 - 0.55 = +0.125

    — positive before any evidence at all, with `risk: "acceptable"`. The
    break-even win rate at a payoff of R is `1 / (1 + R)`, which is 0.40 at
    1.5:1, so a 0.45 prior ASSUMED an edge of five points and then reported the
    result as a computed one (invariant 6).

    Both defaults now come from the system rather than from an assumption:

      * `reward_risk_ratio` from the REAL risk model
        (`ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER` = 2.0), because that is
        the payoff every position is actually given.
      * `base_win_rate` from the MEASURED record when there is enough of one,
        and otherwise from BREAK-EVEN — so with no evidence EV is exactly 0.0
        and the gate can refuse. An unmeasured system is not a losing one, but
        it is not a winning one either.

    `priorSource` travels with the answer so a reader can tell a measured
    prior from a break-even assumption.
    """
    from backend.core.risk_manager import ATR_STOP_MULTIPLIER, ATR_TARGET_MULTIPLIER

    if reward_risk_ratio is None:
        reward_risk_ratio = ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER

    breakeven = 1.0 / (1.0 + reward_risk_ratio)
    prior_source = "break-even (no measured record)"
    if base_win_rate is None:
        measured = _measured_win_rate()
        if measured is not None:
            base_win_rate = measured
            prior_source = "measured from this account's closed trades"
        else:
            base_win_rate = breakeven
    else:
        prior_source = "supplied by the caller"

    direction = debate_result.get("direction", "HOLD")
    confidence = debate_result.get("confidence", 0)  # assumed 0.0 to 1.0 from debate.py
    
    if direction == "HOLD" or direction == "UNKNOWN":
        return {
            "p_profit": 0.0,
            "p_loss": 0.0,
            "expected_value": 0.0,
            "confidence": confidence,
            "risk": "N/A"
        }
        
    # Bayesian Update: P(Profit | Signal)
    # We treat the ensemble's 'confidence' as the likelihood of the signal being correct
    # given a profitable setup: P(Signal | Profit)
    # P(Profit | Signal) = [P(Signal | Profit) * P(Profit)] / P(Signal)
    
    # We will simplify by interpreting 'confidence' as a direct modifier to the base rate.
    # If confidence is 0, we revert to base_win_rate. If confidence is 1, we cap at 0.85 (never 100%).
    
    max_p = 0.85
    min_p = 0.15
    
    # Linear blend between base rate and the confidence signal
    p_profit = base_win_rate + (confidence * (max_p - base_win_rate))
    
    # Ensure bounds
    p_profit = max(min_p, min(max_p, p_profit))
    p_loss = 1.0 - p_profit
    
    # Expected Value = (P(Profit) * Reward) - (P(Loss) * Risk)
    # Assuming Risk = 1 unit, Reward = reward_risk_ratio units
    expected_value = (p_profit * reward_risk_ratio) - (p_loss * 1.0)
    
    return {
        "p_profit": round(p_profit, 3),
        "p_loss": round(p_loss, 3),
        "expected_value": round(expected_value, 3),
        "confidence": confidence,
        "priorWinRate": round(base_win_rate, 4),
        "priorSource": prior_source,
        "rewardRiskRatio": round(reward_risk_ratio, 3),
        "breakevenWinRate": round(breakeven, 4),
        "risk": "acceptable" if expected_value > 0 else "rejected"
    }


# The smallest sample that may steer a prior. Matches
# `strategy_performance.MIN_SAMPLE` and `probability.MIN_TRADES_FOR_ACCURACY`:
# below it one lucky run would entrench an edge the account has not shown.
MIN_TRADES_FOR_PRIOR = 20


def _measured_win_rate() -> Optional[float]:
    """This account's own win rate, or None when there is not enough of one."""
    try:
        from backend.services.ai_memory import _load_memory

        stats = (_load_memory() or {}).get("global_stats") or {}
        total = int(stats.get("total_trades") or 0)
        if total < MIN_TRADES_FOR_PRIOR:
            return None
        wins = int(stats.get("wins") or 0)
        return max(0.05, min(0.95, wins / total))
    except Exception:  # noqa: BLE001 - a missing record is "no prior", not a crash
        return None
