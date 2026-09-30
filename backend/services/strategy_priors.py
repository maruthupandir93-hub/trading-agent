"""Backtested win rates, used as a PRIOR while no strategy has a live record.

THE LOOP THIS CLOSES, AND WHY IT WAS STILL OPEN
===============================================
`services/strategy_performance` measures each strategy's REALISED win rate from
closed trades and feeds it into the scorer's 0.2 track-record weight. It is
correct and it has never produced a number, because it requires `MIN_SAMPLE`
(20) closed trades per strategy and this account's `trades` table holds zero.

So all eleven profiles carry `historical_success_rate=None`, every strategy
scores the neutral 0.5 on that component, and selection is decided entirely by
how well a strategy FITS current conditions. Read from the operator's live
session on 2026-09-30:

    "DO NOT TRADE: the Grid setup is LONG but the specialist panel reads
     NEUTRAL at 0.05."

In a Range regime the conditions-fit scorer keeps choosing Grid, Range and
MeanReversion — which are the three worst strategies in this project's own
stored backtest (Grid -0.206R, Range -0.184R, MeanReversion -0.103R) — and the
specialist panel then correctly refuses to act on them. Nothing was wrong; the
agent simply had no way to know, at SELECTION time, what it already knew from
`backtests/`.

WHAT THIS IS ALLOWED TO DO, AND WHAT IT IS NOT
==============================================
This does NOT violate invariant 5 ("learning never auto-deploys"), and the
argument is the same one `strategy_performance` makes:

  * it is deterministic arithmetic over a stored file — same file, same numbers,
    every time, and a test asserts the module contains no model call;
  * it cannot invent, edit, disable or author a strategy. It supplies ONE number
    that Section 11.3 already lists as a required field of a profile;
  * a human ran `scripts/run_backtests.py` and committed the result. Nothing
    here generates the evidence it reads.

CLAUDE.md is explicit that "a backtest INFORMS; it does not deploy", and this
module is built around that sentence rather than around it:

  1. **A LIVE MEASUREMENT ALWAYS WINS.** `opportunity._rate` asks
     `strategy_performance` first and only falls back here. The moment a
     strategy reaches MIN_SAMPLE real closed trades, its prior stops being
     consulted for it. MIN_SAMPLE still governs promotion exactly as before.
  2. **THE PRIOR IS SHRUNK TOWARD NEUTRAL, HALFWAY.** The backtest is IN-SAMPLE,
     GROSS of fees and was measured over a mostly TRENDING window — its own
     `run_backtests.py` output says so. The range strategies losing in a trend is
     partly that they were tested in a trend, which their regime gate would have
     muted live. A shrunk prior says "this is evidence, not a verdict".
  3. **NO BACKTEST MEANS NEUTRAL, NOT ZERO.** Same rule as a missing live
     record: scoring an unmeasured strategy as a failure would permanently
     freeze out the one that would have worked.

THE SHRINKAGE, STATED AS A SENTENCE
===================================
*A backtest can move a strategy off neutral, but only half as far as a live
measurement of the same size would.*

    effective_n = min(backtest_trades, MIN_SAMPLE)
    shrunk      = (effective_n * w + MIN_SAMPLE * NEUTRAL_EQUIVALENT)
                  / (effective_n + MIN_SAMPLE)

Two properties fall out of that and both are deliberate:

  * capping `effective_n` at MIN_SAMPLE means a 1,000-candle backtest is worth
    no more than the minimum live sample this system would accept. More
    backtested trades is more of the same window, not more independent evidence.
  * `NEUTRAL_EQUIVALENT` is DERIVED from the scorer's own scale — it is the win
    rate that maps to exactly 0.5 — rather than hardcoded. So a strategy with no
    backtest and a strategy whose backtest is exactly average score identically,
    and turning this module on cannot shift every strategy down relative to
    having no data at all. That was the first version's bug: anchoring on
    break-even (33.3%) instead made EVERY strategy score below the 0.5 that an
    unmeasured one gets, which pushes the whole field toward
    `MIN_SCORE_TO_SELECT` and trades less rather than better.

HONEST ABOUT THE SIZE OF THE EFFECT
===================================
Measured against the stored 2026-09-06 backtest, this separates the best
strategy from the worst by about 0.035 of final score (0.174 of the component,
times its 0.2 weight). That is a tie-breaker and a nudge past
`MIN_SCORE_TO_SELECT`, not an override — by construction, since conditions carry
0.8. It will not stop Grid being proposed in a market where Grid is the only
thing seeing a setup. It stops Grid being preferred to Breakout when both do.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Where `scripts/run_backtests.py` writes. Tracked in git, so it reaches the
# deployed backend with the code — a prior that only existed on the developer's
# machine would make the agent behave differently in the two places.
BACKTEST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backtests",
)

# A strategy with fewer than this many backtested trades is not judged at all.
# Deliberately the same floor the live path uses: a handful of simulated trades
# is exactly as unreliable as a handful of real ones.
MIN_BACKTEST_TRADES = 20

# Re-read at most this often. The file changes only when a human runs the
# script, so this is about not stat-ing the disk on every scoring pass.
_TTL_S = 300.0

_cache: Optional[Dict[str, Any]] = None
_cached_at = 0.0


def _neutral_equivalent_win_rate() -> float:
    """The win rate that the scorer maps to exactly 0.5.

    DERIVED, NOT HARDCODED, and that matters: the shrinkage target has to be the
    scorer's own neutral point or turning this module on moves every strategy in
    one direction. Importing the scale's two constants keeps the two files from
    drifting apart silently — which is the failure mode `lib/riskManager.ts` and
    `core/risk_manager.py` already had once with the ATR multipliers.
    """
    from backend.graphs.nodes.opportunity import (
        TRACK_RECORD_FLOOR_WIN_RATE,
        TRACK_RECORD_SPAN,
    )

    return TRACK_RECORD_FLOOR_WIN_RATE + 0.5 * TRACK_RECORD_SPAN


def _newest_summary() -> Optional[str]:
    """The most recent backtest run's summary.json, or None.

    Newest by DIRECTORY NAME, which `run_backtests.py` stamps with the UTC date,
    rather than by mtime: a `git clone` or a file copy rewrites every mtime to
    the same instant, so mtime would pick an arbitrary run on a fresh deploy.
    """
    try:
        names = sorted(
            d for d in os.listdir(BACKTEST_DIR)
            if os.path.isdir(os.path.join(BACKTEST_DIR, d))
        )
    except OSError:
        return None
    for name in reversed(names):
        path = os.path.join(BACKTEST_DIR, name, "summary.json")
        if os.path.isfile(path):
            return path
    return None


def load(*, force: bool = False) -> Dict[str, Any]:
    """Pooled backtest results per strategy. Never raises, never partial.

    Returns `{"strategies": {name: {...}}, "source": ..., "generatedAt": ...}`,
    or a dict whose `strategies` is empty when there is nothing usable — which
    every caller must treat as "no prior", never as "every strategy is bad".
    """
    global _cache, _cached_at

    if _cache is not None and not force and (time.time() - _cached_at) < _TTL_S:
        return _cache

    empty: Dict[str, Any] = {
        "strategies": {},
        "source": None,
        "generatedAt": None,
        "reason": "no backtest has been run — `python scripts/run_backtests.py`",
    }

    path = _newest_summary()
    if path is None:
        _cache, _cached_at = empty, time.time()
        return _cache

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - a bad file is "no prior", not a crash
        logger.warning(
            "Could not read the backtest summary at %s (%s). Strategy scoring falls "
            "back to a neutral track record, exactly as it behaved before priors "
            "existed.", path, exc,
        )
        _cache, _cached_at = dict(empty, reason=f"unreadable: {exc}"), time.time()
        return _cache

    # POOLED ACROSS EVERY SYMBOL AND TIMEFRAME IN THE RUN. A per-symbol prior
    # would be the more precise thing and is NOT what this file can honestly
    # support: the run covers three symbols at two timeframes, so a per-symbol
    # sample is a third the size and well under any floor worth trusting.
    pooled: Dict[str, Dict[str, float]] = {}
    for run in data.get("runs") or []:
        for row in run.get("strategies") or []:
            name = row.get("strategy")
            if not name:
                continue
            acc = pooled.setdefault(name, {"trades": 0.0, "wins": 0.0, "total_r": 0.0})
            try:
                acc["trades"] += float(row.get("trades") or 0)
                acc["wins"] += float(row.get("wins") or 0)
                acc["total_r"] += float(row.get("total_r") or 0)
            except (TypeError, ValueError):
                continue

    neutral = _neutral_equivalent_win_rate()
    out: Dict[str, Any] = {}
    for name, acc in pooled.items():
        n = int(acc["trades"])
        if n <= 0:
            continue
        raw = acc["wins"] / n
        usable = n >= MIN_BACKTEST_TRADES
        if usable:
            eff = min(n, MIN_BACKTEST_TRADES)
            shrunk = (eff * raw + MIN_BACKTEST_TRADES * neutral) / (eff + MIN_BACKTEST_TRADES)
        else:
            # Reported so the panel can show it, but NOT offered as a prior.
            shrunk = None
        out[name] = {
            "trades": n,
            "wins": int(acc["wins"]),
            "rawWinRate": round(raw, 4),
            "shrunkWinRate": round(shrunk, 4) if shrunk is not None else None,
            "expectancyR": round(acc["total_r"] / n, 4),
            "usable": usable,
        }

    _cache = {
        "strategies": out,
        "source": os.path.relpath(path, os.path.dirname(BACKTEST_DIR)).replace("\\", "/"),
        "generatedAt": data.get("generatedAt"),
        "neutralEquivalentWinRate": round(neutral, 4),
        "minTrades": MIN_BACKTEST_TRADES,
        "reason": None,
    }
    _cached_at = time.time()
    return _cache


def prior_win_rate(strategy: str) -> Optional[float]:
    """The shrunk backtested win rate for one strategy, or None.

    None means "no prior", and the caller must map that to the SAME neutral score
    a strategy with no live record gets. It never means "this strategy lost".
    """
    entry = load()["strategies"].get(strategy)
    if not entry or not entry.get("usable"):
        return None
    return entry.get("shrunkWinRate")


def reset() -> None:
    """Drop the cache. For tests, and after a fresh backtest run."""
    global _cache, _cached_at
    _cache, _cached_at = None, 0.0


def status() -> Dict[str, Any]:
    """For `GET /api/graphs/strategy-performance` and the Strategies panel.

    Carries the CAVEATS with the numbers, because a ranked table with no context
    reads as a verdict. `run_backtests.py` prints the same warnings and they do
    not survive into a JSON file on their own.
    """
    data = load()
    return {
        **data,
        "caveats": [
            "IN-SAMPLE, and mostly a trending window. The range strategies losing "
            "here is partly that they were tested in a trend, which their own "
            "regime gate would have muted live.",
            "GROSS of fees and slippage. At this system's stop distance the round "
            "trip is about 0.093R.",
            "The payoff is a fixed 2:1 (the ATR risk model), so win rate alone "
            "decides the sign — break-even is 33.3%.",
            "Shrunk halfway toward neutral before it is allowed to score, and "
            f"superseded entirely once a strategy has {MIN_BACKTEST_TRADES} real "
            "closed trades. A backtest informs; it does not deploy.",
        ],
    }
