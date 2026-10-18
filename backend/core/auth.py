"""Shared-secret auth for state-changing FastAPI routes.

THE GAP THIS CLOSES
-------------------
`Recommended_Technology_Stack.md` lists "Authentication and authorization" as a
FastAPI responsibility, and there was none — not on any route. The Next.js layer
already had it (`app/api/trades/route.ts::isAuthorized`), so the two halves of
the same application disagreed about whether writes needed a credential.

What was reachable unauthenticated by anything that could open the port:

  POST /api/admin/emergency-stop            halt the whole system
  POST /api/admin/pause  /resume            start and stop trading
  POST /api/research/hypotheses/{id}/status THE HUMAN APPROVAL GATE
  POST /api/knowledge/relationship          write into the knowledge base
  POST /api/memory/lesson                   write into agent memory
  POST /api/agents/tasks                    create a trading agent
  DELETE /api/agents/tasks/{id}             cancel one

The approval gate is the one that matters most. `research_store` refuses to let
automated code mark its own hypothesis validated, and `set_by_human=True` is
passed only by that route — but with no auth, "human" meant "any HTTP client".
The enforcement was real and the identity check behind it was missing.

THE CONVENTION IS DELIBERATELY THE SAME AS THE NEXT.JS SIDE
-----------------------------------------------------------
Same env var (`TRADES_API_KEY`), same `Bearer` scheme, same default. One secret
governs both servers, because two independent auth schemes for one application
is how one of them ends up unprotected.

  * Unset  -> everything is open. Local development needs zero setup, and this
              matches the existing Next.js behaviour exactly. A different
              default here would mean enabling auth on one server silently left
              the other open.
  * Set    -> state-changing routes require `Authorization: Bearer <key>`.

READS STAY OPEN, WRITES DO NOT
------------------------------
Also matching the Next.js side: the browser UI polls read endpoints directly and
cannot hold a server secret without shipping it to every client. Protecting
reads would either break the UI or push the secret into the bundle, which is
worse than leaving reads open on a service the operator is expected to keep off
the public internet.

NOT A SUBSTITUTE FOR REAL AUTH
------------------------------
A shared bearer token is not user authentication: there are no users, no roles,
and no audit of WHO acted. It is the honest minimum for a single-operator
service, and it is stated as such rather than described as "authentication and
authorization". Multi-user auth needs a user model, which this system does not
have.
"""

import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# Same variable the Next.js routes read.
ENV_VAR = "TRADES_API_KEY"


def auth_required() -> bool:
    """True when a secret is configured."""
    return bool(os.getenv(ENV_VAR))


def _matches(provided: str, required: str) -> bool:
    # Constant-time comparison. A plain `==` on a secret leaks its length and,
    # in principle, its content through timing — cheap to avoid, so avoided.
    return hmac.compare_digest(provided, required)


