"""Shared test fixtures, plus a network guard.

WHY THE NETWORK GUARD EXISTS
----------------------------
A test in `test_portfolio_controls.py` monkeypatched the portfolio store but
not `fetch_klines`, so `CIOAgent._returns_for` called the real ccxt client.
`services/market_data.fetch_klines` retries with exponential backoff
(`MAX_RETRIES` attempts, sleeping 1s, 2s, 4s), and this environment has no
route to the exchange APIs — so the suite went from 2 seconds to a 7-minute
timeout with no indication of why.

A hang is the worst failure mode for a test suite: it looks like an infra
problem rather than a bug in the test. This fixture converts an accidental
network call into an immediate, named failure.

It is autouse and applies to every test. A test that genuinely needs to reach
the network must ask for the `allow_network` fixture explicitly, which makes
that dependency visible in the test's signature rather than hidden in its call
graph.
"""

import socket
from typing import Any, Dict, List

import pytest


class _BlockedNetwork(RuntimeError):
    pass


# Loopback must stay open. On Windows, asyncio's ProactorEventLoop builds its
# internal self-pipe with `socket.socketpair()`, which falls back to a real
# TCP connection to 127.0.0.1 — so blocking every connect breaks the event
# loop itself and every async test errors during setup rather than running.
# Only non-loopback destinations are of interest here anyway: the bug this
# guards against is a test reaching api.binance.com.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        return str(address[0]) in _LOOPBACK_HOSTS
    # AF_UNIX paths and anything unrecognised: treat as local, since the
    # failure mode being prevented is specifically a remote HTTP call.
    return True


def _guarded(original):
    def wrapper(self_or_addr, *args, **kwargs):
        # socket.socket.connect(self, address) vs socket.create_connection(address)
        if isinstance(self_or_addr, socket.socket):
            address = args[0] if args else None
            if not _is_loopback(address):
                raise _BlockedNetwork(
                    f"This test attempted a real network connection to {address}. Tests must "
                    f"stub their data sources — see tests/conftest.py. A real call retries with "
                    f"exponential backoff against an unreachable host, which hangs the suite "
                    f"instead of failing it. If a network call is genuinely intended, request "
                    f"the `allow_network` fixture."
                )
            return original(self_or_addr, *args, **kwargs)
        if not _is_loopback(self_or_addr):
            raise _BlockedNetwork(
                f"This test attempted a real network connection to {self_or_addr}. "
                f"See tests/conftest.py."
            )
        return original(self_or_addr, *args, **kwargs)

    return wrapper


