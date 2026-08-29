"""One outbound HTTP client for every third-party call the backend makes.

WHY THIS EXISTS — THE VERCEL 451
--------------------------------
The Next.js layer used to call Binance, Yahoo and the news providers directly
from its own route handlers. On Vercel those handlers execute in whatever region
Vercel picks, and when that region is the United States, Binance answers:

    HTTP 451 — "Service unavailable from a restricted location according to
                'b. Eligibility'"

451 is literally "Unavailable For Legal Reasons". It is not a bug in the calling
code and no retry, key or header fixes it: the caller is in a region the upstream
refuses to serve. The frontend surfaced it as a bare 502 on /api/candles,
/api/orderflow and /api/quote.

The fix is structural — the request has to originate from a machine in a region
the upstream will serve. That machine is the Oracle box this backend runs on, so
every outbound call now happens HERE and the Next.js routes proxy to it. See
`backend/api/marketdata.py`.

WHY ONE CLIENT AND NOT `httpx.get` AT EACH CALL SITE
----------------------------------------------------
A new `AsyncClient` per call opens a new TCP+TLS connection every time. These
endpoints are polled every few seconds by the dashboard, so that is a handshake
per poll per widget. One module-level client keeps a connection pool.

It also gives one place to:
  * apply a timeout — without one, httpx waits forever and the Next proxy times
    out first, turning "Binance is slow" into "the backend is down";
  * recognise a geo-block and say so in words, rather than passing "451" up to a
    UI that will render it as a generic failure;
  * set a User-Agent, which Yahoo requires and rejects the request without.

FAILURES ARE RETURNED, NOT RAISED
---------------------------------
`fetch_json` returns an `UpstreamResult` whose `data is None` means "no data".
Callers must handle that rather than receive a substituted value — the same rule
`llm/provider.py` follows, and for the same reason: this codebase has repeatedly
been bitten by a plausible-looking value standing in for a failed fetch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Yahoo rejects requests without a browser-ish User-Agent, and Binance rate-limits
# unidentified clients more aggressively. Same string the Next.js routes used, so
# upstream behaviour does not change with the move.
USER_AGENT = "Mozilla/5.0 (QUANT-terminal backend fetch)"

# Long enough for a slow exchange response, short enough that the Next.js proxy
# in front of this does not give up first. The proxy's own timeout is set higher
# on purpose — see lib/api/backendProxy.server.ts.
DEFAULT_TIMEOUT_S = 12.0

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """The shared client, created lazily.

    Lazy rather than at import time so importing this module never opens
    sockets — the test suite imports the whole backend and must not.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _client


async def close_client() -> None:
    """Release the pool. Called from main.py's lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# HTTP statuses that mean "this location is refused", as opposed to "this request
# was wrong". Worth naming because the operator response is completely different:
# a 451 is fixed by moving the caller, a 400 by fixing the call.
GEO_BLOCKED_STATUSES = frozenset({451, 403})


@dataclass
class UpstreamResult:
    """The outcome of one outbound call.

    `data is None` means no usable response. `error` is always populated when
    data is None, and is phrased for a human reading a dashboard, not for a log
    grep — the whole point of this layer is that "why is my chart empty" has an
    answer.
    """

    data: Optional[Any]
    status: Optional[int] = None
    error: Optional[str] = None
    geo_blocked: bool = False

    @property
    def ok(self) -> bool:
        return self.data is not None


def _describe_geo_block(url: str, status: int, body: str) -> str:
    host = httpx.URL(url).host
    return (
        f"{host} returned {status} — this location is refused by the provider, not a bad "
        f"request. The backend host's region is blocked. Upstream said: {body[:180]!r}"
    )


async def fetch_json(
    url: str,
    *,
    label: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    retries: int = 1,
    headers: Optional[Dict[str, str]] = None,
) -> UpstreamResult:
    """GET `url` and parse JSON, returning an UpstreamResult instead of raising.

    `retries` retries only what retrying can fix. A timeout or a connection
    error is retried; a 4xx is NOT — the same request will get the same answer,
    and retrying a 451 three times just makes the dashboard three times slower
    to report that the region is blocked. This mirrors the reasoning already in
    `services/market_data.fetch_prices`.
    """
    client = get_client()
    attempt = 0
    last_error = "no attempt was made"

    while attempt <= retries:
        try:
            response = await client.get(url, timeout=timeout, headers=headers)
        except httpx.TimeoutException:
            last_error = f"{label}: timed out after {timeout}s"
            logger.warning("%s (attempt %d/%d)", last_error, attempt + 1, retries + 1)
        except httpx.HTTPError as e:
            last_error = f"{label}: connection failed ({type(e).__name__}: {e})"
            logger.warning("%s (attempt %d/%d)", last_error, attempt + 1, retries + 1)
        else:
            if response.is_success:
                try:
                    return UpstreamResult(data=response.json(), status=response.status_code)
                except ValueError:
                    # A 200 whose body is not JSON is a provider problem, and it
                    # is worth distinguishing from a transport failure.
                    return UpstreamResult(
                        data=None,
                        status=response.status_code,
                        error=f"{label}: upstream returned {response.status_code} with a non-JSON body",
                    )

            body = response.text[:300]
            if response.status_code in GEO_BLOCKED_STATUSES:
                message = _describe_geo_block(url, response.status_code, body)
                logger.error("%s — %s", label, message)
                return UpstreamResult(
                    data=None,
                    status=response.status_code,
                    error=message,
                    geo_blocked=True,
                )

            # Any other non-2xx: report and stop. Retrying will not change it.
            return UpstreamResult(
                data=None,
                status=response.status_code,
                error=f"{label}: upstream returned {response.status_code}: {body!r}",
            )

        attempt += 1
        if attempt <= retries:
            await asyncio.sleep(2 ** attempt * 0.25)

    return UpstreamResult(data=None, error=last_error)


async def fetch_text(url: str, *, label: str, timeout: float = DEFAULT_TIMEOUT_S,
                     headers: Optional[Dict[str, str]] = None) -> UpstreamResult:
    """Same contract as `fetch_json`, for RSS and other non-JSON upstreams."""
    client = get_client()
    try:
        response = await client.get(url, timeout=timeout, headers=headers)
    except httpx.TimeoutException:
        return UpstreamResult(data=None, error=f"{label}: timed out after {timeout}s")
    except httpx.HTTPError as e:
        return UpstreamResult(data=None, error=f"{label}: connection failed ({type(e).__name__}: {e})")

    if response.is_success:
        return UpstreamResult(data=response.text, status=response.status_code)

    body = response.text[:300]
    if response.status_code in GEO_BLOCKED_STATUSES:
        message = _describe_geo_block(url, response.status_code, body)
        logger.error("%s — %s", label, message)
        return UpstreamResult(data=None, status=response.status_code, error=message, geo_blocked=True)

    return UpstreamResult(
        data=None,
        status=response.status_code,
        error=f"{label}: upstream returned {response.status_code}: {body!r}",
    )
