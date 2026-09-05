"""The LLM rate limiter — why the learning center kept showing the same lesson.

THE BUG IT FIXES
================
`budget.py` caps calls PER RUN. Nothing capped them across runs, so a busy minute
blew past NVIDIA's free 40/min ceiling, the API returned 429, `complete()`
correctly returned text=None, and the reflection node fell back to its
deterministic floor — the exact string the operator kept seeing:

    "Check if losses cluster in this regime before changing weighting."

The learning looked shallow; the model was simply never reached. This limiter
WAITS for a slot instead of failing, which is what the operator asked for.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.llm.rate_limit import (
    AsyncRateLimiter,
    get_rate_limiter,
    parse_retry_after,
    reset_rate_limiters,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_rate_limiters()
    yield
    reset_rate_limiters()


@pytest.mark.asyncio
async def test_calls_under_the_limit_never_wait():
    limiter = AsyncRateLimiter(rpm=40)
    waited = 0.0
    for _ in range(40):
        waited += await limiter.acquire()
    assert waited == 0.0


@pytest.mark.asyncio
async def test_the_call_that_would_exceed_the_limit_WAITS(monkeypatch):
    """The keystone behaviour. The 3rd call in a window of 2 must block until the
    oldest ages out — verified against a mocked clock so the test is instant."""
    limiter = AsyncRateLimiter(rpm=2)

    clock = {"t": 1000.0}
    sleeps: list = []
    monkeypatch.setattr(limiter, "_now", lambda: clock["t"])

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds  # advancing the clock ages the window

    monkeypatch.setattr("backend.llm.rate_limit.asyncio.sleep", _fake_sleep)

    await limiter.acquire()  # slot 1 at t=1000
    await limiter.acquire()  # slot 2 at t=1000
    waited = await limiter.acquire()  # must wait ~60s for slot 1 to age out

    assert waited > 0, "the third call in a 2/min window must have waited"
    assert sleeps, "it should have slept at least once"
    # It waited about one window (60s), not forever.
    assert 59 <= waited <= 66


@pytest.mark.asyncio
async def test_a_non_positive_rate_is_treated_as_unlimited_not_a_deadlock():
    """A misconfigured rpm=0 must not block every LLM call forever — a hung
    reasoning layer is the exact failure this module exists to prevent."""
    limiter = AsyncRateLimiter(rpm=0)
    for _ in range(100):
        assert await limiter.acquire() == 0.0


@pytest.mark.asyncio
async def test_one_bucket_per_api_key_so_main_and_panel_share_it():
    """The main provider and the consultation panel use the same NVIDIA key. The
    40/min ceiling is the KEY's, so both must draw from ONE limiter or they spend
    80/min between them and NVIDIA throttles anyway."""
    a = get_rate_limiter(api_key="nvapi-SAME", provider_id="nvidia")
    b = get_rate_limiter(api_key="nvapi-SAME", provider_id="nvidia")
    assert a is b

    c = get_rate_limiter(api_key="nvapi-OTHER", provider_id="nvidia")
    assert c is not a


@pytest.mark.asyncio
async def test_keyless_endpoints_bucket_by_provider():
    a = get_rate_limiter(api_key=None, provider_id="ollama")
    b = get_rate_limiter(api_key=None, provider_id="ollama")
    assert a is b


def test_the_api_key_is_never_stored_in_the_clear():
    """This dict is long-lived and module-global; a raw key in it is exactly what
    ends up in a log or a repr."""
    get_rate_limiter(api_key="nvapi-SECRET-VALUE", provider_id="nvidia")
    from backend.llm import rate_limit

    for key in rate_limit._limiters:
        assert "SECRET" not in key
        assert "nvapi-SECRET-VALUE" not in key


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


def test_retry_after_parses_plain_seconds():
    assert parse_retry_after("30") == 30.0


def test_retry_after_is_bounded():
    """A server asking us to wait ten minutes is one we fail against rather than
    hang a task on."""
    assert parse_retry_after("600") <= 66.0


def test_a_missing_or_garbage_retry_after_falls_back_not_crashes():
    assert parse_retry_after(None) > 0
    assert parse_retry_after("not-a-number") > 0
    assert parse_retry_after("") > 0


def test_retry_after_never_negative():
    # An HTTP date already in the past.
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") >= 0.0


# ---------------------------------------------------------------------------
# The provider actually calls the limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_provider_acquires_a_slot_before_the_request(monkeypatch):
    """Wiring, not just the limiter in isolation: a limiter nothing calls is inert."""
    from backend.llm.provider import ModelTier, OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="nvapi-TESTKEY",
        models={ModelTier.REASONING: "openai/gpt-oss-120b"},
    )

    acquired = {"n": 0}
    real_limiter = get_rate_limiter(api_key="nvapi-TESTKEY", provider_id="nvidia")

    async def _counting_acquire():
        acquired["n"] += 1
        return 0.0

    monkeypatch.setattr(real_limiter, "acquire", _counting_acquire)

    # Stop the actual HTTP call — we only care that acquire ran first.
    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        headers: dict = {}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    result = await provider.complete(
        system="s", user="u", tier=ModelTier.REASONING, max_tokens=100,
    )
    assert result.ok
    assert acquired["n"] == 1, "the provider must acquire a rate-limit slot before posting"
