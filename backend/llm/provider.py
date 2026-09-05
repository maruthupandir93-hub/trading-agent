"""LLM provider — the prerequisite Phase 23 could not start without.

There is no LLM client anywhere in `backend/`. `api/ai.py::/reason` returns 501
with the reason stated, and `settings.OPENAI_API_KEY` is read by nothing. So
"build the LangGraph runtime" could not have been the first task: every LLM node
in Phases 24-50 would block on this.

IT FAILS CLOSED — THIS IS THE WHOLE POINT
-----------------------------------------
`complete()` returns `LLMResult` with `text=None` on any failure and never
raises into a node. It does not retry into a fabricated answer, and it has no
"fallback text" path.

That rule is not abstract here. This codebase has already had four separate
places where a failure produced a plausible-looking value:

  * `create_market_order` returned a fake filled order at $60,000 on any error
  * `fetch_macro_data` returned fng=50 / "Neutral" when the request failed
  * `compute_stop_loss_take_profit` invented `atr = price * 0.01`
  * `monte_carlo_simulation` reported `prob_of_ruin: 0.0` with no data

An LLM client with a fallback string would be the fifth and the worst, because
prose is the hardest kind of fabricated output to spot.

SECTION 39.6 — TIERED MODELS ARE A COST CONTROL, NOT A PREFERENCE
-----------------------------------------------------------------
    "A multi-agent debate graph with several specialist nodes plus a supervisor
     can consume tens of thousands of tokens per single decision cycle ...
     Reserve your strongest model for the Supervisor/debate/decision nodes where
     judgment actually matters; use smaller, cheaper models for mechanical nodes."

`ModelTier` makes that explicit at the call site, so a mechanical node cannot
quietly use the reasoning model.

PROVIDER-AGNOSTIC BY CONSTRUCTION
---------------------------------
The TypeScript half already supports a configurable provider plus a separate
second-opinion model, and spec Section 31 (Phase 48) requires consulting several
different models. So this is an interface with adapters, not a hardcoded vendor.
No adapter is wired yet — `NullProvider` is the default and it refuses honestly.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModelTier(str, Enum):
    """Which class of model a call needs. See Section 39.6.

    Declared per call so cost is a visible property of the node, not an
    accident of whichever model happened to be configured.
    """

    # Data validation, formatting, extraction. Cheap model.
    MECHANICAL = "mechanical"
    # Narration over already-computed evidence. Mid model.
    NARRATIVE = "narrative"
    # Supervisor, debate synthesis, research questions. Strongest model.
    REASONING = "reasoning"


@dataclass
class LLMResult:
    """The result of one completion.

    `text is None` means the call did not produce usable output. There is no
    other signal to check and no partial-success state: a caller that sees None
    must handle "no answer", not substitute one.
    """

    text: Optional[str]
    model: Optional[str] = None
    tier: Optional[ModelTier] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: Optional[float] = None
    # Present exactly when text is None. Surfaced so a node can report WHY it
    # had no answer rather than reporting that it found nothing.
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.text is not None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider(ABC):
    """Adapter interface. One implementation per vendor."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """False when unconfigured. Checked before a graph run starts so a run
        that needs a model fails at the boundary rather than mid-reasoning."""
        ...

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: ModelTier,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResult:
        """Never raises. Returns `LLMResult` with `text=None` on failure."""
        ...


class NullProvider(LLMProvider):
    """The default. Refuses every call, honestly.

    Not a stub that returns placeholder text — that would let every LLM node
    "work" in development and produce fiction, which is exactly how the
    DebateVisualizer ended up displaying invented reasoning as an agent's own.

    A graph configured with LLM nodes and this provider will have those nodes
    record `unavailable` and continue degraded. That is the correct behaviour
    until a real provider is configured.
    """

    @property
    def name(self) -> str:
        return "null"

    @property
    def available(self) -> bool:
        return False

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: ModelTier,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResult:
        return LLMResult(
            text=None,
            tier=tier,
            error=(
                "No LLM provider is configured. Set one up in backend/llm/provider.py "
                "and select it via LLM_PROVIDER. This provider deliberately returns no "
                "text rather than placeholder output."
            ),
        )


# Default temperature for every call in this system.
#
# 0.0, not a creative default. Every LLM node here narrates or synthesises over
# evidence that has already been computed; there is no task in the graph where
# variety is desirable, and a non-zero temperature makes two runs over identical
# state produce different rationales — which destroys the ability to compare
# decisions across runs.
DEFAULT_TEMPERATURE = 0.0


# ---------------------------------------------------------------------------
# The OpenAI-compatible adapter — one adapter, every vendor this project uses
# ---------------------------------------------------------------------------

# Mirrors `lib/constants.ts::PROVIDERS`, deliberately.
#
# The two halves of this application must agree on what "nvidia" means. The
# frontend already lets an operator pick a provider from that list for chat; if
# the backend understood a different set, the same word would select a different
# endpoint on each side and the difference would only show up as an
# authentication failure on one of them.
#
# `needs_key=False` for Ollama because a local server has no key, and requiring
# one would make the only zero-cost option the only one that cannot be
# configured.
_KNOWN_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "needs_key": True},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "needs_key": True},
    "xai": {"base_url": "https://api.x.ai/v1", "needs_key": True},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "needs_key": True},
    "openai": {"base_url": "https://api.openai.com/v1", "needs_key": True},
    "ollama": {"base_url": "http://localhost:11434/v1", "needs_key": False},
    # No default URL: a custom endpoint must supply LLM_BASE_URL, and guessing
    # one would produce a confusing connection error instead of a clear
    # "you did not configure this" message.
    "custom": {"base_url": "", "needs_key": True},
}

# Per-tier request ceilings. Section 39.6: "Reserve your strongest model for the
# Supervisor/debate/decision nodes where judgment actually matters; use smaller,
# cheaper models for mechanical nodes."
#
# The caller passes `max_tokens`; these are the CAPS that clamp it. A mechanical
# node asking for 4000 tokens is a bug in the node, and clamping surfaces it as
# truncated output rather than as a bill.
#
# MECHANICAL WAS RAISED FROM 512, AND NOT AS A LOOSENING OF THE COST CONTROL.
# Every model this project is now pointed at is a REASONING model: NVIDIA NIM
# returns `reasoning_content` alongside `content`, and the tokens spent in that
# scratchpad are billed against `max_tokens` before a single character of the
# answer is emitted. Measured on this account, `openai/gpt-oss-20b` spent 294
# completion tokens to produce a 498-character answer — most of it scratchpad.
# At 512 the scratchpad alone can consume the whole budget, the answer comes
# back empty with `finish_reason="length"`, and `complete()` correctly reports
# "no answer" — so the node degrades for a reason that looks like a model
# failure and is actually a budget we set. 1536 leaves room for the thinking.
_TIER_MAX_TOKENS: Dict[ModelTier, int] = {
    ModelTier.MECHANICAL: 2_048,
    ModelTier.NARRATIVE: 4_096,
    ModelTier.REASONING: 6_144,
}

# The numbers above were re-raised once more after they were first widened, and
# the reason is worth recording because it looks like the cost control being
# quietly abandoned and is not.
#
# At NARRATIVE=2560 a live run produced exactly the failure the diagnosis below
# was written to name:
#
#     thesis narrative (model call failed: nvidia: openai/gpt-oss-120b returned
#     an empty completion (finish_reason=length))
#
# The model spent the entire 2560-token budget in `reasoning_content` and emitted
# no answer. The tier ratio (mechanical < narrative < reasoning) is intact and
# still does its job; what changed is the FLOOR, because every model this project
# is configured against thinks before it writes and that thinking is billed
# first. A cap below the scratchpad is not a budget, it is a guaranteed failure
# that costs the tokens anyway and returns nothing for them.

# Read timeouts, PER TIER. This used to be one 90-second number for every call.
#
# WHY ONE NUMBER WAS WRONG, MEASURED RATHER THAN ARGUED
# -----------------------------------------------------
# Against this project's configured NVIDIA account on 2026-08-28, one prompt,
# `max_tokens=1536`:
#
#     moonshotai/kimi-k3      68,000-85,000 ms
#     openai/gpt-oss-20b           4,331 ms
#
# kimi-k3 needed 68 seconds to reply "OK". Under the single 90s ceiling a real
# analysis prompt lost that race every time, and a live 21-node run recorded
# exactly that:
#
#     "thesis narrative (model call failed: nvidia: timed out after 90.0s)"
#
# Every LLM node in the graph reported itself unavailable, which reads from the
# outside as "the agent ignores the model".
#
# Raising the single number to 300s instead would have been the wrong fix: the
# mechanical and narrative tiers run on the critical path of EVERY graph run, so
# a hung fast model would stall a run for five minutes while the trigger worker
# kept firing and queued more. The tier already declares how much judgment a call
# needs; it should declare how long that judgment is allowed to take.
_TIER_TIMEOUT_S: Dict[ModelTier, float] = {
    # Fast models on the critical path. Generous against the 4.3s measurement,
    # tight enough that a hung provider degrades one node rather than the run.
    ModelTier.MECHANICAL: 60.0,
    ModelTier.NARRATIVE: 90.0,
    # A slow reasoning model answering a hard question. 300s is ~3.5x the worst
    # kimi-k3 measurement above, which is headroom for a longer prompt rather
    # than an invitation to hang: a run reaching this has still failed, it has
    # just failed honestly instead of before the model finished thinking.
    ModelTier.REASONING: 300.0,
}
_DEFAULT_TIMEOUT_S = 90.0
_CONNECT_TIMEOUT_S = 15.0


def timeout_for(tier: ModelTier) -> float:
    """Read timeout for one tier. See `_TIER_TIMEOUT_S`."""
    return _TIER_TIMEOUT_S.get(tier, _DEFAULT_TIMEOUT_S)


# Room for a reasoning model's scratchpad, on top of whatever the caller wants
# the ANSWER to be. See `request_budget`.
#
# 1200 is sized from measurement, not guessed. The narrative node asked for 400
# tokens and the model came back with `finish_reason="length"` after emitting
# 1926 CHARACTERS of `reasoning_content` and no answer at all — roughly 500
# tokens of thinking for a request that had budgeted none. 1200 covers that with
# margin for a longer prompt, and the tier cap is still the hard ceiling above it.
_REASONING_HEADROOM_TOKENS = 1_200


def request_budget(answer_tokens: int) -> int:
    """Turn "how long should the ANSWER be" into "what should max_tokens be".

    WHY CALL SITES CANNOT JUST PASS THE ANSWER LENGTH ANY MORE
    ----------------------------------------------------------
    `max_tokens` is a ceiling on EVERYTHING the model emits, and every model this
    project is configured against emits a `reasoning_content` scratchpad first
    and bills it against that same ceiling. So a node asking for 400 tokens
    because it wants three to five sentences was not asking for a short answer —
    it was asking for the model to be cut off mid-thought and return nothing:

        thesis narrative (model call failed: openai/gpt-oss-120b returned an
        empty completion (finish_reason=length) — the model emitted 1926
        characters of reasoning_content but no answer)

    Observed live on 2026-08-28. The node had spent the tokens and got no prose.

    WHY THIS IS A FUNCTION AND NOT A SILENT ADJUSTMENT INSIDE `complete()`
    ---------------------------------------------------------------------
    Adding headroom invisibly would make `max_tokens` not mean max_tokens, and
    the next person reading a call site would have no way to know the number they
    wrote is not the number sent. Written here, each call site still states the
    length it actually wants and the arithmetic is in one documented place.

    The tier cap in `_TIER_MAX_TOKENS` still clamps the result, so this widens a
    budget but can never escape the cost control.
    """
    if answer_tokens <= 0:
        raise ValueError("answer_tokens must be positive")
    return answer_tokens + _REASONING_HEADROOM_TOKENS


class OpenAICompatibleProvider(LLMProvider):
    """One adapter for every `/v1/chat/completions` endpoint.

    WHY ONE ADAPTER AND NOT ONE PER VENDOR
    --------------------------------------
    NVIDIA NIM, Groq, xAI, DeepSeek, OpenAI, Ollama, vLLM, LM Studio, LiteLLM and
    text-generation-webui all speak the same request and response shape. A class
    per vendor would be six copies of one HTTP call differing only in a base URL
    — and six places for the fail-closed contract below to drift.

    Vendor differences that DO exist are handled as data (`_KNOWN_PROVIDERS`),
    not as code.

    IT FAILS CLOSED, AND THAT IS THE ENTIRE POINT OF THIS CLASS
    ----------------------------------------------------------
    `complete()` never raises and never invents text. On any failure — timeout,
    bad key, rate limit, malformed response — it returns `LLMResult(text=None)`
    with the reason in `error`. The calling node then records itself
    `unavailable` and the run continues on its deterministic nodes.

    This matters more here than in most applications. The module docstring lists
    four separate places in this codebase where a plausible-looking value stood
    in for a failed computation and was believed. Prose is the hardest of those
    to spot: a fabricated funding rate looks like a number, but a fabricated
    trade rationale looks like reasoning.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: Optional[str],
        models: Dict[ModelTier, str],
        needs_key: bool = True,
    ) -> None:
        self._provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._models = models
        self._needs_key = needs_key

    @property
    def name(self) -> str:
        return self._provider_id

    @property
    def available(self) -> bool:
        """Configured enough to be worth calling.

        Checked BEFORE a graph run starts, so a run that needs a model reports
        the problem at the boundary rather than producing half a decision and
        then discovering there is nothing to reason with.
        """
        if not self._base_url:
            return False
        if self._needs_key and not self._api_key:
            return False
        return bool(self._models)

    def model_for(self, tier: ModelTier) -> Optional[str]:
        return self._models.get(tier)

    def describe(self) -> Dict[str, Any]:
        """Configuration summary for the status API. Never returns the key.

        Reports whether a key is PRESENT, not what it is. A masked key still
        confirms its length and prefix, which is more than an operator needs to
        answer "is this configured?".
        """
        return {
            "provider": self._provider_id,
            "baseUrl": self._base_url or None,
            "available": self.available,
            "apiKeyConfigured": bool(self._api_key),
            "apiKeyRequired": self._needs_key,
            "models": {tier.value: model for tier, model in self._models.items()},
        }

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: ModelTier,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> LLMResult:
        import time

        import httpx

        model = self._models.get(tier)
        if not self.available or not model:
            return LLMResult(
                text=None,
                tier=tier,
                error=(
                    f"LLM provider '{self._provider_id}' is not usable: "
                    f"{'no base URL' if not self._base_url else ''}"
                    f"{'no API key' if self._needs_key and not self._api_key else ''}"
                    f"{f'no model configured for tier {tier.value}' if not model else ''}"
                ).strip(),
            )

        # Clamped, not trusted. See _TIER_MAX_TOKENS.
        capped = min(max_tokens, _TIER_MAX_TOKENS.get(tier, max_tokens))

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": capped,
            # Explicitly non-streaming. Every caller here awaits a complete
            # answer before writing it into state; streaming would add
            # complexity with nothing to show it to.
            "stream": False,
        }

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat/completions"
        read_timeout = timeout_for(tier)
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_S, read=read_timeout,
            write=30.0, pool=_CONNECT_TIMEOUT_S,
        )

        # RATE LIMIT, before the request leaves. The free NVIDIA tier is 40/min
        # per key, and blowing past it returns 429 — which used to fall straight
        # through to the deterministic floor and make the learning look shallow
        # when the model simply was not reached. This blocks until a slot is free
        # rather than failing; every LLM call here is off the trading critical
        # path (the trade is on the bus long before narration or reflection runs),
        # so a wait costs latency on prose, never on a fill. See rate_limit.py.
        from backend.llm.rate_limit import get_rate_limiter, parse_retry_after

        limiter = get_rate_limiter(api_key=self._api_key, provider_id=self._provider_id)

        started = time.monotonic()

        async def _post_once() -> "httpx.Response":
            await limiter.acquire()
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.post(url, json=payload, headers=headers)

        try:
            response = await _post_once()
            # ONE retry on 429, honouring Retry-After. The limiter keeps US under
            # the ceiling but cannot see other processes on the same key (a second
            # backend, a script, the frontend's chat), so a 429 can still arrive.
            # A single, header-timed retry is the difference between recovering
            # and a retry storm that keeps the key throttled.
            if response.status_code == 429:
                wait = parse_retry_after(response.headers.get("Retry-After"))
                logger.warning(
                    "LLM provider %s returned 429 (rate limited); waiting %.1fs and retrying once.",
                    self._provider_id, wait,
                )
                import asyncio as _asyncio

                await _asyncio.sleep(wait)
                response = await _post_once()
        except httpx.TimeoutException:
            elapsed = (time.monotonic() - started) * 1000
            # The MODEL is named in the message, not just the provider. "nvidia
            # timed out" sent an operator hunting for a network fault when the
            # real answer was that the tier was pointed at a 68-second reasoning
            # model behind a 90-second ceiling.
            logger.warning(
                "LLM call to %s (%s, tier=%s) timed out after %.0fms",
                self._provider_id, model, tier.value, elapsed,
            )
            return LLMResult(
                text=None, model=model, tier=tier, latency_ms=elapsed,
                error=(
                    f"{self._provider_id}: model {model} timed out after "
                    f"{read_timeout:.0f}s on the {tier.value} tier. Either the model "
                    f"is slower than this tier allows (see _TIER_TIMEOUT_S) or the "
                    f"endpoint is not responding."
                ),
            )
        except httpx.HTTPError as e:
            elapsed = (time.monotonic() - started) * 1000
            logger.warning("LLM call to %s failed: %s", self._provider_id, e)
            return LLMResult(
                text=None, model=model, tier=tier, latency_ms=elapsed,
                error=f"{self._provider_id}: {type(e).__name__}: {e}",
            )

        elapsed = (time.monotonic() - started) * 1000

        if response.status_code >= 400:
            body = response.text[:300]
            # Logged at ERROR for auth and at WARNING for rate limits, because
            # the operator response differs: a 401 needs a new key, a 429 needs
            # patience or a cheaper tier.
            if response.status_code in (401, 403):
                logger.error(
                    "LLM provider %s rejected the credential (HTTP %d). LLM nodes will "
                    "report unavailable until the key is fixed. Body: %s",
                    self._provider_id, response.status_code, body,
                )
            else:
                logger.warning(
                    "LLM provider %s returned HTTP %d: %s",
                    self._provider_id, response.status_code, body,
                )
            return LLMResult(
                text=None, model=model, tier=tier, latency_ms=elapsed,
                error=f"{self._provider_id}: HTTP {response.status_code}: {body}",
            )

        try:
            data = response.json()
        except ValueError:
            return LLMResult(
                text=None, model=model, tier=tier, latency_ms=elapsed,
                error=f"{self._provider_id}: returned a non-JSON body",
            )

        choices = data.get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        text = message.get("content")

        # An empty or whitespace-only completion is NO ANSWER, not an answer.
        # Returning "" would let a node write an empty rationale into the
        # decision record and present it as reasoning that happened.
        if not text or not text.strip():
            finish = (choices[0] or {}).get("finish_reason") if choices else None

            # REASONING MODELS FAIL THIS WAY SPECIFICALLY, AND THE GENERIC
            # MESSAGE SENT US THE WRONG WAY ONCE ALREADY.
            #
            # Every model this project is configured against returns
            # `reasoning_content` next to `content`, and the scratchpad is billed
            # against max_tokens BEFORE the answer starts. When the budget runs
            # out mid-thought the API returns finish_reason="length", a full
            # `reasoning_content` and an EMPTY `content` — a 200 OK that looks
            # like the model had nothing to say.
            #
            # `reasoning_content` is deliberately NOT used as the answer. It is a
            # scratchpad, not a conclusion, and this module exists to refuse
            # plausible-looking substitutes (see the four fabrications in the
            # module docstring). What it does instead is name the real cause so
            # the fix is "raise the tier's cap", not "the model is broken".
            reasoning = message.get("reasoning_content")
            if reasoning and str(reasoning).strip():
                detail = (
                    f" — the model emitted {len(str(reasoning))} characters of "
                    f"reasoning_content but no answer"
                    + (
                        f", and finish_reason={finish} means it ran out of tokens "
                        f"while still thinking (raise _TIER_MAX_TOKENS for the "
                        f"{tier.value} tier; it is currently {capped})"
                        if finish == "length" else
                        ". The scratchpad is NOT returned as the answer: it is "
                        "working-out, not a conclusion."
                    )
                )
            else:
                detail = ""

            return LLMResult(
                text=None, model=model, tier=tier, latency_ms=elapsed,
                error=(
                    f"{self._provider_id}: {model} returned an empty completion"
                    + (f" (finish_reason={finish})" if finish else "")
                    + detail
                ),
            )

        usage = data.get("usage") or {}
        return LLMResult(
            text=text.strip(),
            model=data.get("model") or model,
            tier=tier,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=elapsed,
        )


