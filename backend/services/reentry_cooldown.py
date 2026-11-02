"""Do not re-enter the same side of a symbol the moment the system closed it at a loss.

THE CASE THAT MOTIVATED IT, from an external forensic review of six XRP/USDT
paper shorts (2026-10-30, docs/DIAGNOSIS.md):

    00:56:43  trade 1 (SHORT) closed: thesis-invalidated — the panel read LONG
    00:56:54  trade 2 (SHORT) opened                       — the panel read SHORT

The panel reversed itself in ELEVEN SECONDS. At that time scale its verdicts
are noise, not information: nothing about the market a 15-minute strategy
trades changes in eleven seconds. Trade 2 then lost too. The rule is not
"the second trade lost" — one trade proves nothing — it is that re-entering
the side the system has JUST declared wrong, before a single new decision bar
has formed, is acting on the panel's noise rather than on its signal, and pays
the round-trip fee for it.

WHAT IT BLOCKS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
Blocks: a NEW entry in the SAME direction on the SAME symbol within
`REENTRY_COOLDOWN_S` of a close that was a LOSS or a thesis-invalidated exit.

Does not block:
  * EXITS, ever (invariant 4). Nothing here is consulted for a close.
  * The OPPOSITE direction. A thesis exit says the panel now argues the other
    way; acting on that is consistent with the exit, not a contradiction of it.
  * Re-entry after a WIN (take-profit, a profitable clock exit). Nothing was
    declared wrong.
  * Other symbols. A rotating session moves straight on to the next coin.

THE REPLAY THAT BOUNDS WHAT THIS IS WORTH. The same review ranked the five
thesis-invalidated exits as a cause of the losses. Replayed on real Binance
candles with the live stop rule, the opposite held: holding those five to the
stop or target would have lost -2.90R against the -2.37R the exits actually
took. The exit was protecting the account. This module therefore does NOT
touch the exit threshold; it only stops the system walking straight back into
the position it had just correctly left.

STATE IS IN-MEMORY, and a restart clears it. That is the honest scope: the
cooldown exists for the seconds-to-minutes window after a close, and a restart
already takes the decision loop out of that window.

Read at call time (the `simulation_mode` rule): `REENTRY_COOLDOWN_S=0` turns it
off without a restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

COOLDOWN_VAR = "REENTRY_COOLDOWN_S"

# Two 15-minute decision bars. Long enough that at least one genuinely new
# candle has closed since the exit; short enough not to sit out a real move.
DEFAULT_COOLDOWN_S = 1800.0

# symbol (normalised) -> (closed_at epoch s, direction LONG/SHORT, reason, pnl)
_recent: Dict[str, Tuple[float, str, str, float]] = {}
_lock = threading.Lock()


def cooldown_seconds() -> float:
    raw = (os.getenv(COOLDOWN_VAR) or "").strip()
    if not raw:
        return DEFAULT_COOLDOWN_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.0fs.", COOLDOWN_VAR, raw,
                       DEFAULT_COOLDOWN_S)
        return DEFAULT_COOLDOWN_S
    return max(0.0, value)


def _norm(symbol: str) -> str:
    return str(symbol or "").split(":")[0].upper().strip()


def _direction(side_or_direction: str) -> Optional[str]:
    s = str(side_or_direction or "").strip().lower()
    if s in ("buy", "long"):
        return "LONG"
    if s in ("sell", "short"):
        return "SHORT"
    return None


def record_close(symbol: str, held_side: str, reason: str, realized_pnl: float,
                 *, at: Optional[float] = None) -> None:
    """Called by the position monitor once a FULL close has completed. Never raises."""
    try:
        direction = _direction(held_side)
        if direction is None:
            return
        with _lock:
            _recent[_norm(symbol)] = (
                float(at if at is not None else time.time()), direction,
                str(reason or ""), float(realized_pnl or 0.0),
            )
    except Exception:  # noqa: BLE001 - bookkeeping must never break a close
        logger.debug("reentry_cooldown.record_close failed", exc_info=True)


def refusal(symbol: str, direction: str, *, now: Optional[float] = None) -> Optional[str]:
    """Why this ENTRY may not open yet, or None. NEVER call this for an exit."""
    window = cooldown_seconds()
    if window <= 0:
        return None
    # A pause after a loss is one of the brakes RUN_UNTIL_TARGET switches off.
    from backend.services.loss_brakes import run_until_target

    if run_until_target():
        return None
    wanted = _direction(direction)
    if wanted is None:
        return None
    with _lock:
        last = _recent.get(_norm(symbol))
    if last is None:
        return None
    closed_at, closed_dir, reason, pnl = last
    if closed_dir != wanted:
        return None
    if not (pnl < 0 or reason == "thesis-invalidated"):
        return None
    age = (now if now is not None else time.time()) - closed_at
    if age >= window:
        return None
    return (
        f"re-entry cooldown: a {closed_dir} in {_norm(symbol)} was closed "
        f"{age:.0f}s ago ({reason}, P&L {pnl:+.4f}). Re-opening the same side before "
        f"{window:.0f}s have passed would act on the panel's second-to-second noise "
        f"rather than on a new decision bar. The opposite side and other symbols are "
        f"not affected; {COOLDOWN_VAR}=0 disables this."
    )


def reset() -> None:
    """For tests."""
    with _lock:
        _recent.clear()
