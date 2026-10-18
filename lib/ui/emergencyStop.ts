// ---------------------------------------------------------------------
// The arming rule and the failure wording for the emergency stop.
//
// EXTRACTED SO IT CAN BE TESTED. `components/shell/EmergencyStopModal.tsx` is a
// `.tsx`, and this project's vitest setup cannot parse JSX (`jsx: "preserve"`),
// so anything asserted about the stop has to live in a `.ts` module. That is a
// constraint worth paying here specifically: the emergency stop is the one control
// where a subtle behaviour change — arming on the wrong string, or reporting a
// failed stop as a success — is materially dangerous.
// ---------------------------------------------------------------------

export const CONFIRM_WORD = 'STOP';

/**
 * Whether the typed text arms the stop button.
 *
 * Trimmed and upper-cased deliberately: an operator typing under pressure should
 * not be defeated by a trailing space or caps lock. It is still an exact word
 * match — a prefix like "STO" or an extra character like "STOPP" must NOT arm,
 * or the type-to-confirm gate stops being a gate.
 */
export function isArmed(typed: string): boolean {
  return typed.trim().toUpperCase() === CONFIRM_WORD;
}

/**
 * The message shown when the stop did not apply.
 *
 * It always ends by stating the system is NOT stopped. A failed stop that reads
 * like a successful one is the worst outcome this component can produce — worse
 * than any error text — so the claim is part of the function, not left to the
 * call site to remember.
 */
export function stopFailureMessage(cause: { httpStatus?: number; networkError?: string }): string {
  const prefix =
    typeof cause.httpStatus === 'number'
      ? `Backend refused the stop: HTTP ${cause.httpStatus}.`
      : `Could not reach the backend (${cause.networkError || 'network error'}).`;

  // NAME THE LIKELY CAUSE FOR THE TWO STATUSES THAT HAVE A FIXED ONE.
  //
  // Writes fail closed when TRADES_API_KEY (backend, 503) or
  // DASHBOARD_PASSWORD (proxy, 401) is unset — correct on a real-money
  // service, and a bare "HTTP 503" on THIS button sends the operator looking
  // for a backend outage during the moment they most need to act. The status
  // is still printed, because a guess that displaced the fact would be worse.
  const hint =
    cause.httpStatus === 503
      ? ' This is what an unset TRADES_API_KEY looks like: the backend refuses every write. Stop the agent at the exchange if you need it stopped now.'
      : cause.httpStatus === 401
        ? ' This is what an unset DASHBOARD_PASSWORD looks like: the dashboard proxy refuses every write. Stop the agent at the exchange if you need it stopped now.'
        : '';

  return `${prefix} The system is NOT stopped.${hint}`;
}
