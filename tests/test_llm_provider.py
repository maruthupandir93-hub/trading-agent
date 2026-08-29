"""The LLM adapter — the one that turns `NullProvider` into a working model call.

WHAT THIS UNBLOCKS
------------------
`backend/llm/provider.py` shipped with the interface, the model tiering and the
fail-closed contract, but no concrete adapter — `NullProvider` was the default, so
every LLM node reported itself unavailable. That was correct behaviour and it was
also the single thing standing between this system and using a model at all.

WHAT IT DELIBERATELY DOES NOT CHANGE
------------------------------------
24 of the 25 registered graph nodes are `deterministic=True` and may not call a
model. Exactly one — `trade_thesis_narrative` — is `may_call_llm=True`. Wiring a
provider does NOT hand decisions to an LLM; it lets the system EXPLAIN a decision
it already computed, and ask for a second opinion when uncertainty is high.

`tests/test_graph_contracts.py` is what keeps that true. These tests cover the
adapter's own contract.

THE MOST IMPORTANT TESTS HERE ARE THE FAILURE ONES. A provider that returns
plausible text on failure is worse than one that returns nothing, and prose is
the hardest fabricated output to notice — a made-up funding rate looks like a
number, a made-up trade rationale looks like reasoning.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.llm.provider import (
    DEFAULT_TEMPERATURE,
    ModelTier,
    NullProvider,
    OpenAICompatibleProvider,
    get_provider,
    provider_status,
    reset_provider,
)

# ---------------------------------------------------------------------------
# A fake OpenAI-compatible server
#
# Real HTTP on loopback, not a monkeypatched httpx. The adapter's job IS the HTTP
# conversation — status handling, JSON shape, empty completions — and a mocked
# client would let a bug in exactly that layer pass. conftest.py's network guard
# allows loopback for this reason.
# ---------------------------------------------------------------------------

_MODE = {"value": "ok"}
_LAST_REQUEST = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        _LAST_REQUEST.clear()
        _LAST_REQUEST.update(json.loads(self.rfile.read(length) or b"{}"))
        _LAST_REQUEST["_auth"] = self.headers.get("Authorization")

        mode = _MODE["value"]

        if mode == "non_json":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not json</html>")
            return

        if mode == "ok":
            payload, code = {
                "model": _LAST_REQUEST.get("model"),
                "choices": [{"message": {"content": "  Trend bullish; funding neutral.  "},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 18},
            }, 200
        elif mode == "empty":
            payload, code = {
                "choices": [{"message": {"content": "   "}, "finish_reason": "length"}]
            }, 200
        elif mode == "no_choices":
            payload, code = {"choices": []}, 200
        elif mode == "unauthorized":
            payload, code = {"error": {"message": "invalid api key"}}, 401
        elif mode == "rate_limited":
            payload, code = {"error": {"message": "slow down"}}, 429
        else:  # pragma: no cover - defensive
            payload, code = {"error": "unknown test mode"}, 500

        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def provider(fake_server):
    return OpenAICompatibleProvider(
        provider_id="test",
        base_url=fake_server,
        api_key="test-key",
        models={
            ModelTier.MECHANICAL: "small-model",
            ModelTier.NARRATIVE: "mid-model",
            ModelTier.REASONING: "big-model",
        },
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_successful_call_returns_text_tokens_and_latency(provider):
    _MODE["value"] = "ok"
    result = await provider.complete(system="s", user="u", tier=ModelTier.NARRATIVE)

    assert result.ok
    # Stripped: the fake server pads with spaces, and a rationale rendered into a
    # decision record should not carry them.
    assert result.text == "Trend bullish; funding neutral."
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 18
    assert result.total_tokens == 138
    # Latency is a first-class metric alongside token cost (Section 39.6).
    assert result.latency_ms is not None and result.latency_ms >= 0


async def test_the_tier_selects_the_model(provider):
    _MODE["value"] = "ok"

    await provider.complete(system="s", user="u", tier=ModelTier.MECHANICAL)
    assert _LAST_REQUEST["model"] == "small-model"

    await provider.complete(system="s", user="u", tier=ModelTier.REASONING)
    assert _LAST_REQUEST["model"] == "big-model"


async def test_temperature_defaults_to_zero_and_streaming_is_off(provider):
    """Two runs over identical state must produce the same rationale.

    A non-zero temperature would make decisions incomparable across runs, which
    is the whole reason DEFAULT_TEMPERATURE is 0.0. Streaming is off because
    every caller awaits a complete answer before writing it into state.
    """
    _MODE["value"] = "ok"
    await provider.complete(system="s", user="u", tier=ModelTier.NARRATIVE)

    assert _LAST_REQUEST["temperature"] == DEFAULT_TEMPERATURE == 0.0
    assert _LAST_REQUEST["stream"] is False
    assert _LAST_REQUEST["_auth"] == "Bearer test-key"


async def test_max_tokens_is_clamped_per_tier(provider):
    """A mechanical node asking for 4000 tokens is a bug in the node.

    Clamping surfaces it as truncated output rather than as a bill. Section 39.6:
    reserve the strongest model for judgment, use cheap models for mechanical work.
    """
    _MODE["value"] = "ok"

    await provider.complete(system="s", user="u", tier=ModelTier.MECHANICAL, max_tokens=4000)

    # Asserted against the CONSTANT, not against a literal. The caps were retuned
    # once already (reasoning models bill their scratchpad against max_tokens, so
    # the old 512 guaranteed an empty completion rather than a short one), and a
    # test hardcoding the old number fails for the tuning rather than for a
    # regression in the thing it is actually guarding: that a mechanical call
    # cannot escape its tier's ceiling.
    from backend.llm.provider import _TIER_MAX_TOKENS

    assert _LAST_REQUEST["max_tokens"] == _TIER_MAX_TOKENS[ModelTier.MECHANICAL]
    assert _LAST_REQUEST["max_tokens"] < 4000, "the caller's request must be clamped down"
    assert (
        _TIER_MAX_TOKENS[ModelTier.MECHANICAL]
        < _TIER_MAX_TOKENS[ModelTier.NARRATIVE]
        < _TIER_MAX_TOKENS[ModelTier.REASONING]
    ), "the tier ordering is the cost control; raising a floor must not invert it"

    # A request UNDER the cap is passed through untouched.
    await provider.complete(system="s", user="u", tier=ModelTier.NARRATIVE, max_tokens=400)
    assert _LAST_REQUEST["max_tokens"] == 400


# ---------------------------------------------------------------------------
# Failure — the tests that matter most
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected_in_error",
    [
        ("unauthorized", "401"),
        ("rate_limited", "429"),
        ("non_json", "non-JSON"),
        ("empty", "empty completion"),
        ("no_choices", "empty completion"),
    ],
)
async def test_every_failure_returns_no_text_and_a_reason(provider, mode, expected_in_error):
    """`text is None` on every failure, with the reason stated.

    There is no partial-success state and no fallback string. A caller that sees
    None must handle "no answer" rather than substitute one — the rule the module
    docstring lists four historical bugs to justify.
    """
    _MODE["value"] = mode
    result = await provider.complete(system="s", user="u", tier=ModelTier.NARRATIVE)

    assert result.ok is False
    assert result.text is None
    assert result.error and expected_in_error in result.error
    # The tier is echoed back even on failure, so a caller can report WHICH kind
    # of call it failed to make.
    assert result.tier is ModelTier.NARRATIVE


async def test_an_empty_completion_is_not_an_empty_answer(provider):
    """A whitespace-only completion is NO ANSWER.

    Returning "" would let a node write an empty rationale into the decision
    record and present it as reasoning that happened. The finish_reason is
    included because `length` (truncated) and `stop` (the model said nothing)
    are different problems.
    """
    _MODE["value"] = "empty"
    result = await provider.complete(system="s", user="u", tier=ModelTier.NARRATIVE)

    assert result.text is None
    assert "finish_reason=length" in result.error


async def test_an_unreachable_endpoint_does_not_raise():
    """A dead provider must degrade the reasoning layer, never crash a graph run.

    A raised exception here would propagate into a node and abort a run that had
    already done useful deterministic work.
    """
    dead = OpenAICompatibleProvider(
        provider_id="dead",
        # Port 1 is reserved and never listening.
        base_url="http://127.0.0.1:1/v1",
        api_key="k",
        models={ModelTier.NARRATIVE: "m"},
    )
    result = await dead.complete(system="s", user="u", tier=ModelTier.NARRATIVE)

    assert result.ok is False
    assert result.error and "dead:" in result.error


async def test_a_tier_with_no_model_is_refused_not_substituted(fake_server):
    """Using the cheap model where the reasoning model was asked for would quietly
    degrade exactly the decisions Section 39.6 says to protect."""
    partial = OpenAICompatibleProvider(
        provider_id="partial",
        base_url=fake_server,
        api_key="k",
        models={ModelTier.NARRATIVE: "mid-model"},  # no REASONING model
    )

    assert partial.available is True  # it can serve SOME tiers
    assert partial.model_for(ModelTier.REASONING) is None

    result = await partial.complete(system="s", user="u", tier=ModelTier.REASONING)
    assert result.ok is False
    assert "tier reasoning" in result.error


# ---------------------------------------------------------------------------
# Availability and configuration
# ---------------------------------------------------------------------------


def test_unavailable_without_a_key_when_one_is_required(fake_server):
    """Checked BEFORE a run starts, so a run needing a model fails at the boundary
    rather than producing half a decision and then finding nothing to reason with."""
    keyless = OpenAICompatibleProvider(
        provider_id="needs-key", base_url=fake_server, api_key=None,
        models={ModelTier.NARRATIVE: "m"}, needs_key=True,
    )
    assert keyless.available is False


def test_a_local_provider_needs_no_key(fake_server):
    """Ollama has no key. Requiring one would make the only zero-cost option the
    only one that cannot be configured."""
    local = OpenAICompatibleProvider(
        provider_id="ollama", base_url=fake_server, api_key=None,
        models={ModelTier.NARRATIVE: "llama3.1"}, needs_key=False,
    )
    assert local.available is True


def test_unavailable_with_no_models(fake_server):
    assert OpenAICompatibleProvider(
        provider_id="p", base_url=fake_server, api_key="k", models={},
    ).available is False


def test_describe_never_returns_the_api_key(fake_server):
    """A masked key still confirms its length and prefix, which is more than an
    operator needs to answer "is this configured?"."""
    p = OpenAICompatibleProvider(
        provider_id="p", base_url=fake_server, api_key="sk-super-secret-value",
        models={ModelTier.NARRATIVE: "m"},
    )
    rendered = json.dumps(p.describe())

    assert "sk-super-secret-value" not in rendered
    assert "secret" not in rendered
    assert p.describe()["apiKeyConfigured"] is True


# ---------------------------------------------------------------------------
# Selection via the environment
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The provider is a module-level singleton; leaking it across tests would
    make one test's configuration another test's starting state."""
    for var in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
                "LLM_MODEL_MECHANICAL", "LLM_MODEL_NARRATIVE", "LLM_MODEL_REASONING",
                "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    reset_provider()
    yield
    reset_provider()


