"""WHICH symbols the agent may open a position in — and why that is not the same
set it watches.

THE DISTINCTION THIS MODULE EXISTS FOR
======================================
Two different questions were being answered by one list:

    OBSERVED   what do we need PRICES and CONTEXT for?
    TRADEABLE  what may we open a POSITION in?

They are not the same set, and collapsing them is what made BTC/USDT tradeable.

BTC is the market's beta. Its regime drives every alt: `triggers.py` attributes
regime triggers to `BTC_SYMBOL` precisely because "the underlying condition is
market-wide, not about a caller-supplied symbol", and `REGIME_WATCH` polls BTC
and ETH for exactly that reason. All of that is INFORMATION the agent should
keep using.

But because BTC was also in the observed set, its ticks produced tick triggers
and its regime triggers were attributed to BTC itself — so the analysis graph ran
on BTC and could open a BTC position. Measured on the live ledger:

    BTC/USDT    2 closed trades, 0 wins, -43.59
    SOL/USDT   10 closed trades, 3 wins, -13.42

Two of twelve trades produced 76% of the total loss. That is not a large enough
sample to prove BTC is a bad instrument, and this module does not claim it is.
The operator asked not to trade it; this makes that a configured fact rather than
something to be arranged by deleting BTC from a watch list — which would ALSO
remove the market-regime signal every other symbol depends on.

WHAT THIS DOES NOT DO
=====================
It never blocks a CLOSE. CLAUDE.md invariant 4: closes are never blocked, not by
pause, not by risk checks, not by a veto. A position already open in a
now-untradeable symbol must still be monitorable, stoppable and closable —
refusing to let someone exit is actively harmful, and it is worse for a symbol
the operator has just decided they do not want.

It is also not a risk control. It expresses a preference about instruments, and
the gates that exist to bound loss are elsewhere and unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# The operator's default. BTC is excluded from TRADING and remains fully present
# as an observed symbol — `live_market_data.DEFAULT_SYMBOLS` still subscribes it,
# `REGIME_WATCH` still polls it, and the market-context pack still reads it.
DEFAULT_BLOCKLIST = ("BTC/USDT",)

_ENV_VAR = "UNTRADEABLE_SYMBOLS"


def _normalise(symbol: str) -> str:
    """`SOL/USDT:USDT` and `sol/usdt` are the same instrument to this module.

    The settle suffix is stripped for the reason `reconciliation._norm` strips it:
    ccxt's unified perpetual symbol and the agent's own form are two spellings of
    one market, and a blocklist that matched only one spelling would be trivially
    bypassed by whichever hop happened to resolve first.
    """
    return (symbol or "").split(":")[0].strip().upper()


def blocked_symbols() -> Set[str]:
    """The configured blocklist, read at call time.

    Read from the environment on every call rather than captured at import, so the
    Settings page can change it without a restart — the same reasoning as
    `config.set_live_trading`, and for a sharper version of the same failure: a
    value frozen at process start means the operator turns an instrument off, is
    told it worked, and the agent keeps trading it.

    An empty `UNTRADEABLE_SYMBOLS=` means "block nothing" and is honoured. That is
    distinct from the variable being ABSENT, which takes the default. A setting
    the operator deliberately cleared must not be silently repopulated.
    """
    raw = os.getenv(_ENV_VAR)
    if raw is None:
        return {_normalise(s) for s in DEFAULT_BLOCKLIST}
    return {_normalise(s) for s in raw.split(",") if s.strip()}


def is_tradeable(symbol: str) -> bool:
    """May the agent OPEN a position in this symbol?"""
    return _normalise(symbol) not in blocked_symbols()


def refusal_reason(symbol: str) -> Optional[str]:
    """Why not, phrased for a rejection record. None when it is tradeable."""
    if is_tradeable(symbol):
        return None
    return (
        f"{_normalise(symbol)} is on the untradeable list ({_ENV_VAR}), so no new "
        f"position may be opened in it. It is still watched, still priced, and its "
        f"market regime still informs decisions on other symbols — this excludes the "
        f"INSTRUMENT, not the information. Any position already open in it can still "
        f"be monitored and closed."
    )


def tradeable(symbols: Iterable[str]) -> List[str]:
    """Filter a candidate list, preserving order and dropping duplicates."""
    seen: Set[str] = set()
    out: List[str] = []
    for s in symbols:
        key = _normalise(s)
        if key in seen or not is_tradeable(s):
            continue
        seen.add(key)
        out.append(s)
    return out
