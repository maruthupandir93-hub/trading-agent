'use client';

// ---------------------------------------------------------------------
// Which exchange the agent trades on.
//
// THIS IS AN ACCOUNT CHANGE, NOT A PREFERENCE. Binance and Bybit are different
// accounts holding different money, with separate credentials. Switching changes
// where every order, balance read and position query goes. It moves no funds and
// closes nothing — which is exactly why it is dangerous with a position open:
// the position stays at the OLD venue while this process starts talking to the
// new one.
//
// So the panel leads with the blocker rather than the button. The backend
// refuses the switch while a real position is open and returns the reason; this
// shows that reason BEFORE the operator presses anything, because discovering it
// from a 409 is a worse experience than never being offered the action.
//
// CREDENTIALS ARE SHOWN PER VENUE for the same reason. Switching to a venue with
// no keys is legal — market data needs none — but every private call then fails.
// That is a fact worth knowing in advance, not at the first order.
// ---------------------------------------------------------------------

import { useCallback, useEffect, useState } from 'react';

import { Badge } from '@/components/ui/Badge';
import { Card, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';

type VenueRow = {
  id: string;
  current: boolean;
  credentialsConfigured: boolean;
  keyVariable: string;
};

type Status = {
  current: string;
  venues: VenueRow[];
  liveTrading: boolean;
  openPositions: number;
  realOpenPositions: number;
  canSwitch: boolean;
  blockedReason: string | null;
  meaning: string;
};

export function VenueSwitchPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [target, setTarget] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(backendProxyPath('/api/admin/venue'), { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStatus((await res.json()) as Status);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not read the venue status');
    }
  }, []);

  useEffect(() => {
    void load();
    // 30s: this changes only when the operator changes it, or when a position
    // opens or closes and flips `canSwitch`.
    const id = setInterval(() => void load(), 30_000);
    return () => clearInterval(id);
  }, [load]);

  const chosen = status?.venues.find((v) => v.id === target) ?? null;
  const ready = Boolean(status?.canSwitch) && target !== '' && confirm.trim().toLowerCase() === target;

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const res = await fetch(backendProxyPath('/api/admin/venue'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ venue: target, confirm: target }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`);
      setDone(
        body.warning
          ? `Now trading on ${body.current}. ${body.warning}`
          : `Now trading on ${body.current}.`,
      );
      setTarget('');
      setConfirm('');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the switch failed');
    } finally {
      setBusy(false);
    }
  }, [target, load]);

  return (
    <Card>
      <SectionTitle
        action={
          status ? (
            <span className="mono text-[11px]" style={{ color: 'var(--text-secondary)' }}>
              {status.current}
            </span>
          ) : null
        }
      >
        Exchange
      </SectionTitle>

      <div className="text-[11px] mb-3 leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
        {status?.meaning ??
          'Which exchange every order, balance read and position query goes to.'}
      </div>

      {/* Per-venue credentials. A venue with no keys still serves market data,
          so the switch is legal — but every private call fails, and that is
          worth seeing before pressing rather than at the first order. */}
      <div className="space-y-1.5 mb-3">
        {(status?.venues ?? []).map((v) => (
          <div key={v.id} className="flex items-center gap-2 text-[11.5px]">
            <span className="mono w-[70px]">{v.id}</span>
            {v.current ? <Badge state="ACTIVE" label="Trading" /> : <Badge state="IDLE" label="Idle" />}
            <Badge
              state={v.credentialsConfigured ? 'PASS' : 'WARN'}
              label={v.credentialsConfigured ? 'Keys set' : `No ${v.keyVariable}`}
            />
          </div>
        ))}
      </div>

      {/* THE BLOCKER, SHOWN BEFORE THE CONTROL. */}
      {status && !status.canSwitch ? (
        <div
          className="text-[11px] p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--negative) 12%, transparent)',
            color: 'var(--negative)',
          }}
        >
          <strong>Cannot switch right now.</strong> {status.blockedReason}
        </div>
      ) : (
        <>
          {status?.liveTrading ? (
            <div
              className="text-[11px] mb-2 p-2 rounded leading-relaxed"
              style={{
                background: 'color-mix(in srgb, var(--warning) 12%, transparent)',
                color: 'var(--warning)',
              }}
            >
              <strong>LIVE_TRADING is on.</strong> The next order after this switch goes to the
              new exchange with real funds.
            </div>
          ) : null}

          <div className="grid grid-cols-2 gap-2 mb-2">
            <div>
              <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
                Switch to
              </label>
              <select
                className="w-full mono text-[12px] px-2 py-1.5 rounded"
                style={inputStyle}
                value={target}
                onChange={(e) => {
                  setTarget(e.target.value);
                  setConfirm('');
                }}
              >
                <option value="">— choose —</option>
                {(status?.venues ?? [])
                  .filter((v) => !v.current)
                  .map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.id}
                    </option>
                  ))}
              </select>
            </div>
            <div>
              <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
                {target ? `Type "${target}" to confirm` : 'Confirm'}
              </label>
              <input
                type="text"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder={target || '—'}
                disabled={!target}
                className="w-full mono text-[12px] px-2 py-1.5 rounded"
                style={inputStyle}
              />
            </div>
          </div>

          {chosen && !chosen.credentialsConfigured ? (
            <div className="text-[10.5px] mb-2 leading-relaxed" style={{ color: 'var(--warning)' }}>
              {chosen.id} has no API keys. Market data will work; orders, balance and positions
              will be refused until <span className="mono">{chosen.keyVariable}</span> and its
              secret are set.
            </div>
          ) : null}

          <button
            type="button"
            disabled={!ready || busy}
            onClick={() => void submit()}
            className="w-full py-2 rounded text-[12.5px] font-semibold uppercase tracking-wide"
            style={{
              background: ready ? 'color-mix(in srgb, var(--accent) 18%, transparent)' : 'var(--bg-surface-2)',
              color: ready ? 'var(--accent)' : 'var(--text-muted)',
              border: `1px solid ${ready ? 'var(--accent)' : 'var(--border)'}`,
              cursor: ready ? 'pointer' : 'not-allowed',
            }}
          >
            {busy ? 'Switching…' : target ? `Switch to ${target}` : 'Switch exchange'}
          </button>
        </>
      )}

      {done ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--positive)' }}>
          {done}
        </div>
      ) : null}
      {error ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--negative)' }}>
          {error}
        </div>
      ) : null}

      <div className="text-[10px] mt-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        Persisted to <span className="mono">.env</span> as{' '}
        <span className="mono">EXCHANGE_ID</span>, so a restart keeps trading the venue you
        chose. Switching moves no funds and closes nothing.
      </div>
    </Card>
  );
}

const inputStyle: React.CSSProperties = {
  background: 'var(--bg-surface-2)',
  border: '1px solid var(--border)',
  color: 'var(--text-primary)',
};