def test_the_default_is_null_not_a_stub():
    """A stub returning placeholder text would let every LLM node "work" in
    development and produce fiction — which is how the DebateVisualizer once
    displayed invented reasoning as an agent's own."""
    assert isinstance(get_provider(), NullProvider)
    assert get_provider().available is False


def test_an_unknown_provider_degrades_instead_of_raising(monkeypatch, caplog):
    """A typo in an env var must not prevent the backend from starting.

    This process is the only thing enforcing stop-losses on open positions;
    refusing to boot over a misconfigured model would turn a cosmetic mistake
    into an unmonitored leveraged position.
    """
    monkeypatch.setenv("LLM_PROVIDER", "gpt5-turbo-ultra")
    with caplog.at_level("WARNING"):
        assert isinstance(get_provider(), NullProvider)
    assert "not a known provider" in caplog.text


def test_a_known_provider_resolves_its_base_url(monkeypatch):
    """The provider table mirrors lib/constants.ts so both halves of the app agree
    on what a provider name means."""
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    monkeypatch.setenv("LLM_API_KEY", "nvapi-x")
    monkeypatch.setenv("LLM_MODEL", "z-ai/glm-5.2")

    status = provider_status()
    assert status["provider"] == "nvidia"
    assert status["baseUrl"] == "https://integrate.api.nvidia.com/v1"
    assert status["available"] is True
    assert status["models"]["reasoning"] == "z-ai/glm-5.2"


