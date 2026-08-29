"""AI API (`/api/ai`) — spec Section 8: *"Routes reasoning requests to the
correct model/agent."*

WHAT THIS FILE USED TO BE
------------------------
Three unrelated files concatenated under one name: an agent task store
(duplicating `api/agents.py`), the missions CRUD routes (now
`api/missions.py`), and this header — with `os`, `json`, `uuid`, `Dict`,
`List`, `Any`, `Body`, `Mission` and `mission_store` all used but never
imported. It raised `NameError` on import, which took `backend.main` down
with it. Rewritten to be only the AI API.

WHAT IT DOES AND DOES NOT DO
----------------------------
The "routes to the correct agent" half is implemented: the agent registry
knows every agent's declared capabilities (spec Section 5's contract), so
resolving "who owns this capability" is a real, deterministic lookup.

The "routes to the correct model" half is **not implemented**, and this
module returns HTTP 501 for it rather than a plausible-looking answer. There
is no LLM client in the backend — no model is configured, and no agent
exposes a generic `reason()` entry point (they are event-driven via
`BaseAgent.handle_event`). A `/reason` route that returned invented
reasoning text would be exactly the failure mode CLAUDE.md invariant 6
forbids: a fabricated output that reads as a real one. `docs/README.md`
states the same rule for documentation — mark it not-implemented instead of
describing it as if it exists.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.agent_os import get_agent_os

logger = logging.getLogger(__name__)

router = APIRouter()


class ReasoningRequest(BaseModel):
    capability: str
    symbol: Optional[str] = None
    context: Dict[str, Any] = {}


@router.get("/agents", response_model=List[Dict[str, Any]])
async def list_agents() -> List[Dict[str, Any]]:
    """Every registered agent's contract, plus live health.

    This is the explainability surface for spec Section 5 — *"Every agent
    must be able to explain every decision it makes."* An operator can read
    exactly what each agent claims to do, what it depends on, and whether it
    is currently healthy.
    """
    kernel = get_agent_os()
    out = []
    for agent in kernel.agents.values():
        d = agent.descriptor
        out.append(
            {
                "id": d.id,
                "name": d.name,
                "version": d.version,
                "description": d.description,
                "category": d.category,
                "capabilities": d.capabilities,
                "dependencies": d.dependencies,
                "priority": d.priority,
                "tickIntervalMs": d.tickIntervalMs,
                "health": agent.health.model_dump(),
            }
        )
    return out


@router.get("/agents/{agent_id}", response_model=Dict[str, Any])
async def get_agent(agent_id: str) -> Dict[str, Any]:
    """One agent's contract and health."""
    kernel = get_agent_os()
    agent = kernel.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no agent registered with id {agent_id}")
    return {
        **agent.descriptor.model_dump(),
        "health": agent.health.model_dump(),
    }


@router.post("/route", response_model=Dict[str, Any])
async def route_reasoning_request(req: ReasoningRequest) -> Dict[str, Any]:
    """Resolve which agent owns a capability. Does NOT invoke it.

    Deterministic registry lookup — no model call, so nothing here can
    hallucinate. Returns every match rather than silently picking one, and
    reports each candidate's health so the caller can see that the owning
    agent is (for example) in an error state instead of getting a confident
    answer from a dead agent.
    """
    kernel = get_agent_os()
    matches = [
        {
            "id": a.descriptor.id,
            "name": a.descriptor.name,
            "priority": a.descriptor.priority,
            "status": a.health.status,
            "healthy": a.health.status in ("ready", "running"),
        }
        for a in kernel.agents.values()
        if req.capability in a.descriptor.capabilities
    ]
    matches.sort(key=lambda m: m["priority"])

    if not matches:
        # 404, not an empty 200: "no agent has this capability" is a
        # different fact from "an agent handled it and found nothing", and
        # collapsing the two hides a misconfiguration.
        raise HTTPException(
            status_code=404,
            detail=(
                f"no registered agent declares capability '{req.capability}'. "
                f"Known capabilities: "
                f"{sorted({c for a in kernel.agents.values() for c in a.descriptor.capabilities})}"
            ),
        )

    return {
        "status": "success",
        "capability": req.capability,
        "symbol": req.symbol,
        "candidates": matches,
        "selected": matches[0]["id"],
        "healthyCandidates": [m["id"] for m in matches if m["healthy"]],
        # Stated in the response, not just in a docstring, so a caller can't
        # mistake a routing result for a reasoning result.
        "invoked": False,
        "note": (
            "Routing only — the selected agent was NOT invoked. Model "
            "invocation is not implemented; see POST /api/ai/reason."
        ),
    }


@router.post("/reason")
async def reason(req: ReasoningRequest) -> Dict[str, Any]:
    """Not implemented — returns 501.

    Deliberately fails rather than returning invented reasoning. See the
    module docstring: no LLM client is configured in the backend and no
    agent exposes a generic reasoning entry point, so any response this
    route could produce today would be fabricated.

    To implement honestly it needs: a configured model provider, a prompt
    from the versioned prompt library (spec Section 9), and a recorded
    request/response pair for audit (spec Section 16's requirement that
    external reasoning be recorded and attributed).
    """
    raise HTTPException(
        status_code=501,
        detail=(
            "Model invocation is not implemented. No LLM provider is configured in "
            "the backend and no agent exposes a generic reason() entry point, so "
            "this route cannot return a real answer and will not return a fake "
            "one. Use POST /api/ai/route to resolve which agent owns a capability."
        ),
    )