@pytest.fixture(autouse=True)
def block_network(request, monkeypatch):
    """Fail fast on a real (non-loopback) network call.

    Covers two layers, because the socket layer alone is not enough:

    1. `socket.socket.connect` / `connect_ex` / `create_connection` — catches
       synchronous and ccxt-style calls.
    2. `httpx.AsyncClient.request` — async httpx on Windows goes through the
       proactor event loop's overlapped IO rather than `socket.connect`, so the
       socket patches never see it. A test asserting that a macro fetch
       degrades gracefully silently made a REAL request to api.alternative.me
       and got a live Fear & Greed value back, which is how this gap was found.
    """
    if "allow_network" in request.fixturenames:
        return
    monkeypatch.setattr(socket.socket, "connect", _guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded(socket.socket.connect_ex))
    monkeypatch.setattr(socket, "create_connection", _guarded(socket.create_connection))

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a declared dependency
        return

    original_request = httpx.AsyncClient.request

    async def _blocked_request(self, method, url, *args, **kwargs):
        # LOOPBACK IS ALLOWED HERE, matching the socket layer above.
        #
        # This guard used to block EVERY httpx request, including 127.0.0.1,
        # while the socket layer three functions up deliberately allowed
        # loopback. That inconsistency meant a test could not stand up a local
        # fake server and exercise real HTTP against it — the only way to test
        # an HTTP adapter's status handling, JSON parsing and timeout behaviour
        # without mocking the very layer under test.
        #
        # The bug this fixture exists to prevent is unchanged and still caught:
        # a test reaching api.binance.com. A test reaching a server it started
        # itself on loopback is not that bug.
        if str(url).split("//")[-1].split("/")[0].split(":")[0] in _LOOPBACK_HOSTS:
            return await original_request(self, method, url, *args, **kwargs)

        raise _BlockedNetwork(
            f"This test attempted a real HTTP request: {method} {url}. Stub the client or the "
            f"function under test — see tests/conftest.py. Request `allow_network` if a live "
            f"call is genuinely intended."
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", _blocked_request)


@pytest.fixture
def allow_network():
    """Opt out of the network block. Presence in a signature is the point."""
    return True


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def make_candles(
    n: int = 120,
    base: float = 100.0,
    drift: float = 0.1,
    spread: float = 2.0,
    volume: float = 1000.0,
) -> List[Dict[str, Any]]:
    """Synthetic OHLCV with a genuine high/low range so ATR is non-zero.

    `drift` per candle gives a deterministic trend; a flat series would make
    every correlation and ATR calculation degenerate, which is a different test
    case (and one the code deliberately reports as unmeasurable).
    """
    out = []
    for i in range(n):
        close = base + i * drift
        out.append(
            {
                "openTime": i * 900_000,
                "open": close - drift,
                "high": close + spread,
                "low": close - spread,
                "close": close,
                "volume": volume,
            }
        )
    return out


def make_correlated_candles(n: int = 120, base: float = 50.0, sign: float = 1.0) -> List[Dict[str, Any]]:
    """A series whose returns correlate +1 (sign=1) or -1 (sign=-1) with
    `make_candles()`'s returns, for exercising the correlation threshold."""
    out = []
    for i in range(n):
        close = base + sign * i * 0.05
        out.append(
            {
                "openTime": i * 900_000,
                "open": close - sign * 0.05,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 500.0,
            }
        )
    return out


@pytest.fixture
def candles():
    return make_candles()


# ---------------------------------------------------------------------------
# The test suite must not read the operator's real LLM configuration
# ---------------------------------------------------------------------------
#
# THE FAILURE THIS PREVENTS, WHICH ACTUALLY HAPPENED
# --------------------------------------------------
# `backend/llm/provider.get_provider()` reads `LLM_PROVIDER` / `LLM_API_KEY` /
# `LLM_MODEL` from the process environment, and `.env` is loaded at import. While
# this project had no model configured, every test that called `reset_provider()`
# and expected `NullProvider` passed for the wrong reason: there was nothing to
# configure, not because the test had isolated anything.
#
# The moment a real NVIDIA key went into `.env`, two tests started building a live
# provider and issuing real HTTP requests to integrate.api.nvidia.com:
#
#     tests/test_graph_contracts.py::test_no_provider_is_configured_by_default
#     tests/test_opportunity_graph.py::test_an_unconfigured_provider_degrades_...
#
# The network guard above caught them, which is the only reason this surfaced as
# a failure rather than as a test suite that quietly bills an API and passes or
# fails depending on whose machine it runs on.
#
# `test_llm_provider.py` already had exactly this fixture locally. Its being
# local was the bug: provider configuration is global state, so the isolation has
# to be global too. That file keeps its own copy — it is harmless, and the
# reasoning belongs next to the tests that deliberately set these variables.
#
# A test that WANTS a configured provider still gets one: `monkeypatch.setenv`
# inside the test runs after this fixture, and `set_provider()` bypasses the
# environment entirely.
# ---------------------------------------------------------------------------
# The tradeable universe must not leak into unrelated tests
# ---------------------------------------------------------------------------
#
# `tradeable_universe` defaults to blocking BTC/USDT, which is the operator's
# preference and not a property of the risk gateway. Most fixtures in this suite
# use BTC/USDT as a generic symbol, so without this every one of them would start
# asserting against an instrument refusal instead of the thing it was written to
# test — and worse, would start PASSING again if the operator later changed their
# mind about BTC.
#
# Empty means "block nothing", which `blocked_symbols` honours as distinct from
# the variable being absent. Tests that are ABOUT the universe set it themselves.
@pytest.fixture(autouse=True)
def isolate_tradeable_universe(monkeypatch):
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "")


