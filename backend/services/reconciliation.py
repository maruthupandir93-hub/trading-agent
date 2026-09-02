"""Compare what this system thinks it holds against what the VENUE says it holds.

WHY THIS EXISTS
===============
The agent's idea of its own book came from one source: its own fills. Nothing ever
asked the exchange. So every one of these left the two silently disagreeing, with
no symptom until money was already lost:

  * The operator closed a position by hand in the Binance app. The monitor kept
    watching a position that no longer existed and would eventually send a close
    for it — which, without `reduceOnly`, would have OPENED the opposite position.
  * A liquidation or an ADL closed it. Same, but the operator did not even know.
  * A fill arrived that this process never saw, because it was restarting.
  * A partial fill left the venue holding less than the local book records, so
    every P&L figure was computed against a size that was never held.

REPORTS, DOES NOT REPAIR — AND THAT IS THE IMPORTANT DECISION
=============================================================
This module never closes a position, never opens one, and never edits the watch
list. It produces a list of discrepancies and raises the loud ones.

Auto-repair is tempting and it is wrong here. Every "fix" is itself a trade: a
local position the venue does not have would be "fixed" by forgetting it — but if
the venue read was stale or partial, forgetting it abandons a real open position
with no stop. A venue position the agent does not know about would be "fixed" by
closing it — a market order nobody asked for, possibly on an instrument the
operator is trading by hand in the same account.

The one exception this codebase already accepts is the direction of
`PositionMonitorAgent`, which closes on a stop it was told about. A reconciler
acting on an inference is a different thing entirely, and CLAUDE.md's rule that
closes are never blocked does not imply that closes may be invented.

`None` FROM THE VENUE IS NOT AN EMPTY BOOK
==========================================
`Venue.open_positions()` returns None when it could not ask and [] when the venue
answered "nothing open". Treating the first as the second would report every local
position as a phantom on any network blip — and if anything ever acted on that
report, a transient timeout would flatten the book.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sizes never match to the last bit: fees can be taken in the base asset, and both
# venues round to their own step. Below this fraction of the position, a
# difference is arithmetic rather than a discrepancy worth waking anyone for.
QTY_TOLERANCE_FRACTION = 0.02  # 2%

# Reconciliation is a PRIVATE call per run, and the balance and positions move
# only on a fill. Every 60s is often enough to catch a manual close within a
# minute and rare enough not to compete with order placement for the key's budget.
DEFAULT_INTERVAL_S = 60.0


@dataclass
class Discrepancy:
    kind: str  # missing_at_venue | unknown_locally | size_mismatch | side_mismatch
    symbol: str
    detail: str
    severity: str  # critical | warning
    local: Optional[Dict[str, Any]] = None
    venue: Optional[Dict[str, Any]] = None


@dataclass
class ReconciliationReport:
    ok: bool
    checked_at: float
    venue_id: str
    # None means the venue could not be asked. Distinct from 0 — see the module
    # docstring; every consumer must branch on it.
    venue_positions: Optional[int]
    local_positions: int
    discrepancies: List[Discrepancy] = field(default_factory=list)
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checkedAt": self.checked_at,
            "venue": self.venue_id,
            "venuePositions": self.venue_positions,
            "localPositions": self.local_positions,
            "discrepancies": [
                {
                    "kind": d.kind,
                    "symbol": d.symbol,
                    "detail": d.detail,
                    "severity": d.severity,
                    "local": d.local,
                    "venue": d.venue,
                }
                for d in self.discrepancies
            ],
            "error": self.error,
            "meaning": (
                "Reports only. Nothing here closes, opens or forgets a position — "
                "every automatic 'fix' for a discrepancy is itself a trade, and a "
                "stale or partial venue read would make it the wrong one."
            ),
        }


def _compare(
    local: List[Dict[str, Any]], venue_positions: List[Dict[str, Any]]
) -> List[Discrepancy]:
    """Pure. Given both books, name every way they disagree.

    Separated from the I/O so the comparison itself is testable without a network
    or an exchange account — which is the only way the severity rules get checked
    at all.
    """
    out: List[Discrepancy] = []
    by_symbol_venue = {p["symbol"]: p for p in venue_positions if p.get("symbol")}
    by_symbol_local = {p["symbol"]: p for p in local if p.get("symbol")}

    for symbol, lp in by_symbol_local.items():
        vp = by_symbol_venue.get(symbol)
        if vp is None:
            # CRITICAL: the monitor is watching something that is not there. Its
            # next stop touch sends a close for a position that does not exist.
            out.append(Discrepancy(
                kind="missing_at_venue",
                symbol=symbol,
                severity="critical",
                detail=(
                    f"this system is monitoring {symbol} but the venue reports no such "
                    f"position. It was probably closed by hand, liquidated, or ADL'd. "
                    f"The stop being enforced here protects nothing."
                ),
                local=lp,
            ))
            continue

        lq, vq = float(lp.get("qty") or 0), float(vp.get("qty") or 0)
        if vq > 0 and abs(lq - vq) / vq > QTY_TOLERANCE_FRACTION:
            out.append(Discrepancy(
                kind="size_mismatch",
                symbol=symbol,
                severity="critical",
                detail=(
                    f"{symbol}: this system holds {lq:g}, the venue holds {vq:g}. Every "
                    f"P&L and risk figure here is computed against a size that is not "
                    f"the one at risk."
                ),
                local=lp, venue=vp,
            ))

        # 'buy' locally is a long at the venue.
        local_dir = "long" if lp.get("side") == "buy" else "short"
        venue_dir = (vp.get("side") or "").lower()
        if venue_dir and venue_dir != local_dir:
            out.append(Discrepancy(
                kind="side_mismatch",
                symbol=symbol,
                severity="critical",
                detail=(
                    f"{symbol}: this system believes it is {local_dir}, the venue says "
                    f"{venue_dir}. The stop is on the wrong side of the price and would "
                    f"widen the loss rather than cap it."
                ),
                local=lp, venue=vp,
            ))

    for symbol, vp in by_symbol_venue.items():
        if symbol in by_symbol_local:
            continue
        # WARNING, not critical: the operator may legitimately be trading this
        # account by hand. It is unmonitored by this system either way, and saying
        # so is the point.
        out.append(Discrepancy(
            kind="unknown_locally",
            symbol=symbol,
            severity="warning",
            detail=(
                f"the venue holds {vp.get('qty')} {symbol} that this system is not "
                f"monitoring. If this is the agent's, its stop is not being enforced; "
                f"if it is yours, nothing here will touch it."
            ),
            venue=vp,
        ))

    return out


async def reconcile() -> ReconciliationReport:
    """Ask the venue what it holds and compare. Never raises."""
    from backend.agents.position_monitor import get_position_monitor
    from backend.services.venue import get_venue

    venue = get_venue()
    monitor = get_position_monitor()

    # Only REAL positions can be reconciled. A paper position has no venue
    # counterpart, and comparing it to one would report every simulated trade as
    # a phantom.
    local = [p for p in monitor.snapshot_open() if p.get("tab") == "real"]

    if not venue.has_credentials():
        return ReconciliationReport(
            ok=True, checked_at=time.time(), venue_id=venue.id,
            venue_positions=None, local_positions=len(local),
            error="no venue credentials configured, so the venue could not be asked",
        )

    venue_positions = await venue.open_positions()
    if venue_positions is None:
        # NOT an empty book. Reporting zero here would flag every real position as
        # missing on a single network blip.
        return ReconciliationReport(
            ok=False, checked_at=time.time(), venue_id=venue.id,
            venue_positions=None, local_positions=len(local),
            error=(
                "the venue could not be reached, so nothing was compared. This is NOT "
                "a report that the venue holds nothing."
            ),
        )

    discrepancies = _compare(local, venue_positions)

    for d in discrepancies:
        log = logger.critical if d.severity == "critical" else logger.warning
        log("RECONCILIATION (%s): %s", d.kind, d.detail)

    return ReconciliationReport(
        ok=not any(d.severity == "critical" for d in discrepancies),
        checked_at=time.time(),
        venue_id=venue.id,
        venue_positions=len(venue_positions),
        local_positions=len(local),
        discrepancies=discrepancies,
    )


# ---------------------------------------------------------------------------
# The periodic loop
# ---------------------------------------------------------------------------

_last_report: Optional[ReconciliationReport] = None


def last_report() -> Optional[ReconciliationReport]:
    """The most recent result, for the API to serve without forcing a fresh call."""
    return _last_report


async def run_forever(interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """Reconcile on a timer. Started from `main.py`'s lifespan.

    ONLY WHILE LIVE TRADING IS ON. With `LIVE_TRADING=false` there is no real book
    to reconcile, and polling a private endpoint every minute to compare two empty
    lists would spend the operator's rate budget on nothing.

    Never raises out of the task: a reconciler that killed itself on one bad
    response would stop reporting exactly when something had gone wrong.
    """
    import asyncio

    from backend.core.config import settings

    global _last_report
    while True:
        try:
            await asyncio.sleep(interval_s)
            if not settings.LIVE_TRADING:
                continue
            _last_report = await reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("Reconciliation pass failed; will retry on the next interval.")