def _models_from_env(provider_id: str) -> Dict[ModelTier, str]:
    """Resolve one model per tier from the environment.

    `LLM_MODEL` sets all three; the per-tier variables override it. That ordering
    is deliberate: the common case is one model for everything, and forcing three
    variables to get started would make the tiering feel like an obstacle rather
    than an optimisation.

    A tier with no model resolves to nothing and `complete()` refuses that tier
    specifically, rather than silently substituting another tier's model — using
    the cheap model where the reasoning model was asked for would quietly degrade
    exactly the decisions Section 39.6 says to protect.
    """
    shared = (os.getenv("LLM_MODEL") or "").strip()
    per_tier = {
        ModelTier.MECHANICAL: (os.getenv("LLM_MODEL_MECHANICAL") or "").strip(),
        ModelTier.NARRATIVE: (os.getenv("LLM_MODEL_NARRATIVE") or "").strip(),
        ModelTier.REASONING: (os.getenv("LLM_MODEL_REASONING") or "").strip(),
    }
    resolved = {tier: (value or shared) for tier, value in per_tier.items()}
    return {tier: model for tier, model in resolved.items() if model}


_provider: Optional[LLMProvider] = None


def get_provider() -> LLMProvider:
    """The configured provider, or `NullProvider`.

    Selected by `LLM_PROVIDER`. Every failure path here returns NullProvider WITH
    A WARNING rather than raising, and that is a safety decision, not laziness: a
    typo in an env var must degrade the reasoning layer, never prevent the
    backend from starting. This process is the only thing enforcing stop-losses
    on open positions, and refusing to boot over a misconfigured model would turn
    a cosmetic mistake into an unmonitored leveraged position.

    Configuration:

        LLM_PROVIDER=nvidia|groq|xai|deepseek|openai|ollama|custom|null
        LLM_API_KEY=...            (falls back to OPENAI_API_KEY)
        LLM_MODEL=...              one model for every tier
        LLM_MODEL_REASONING=...    optional per-tier overrides
        LLM_BASE_URL=...           required for 'custom', overrides the default
    """
    global _provider
    if _provider is not None:
        return _provider

    choice = (os.getenv("LLM_PROVIDER") or "").strip().lower()

    if not choice or choice == "null":
        _provider = NullProvider()
        return _provider

    spec = _KNOWN_PROVIDERS.get(choice)
    if spec is None:
        logger.warning(
            "LLM_PROVIDER=%r is not a known provider, so no model is configured and "
            "LLM nodes will report themselves unavailable. Known values: %s.",
            choice, ", ".join(sorted(_KNOWN_PROVIDERS) + ["null"]),
        )
        _provider = NullProvider()
        return _provider

    base_url = (os.getenv("LLM_BASE_URL") or "").strip() or spec["base_url"]
    # `LLM_API_KEY` first, `OPENAI_API_KEY` second. The fallback exists because
    # that variable is already in this project's .env and every one of these
    # vendors is OpenAI-compatible, so it is the key an operator will reach for.
    api_key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    models = _models_from_env(choice)

    provider = OpenAICompatibleProvider(
        provider_id=choice,
        base_url=base_url,
        api_key=api_key,
        models=models,
        needs_key=bool(spec["needs_key"]),
    )

    if not provider.available:
        # Names exactly what is missing. "LLM nodes are unavailable" with no
        # reason is the kind of log line that costs an hour.
        missing = []
        if not base_url:
            missing.append("LLM_BASE_URL (required for provider 'custom')")
        if spec["needs_key"] and not api_key:
            missing.append("LLM_API_KEY (or OPENAI_API_KEY)")
        if not models:
            missing.append("LLM_MODEL (or the per-tier LLM_MODEL_* variables)")
        logger.warning(
            "LLM_PROVIDER=%s is selected but not usable — missing: %s. LLM nodes will "
            "report themselves unavailable and the deterministic nodes will run as "
            "normal.",
            choice, "; ".join(missing),
        )
    else:
        logger.info(
            "LLM provider ready: %s at %s (models: %s)",
            choice, base_url,
            ", ".join(f"{t.value}={m}" for t, m in sorted(models.items(), key=lambda kv: kv[0].value)),
        )

    _provider = provider
    return _provider


