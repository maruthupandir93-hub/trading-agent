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


def _norm(symbol: Any) -> str:
    """`SOL/USDT:USDT` and `SOL/USDT` are the same instrument. Compare them as one."""
    return str(symbol or "").split(":")[0]


def _compare(
    local: List[Dict[str, Any]], venue_positions: List[Dict[str, Any]]
) -> List[Discrepancy]:
    """Pure. Given both books, name every way they disagree.

    Separated from the I/O so the comparison itself is testable without a network
    or an exchange account — which is the only way the severity rules get checked
    at all.
    """
    out: List[Discrepancy] = []
    # NORMALISED ON BOTH SIDES. The local book holds "SOL/USDT"; ccxt's unified
    # symbol for the same perpetual is "SOL/USDT:USDT". Keying on the raw strings
    # matched nothing, so every real position was reported as CRITICAL "missing at
    # venue" AND the venue's own position as unknown — a false alarm on precisely
    # the alert that is supposed to mean a liquidation or a manual close.
    #
    # `Venue.open_positions` already returns the display form; this normalises
    # again so a caller passing raw venue rows cannot resurrect the bug.
    by_symbol_venue = {_norm(p["symbol"]): p for p in venue_positions if p.get("symbol")}
    by_symbol_local = {_norm(p["symbol"]): p for p in local if p.get("symbol")}

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


async def _reconcile_mirror(monitor) -> List[Discrepancy]:
    """Compare the MIRRORED PAPER book against the demo exchange it lives on.

    WHY THIS IS A SEPARATE PASS AND NOT A WIDER FILTER
    --------------------------------------------------
    A mirrored paper position is a real order at a demo venue, and that venue
    is NOT `get_venue()` — it is `paper_testnet.get_venue()`, which for Binance
    is a direct signed-REST facade because ccxt cannot reach the demo host at
    all. Pointing one comparison at both books would ask the mainnet account
    about positions it has never heard of and report every single one as a
    phantom, which is the false alarm that teaches an operator to ignore the
    one alert that means a liquidation.

    IT ASKS PER SYMBOL, through `paper_testnet.venue_flat`, because that is the
    one reader both venue shapes implement (`BinanceTestnetVenue.open_positions`
    takes a symbol; `Venue.open_positions` answers for the whole account) and it
    already carries the True/False/None distinction this module is built on.

    ONLY A POSITION WITH A VENUE-SIDE ARTEFACT IS COMPARED. `protection` says
    whether the exchange actually confirmed an order for it. A paper position
    opened while the mirror was OFF has no counterpart at the demo venue, and
    asking about it would report a phantom for a position that was never
    supposed to be there — the same mistake in a smaller place.

    REPORTS ONLY, exactly as the rest of this module does. It never closes,
    opens or forgets anything; the close path's own `venue_flat` check (F1) is
    what settles a position the venue has already closed, and it runs with the
    full context of that trade.
    """
    out: List[Discrepancy] = []
    try:
        from backend.services import paper_testnet

        if not paper_testnet.active():
            return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("Mirror reconciliation skipped: %s", exc)
        return out

    try:
        from backend.agents.position_monitor import (
            PROTECTION_PARTIAL, PROTECTION_VENUE,
        )
        venue_side = (PROTECTION_VENUE, PROTECTION_PARTIAL)
    except Exception:  # noqa: BLE001
        venue_side = ("venue", "partial")

    try:
        rows = [
            p for p in monitor.snapshot_open()
            if p.get("tab") != "real" and p.get("protection") in venue_side
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the watch list for mirror reconciliation: %s", exc)
        return out

    venue_id = f"{paper_testnet.venue_choice()}-testnet"
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        try:
            flat = await paper_testnet.venue_flat(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Mirror reconciliation could not read %s: %s", symbol, exc)
            continue

        # None is "could not ask", NOT "the venue holds nothing". Collapsing
        # those is the distinction this codebase has now paid for four times.
        if flat is not True:
            continue

        out.append(Discrepancy(
            kind="missing_at_venue",
            symbol=symbol,
            severity="critical",
            detail=(
                f"{symbol} is open on the PAPER book with a confirmed order at "
                f"{venue_id}, and that account now holds nothing in it. Its resting "
                f"stop or take-profit has almost certainly fired. Until the monitor's "
                f"next close attempt settles the book, this position is counted "
                f"against the concurrent-position limit while no longer existing — "
                f"which is what kept a session frozen for four days (F1)."
            ),
            local=row,
            venue={"flat": True, "venue": venue_id},
        ))

    for d in out:
        logger.critical("RECONCILIATION (%s): %s", d.kind, d.detail)
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

    # THE MIRRORED PAPER BOOK IS COMPARED TOO, AND IT WAS NOT (#16).
    #
    # F7 made the LOOP wake up for a mirrored book — correctly, because a
    # mirrored paper position holds a real resting stop at the demo exchange
    # and F1 is what happens when nothing notices it firing. But `reconcile()`
    # still filtered to `tab == "real"` and still asked `get_venue()`, the
    # MAINNET client. So on a mirror-only deployment the loop ran every minute
    # and compared an empty list against an account that has never heard of
    # these positions: the comparison that would have caught F1 on its first
    # minute still did not happen.
    #
    # It is a SEPARATE pass rather than a wider filter because the two books
    # live at two different exchanges. Merging them into one `_compare` would
    # ask the mainnet venue about demo positions and report every one of them
    # as a phantom — the false alarm that teaches an operator to ignore the
    # one alert that means a liquidation.
    mirror = await _reconcile_mirror(monitor)

    if not venue.has_credentials():
        # The mirror's findings still stand. A deployment with no mainnet keys
        # and an active mirror is the normal paper configuration, and returning
        # here used to discard the only comparison it could make.
        return ReconciliationReport(
            ok=not any(d.severity == "critical" for d in mirror),
            checked_at=time.time(), venue_id=venue.id,
            venue_positions=None, local_positions=len(local),
            discrepancies=mirror,
            error="no venue credentials configured, so the venue could not be asked",
        )

    venue_positions = await venue.open_positions()
    if venue_positions is None:
        # NOT an empty book. Reporting zero here would flag every real position as
        # missing on a single network blip.
        return ReconciliationReport(
            ok=False, checked_at=time.time(), venue_id=venue.id,
            venue_positions=None, local_positions=len(local),
            discrepancies=mirror,
            error=(
                "the real venue could not be reached, so the REAL book was not "
                "compared. This is NOT a report that the venue holds nothing."
            ),
        )

    discrepancies = _compare(local, venue_positions) + mirror

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
            # A MIRRORED PAPER POSITION HAS REAL ORDERS TOO (F7). The old
            # `if not settings.LIVE_TRADING` skipped the one comparison that
            # would have caught F1 on its first minute: the venue flat, the
            # local book holding. See `order_stream.venue_backed_book_exists`.
            from backend.services.order_stream import venue_backed_book_exists

            if not venue_backed_book_exists():
                continue
            _last_report = await reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("Reconciliation pass failed; will retry on the next interval.")
