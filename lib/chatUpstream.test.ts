// Upstream error translation for /api/chat, plus a guard on the route's runtime.
//
// THE RUNTIME ASSERTION IS THE IMPORTANT ONE AND IT IS AT THE BOTTOM.
//
// "Ask Agent" failed in production with a 403 whose body was written by Vercel:
// its Edge runtime refuses fetch() to a bare IP address, and the backend is
// configured as one. The bug is invisible in local development, because
// `localhost` is a hostname and edge dials it happily — so nothing short of
// deploying reproduces it, and a unit test on the file is the only cheap guard.

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { looksLikeMissingV1, looksLikeVercelEdgeIpBlock, parseUpstreamErrorMessage } from './chatUpstream';

const VERCEL_403 = "Direct IP access is not allowed in Vercel's Edge environment (hostname: 203.0.113.9)";

describe('looksLikeVercelEdgeIpBlock', () => {
  it('recognises the Vercel edge refusal', () => {
    expect(looksLikeVercelEdgeIpBlock(403, VERCEL_403)).toBe(true);
  });

  it('does not claim a provider auth failure is an edge block', () => {
    // A 403 from a model provider is a real credential problem and must keep
    // saying so — mislabelling it would send the operator to change a runtime
    // setting while their API key stays wrong.
    expect(looksLikeVercelEdgeIpBlock(403, '{"error":{"message":"invalid api key"}}')).toBe(false);
  });

  it('is scoped to 403', () => {
    expect(looksLikeVercelEdgeIpBlock(500, VERCEL_403)).toBe(false);
  });
});

describe('parseUpstreamErrorMessage', () => {
  it('explains the edge block instead of echoing a bare hostname', () => {
    const msg = parseUpstreamErrorMessage(403, VERCEL_403, 'https://provider/v1/chat/completions');
    expect(msg).toMatch(/never left Vercel/i);
    expect(msg).toMatch(/runtime = 'nodejs'/);
    // It must rule out the three things an operator would otherwise go and check.
    expect(msg).toMatch(/not an LLM, API-key or backend fault/i);
  });

  it('still surfaces a provider`s own JSON error message', () => {
    expect(parseUpstreamErrorMessage(401, '{"error":{"message":"bad key"}}', 'https://p/v1/chat/completions')).toBe('bad key');
  });

  it('still detects the missing-/v1 signature', () => {
    expect(looksLikeMissingV1(404, '404 page not found', 'http://localhost:1234/chat/completions')).toBe(true);
  });
});

describe('the /api/chat route runtime', () => {
  const source = readFileSync(path.join(__dirname, '..', 'app', 'api', 'chat', 'route.ts'), 'utf8');

  it('runs on Node, not Edge', () => {
    // Edge cannot reach an IP-addressed backend, and this is the only route in
    // the app that ever ran there — which is why "Ask Agent" was the single
    // broken page while every other backend-backed view kept working.
    expect(source).toMatch(/export const runtime = 'nodejs'/);
    expect(source).not.toMatch(/export const runtime = 'edge'/);
  });
});
