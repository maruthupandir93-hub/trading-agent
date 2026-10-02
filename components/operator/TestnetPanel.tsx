'use client';

// ---------------------------------------------------------------------
// BYBIT TESTNET MIRROR — make a paper fill a real fill.
//
// The operator wanted to validate execution before risking money: paper trades
// that place REAL orders on Bybit's testnet, so the fill price is the exchange's
// and not a model of one. A simulated fill books at the last observed price,
// instantly, in full — no spread crossed, no slippage, no partial fill, no
// minimum size, no leverage rejection. Every one of those is a real cost that
// turns up on day one of real money and on none of the paper days before it.
//
// WHAT THIS PANEL MUST MAKE UNMISTAKABLE, because getting it wrong is expensive:
//
//   * this is NOT live trading and cannot become it — the mirror is only
//     reachable from the execution agent's simulation branch, which is closed
//     whenever LIVE_TRADING is on;
//   * the P&L stays PAPER. Only the FILL changes;
//   * enabling it without credentials is refused, not accepted-and-broken —
//     a switch that reports success while every order is rejected is the
//     `simulation_mode` failure again;
//   * a testnet outage costs faithfulness, never a trade: the fill falls back to
//     the simulated one and the row records which.
//
// The blocker is rendered BEFORE the control, for the reason the venue panel
// gives: discovering a refusal from a 400 is worse than never being offered the
// action.
// ---------------------------------------------------------------------