def build_consultation_panel() -> List[LLMProvider]:
    """The DISTINCT providers for Phase 48's second-opinion panel.

    Configured separately from the main provider, and that separation is the whole
    point. `services/ai_consultation.consult()` refuses to call itself a panel
    with fewer than two distinct providers, because asking one provider three
    times is one prior sampled repeatedly — and reporting that as multi-model
    consensus would manufacture agreement out of nothing.

    So there is deliberately NO fallback to `get_provider()`. An operator with one
    key gets an honest "single outside opinion", never a fake panel.

        LLM_CONSULT_PANEL=groq,deepseek
        LLM_CONSULT_KEY_GROQ=gsk-...
        LLM_CONSULT_MODEL_GROQ=llama-3.3-70b-versatile
        LLM_CONSULT_KEY_DEEPSEEK=sk-...
        LLM_CONSULT_MODEL_DEEPSEEK=deepseek-reasoner

    Unset means no consultation happens, which is the default and is fine: the
    consultation is advisory evidence, and its absence costs nothing a gate reads.

    A panel entry missing a key or model is SKIPPED WITH A WARNING rather than
    silently dropped — a two-name panel that quietly became one would still be
    reported as a panel by name while being one opinion in fact.
    """
    spec = (os.getenv("LLM_CONSULT_PANEL") or "").strip()
    if not spec:
        return []

    panel: List[LLMProvider] = []
    seen: set = set()

    for raw in spec.split(","):
        provider_id = raw.strip().lower()
        if not provider_id or provider_id in seen:
            continue
        seen.add(provider_id)

        known = _KNOWN_PROVIDERS.get(provider_id)
        if known is None:
            logger.warning(
                "LLM_CONSULT_PANEL names %r, which is not a known provider. Skipped. "
                "Known: %s.", provider_id, ", ".join(sorted(_KNOWN_PROVIDERS)),
            )
            continue

        suffix = provider_id.upper()
        base_url = (os.getenv(f"LLM_CONSULT_BASE_URL_{suffix}") or "").strip() or known["base_url"]
        api_key = (os.getenv(f"LLM_CONSULT_KEY_{suffix}") or "").strip()
        model = (os.getenv(f"LLM_CONSULT_MODEL_{suffix}") or "").strip()

        if not model or (known["needs_key"] and not api_key):
            logger.warning(
                "LLM_CONSULT_PANEL names %r but it is not configured (need "
                "LLM_CONSULT_MODEL_%s%s). Skipped — the panel will be smaller than "
                "it looks in the config.",
                provider_id, suffix,
                f" and LLM_CONSULT_KEY_{suffix}" if known["needs_key"] else "",
            )
            continue

        panel.append(OpenAICompatibleProvider(
            provider_id=provider_id,
            base_url=base_url,
            api_key=api_key,
            # One model per provider. Tiering is a cost control within our own
            # reasoning; a second opinion is a single question and the panel
            # member either answers it or does not.
            models={tier: model for tier in ModelTier},
            needs_key=bool(known["needs_key"]),
        ))

    if panel:
        logger.info(
            "Consultation panel: %s (%d distinct provider(s)).",
            ", ".join(p.name for p in panel), len(panel),
        )
    return panel


