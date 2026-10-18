/**
 * Why a write to the backend was refused, said once and said usefully.
 *
 * WHAT WENT WRONG IN PRODUCTION
 * -----------------------------
 * Making the backend fail CLOSED (F6) was right — an unauthenticated write
 * surface on a real-money service reachable from the internet is the exposure,
 * and `require_write_auth` used to pass when it was not configured. But the
 * first deployment of it put this in front of the operator, on every control,
 * after every click:
 *
 *     {"detail":"This endpoint changes state and TRADES_API_KEY is not
 *      configured, so writes are refused. Set TRADES_API_KEY on the backend
 *      (and in the dashboard's environment), or set
 *      ALLOW_UNAUTHENTICATED_WRITES=true if this process is only reachable
 *      from localhost."}
 *
 * That text is accurate and it is still the wrong shape for a UI. It arrives
 * AFTER the action, as raw JSON, once per button, and it offers an escape hatch
 * (`ALLOW_UNAUTHENTICATED_WRITES`) as though it were an equal option — on a
 * public deployment it is not. An operator reading it on the Settings page has
 * no way to tell whether they have misconfigured one variable or broken the
 * whole system.
 *
 * TWO DIFFERENT CAUSES, AND THEY NEED DIFFERENT ACTIONS
 * -----------------------------------------------------
 * There are two independent gates between a button and the backend, and both
 * refuse by design when unset. Telling them apart is the whole job here,
 * because fixing the wrong one changes nothing:
 *
 *   DASHBOARD_PASSWORD   on the FRONTEND (Vercel). The proxy attaches the
 *                        backend key to whatever it forwards, so it must
 *                        authenticate the incoming caller first. Unset ->
 *                        reads pass, writes are refused with 401 by
 *                        `app/api/backend/[...path]/route.ts`.
 *   TRADES_API_KEY       on the BACKEND (and the same value on the frontend, so
 *                        the proxy has something to attach). Unset -> writes
 *                        are refused with 503 by `backend/core/auth.py`.
 *
 * A 401 here is almost never "wrong password typed" — the browser sends the
 * Basic credential automatically once the middleware has challenged it — so the
 * actionable reading is "the frontend has no password configured".
 *
 * WHERE THIS IS USED
 * ------------------
 * Twice, deliberately. `describeWriteRefusal` rewrites a refusal that already
 * happened; `writeBlocker` reads `/api/admin/status` so the UI can render the
 * blocker BEFORE the control. The second is the one that matters — it is the
 * rule `VenueSwitchPanel` already follows, and the reason is the same:
 * discovering a refusal from a toast after clicking is worse than never being
 * offered the action.
 */

/** The `auth` block of `GET /api/admin/status`. All fields optional: an older
 *  backend, or one that could not be reached, simply does not supply them. */
export interface BackendAuthStatus {
  writeAuthEnabled?: boolean;
  /** Added with this fix. Absent on a backend that predates it. */
  writesPossible?: boolean;
  openWritesAllowed?: boolean;
  remedy?: string | null;
  envVar?: string;
  note?: string;
}

export interface WriteBlocker {
  /** One line, suitable for a banner heading. */
  title: string;
  /** What to do, in the order it has to be done. */
  steps: string[];
  /** Which side is misconfigured, so the operator knows where to look. */
  where: 'frontend' | 'backend' | 'both';
}

const BACKEND_KEY_BLOCKER: WriteBlocker = {
  title: 'The backend is refusing every write: TRADES_API_KEY is not set.',
  where: 'both',
  steps: [
    'Generate one secret value, for example: python -c "import secrets;print(secrets.token_urlsafe(32))"',
    'Set TRADES_API_KEY to that value in the backend\'s .env on the server, then restart the backend.',
    'Set TRADES_API_KEY to the SAME value in the dashboard\'s environment (Vercel) and redeploy — the proxy attaches it, so a mismatch fails exactly like an absence.',
  ],
};

const DASHBOARD_PASSWORD_BLOCKER: WriteBlocker = {
  title: 'The dashboard is refusing every write: DASHBOARD_PASSWORD is not set.',
  where: 'frontend',
  steps: [
    'Set DASHBOARD_PASSWORD in the dashboard\'s environment (Vercel) and redeploy.',
    'Reload this page and sign in when the browser asks — it then sends that credential automatically.',
  ],
};

/**
 * Read the blocker from `/api/admin/status` before anything is clicked.
 *
 * Returns null when writes can succeed, when the status could not be read (an
 * unreachable backend is a different problem with a different message, and
 * claiming a missing variable would send the operator to the wrong file), and
 * when the backend predates `writesPossible` — in that last case `undefined`
 * means "this backend cannot tell us", which must not be read as "blocked".
 */
export function writeBlocker(auth: BackendAuthStatus | null | undefined): WriteBlocker | null {
  if (!auth) return null;
  if (auth.writesPossible === undefined) return null;
  if (auth.writesPossible) return null;
  return BACKEND_KEY_BLOCKER;
}

/**
 * Rewrite a refusal that has already happened.
 *
 * Falls through to the backend's own text for anything it does not recognise:
 * a 409 from the venue switch, a 400 from a bad body and a 503 from a missing
 * key are different facts, and flattening them into one friendly sentence is
 * how a real refusal stops being readable.
 */
export function describeWriteRefusal(status: number, detail: string): string {
  const text = (detail || '').toLowerCase();

  if (text.includes('trades_api_key')) {
    return (
      `${BACKEND_KEY_BLOCKER.title} Writes fail closed, so reads still work and the ` +
      `dashboard renders while its buttons do nothing. ${BACKEND_KEY_BLOCKER.steps.join(' ')}`
    );
  }

  if (text.includes('dashboard_password')) {
    return `${DASHBOARD_PASSWORD_BLOCKER.title} ${DASHBOARD_PASSWORD_BLOCKER.steps.join(' ')}`;
  }

  if (status === 401) {
    // Not "wrong password": the browser replays the Basic credential on every
    // same-origin call once the middleware has challenged it, so a 401 here
    // means the proxy had nothing configured to check against.
    return (
      `${DASHBOARD_PASSWORD_BLOCKER.title} ${DASHBOARD_PASSWORD_BLOCKER.steps.join(' ')} ` +
      `(Original: ${detail || 'no detail given'}.)`
    );
  }

  return detail || `HTTP ${status}`;
}