@pytest.fixture(autouse=True)
def isolate_llm_configuration(monkeypatch):
    from backend.llm.provider import reset_provider

    for var in (
        "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
        "LLM_MODEL_MECHANICAL", "LLM_MODEL_NARRATIVE", "LLM_MODEL_REASONING",
        "OPENAI_API_KEY",
        # The consultation panel reads its own variables and would otherwise let
        # a configured second opinion reach the network from a test too.
        "LLM_CONSULT_PANEL",
    ):
        monkeypatch.delenv(var, raising=False)

    # Reset BOTH SIDES of the singleton. Clearing the environment does nothing if
    # an earlier test already cached a live provider — which is precisely how
    # `test_no_provider_is_configured_by_default` passed alone and failed in a
    # full run.
    reset_provider()
    yield
    reset_provider()


# ---------------------------------------------------------------------------
# The test suite must not reach the operator's real database, or inherit
# another test's agent singletons
# ---------------------------------------------------------------------------
#
# WHY BOTH OF THESE BECAME NECESSARY AT THE SAME MOMENT
# ------------------------------------------------------
# For most of this project's life `DATABASE_URL` was wrong, so `init_db()` failed
# and `get_db_pool()` returned None everywhere. `test_position_persistence.py`
# says so in its own docstring: "There is no Postgres in this suite". Every test
# that touches storage passes a fake pool, and the ones that do not were relying
# on a real connection being IMPOSSIBLE rather than on being isolated.
#
# That stopped being true the moment the working credentials were restored. A
# single test that starts the app (a TestClient triggers the lifespan, which
# calls `init_db`) would now connect to the operator's live trading database and
# apply the schema to it. That is the same class of accident as the LLM guard
# above, where two tests began issuing real HTTP requests the day a real key
# landed in `.env` — and the consequences here are worse than a billed token.
#
# Pointing DATABASE_URL at an unroutable address makes the failure mode the one
# every storage path is already written and tested against: no pool, and a
# stated reason.
#
# The singleton reset is the second half. `get_position_monitor` and
# `get_execution_agent` used to construct a NEW agent per call — which was a bug,
# and is now fixed — but the fix means one test's open positions would otherwise
# be the next test's starting book.
@pytest.fixture(autouse=True)
def isolate_backend_state(monkeypatch):
    # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): reserved for documentation and
    # guaranteed not to route. Chosen over localhost-with-a-bad-port so a
    # developer running Postgres on a non-default port cannot be reached either.
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://tests:tests@192.0.2.1:5432/does-not-exist"
    )

    from backend.agents.execution_agent import reset_execution_agent
    from backend.agents.position_monitor import reset_position_monitor

    reset_position_monitor()
    reset_execution_agent()
    yield
    reset_position_monitor()
    reset_execution_agent()


# ---------------------------------------------------------------------------
# The test suite must not reach the operator's real database or agent singletons
# ---------------------------------------------------------------------------
#
# WHY THIS APPEARED ONLY NOW
# --------------------------
# Until the working DATABASE_URL was recovered, `init_db()` always failed and
# `get_db_pool()` returned None for every test — so "there is no Postgres in this
# suite" (tests/test_position_persistence.py says exactly that) was true by
# accident rather than by design. The moment the credentials were fixed, any test
# that drives the FastAPI app through its lifespan would connect to the
# operator's live trading database and write to it.
#
# That is the same shape as the LLM-config leak above: a suite that passed for
# the wrong reason, one config change away from doing real damage. Tests that
# genuinely want a pool already inject a fake one with `monkeypatch.setattr(...,
# get_db_pool, ...)`, which is unaffected by this.
#
# The URL is pointed at a port nothing listens on rather than deleted, because
# `settings.DATABASE_URL` has a non-empty default and code paths that build a DSN
# string should still get a well-formed one.
@pytest.fixture(autouse=True)
def isolate_database(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://test:test@127.0.0.1:1/tradingos_test_never_reachable",
    )
    yield


# The agent accessors are process-wide singletons (`get_position_monitor`,
# `get_execution_agent`). Without this, one test's open positions become the next
# test's starting book — and the failure would show up as an unrelated assertion
# about a position nobody opened.
@pytest.fixture(autouse=True)
def isolate_agent_singletons():
    from backend.agents.execution_agent import reset_execution_agent
    from backend.agents.position_monitor import reset_position_monitor

    reset_position_monitor()
    reset_execution_agent()
    yield
    reset_position_monitor()
    reset_execution_agent()