def open_writes_allowed() -> bool:
    """May write routes run with NO key configured?

    DEFAULT NO, AND THAT IS THE F6 FIX. `require_write_auth` used to be a
    complete no-op when `TRADES_API_KEY` was unset, and the live backend
    reported exactly that:

        "writeAuthEnabled": false,
        "note": "TRADES_API_KEY is not set, so ALL endpoints are open."

    The VM's port 8000 is reachable from the internet, so anyone who found it
    could turn on live trading, switch venue, start or stop sessions, reset the
    paper book, or place real orders through `/api/operator/exchange/order`.

    An auth dependency that passes when it is not configured fails OPEN, which
    is the wrong direction for a system that moves money. It fails CLOSED now:
    with no key, writes are refused and the refusal says what to set.

    `ALLOW_UNAUTHENTICATED_WRITES=true` restores the old behaviour for a
    genuinely local, non-routable deployment. It is deliberately verbose and
    deliberately not the default: an operator who wants an open write surface
    on a trading backend should have to say so.
    """
    raw = (os.getenv("ALLOW_UNAUTHENTICATED_WRITES") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def require_write_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency guarding a state-changing route.

    Raises 401 when the header is missing or wrong, and 503 when no key is
    configured at all — see `open_writes_allowed` for why the unconfigured case
    is a refusal rather than a pass.
    """
    required = os.getenv(ENV_VAR)
    if not required:
        if open_writes_allowed():
            return
        logger.error(
            "Refused a write: %s is not set. This backend changes real state — "
            "live trading, venue, sessions, the paper book and real orders — so "
            "writes fail CLOSED rather than open. Set %s, or set "
            "ALLOW_UNAUTHENTICATED_WRITES=true if this process is genuinely "
            "unreachable from anywhere but localhost.",
            ENV_VAR, ENV_VAR,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"This endpoint changes state and {ENV_VAR} is not configured, so "
                f"writes are refused. Set {ENV_VAR} on the backend (and in the "
                f"dashboard's environment), or set ALLOW_UNAUTHENTICATED_WRITES=true "
                f"if this process is only reachable from localhost."
            ),
        )

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail=(
                f"Unauthorized — this endpoint changes state and {ENV_VAR} is configured. "
                f"Send 'Authorization: Bearer <key>'."
            ),
            # Tells a client HOW to authenticate rather than only that it failed.
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    # `hmac.compare_digest` raises TypeError on a non-ASCII str, which FastAPI
    # turns into a 500 — so a malformed token answered "server error" instead
    # of "unauthorized", and told a prober that something unusual happened
    # (F87). Treated as a failed comparison, which is what it is.
    try:
        ok = bool(token) and _matches(token, required)
    except (TypeError, ValueError):
        ok = False
    if scheme.lower() != "bearer" or not ok:
        # The reason is deliberately not narrowed to "wrong key" vs "wrong
        # scheme" — distinguishing them tells an attacker which half to fix.
        logger.warning("Rejected an unauthorized write request.")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized — missing or wrong Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def auth_status() -> dict:
    """For the status endpoint. Never returns the key itself.

    THIS USED TO MAKE A FALSE CLAIM, and it is the kind that wastes a day.
    With no key configured it said *"ALL endpoints are open"* -- which was true
    when `require_write_auth` was a no-op and became FALSE the moment writes
    began failing closed. An operator reading it would conclude the opposite of
    what the backend was doing: the status page said everything was permitted
    while every button on the dashboard was being refused with a 503.

    The three machine-readable fields exist so the UI can render the BLOCKER
    BEFORE THE CONTROL rather than after a click, which is the rule the venue
    switch panel already follows -- discovering a refusal from an error toast is
    worse than never being offered the action.
    """
    configured = auth_required()
    open_writes = open_writes_allowed()
    # Writes work when a key is configured, or when the operator has explicitly
    # opted into an open write surface. Not otherwise.
    writes_possible = configured or open_writes

    if configured:
        note = (
            "Reads are intentionally open so the browser UI need not hold a server secret. "
            "A shared bearer token is not user authentication — there are no users or roles, "
            "and actions are not attributed to an identity."
        )
        remedy = None
    elif open_writes:
        note = (
            f"{ENV_VAR} is not set and ALLOW_UNAUTHENTICATED_WRITES is on, so writes are "
            f"accepted from anyone who can reach this port. That is only safe if this "
            f"process is genuinely unreachable from the internet."
        )
        remedy = (
            f"Set {ENV_VAR} and remove ALLOW_UNAUTHENTICATED_WRITES if this backend is "
            f"reachable from anywhere but localhost."
        )
    else:
        note = (
            f"{ENV_VAR} is not set, so every STATE-CHANGING route is refused with HTTP 503. "
            f"Reads still work, which is why the dashboard renders while its buttons do "
            f"nothing. Nothing is open: writes fail closed."
        )
        remedy = (
            f"Set {ENV_VAR} to the same value in the backend's .env AND in the dashboard's "
            f"environment, then restart the backend. Trading cannot start until you do: "
            f"starting a session, enabling live trading and the emergency stop are all "
            f"write routes."
        )

    return {
        "writeAuthEnabled": configured,
        # NEW, and the reason this function was touched: a UI needs to know
        # whether a write can succeed, not merely whether auth is enabled.
        "writesPossible": writes_possible,
        "openWritesAllowed": open_writes,
        "remedy": remedy,
        "scheme": "Bearer" if configured else None,
        "envVar": ENV_VAR,
        "readsProtected": False,
        "note": note,
    }
