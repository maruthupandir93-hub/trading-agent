/**
 * F36: the server must not fetch our own network on a caller's say-so.
 *
 * Two routes take a caller-chosen URL and dial it server-side — `/api/mcp-status`
 * (a GET probe) and `/api/chat` (which hands the address to the backend, which
 * then POSTs to it from the VM). Authentication is the first half of the fix;
 * these are the second. An authenticated operator still should not be able to
 * turn the server into a scanner, and a leaked dashboard password should not
 * come with one.
 */

import { describe, expect, it, afterEach } from 'vitest';
import { checkLlmBaseUrl, checkOutboundUrl } from './ssrfGuard';

describe('checkOutboundUrl', () => {
  it('allows an ordinary public https endpoint', () => {
    const out = checkOutboundUrl('https://integrate.api.nvidia.com/v1');
    expect(out.ok).toBe(true);
  });

  it.each([
    ['http://127.0.0.1:8000/api/admin/status', 'the backend admin API on loopback'],
    ['http://localhost:8000/', 'the same thing by name'],
    ['http://[::1]:8000/', 'loopback over IPv6'],
    ['http://169.254.169.254/latest/meta-data/', 'the cloud metadata service'],
    ['http://10.0.0.5/', 'a private 10/8 address'],
    ['http://172.16.0.1/', 'private 172.16/12'],
    ['http://192.168.1.1/', 'a home router'],
    ['http://0.0.0.0/', 'the unspecified address'],
    ['http://[::ffff:127.0.0.1]/', 'loopback wearing an IPv6 mapping'],
  ])('refuses %s (%s)', (url) => {
    const out = checkOutboundUrl(url);
    expect(out.ok).toBe(false);
  });

  it('refuses a scheme that is not http(s)', () => {
    for (const url of ['file:///etc/passwd', 'gopher://x/', 'ftp://x/']) {
      expect(checkOutboundUrl(url).ok).toBe(false);
    }
  });

  it('refuses an empty or unparseable url rather than throwing', () => {
    expect(checkOutboundUrl('').ok).toBe(false);
    expect(checkOutboundUrl(undefined).ok).toBe(false);
    expect(checkOutboundUrl('not a url').ok).toBe(false);
  });

  it('says WHY, because the caller may have a legitimate reason to retry', () => {
    const out = checkOutboundUrl('http://10.0.0.5/');
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.reason.toLowerCase()).toContain('private');
  });
});

describe('checkLlmBaseUrl', () => {
  afterEach(() => {
    delete process.env.LLM_BASE_URL_ALLOWLIST;
  });

  it('falls back to the SSRF check when no allow-list is configured', () => {
    expect(checkLlmBaseUrl('https://api.openai.com/v1').ok).toBe(true);
    expect(checkLlmBaseUrl('http://127.0.0.1:11434/v1').ok).toBe(false);
  });

  it('honours an allow-list when one is set, including subdomains', () => {
    process.env.LLM_BASE_URL_ALLOWLIST = 'nvidia.com, openai.com';
    expect(checkLlmBaseUrl('https://integrate.api.nvidia.com/v1').ok).toBe(true);
    expect(checkLlmBaseUrl('https://api.openai.com/v1').ok).toBe(true);
    expect(checkLlmBaseUrl('https://evil.example.com/v1').ok).toBe(false);
  });

  it('still refuses a private address that IS on the allow-list', () => {
    // The allow-list is about which providers may be reached, not a licence to
    // dial inside the network. A hostname entry must never widen the SSRF rule.
    process.env.LLM_BASE_URL_ALLOWLIST = 'localhost';
    expect(checkLlmBaseUrl('http://localhost:11434/v1').ok).toBe(false);
  });
});