def consultation_panel_status() -> Dict[str, Any]:
    """Panel readiness, for the monitoring API. Never returns a key."""
    panel = build_consultation_panel()
    return {
        "configured": bool(panel),
        "providers": [p.name for p in panel],
        "distinctCount": len({p.name for p in panel}),
        "isPanel": len({p.name for p in panel}) >= 2,
        "note": (
            "Advisory evidence only. No field of a consultation result is read by the "
            "Risk Gateway, the Supervisor's action branches or position sizing."
            if panel else
            "No consultation panel configured (LLM_CONSULT_PANEL is unset), so no "
            "second opinion is sought. This costs nothing a gate reads."
        ),
    }


def provider_status() -> Dict[str, Any]:
    """Configuration and readiness, for the monitoring API. Never returns a key."""
    provider = get_provider()
    if isinstance(provider, OpenAICompatibleProvider):
        return provider.describe()
    return {
        "provider": provider.name,
        "available": provider.available,
        "baseUrl": None,
        "apiKeyConfigured": False,
        "apiKeyRequired": False,
        "models": {},
        "note": (
            "No LLM provider is configured. Every deterministic node runs as normal; "
            "LLM nodes report themselves unavailable rather than inventing output. "
            "Set LLM_PROVIDER, LLM_API_KEY and LLM_MODEL to enable them."
        ),
    }


def set_provider(provider: LLMProvider) -> None:
    """Override the provider. For tests and for explicit wiring at startup."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = None
