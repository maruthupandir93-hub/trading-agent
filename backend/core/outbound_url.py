"""Is this URL safe for THIS SERVER to fetch on a caller's behalf?

WHY THIS EXISTS
---------------
`POST /api/ai/chat` takes `baseUrl` from the request body and makes the backend
POST to it. With no auth on that route (fixed alongside this module) and no
check on the address, anyone who could reach port 8000 could make the server
issue requests to any host it can see and read the streamed reply:

    127.0.0.1 / ::1          every other service on the box, including the
                             backend's own unauthenticated routes
    10/172.16/192.168        anything else on the VM's private network
    169.254.169.254          the cloud metadata endpoint, which on most
                             providers hands out instance credentials

That is server-side request forgery, and the streaming response makes it a
READ primitive rather than a blind one — the attacker gets the body back.

THE FRONTEND ALREADY HAD THIS (`lib/ssrfGuard.ts`) AND THE BACKEND DID NOT,
which is the two-halves-disagree shape this codebase keeps hitting: the same
application enforced a rule on one server and not the other. The two are kept
deliberately parallel, and `tests/test_outbound_url.py` asserts the shared
cases so neither can quietly drift.

WHAT IT DOES NOT DO
-------------------
It does not re-resolve DNS at connect time, so a name that resolves to a public
address here and a private one microseconds later (DNS rebinding) is not
stopped by this alone. Saying so plainly matters more than implying it is
complete: the real defences against that are the auth this route now requires
and an allow-list, and an allow-list is available through
`LLM_BASE_URL_ALLOWLIST` for an operator who wants it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWLIST_VAR = "LLM_BASE_URL_ALLOWLIST"

# Schemes a caller may ask this server to speak. `file://`, `gopher://` and
# friends are not fetches a chat proxy has any reason to make, and several of
# them read the local disk.
ALLOWED_SCHEMES = ("https", "http")


@dataclass(frozen=True)
class UrlVerdict:
    ok: bool
    reason: str = ""
    host: Optional[str] = None


def _allowlist() -> list[str]:
    """Hosts the operator has explicitly permitted. Read at call time."""
    raw = os.getenv(ALLOWLIST_VAR) or ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> Optional[str]:
    """Name the reason this address is refused, or None.

    EVERY CATEGORY IS NAMED SEPARATELY rather than collapsed into "private",
    because the reasons differ and an operator reading a refusal needs to know
    which one they hit. `is_private` alone also MISSES the two that matter
    most here: link-local (the metadata endpoint) and loopback are not private
    under that predicate on every Python version.
    """
    if ip.is_loopback:
        return "a loopback address — that is this server's own services"
    if ip.is_link_local:
        return (
            "a link-local address — on most cloud providers 169.254.169.254 "
            "serves instance credentials"
        )
    if ip.is_private:
        return "a private address — that is this server's own network"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "a reserved, multicast or unspecified address"
    # IPv4-MAPPED IPv6 IS THE ONE THAT SLIPS THROUGH.
    #
    # `::ffff:127.0.0.1` is loopback wearing an IPv6 coat, and the equivalent
    # check on the TypeScript side was caught out by exactly this — `new URL()`
    # renders it as `::ffff:7f00:1`, so a string comparison missed it.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_blocked_ip(mapped)
    return None


def check(url: str) -> UrlVerdict:
    """May this server fetch `url` on a caller's behalf?

    Refuses by DEFAULT on anything it cannot parse or resolve. An outbound
    fetch the server cannot characterise is not one it should make — the
    opposite of this file's usual True/False/None rule, and deliberately so:
    here "could not tell" is a reason to decline, not to proceed, because the
    cost of a wrong allow is credential disclosure.
    """
    if not url or not isinstance(url, str):
        return UrlVerdict(False, "no URL was given")

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        return UrlVerdict(False, f"the URL could not be parsed ({exc})")

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return UrlVerdict(
            False,
            f"scheme {scheme or '(none)'!r} is not allowed; "
            f"only {', '.join(ALLOWED_SCHEMES)} are",
        )

    host = (parts.hostname or "").strip().lower()
    if not host:
        return UrlVerdict(False, "the URL carries no host")

    allowed = _allowlist()
    if allowed:
        # An explicit allow-list is the strongest form and skips the rest:
        # the operator has named what this server may talk to.
        if host in allowed or any(host.endswith("." + a) for a in allowed):
            return UrlVerdict(True, "host is on the operator's allow-list", host)
        return UrlVerdict(
            False,
            f"host {host!r} is not on {ALLOWLIST_VAR}",
            host,
        )

    # A bare IP literal is checked directly — no resolution needed, and this is
    # the common attack shape.
    try:
        return (
            UrlVerdict(False, f"{host} is {_is_blocked_ip(ipaddress.ip_address(host))}", host)
            if _is_blocked_ip(ipaddress.ip_address(host))
            else UrlVerdict(True, "a public address", host)
        )
    except ValueError:
        pass  # not a literal; resolve it below

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return UrlVerdict(False, f"host {host!r} could not be resolved ({exc})", host)

    # EVERY resolved address must pass, not just the first. A name with one
    # public and one loopback record would otherwise be allowed and then
    # connect to whichever the stack picked.
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return UrlVerdict(False, f"{host!r} resolved to an unreadable address", host)
        blocked = _is_blocked_ip(ip)
        if blocked:
            return UrlVerdict(False, f"{host!r} resolves to {addr}, which is {blocked}", host)

    return UrlVerdict(True, "every resolved address is public", host)