# ---------------------------------------------------------------------------
# Chat completions proxy — replaces the upstream call in app/api/chat/route.ts
# ---------------------------------------------------------------------------


class ChatProxyRequest(BaseModel):
    """One chat completion, forwarded verbatim to an OpenAI-compatible endpoint.

    THE KEY IS SUPPLIED PER REQUEST AND NEVER STORED.

    It arrives from the operator's browser, where the settings page keeps it, and
    is used for the duration of this one call. It is not logged, not cached and
    not written anywhere — the same contract the Next.js route it replaces had.
    Note this DOES mean the key now transits one more machine than it used to;
    both are the operator's own infrastructure, and it is stated here rather than
    left to be discovered.
    """

    apiKey: str
    baseUrl: Optional[str] = None
    model: Optional[str] = None
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = None
    maxTokens: Optional[int] = None


def _chat_completions_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


@router.post("/chat")
async def chat_proxy(req: ChatProxyRequest):
    """Stream a chat completion from an OpenAI-compatible provider.

    WHY THIS IS HERE AT ALL
    -----------------------
    Every other third-party call moved to this backend because Vercel's region is
    refused by Binance. THIS one is not geo-blocked — the LLM providers serve US
    regions perfectly well, and Vercel is arguably the better place to call them
    from. It moved because the operator asked for a single rule with no
    exceptions: no Next.js route calls a third party.

    That rule has a cost worth naming: the response crosses one extra hop
    (provider -> here -> Vercel -> browser) before the first token lands. The
    streaming below keeps that cost to latency rather than to behaviour.

    STREAMING IS PRESERVED END TO END. `client.stream` plus a StreamingResponse
    forwards bytes as they arrive. Buffering the body and returning it whole
    would compile, pass a smoke test, and silently turn a token-by-token answer
    into a long pause followed by a wall of text.

    THE STATUS IS RESOLVED BEFORE THE STREAM STARTS, AND THAT IS LOAD-BEARING.
    An upstream 4xx is returned as a real HTTP error, not as an SSE frame inside
    a 200. The Next.js route in front of this inspects the status to detect the
    "self-hosted server pointed at without /v1" signature and retry once against
    the corrected URL — a 200 carrying an error in its body would make that
    retry impossible and turn a one-character configuration mistake back into an
    opaque failure.
    """
    import httpx
    from fastapi.responses import StreamingResponse

    if not req.apiKey:
        raise HTTPException(status_code=400, detail="Missing API key")
    if not req.messages:
        raise HTTPException(status_code=400, detail="Missing messages")

    base_url = req.baseUrl or "https://integrate.api.nvidia.com/v1"
    url = _chat_completions_url(base_url)
    payload = {
        "model": req.model or "z-ai/glm-5.2",
        "messages": req.messages,
        "temperature": req.temperature if req.temperature is not None else 0.2,
        "top_p": 1,
        "max_tokens": req.maxTokens or 1536,
        "stream": True,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {req.apiKey}"}

    # A SEPARATE client from services/upstream's shared one, deliberately.
    #
    # That client has a 12s timeout tuned for market-data calls. A long completion
    # legitimately takes minutes, and reusing it would cut answers off mid-sentence
    # — which reads as the model stopping rather than as a timeout. `read=None`
    # disables the per-read deadline while keeping a connect timeout, so an
    # unreachable host still fails fast.
    timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0)

    # Entered MANUALLY rather than with `async with`, because the response has to
    # outlive this function: the body is consumed by the generator below, after
    # this function has already returned. A context manager here would close the
    # connection before the first chunk was read.
    client = httpx.AsyncClient(timeout=timeout)
    try:
        request = client.build_request("POST", url, json=payload, headers=headers)
        response = await client.send(request, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        logger.error("Chat proxy could not reach %s: %s", url, e)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach {url}: {type(e).__name__}: {e}",
        )

    if response.status_code >= 400:
        body = (await response.aread()).decode("utf-8", "replace")[:500]
        await response.aclose()
        await client.aclose()
        # The upstream's own status is propagated, not flattened to 502. The
        # caller distinguishes a 401 (bad key) from a 404 (wrong base URL), and
        # the 404 is the one it retries.
        raise HTTPException(status_code=response.status_code, detail=body)

    async def relay():
        try:
            async for chunk in response.aiter_raw():
                if chunk:
                    yield chunk
        except httpx.HTTPError as e:
            # The stream has already started, so the status is long since sent.
            # An SSE error frame is the only way left to tell the browser why it
            # stopped; closing silently would look like the model finishing.
            logger.error("Chat proxy stream broke for %s: %s", url, e)
            yield _sse_error(f"Stream interrupted: {type(e).__name__}: {e}").encode()
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells any reverse proxy in front of this not to buffer. Without it
            # nginx holds the whole response and the stream arrives at once.
            "X-Accel-Buffering": "no",
        },
    )


def _sse_error(message: str) -> str:
    """An error the browser's SSE reader can surface instead of a silent stall."""
    import json as _json

    return f"data: {_json.dumps({'error': message})}\n\ndata: [DONE]\n\n"