import { useCallback, useEffect, useState } from 'react';
import { Card, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';

interface Check {
  ok: boolean;
  checkedAt?: number;
  reason?: string;
  balanceUsdt?: number;
}

interface Status {
  enabled: boolean;
  credentialsPresent: boolean;
  venue: string;
  supportedVenues?: string[];
  keyVariable: string;
  secretVariable: string;
  liveTradingOn: boolean;
  reachable: boolean;
  lastCheck: Check | null;
  note: string;
}

export function TestnetPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [check, setCheck] = useState<Check | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(backendProxyPath('/api/admin/testnet'), { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: Status = await res.json();
      setStatus(json);
      setCheck(json.lastCheck);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not read the testnet status');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggle = useCallback(async (next: boolean, venue?: string) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(backendProxyPath('/api/admin/testnet'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // `verify` makes a REAL authenticated call, so it is only worth paying
        // for when switching ON. Turning off must never be blocked by the venue
        // being unreachable.
        // `venue` is only sent when the operator picked one, so toggling the
        // mirror off and on never silently moves sandbox.
        body: JSON.stringify({ enabled: next, verify: next, ...(venue ? { venue } : {}) }),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json?.detail ?? `HTTP ${res.status}`);
      setCheck(json.verification ?? null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not change the setting');
    } finally {
      setBusy(false);
    }
  }, [load]);

  if (!status) {
    return (
      <Card>
        <SectionTitle>Bybit testnet — real fills for paper trades</SectionTitle>
        <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
          {error ?? 'Loading…'}
        </div>
      </Card>
    );
  }

  const blocked = !status.credentialsPresent;

  return (
    <Card>
      <SectionTitle>Bybit testnet — real fills for paper trades</SectionTitle>

      <p className="text-[11px] mb-3 leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
        Paper trades place a <strong>real market order on Bybit&apos;s testnet</strong> and your
        book is credited with the price the exchange actually returned — spread, slippage and
        partial fills included. The money stays paper; only the fill becomes real.
      </p>

      {/* THE BLOCKER, BEFORE THE CONTROL. */}
      {blocked && (
        <div
          className="text-[10.5px] mb-3 p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--warning) 12%, transparent)',
            border: '1px solid var(--warning)',
            color: 'var(--text-secondary)',
          }}
        >
          <strong>No testnet credentials.</strong> Set <span className="mono">{status.keyVariable}</span>{' '}
          and <span className="mono">{status.secretVariable}</span> in <span className="mono">.env</span>,
          then restart the backend. They are deliberately separate from your mainnet keys:
          verifying on testnet must never require pasting a testnet key over a live one,
          because putting the live one back is where a real key ends up in play by accident.
        </div>
      )}

      {status.enabled && status.liveTradingOn && (
        <div
          className="text-[10.5px] mb-3 p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--warning) 12%, transparent)',
            border: '1px solid var(--warning)',
            color: 'var(--text-secondary)',
          }}
        >
          <strong>Not mirroring right now.</strong> Live trading is on, so orders go to the real
          venue and the mirror is unreachable — it only ever runs on the simulated path. Turn
          live trading off to go back to testnet-backed paper trading.
        </div>
      )}

      {/* WHICH SANDBOX. Both are testnets and neither can reach mainnet, but
          they get there differently and the difference is worth surfacing:
          Bybit goes through ccxt's sandbox client, Binance cannot — ccxt
          hard-refuses binanceusdm sandbox and a hand-rolled url override
          silently reaches mainnet — so it uses a direct client with one
          hardcoded host. Changing venue while the mirror is ON re-verifies. */}
      <div className="flex gap-1.5 mb-2">
        {(status.supportedVenues ?? ['binance', 'bybit']).map((v) => {
          const on = status.venue === v;
          return (
            <button
              key={v}
              type="button"
              disabled={busy}
              onClick={() => void toggle(status.enabled, v)}
              className="flex-1 py-1.5 rounded text-[11px] mono"
              style={{
                background: on ? 'var(--accent)' : 'var(--bg-surface-2)',
                color: on ? 'var(--bg-base)' : 'var(--text-secondary)',
                border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                cursor: busy ? 'default' : 'pointer',
              }}
            >
              {v}
            </button>
          );
        })}
      </div>

      <button
        type="button"
        onClick={() => void toggle(!status.enabled)}
        disabled={busy || (blocked && !status.enabled)}
        className="w-full py-2 rounded text-[12px] font-semibold"
        style={{
          background: status.enabled ? 'var(--accent)' : 'var(--bg-surface-2)',
          color: status.enabled ? 'var(--bg-base)' : 'var(--text-secondary)',
          border: `1px solid ${status.enabled ? 'var(--accent)' : 'var(--border)'}`,
          cursor: busy || (blocked && !status.enabled) ? 'default' : 'pointer',
          opacity: blocked && !status.enabled ? 0.5 : 1,
        }}
      >
        {busy
          ? 'Checking…'
          : !status.enabled
            ? 'Connect to Bybit testnet'
            : /* ENABLED IS NOT CONNECTED, and the label used to say it was.
                 The setting deliberately STAYS ON when verification fails —
                 reverting it would hide a fixable problem (a revoked key, a
                 mainnet key in the testnet slot) behind a switch that silently
                 refused to move. But the operator was then shown "Connected to
                 Bybit testnet" directly above "Verification failed", and the
                 whole reason to turn this on is to trust that paper fills are
                 real. An unverified mirror falls back to simulated fills, so
                 the label has to say so. */
              check && !check.ok
                ? 'Enabled but NOT verified — click to disconnect'
                : 'Connected to Bybit testnet — click to disconnect'}
      </button>

      {/* The verification result. An unchecked connection is not a working one. */}
      {check && (
        <div
          className="text-[10.5px] mt-2 p-2 rounded leading-relaxed"
          style={{
            background: check.ok
              ? 'color-mix(in srgb, var(--success) 12%, transparent)'
              : 'color-mix(in srgb, var(--danger) 12%, transparent)',
            border: `1px solid ${check.ok ? 'var(--success)' : 'var(--danger)'}`,
            color: 'var(--text-secondary)',
          }}
        >
          <strong>{check.ok ? 'Credentials verified.' : 'Verification failed.'}</strong>{' '}
          {check.reason}
        </div>
      )}

      {error && (
        <div className="text-[11px] mt-2" style={{ color: 'var(--danger)' }}>{error}</div>
      )}

      <div className="text-[10px] mt-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        A testnet failure never blocks a trade — the fill falls back to the simulated one and the
        trade row records which happened: an exchange order id is present for a mirrored fill and
        absent for a simulated one. Closes are mirrored too, reduce-only, so the testnet account
        does not drift from your paper book.
      </div>
    </Card>
  );
}
