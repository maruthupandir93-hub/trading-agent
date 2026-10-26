'use client';

// ---------------------------------------------------------------------
// The two numbers that decide whether a session can start at all.
//
// WHY THIS PANEL EXISTS. `services/risk_coherence` refuses a session whose VaR
// budget and drawdown limit cannot survive an ordinary losing streak, and that
// refusal is correct. But the only way to ACT on it was to SSH into the
// server, edit `.env` and restart — and `.env` is gitignored, so a `git pull`
// carries the code fix and leaves the configuration untouched. An operator hit
// the refusal three times, redeployed twice, and was still blocked, because
// the one thing that needed changing was not reachable from anything they
// could click.
//
// IT LEADS WITH THE VERDICT, NOT THE FORM — the rule `VenueSwitchPanel`
// already follows. Discovering from a toast, after pressing Start, that your
// configuration was never going to work is the experience this replaces.
//
// THE TWO LEVERS ARE NOT EQUIVALENT AND THE PANEL SAYS SO. Lowering the VaR
// fraction is the only one that reduces risk; raising the drawdown limit
// accepts a deeper loss instead. They feel like the same fix and they are
// opposites, which is exactly the kind of thing an operator should not have to
// infer from a number.
// ---------------------------------------------------------------------

import { useCallback, useEffect, useState } from 'react';

import { Badge } from '@/components/ui/Badge';
import { Card, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';
import { describeWriteRefusal } from '@/lib/writeBlocker';

type RiskConfig = {
  varFraction: number;
  drawdownLimit: number;
  verdict: 'SAFE' | 'UNSAFE' | 'UNKNOWN';
  sessionAllowed: boolean;
  realMoneyAllowed: boolean;
  lossPerStopPct: number | null;
  lossesToHalt: number | null;
  requiredLosses: number;
  reason: string;
  suggestions: {
    largestVarFractionThatFits: number | null;
    smallestDrawdownThatAccepts: number | null;
    note: string;
  };
};

export function RiskConfigPanel() {
  const [data, setData] = useState<RiskConfig | null>(null);
  const [varText, setVarText] = useState('');
  const [ddText, setDdText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(backendProxyPath('/api/admin/risk-config'), {
        credentials: 'same-origin',
      });
      if (!res.ok) return;
      const json = (await res.json()) as RiskConfig;
      setData(json);
      // Seeded once per load so a half-typed value is not overwritten by the
      // poll — the same reason the exit-rules panel keeps its own draft state.
      setVarText((prev) => (prev === '' ? String(json.varFraction) : prev));
      setDdText((prev) => (prev === '' ? String(json.drawdownLimit) : prev));
    } catch {
      // Unreachable backend is a different problem with a different message;
      // claiming a bad risk setting would send the operator to the wrong file.
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const res = await fetch(backendProxyPath('/api/admin/risk-config'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          varFraction: Number(varText),
          drawdownLimit: Number(ddText),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        // The backend REFUSES an incoherent pair rather than saving it, and
        // its refusal already names the largest value that would fit. Passed
        // through as written: flattening it into a friendly sentence would
        // lose the number the operator needs.
        setError(describeWriteRefusal(res.status, body?.detail ?? ''));
      } else {
        setSaved(
          `Saved. A stop-out now costs about ${body.lossPerStopPct}% of equity, ` +
            `and ${body.lossesToHalt} consecutive losses would halt the account. ` +
            `${body.appliesFrom}.`,
        );
        await load();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'the request did not complete');
    } finally {
      setBusy(false);
    }
  }, [busy, varText, ddText, load]);

  if (!data) return null;

  const blocked = !data.sessionAllowed;
  const suggestion = data.suggestions.largestVarFractionThatFits;

  return (
    <Card>
      <SectionTitle>Risk configuration</SectionTitle>

      {/* THE VERDICT FIRST. This is what decides whether Start works at all. */}
      <div className="flex items-center gap-2 mb-2">
        <Badge
          state={data.verdict === 'SAFE' ? 'HEALTHY' : data.verdict === 'UNSAFE' ? 'FAIL' : 'WARN'}
          label={
            data.verdict === 'SAFE'
              ? 'Sessions can start'
              : data.verdict === 'UNSAFE'
                ? 'Sessions are REFUSED'
                : 'Not measurable'
          }
        />
        {data.lossPerStopPct !== null && data.lossesToHalt !== null ? (
          <span className="text-xs" style={{ color: 'var(--text-dim)' }}>
            {data.lossPerStopPct}% per stop-out · {data.lossesToHalt} losses would halt ·
            needs {data.requiredLosses}
          </span>
        ) : null}
      </div>

      {blocked ? (
        <p
          className="text-xs mb-3"
          style={{ color: 'var(--negative)' }}
          data-testid="risk-blocker"
        >
          {data.reason}
          {suggestion
            ? ` At this drawdown limit the largest VaR fraction that fits is ${suggestion}.`
            : ''}
        </p>
      ) : null}

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs">
          <span style={{ color: 'var(--text-dim)' }}>VaR fraction per trade</span>
          <input
            className="block mt-1 px-2 py-1 w-28"
            value={varText}
            onChange={(e) => setVarText(e.target.value)}
            inputMode="decimal"
          />
        </label>
        <label className="text-xs">
          <span style={{ color: 'var(--text-dim)' }}>Drawdown limit</span>
          <input
            className="block mt-1 px-2 py-1 w-28"
            value={ddText}
            onChange={(e) => setDdText(e.target.value)}
            inputMode="decimal"
          />
        </label>
        <button type="button" className="chip" onClick={save} disabled={busy}>
          {busy ? '…' : 'Save'}
        </button>
        {suggestion && blocked ? (
          <button
            type="button"
            className="chip"
            onClick={() => setVarText(String(suggestion))}
            disabled={busy}
          >
            Use {suggestion}
          </button>
        ) : null}
      </div>

      {/* THE TWO LEVERS ARE OPPOSITES, and that has to be said rather than
          inferred: they both make the refusal go away. */}
      <p className="text-xs mt-3" style={{ color: 'var(--text-dim)' }}>
        {data.suggestions.note}
      </p>

      {error ? (
        <p className="text-xs mt-2" style={{ color: 'var(--negative)' }}>
          {error}
        </p>
      ) : null}
      {saved ? (
        <p className="text-xs mt-2" style={{ color: 'var(--positive)' }}>
          {saved}
        </p>
      ) : null}
    </Card>
  );
}
