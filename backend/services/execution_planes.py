"""What "Real" actually means here, said four different ways.

THE REVIEW'S ISSUE 9, IN ITS OWN WORDS
--------------------------------------
    *"The backend venue adapter is designed for Binance and Bybit linear
    perpetual markets. The manual frontend exchange clients are described as
    spot clients... That means the project has different trading capabilities
    depending on which interface you use. The UI's 'Real' mode must not be
    assumed to mean the same thing as the autonomous futures execution path.
    ... Do not let a generic 'Real' label hide these distinctions."*

That is accurate and it is worse than it sounds, because the distinction is
already load-bearing in code and was nowhere in the UI. `operator_exchange`
pins `defaultType: "spot"` deliberately, and its own comment records the
consequence: a spot position CANNOT be closed by `PositionMonitorAgent`,
because the monitor closes through a linear-PERPETUAL client. The route
already refuses to register one for exactly that reason. So "Real" on one
panel means a leveraged perpetual with a reduce-only stop resting at the
venue, and "Real" on another means an unleveraged spot holding with no
position to rest anything against — and both rendered as the word "Real".

WHAT THIS MODULE IS
-------------------
One place that states, per plane: the venue, the market type, the network,
whose account it is, the position mode, and what protective orders actually
exist. It is DESCRIPTIVE — it reads the same configuration the trading paths
read, and it decides nothing. A second place that re-derived "is the mirror
on?" is how `lib/riskManager.ts` and `core/risk_manager.py` drifted apart on
the ATR multipliers, so every field here comes from the module that owns it.

IT REPORTS THE PLANES THAT EXIST, not the ones that are configured. A plane
nobody has set up still has to be describable, because the question an
operator is asking is "what would happen if I pressed this button", and the
answer for an unconfigured plane is part of the answer.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# The four planes the review asks to be kept apart, in the order an operator
# meets them.
PAPER_SIMULATED = "paper-simulated"
PAPER_MIRRORED = "paper-testnet-mirror"
OPERATOR_SPOT = "operator-spot"
AGENT_PERPETUAL = "agent-perpetual"


def _flag(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


def planes() -> List[Dict[str, Any]]:
    """Every execution plane in this system, described rather than summarised.

    Never raises. This is the surface an operator reads when they are unsure
    what a button does, and that is the worst possible moment for it to 500.
    """
    live = _flag("LIVE_TRADING")

    # --- the agent's own plane -------------------------------------------
    try:
        from backend.services.venue import configured_venue

        agent_venue = configured_venue()
    except Exception:  # noqa: BLE001
        agent_venue = os.getenv("EXCHANGE_ID", "binance")

    try:
        from backend.services.venue import get_venue

        agent_creds = bool(get_venue().has_credentials())
    except Exception:  # noqa: BLE001
        agent_creds = None

    # --- the mirror -------------------------------------------------------
    try:
        from backend.services import paper_testnet

        mirror_venue = paper_testnet.venue_choice()
        mirror_on = bool(paper_testnet.enabled())
        mirror_active = bool(paper_testnet.active())
    except Exception:  # noqa: BLE001
        mirror_venue, mirror_on, mirror_active = "binance", False, False

    return [
        {
            "id": PAPER_SIMULATED,
            "label": "Paper — simulated fills",
            "active": not live and not mirror_active,
            "venue": None,
            "marketType": "perpetual (modelled)",
            "network": None,
            "account": "local paper book",
            "positionMode": "one-way (modelled)",
            "leverageAvailable": True,
            "protectiveOrders": "in-process only",
            "closedBy": "PositionMonitorAgent, in this process",
            "note": (
                "A fill is booked at the last observed price, instantly and in full. "
                "That is honest bookkeeping and the most flattering possible "
                "execution: no spread crossed, no slippage, no partial fill, no "
                "minimum size, no venue outage. Every one of those appears on day "
                "one of real money and on none of the paper days before it."
            ),
        },
        {
            "id": PAPER_MIRRORED,
            "label": f"Paper — mirrored to {mirror_venue} demo",
            "active": mirror_active,
            "venue": mirror_venue,
            "marketType": "linear perpetual",
            "network": "testnet / demo",
            "account": f"{mirror_venue.upper()}_TESTNET_* credentials",
            "positionMode": "one-way",
            "leverageAvailable": True,
            "protectiveOrders": "reduce-only stop AND take-profit resting at the demo venue",
            "closedBy": "PositionMonitorAgent, routed to the DEMO venue",
            "configured": mirror_on,
            "credentialsVerified": mirror_active,
            "note": (
                "The paper book is credited with the price the demo exchange "
                "returned, so spread, slippage and step-size truncation are real. "
                "It can never reach mainnet: the venue and sandbox flag are "
                "hardcoded, not read from EXCHANGE_ID."
                if mirror_on else
                "Not enabled. Paper fills are simulated — see the plane above."
            ),
        },
        {
            "id": OPERATOR_SPOT,
            "label": "Operator panel — REAL SPOT",
            "active": True,
            "venue": "chosen per request, with the operator's own keys",
            # THE DISTINCTION THE REVIEW IS ABOUT. `operator_exchange` pins
            # `defaultType: "spot"` deliberately, and that is not a detail:
            # a spot holding has no position for a reduce-only order to
            # reduce, so the perpetual monitor cannot close it at all.
            "marketType": "SPOT",
            "network": "per request (testnet flag on the request body)",
            "account": "the operator's own, credentials supplied per request and never stored",
            "positionMode": "n/a — spot has no position mode",
            "leverageAvailable": False,
            "protectiveOrders": (
                "NONE at the venue. A stop given here registers with the in-process "
                "monitor only, and the route REFUSES to register one while "
                "LIVE_TRADING is off, because the monitor would 'close' it by "
                "simulation while the holding stayed at the exchange."
            ),
            "closedBy": "the operator, by hand",
            "note": (
                "THIS IS NOT THE SAME 'REAL' AS THE AGENT'S. It is unleveraged spot, "
                "it has no position mode, nothing rests at the venue, and the "
                "perpetual position monitor cannot reduce it. Orders are tagged "
                "origin_tag='manual-click' in the trade log so the two can be told "
                "apart afterwards."
            ),
        },
        {
            "id": AGENT_PERPETUAL,
            "label": "Agent — REAL linear perpetual",
            "active": live,
            "venue": agent_venue,
            "marketType": "linear perpetual (USDT-margined)",
            "network": "mainnet",
            "account": f"the {agent_venue.upper()}_* credentials on this server",
            "positionMode": "one-way or hedge, read from the venue",
            "leverageAvailable": True,
            "protectiveOrders": (
                "reduce-only stop AND take-profit resting at the venue on every fill"
            ),
            "closedBy": "PositionMonitorAgent, through the execution chokepoint",
            "credentialsConfigured": agent_creds,
            "note": (
                "The only plane that places real leveraged orders, and the only one "
                "gated by LIVE_TRADING. Every entry passes the Supervisor and the "
                "CRO (invariant 1); the operator spot plane deliberately does not."
                if live else
                "LIVE_TRADING is off, so no order from this plane reaches the venue."
            ),
        },
    ]


def summary() -> Dict[str, Any]:
    """The planes plus the one sentence that answers "where would an order go?".

    Named explicitly because the dangerous reading is the implicit one: an
    operator who sees a Buy button and a book labelled "Real" assumes one
    plane, and which plane it actually is depends on LIVE_TRADING, on the
    mirror, and on WHICH PANEL they pressed.
    """
    rows = planes()
    active = [p for p in rows if p.get("active")]
    return {
        "planes": rows,
        "activeIds": [p["id"] for p in active],
        "note": (
            "A generic 'Real' label hides the difference between an unleveraged "
            "SPOT holding placed from the operator panel and a leveraged PERPETUAL "
            "position opened by the agent. They are different instruments, on "
            "different market types, with different protection, and only one of "
            "them can be closed by the position monitor."
        ),
    }