def test_openai_api_key_is_accepted_as_a_fallback(monkeypatch):
    """That variable is already in this project's .env, and every supported vendor
    is OpenAI-compatible — so it is the key an operator will reach for."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("OPENAI_API_KEY", "gsk-x")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")

    assert provider_status()["available"] is True


def test_per_tier_overrides_beat_the_shared_model(monkeypatch):
    """One model for everything is the common case; per-tier variables are the
    optimisation. Requiring three to get started would make tiering an obstacle."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("LLM_MODEL_REASONING", "gpt-4o")

    models = provider_status()["models"]
    assert models["mechanical"] == "gpt-4o-mini"
    assert models["narrative"] == "gpt-4o-mini"
    assert models["reasoning"] == "gpt-4o"


def test_a_selected_but_unconfigured_provider_names_what_is_missing(monkeypatch, caplog):
    """"LLM nodes are unavailable" with no reason is the kind of log line that
    costs an hour."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with caplog.at_level("WARNING"):
        status = provider_status()

    assert status["available"] is False
    assert "LLM_API_KEY" in caplog.text
    assert "LLM_MODEL" in caplog.text


def test_custom_requires_an_explicit_base_url(monkeypatch, caplog):
    """No default is guessed for 'custom': a guessed URL produces a confusing
    connection error instead of a clear "you did not configure this"."""
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")

    with caplog.at_level("WARNING"):
        status = provider_status()

    assert status["available"] is False
    assert "LLM_BASE_URL" in caplog.text
