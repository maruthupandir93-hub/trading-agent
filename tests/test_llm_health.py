"""LLM health tracking — the number that would have caught the silent degradation.

The reasoning layer once degraded for an unknown period (a dead model + rate
limiting + a thin prompt) and the only reason it was found is a human noticing the
lessons all read the same. Fail-closed is correct, but "how often are LLM calls
failing, and why" needs to be a metric, not a thing someone reads in prose. This
pins that metric.
"""

from __future__ import annotations

import pytest

from backend.llm.health import (
    LLMHealthTracker,
    classify,
    get_health_tracker,
    llm_health_status,
    reset_health_tracker,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_health_tracker()
    yield
    reset_health_tracker()


# ---------------------------------------------------------------------------
# Classification — each class maps to a different operator response
# ---------------------------------------------------------------------------


def test_a_410_is_classified_as_model_eol():
    """The gpt-oss-120b case: not slow, not throttled — GONE."""
    assert classify(410, "HTTP 410: Gone") == "model_eol"


def test_a_404_is_model_not_found():
    assert classify(404, "not found for account") == "model_not_found"


def test_a_429_is_rate_limited():
    assert classify(429, "Too Many Requests") == "rate_limited"


def test_auth_and_timeout_and_empty_are_distinct():
    assert classify(401, "bad key") == "auth"
    assert classify(None, "timed out", timed_out=True) == "timeout"
    assert classify(None, "empty", empty=True) == "empty_completion"


# ---------------------------------------------------------------------------
# The fallback rate
# ---------------------------------------------------------------------------


def test_all_successes_is_zero_fallback_and_healthy():
    t = LLMHealthTracker()
    for _ in range(10):
        t.record(model="m", tier="reasoning", ok=True)
    snap = t.snapshot()
    assert snap["fallbackRatePct"] == 0.0
    assert snap["healthy"] is True


def test_the_fallback_rate_is_failures_over_total():
    t = LLMHealthTracker()
    for _ in range(6):
        t.record(model="m", tier="reasoning", ok=True)
    for _ in range(4):
        t.record(model="m", tier="reasoning", ok=False, error_class="rate_limited", error="429")
    snap = t.snapshot()
    assert snap["fallbackRatePct"] == 40.0
    assert snap["byFailureClass"] == {"rate_limited": 4}


def test_a_mostly_failing_layer_reports_unhealthy():
    t = LLMHealthTracker()
    for _ in range(8):
        t.record(model="m", tier="reasoning", ok=False, error_class="timeout", error="slow")
    for _ in range(2):
        t.record(model="m", tier="reasoning", ok=True)
    snap = t.snapshot()
    assert snap["fallbackRatePct"] == 80.0
    assert snap["healthy"] is False


# ---------------------------------------------------------------------------
# Dead-model detection — the audit's §4.3, derived from real calls
# ---------------------------------------------------------------------------


def test_a_dead_model_is_surfaced_after_repeated_eol():
    """This is the gpt-oss-120b failure, made visible. Two EOL responses is
    enough to distinguish 'gone' from a one-off routing blip."""
    t = LLMHealthTracker()
    t.record(model="openai/gpt-oss-120b", tier="narrative", ok=False,
             error_class="model_eol", error="HTTP 410: has reached its end of life")
    t.record(model="openai/gpt-oss-120b", tier="narrative", ok=False,
             error_class="model_eol", error="HTTP 410: has reached its end of life")

    snap = t.snapshot()
    dead = snap["deadModels"]
    assert len(dead) == 1
    assert dead[0]["model"] == "openai/gpt-oss-120b"
    assert dead[0]["occurrences"] == 2
    assert snap["healthy"] is False
    assert "DEAD" in snap["note"]
    assert "openai/gpt-oss-120b" in snap["note"]


def test_a_single_404_does_not_cry_wolf():
    """One fluke must not flag a model dead — a transient 404 happens."""
    t = LLMHealthTracker()
    t.record(model="some/model", tier="reasoning", ok=False,
             error_class="model_not_found", error="404")
    for _ in range(5):
        t.record(model="some/model", tier="reasoning", ok=True)
    assert t.snapshot()["deadModels"] == []


def test_recovery_pushes_old_failures_out_of_the_window():
    """A model fixed an hour ago should stop showing as failing once healthy
    calls accumulate — the window is 'recent behaviour', not all-time."""
    t = LLMHealthTracker()
    # A burst of failures, then sustained success beyond the point they matter.
    for _ in range(5):
        t.record(model="m", tier="reasoning", ok=False, error_class="rate_limited", error="429")
    for _ in range(95):
        t.record(model="m", tier="reasoning", ok=True)
    snap = t.snapshot()
    assert snap["fallbackRatePct"] == 5.0
    assert snap["healthy"] is True


# ---------------------------------------------------------------------------
# Empty state and wiring
# ---------------------------------------------------------------------------


def test_no_calls_yet_is_healthy_not_a_fault():
    """Zero calls is a quiet market or an unconfigured provider, not an outage."""
    snap = llm_health_status()
    assert snap["callsTracked"] == 0
    assert snap["healthy"] is True
    assert "not a fault" in snap["note"]


@pytest.mark.asyncio
async def test_the_provider_records_a_failure_into_health(monkeypatch):
    """Wiring: a real 410 from the provider must reach the tracker as model_eol."""
    from backend.llm.provider import ModelTier, OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="k",
        models={ModelTier.NARRATIVE: "openai/gpt-oss-120b"},
    )

    class _Resp:
        status_code = 410
        text = '{"detail":"end of life"}'
        headers: dict = {}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    result = await provider.complete(system="s", user="u", tier=ModelTier.NARRATIVE, max_tokens=100)
    assert result.ok is False

    snap = get_health_tracker().snapshot()
    assert snap["callsTracked"] == 1
    assert snap["byFailureClass"].get("model_eol") == 1
