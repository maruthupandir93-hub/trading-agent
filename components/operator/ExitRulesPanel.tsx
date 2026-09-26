'use client';

// ---------------------------------------------------------------------
// EXIT RULES — when a position is closed, editable while the agent runs.
//
// These were environment variables only, so changing how the agent takes
// profit meant an SSH session, an editor and a restart. They are exactly the
// numbers an operator wants to tune while watching it trade, so they belong
// here.
//
// EVERY ONE APPLIES ON THE NEXT TICK. `position_monitor` reads
// PROFIT_TARGET_PCT per price check and `trade_scope` reads the session rules
// per entry, so there is no restart and no stale value — which is the whole
// reason those reads were written call-time rather than import-time.
//
// WHAT THE PANEL DELIBERATELY SURFACES
//   * the scale-out is IGNORED while a profit target is set, because the two
//     together reintroduce the break-even runner that made 53% of this
//     system's trades close at ~0.00. Shown as a warning rather than silently
//     applied, so the operator is not left wondering which one is in force.
//   * what a % target means WITH LEVERAGE, because "2%" is a price move and
//     the account effect is that times the leverage.
// ---------------------------------------------------------------------

import { useCallback, useEffect, useState } from 'react';
import { Card, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';

interface Rule {
  label: string;
  unit: string;
  value: string;
  default: string;
  isDefault: boolean;
  min: number;
  max: number;
  help: string;
  // Present on the settings that are a CHOICE rather than a number — the target
  // basis and the venue-stop mode. Rendered as buttons, because typing one of
  // three exact words into a number box is not a control.
  choices?: string[];
}

interface RulesResponse {
  rules?: Record<string, Rule>;
  partialIgnored?: boolean;
}

const FIELD_FOR_ENV: Record<string, string> = {
  PROFIT_TARGET_PCT: 'profitTargetPct',
  PROFIT_TARGET_BASIS: 'profitTargetBasis',
  RESTING_STOP_MODE: 'restingStopMode',
  RESTING_STOP_ARM_R: 'restingStopArmR',
  TRAILING_STOP_R: 'trailingStopR',
  TRAILING_ACTIVATE_R: 'trailingActivateR',
  PARTIAL_TP_FRACTION: 'partialTpFraction',
  MAX_CONCURRENT_POSITIONS: 'maxConcurrentPositions',
};

// Shown under the profit target so the operator can see the account effect of a
// price move before committing to it. Leverage multiplies the move; it does not
// change what the number means.
const LEVERAGE_PREVIEW = [1, 3, 5, 10];

export function ExitRulesPanel() {
  const [data, setData] = useState<RulesResponse | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(backendProxyPath('/api/admin/exit-rules'), { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: RulesResponse = await res.json();
      setData(json);
      const next: Record<string, string> = {};
      Object.entries(json.rules ?? {}).forEach(([k, v]) => { next[k] = v.value; });
      setDraft(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not read the exit rules');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = useCallback(async () => {
    setBusy(true);
    setNote(null);
    setError(null);
    try {
      const body: Record<string, number | string> = {};
      Object.entries(draft).forEach(([env, raw]) => {
        const field = FIELD_FOR_ENV[env];
        if (!field) return;
        // A choice setting travels as its word. Parsing it as a number would
        // yield NaN and silently drop the one setting the operator just changed.
        if (rules[env]?.choices) { body[field] = raw; return; }
        const n = Number.parseFloat(raw);
        if (Number.isFinite(n)) body[field] = n;
      });
      const res = await fetch(backendProxyPath('/api/admin/exit-rules'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json?.detail ?? `HTTP ${res.status}`);
      setNote('Saved. Applies from the next price tick — no restart needed.');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not save');
    } finally {
      setBusy(false);
    }
  }, [draft, load]);

  const rules = data?.rules ?? {};
  const targetPct = Number.parseFloat(draft.PROFIT_TARGET_PCT ?? '0');
  const accountBasis = (draft.PROFIT_TARGET_BASIS ?? 'account') === 'account';
  const dirty = Object.entries(draft).some(([k, v]) => rules[k] && rules[k].value !== v);

  return (
    <Card>
      <SectionTitle>Exit rules — when a position is closed</SectionTitle>

      {error && (
        <div className="text-[11px] mb-2" style={{ color: 'var(--danger)' }}>{error}</div>
      )}

      {Object.keys(rules).length === 0 && !error && (
        <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Loading…</div>
      )}

      {Object.entries(rules).map(([env, rule]) => (
        <div key={env} className="mb-3">
          <label className="block text-[10px] uppercase tracking-wider mb-1"
                 style={{ color: 'var(--text-muted)' }}>
            {rule.label}{rule.unit ? ` (${rule.unit})` : ''}
            {rule.isDefault && (
              <span className="ml-1" style={{ opacity: 0.6 }}>· default</span>
            )}
          </label>
          {rule.choices ? (
            <div className="grid gap-1.5" style={{ gridTemplateColumns: `repeat(${rule.choices.length}, 1fr)` }}>
              {rule.choices.map((choice) => (
                <button
                  key={choice}
                  type="button"
                  onClick={() => setDraft((d) => ({ ...d, [env]: choice }))}
                  className="py-1.5 rounded text-[11px] mono font-semibold"
                  style={{
                    background: draft[env] === choice
                      ? 'color-mix(in srgb, var(--accent) 20%, transparent)'
                      : 'var(--bg-surface-2)',
                    color: draft[env] === choice ? 'var(--accent)' : 'var(--text-secondary)',
                    border: `1px solid ${draft[env] === choice ? 'var(--accent)' : 'var(--border)'}`,
                  }}
                >
                  {choice}
                </button>
              ))}
            </div>
          ) : (
            <input
              type="number"
              step="0.1"
              min={rule.min}
              max={rule.max}
              value={draft[env] ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, [env]: e.target.value }))}
              className="w-full px-2 py-1.5 rounded text-[13px] mono"
              style={{
                background: 'var(--bg-surface-2)',
                border: '1px solid var(--border)',
                color: 'var(--text-primary)',
              }}
            />
          )}
          <div className="text-[10px] mt-1 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            {rule.help}
          </div>

          {/* What a price-move target actually does to the account, per leverage.
              "2%" is a PRICE move; the account effect is that times leverage, and
              an operator choosing a number should see both. */}
          {env === 'PROFIT_TARGET_PCT' && targetPct > 0 && (
            <div className="text-[10px] mt-1 mono" style={{ color: 'var(--text-secondary)' }}>
              {/* Under "account" the target is fixed and the PRICE MOVE shrinks with
                  leverage; under "price" the move is fixed and the ACCOUNT effect
                  grows. Showing the wrong one would misstate the trade by the
                  leverage factor — at 10x that is 10x. */}
              {accountBasis
                ? LEVERAGE_PREVIEW.map((lev) => (
                    <span key={lev} className="mr-3">
                      {lev}x → {(targetPct / lev).toFixed(3)}% move
                    </span>
                  ))
                : LEVERAGE_PREVIEW.map((lev) => (
                    <span key={lev} className="mr-3">
                      {lev}x → {(targetPct * lev).toFixed(1)}% of margin
                    </span>
                  ))}
            </div>
          )}
        </div>
      ))}

      {data?.partialIgnored && (
        <div
          className="text-[10px] mb-2 p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--warning) 12%, transparent)',
            border: '1px solid var(--warning)',
            color: 'var(--text-secondary)',
          }}
        >
          <strong>The scale-out is ignored right now.</strong> A profit target closes the whole
          position, so there is no runner left to scale out of. Running both moved the
          runner&apos;s stop to break-even and produced trades that closed at ~0.00 — which is
          the failure the profit target exists to remove. Set the target to 0 to use the
          scale-out instead.
        </div>
      )}

      <button
        type="button"
        onClick={() => void save()}
        disabled={busy || !dirty}
        className="w-full py-2 rounded text-[12px] font-semibold"
        style={{
          background: dirty ? 'var(--accent)' : 'var(--bg-surface-2)',
          color: dirty ? 'var(--bg-base)' : 'var(--text-muted)',
          border: '1px solid var(--border)',
          cursor: busy || !dirty ? 'default' : 'pointer',
        }}
      >
        {busy ? 'Saving…' : dirty ? 'Save exit rules' : 'No changes'}
      </button>

      {note && (
        <div className="text-[10px] mt-2" style={{ color: 'var(--success)' }}>{note}</div>
      )}
    </Card>
  );
}
