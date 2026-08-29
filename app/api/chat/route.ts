// Chat completions. The browser cannot call most providers' /chat/completions
// directly (no CORS headers on their side), so this route forwards the request
// and streams the SSE body straight back.
//
// IT NOW FORWARDS TO THE BACKEND, NOT TO THE PROVIDER.
//
// This is the one route where the move was NOT forced by a geo-block. The LLM
// providers serve Vercel's regions perfectly well — arguably better than a
// backend in APAC does. It moved because the rule is that no Next.js route calls
// a third party, and an exception here would be the one place a future reader
// has to remember is different.
//
// THE COST, STATED PLAINLY: the response crosses one extra hop before the first
// token lands (provider -> backend -> here -> browser), so time-to-first-token is
// higher than it was. Streaming is preserved at every hop, so this is latency and
// not a behaviour change — the answer still arrives token by token rather than as
// one block at the end. To revert, have `callBackend` POST to `upstreamUrl` with
// the provider's key instead of to the backend; nothing else here changes.
//
// Nothing is logged or persisted. The API key passes through in memory for the
// duration of one request, exactly as before — it now transits one more machine,
// which is the operator's own.

import {
  buildChatCompletionsUrl,
  looksLikeMissingV1,
  parseUpstreamErrorMessage,
  withV1Inserted,
} from '@/lib/chatUpstream';
import { backendOrigin } from '@/lib/api/backendProxy.server';

// Edge runtime, not Node — belt-and-suspenders for streaming reliability.
// Vercel's Node.js Serverless Functions are documented to buffer a function's
// full response before returning it, unlike Edge Functions. With a proxied
// stream that difference is the whole point of the route: buffering would turn a
// token-by-token answer into a long pause followed by a wall of text.
export const runtime = 'edge';

type ChatBody = {
  apiKey: string;
  baseUrl?: string;
  model?: string;
  messages: { role: string; content: string }[];
  temperature?: number;
  maxTokens?: number;
};

export async function POST(req: Request) {
  let body: ChatBody;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const { apiKey, baseUrl, model, messages, temperature, maxTokens } = body;

  // Validated here, before the hop. A missing key is the caller's mistake and
  // should not cost a round trip to be told so.
  if (!apiKey) {
    return Response.json({ error: 'Missing API key' }, { status: 400 });
  }
  if (!Array.isArray(messages) || messages.length === 0) {
    return Response.json({ error: 'Missing messages' }, { status: 400 });
  }

  // Self-hosted OpenAI-compatible servers (Ollama, LM Studio, vLLM, LiteLLM) are
  // very commonly pointed at without the trailing /v1. The correction is applied
  // HERE rather than in the backend because `lib/chatUpstream.ts` already owns
  // that logic and has tests against the exact 404 signature — duplicating it in
  // Python would create a second, drifting copy of a fiddly heuristic.
  const resolvedBaseUrl = baseUrl || 'https://integrate.api.nvidia.com/v1';
  const upstreamUrl = buildChatCompletionsUrl(resolvedBaseUrl);

  const backendUrl = `${backendOrigin()}/api/ai/chat`;

  async function callBackend(providerBaseUrl: string): Promise<Response> {
    return fetch(backendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(process.env.TRADES_API_KEY ? { Authorization: `Bearer ${process.env.TRADES_API_KEY}` } : {}),
      },
      body: JSON.stringify({
        apiKey,
        baseUrl: providerBaseUrl,
        model,
        messages,
        temperature,
        maxTokens,
      }),
    });
  }

  let upstreamRes: Response;
  try {
    upstreamRes = await callBackend(resolvedBaseUrl);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'unknown error';
    // Names WHICH hop failed. "The backend is unreachable" and "the model
    // provider rejected the key" are both a 502 to the browser and have
    // completely different fixes.
    return Response.json(
      {
        error:
          `Could not reach the trading backend at ${backendOrigin()} (${message}). This is the ` +
          `Vercel-to-backend hop, not the model provider — check the backend is running and its ` +
          `port is open.`,
      },
      { status: 502 },
    );
  }

  // THE MISSING-/v1 RETRY, PRESERVED ACROSS THE MOVE.
  //
  // Self-hosted OpenAI-compatible servers are very commonly pointed at without
  // the trailing /v1, and Go's default handler answers "404 page not found".
  // Rather than fail on that one-character mistake, the specific signature is
  // detected and retried once against the corrected URL.
  //
  // This only works because the backend propagates the PROVIDER's status code
  // instead of wrapping the error in a 200 SSE frame — see its docstring. If it
  // ever starts returning 200-with-an-error, this retry silently stops
  // happening and the misconfiguration becomes opaque again.
  let retriedUrl: string | undefined;
  if (!upstreamRes.ok) {
    const text = await upstreamRes.clone().text().catch(() => '');
    if (looksLikeMissingV1(upstreamRes.status, text, upstreamUrl)) {
      const fixedBase = withV1Inserted(resolvedBaseUrl);
      if (fixedBase) {
        retriedUrl = buildChatCompletionsUrl(fixedBase);
        try {
          upstreamRes = await callBackend(fixedBase);
        } catch {
          // The retry could not even connect — fall through and report the
          // original error rather than the retry's.
        }
      }
    }
  }

  if (!upstreamRes.ok || !upstreamRes.body) {
    const text = await upstreamRes.text().catch(() => '');
    return Response.json(
      { error: parseUpstreamErrorMessage(upstreamRes.status, text, upstreamUrl, retriedUrl) },
      { status: upstreamRes.status || 502 },
    );
  }

  // The body is piped through untouched. Reading it here — even to inspect it —
  // would buffer the stream and defeat the Edge runtime entirely.
  return new Response(upstreamRes.body, {
    status: 200,
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no', // tells nginx-style proxies not to buffer this response
    },
  });
}
