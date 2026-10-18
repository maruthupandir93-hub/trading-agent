/**
 * The production symptom this file exists for:
 *
 *     {"detail":"This endpoint changes state and TRADES_API_KEY is not
 *      configured, so writes are refused. ..."}
 *
 * shown as raw JSON, on every control, only after it was clicked.
 *
 * Two properties matter and they pull in opposite directions, so both are
 * pinned: a refusal with a KNOWN, fixable cause must be turned into
 * instructions, and every OTHER refusal must survive untouched. Flattening a
 * 409 "a real position is open" into a friendly sentence about environment
 * variables would be a worse bug than the one being fixed.
 */

import { describe, expect, it } from 'vitest';
import { describeWriteRefusal, writeBlocker } from './writeBlocker';

const BACKEND_503 =
  'This endpoint changes state and TRADES_API_KEY is not configured, so writes are ' +
  'refused. Set TRADES_API_KEY on the backend (and in the dashboard\'s environment), ' +
  'or set ALLOW_UNAUTHENTICATED_WRITES=true if this process is only reachable from ' +
  'localhost.';

describe('writeBlocker', () => {
  it('reports the blocker when the backend says writes are impossible', () => {
    const out = writeBlocker({ writeAuthEnabled: false, writesPossible: false });
    expect(out).not.toBeNull();
    expect(out!.title).toContain('TRADES_API_KEY');
    expect(out!.steps.length).toBeGreaterThan(1);
    // BOTH SIDES must be named. The proxy attaches the key, so setting it on
    // the backend alone leaves the dashboard sending nothing and the refusal
    // looks identical — which is exactly the loop an operator gets stuck in.
    expect(out!.steps.join(' ')).toMatch(/Vercel|dashboard/i);
    expect(out!.where).toBe('both');
  });

  it('is silent when writes can succeed', () => {
    expect(writeBlocker({ writeAuthEnabled: true, writesPossible: true })).toBeNull();
    // The explicit opt-out is a different state: writes work, so there is no
    // blocker to render. The status card warns about it separately.
    expect(
      writeBlocker({ writeAuthEnabled: false, writesPossible: true, openWritesAllowed: true }),
    ).toBeNull();
  });

  it('is silent when it cannot tell, rather than guessing blocked', () => {
    // An unreachable backend, or one deployed before `writesPossible` existed.
    // Claiming a missing variable here would send the operator to edit a file
    // that is already correct.
    expect(writeBlocker(null)).toBeNull();
    expect(writeBlocker(undefined)).toBeNull();
    expect(writeBlocker({ writeAuthEnabled: false })).toBeNull();
  });
});

describe('describeWriteRefusal', () => {
  it('turns the backend 503 into instructions', () => {
    const out = describeWriteRefusal(503, BACKEND_503);
    expect(out).toContain('TRADES_API_KEY');
    expect(out).toMatch(/Vercel|dashboard/i);
    // THE ESCAPE HATCH IS NOT OFFERED. The backend's own text names
    // ALLOW_UNAUTHENTICATED_WRITES as an alternative, which it is on a laptop
    // and is not on a deployment reachable from the internet. Presenting it as
    // an equal option is how an operator "fixes" this by opening the write
    // surface on a real-money service.
    expect(out).not.toContain('ALLOW_UNAUTHENTICATED_WRITES');
  });

  it('reads a 401 as the frontend password, not as a wrong password', () => {
    // The browser replays its Basic credential automatically on same-origin
    // calls once the middleware has challenged it, so a 401 here means the
    // proxy had nothing configured to check against.
    const out = describeWriteRefusal(401, 'Authentication required for the backend proxy.');
    expect(out).toContain('DASHBOARD_PASSWORD');
  });

  it('leaves an unrelated refusal exactly as the backend worded it', () => {
    const venue =
      'Refusing to switch venue: a REAL position is open on binance. Close it first.';
    expect(describeWriteRefusal(409, venue)).toBe(venue);

    const target = 'The target is below current equity, so the session would end immediately.';
    expect(describeWriteRefusal(400, target)).toBe(target);
  });

  it('still says something when the refusal carried no detail', () => {
    expect(describeWriteRefusal(500, '')).toBe('HTTP 500');
  });
});
