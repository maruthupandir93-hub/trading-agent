'use client';

// ---------------------------------------------------------------------
// Reset the paper book.
//
// WHY A BUTTON RATHER THAN "JUST DELETE THE ROWS"
//
// Clearing `agent_positions`, `agent_paper_account` and `monitored_positions` in
// SQL while the backend is running LOOKS like it works and does not last. The
// backend holds the book in a module-level dict and the watch list in the
// monitor's own memory, and both re-persist themselves over the top on the next
// write — so a deleted position is back within seconds, and the dashboard still
// reads "Open positions: 1".
//
// `POST /api/admin/reset-paper` clears the in-memory state FIRST and lets it
// persist down to empty, which is the only ordering that sticks.
//
// THE CONFIRMATION PHRASE IS NOT DECORATION. This is irreversible and the route
// is reachable over HTTP; the backend checks the literal string too, so a stray
// POST cannot erase a book. Typing it is the operator saying which book they mean.
// ---------------------------------------------------------------------

import { useState } from 'react';

import { Card, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';

const PHRASE = 'RESET PAPER';

type ResetResult = {
  positionsCleared?: number;
  watchedCleared?: number;
  watchListPersisted?: boolean;
  cash?: number;
  tradesDeleted?: number | string | null;
  detail?: string;
};

export function ResetPaperBookPanel({ liveTrading }: { liveTrading: boolean }) {
  const [cash, setCash] = useState('10000');
  const [clearTrades, setClearTrades] = useState(true);
  const [phrase, setPhrase] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ResetResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const cashNum = Number.parseFloat(cash);
  const ready = phrase === PHRASE && Number.isFinite(cashNum) && cashNum > 0 && !liveTrading;

  async function run() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch(backendProxyPath('/api/admin/reset-paper'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ startingCash: cashNum, clearTradeLog: clearTrades, confirm: PHRASE }),
      });
      const body = (await res.json().catch(() => ({}))) as ResetResult;
      if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`);
      setResult(body);
      setPhrase('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the reset failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <SectionTitle>Reset paper book</SectionTitle>

      <div className="text-[11px] mb-3 leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
        Sets paper cash back to a chosen figure, closes nothing but forgets every paper
        position, and empties the stop-loss watch list. Decisions, reflections and graph
        traces are kept — they are the record of how the book got here.
      </div>

      {liveTrading ? (
        // Refused in the UI and again in the backend. Clearing the watch list while
        // real positions are open would leave them on the venue with nothing
        // enforcing their stop — the worst action this app could take.
        <div
          className="text-[11px] p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--negative) 12%, transparent)',
            color: 'var(--negative)',
          }}
        >
          <strong>Unavailable while LIVE_TRADING is on.</strong> Open positions may be real,
          and forgetting them would stop the monitor enforcing their stop-loss while they
          stay open on the exchange. Turn live trading off first.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 mb-2">
            <div>
              <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
                Starting cash ($)
              </label>
              <input
                type="text"
                inputMode="decimal"
                value={cash}
                onChange={(e) => setCash(e.target.value)}
                className="w-full mono text-[13px] px-2 py-1.5 rounded"
                style={inputStyle}
              />
            </div>
            <div>
              <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
                Type {PHRASE} to confirm
              </label>
              <input
                type="text"
                value={phrase}
                onChange={(e) => setPhrase(e.target.value)}
                placeholder={PHRASE}
                className="w-full mono text-[13px] px-2 py-1.5 rounded"
                style={inputStyle}
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-[11px] mb-3" style={{ color: 'var(--text-secondary)' }}>
            <input type="checkbox" checked={clearTrades} onChange={(e) => setClearTrades(e.target.checked)} />
            Also delete the paper trade log (P&amp;L, win rate and history all reset)
          </label>

          <button
            type="button"
            disabled={!ready || busy}
            onClick={() => void run()}
            className="w-full py-2 rounded text-[12.5px] font-semibold uppercase tracking-wide"
            style={{
              background: ready ? 'color-mix(in srgb, var(--negative) 18%, transparent)' : 'var(--bg-surface-2)',
              color: ready ? 'var(--negative)' : 'var(--text-muted)',
              border: `1px solid ${ready ? 'var(--negative)' : 'var(--border)'}`,
              cursor: ready ? 'pointer' : 'not-allowed',
            }}
          >
            {busy ? 'Resetting…' : 'Reset paper book'}
          </button>
        </>
      )}

      {result ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--positive)' }}>
          Done — cash ${result.cash?.toFixed(2)}, {result.positionsCleared ?? 0} position(s) and{' '}
          {result.watchedCleared ?? 0} watched position(s) cleared
          {typeof result.tradesDeleted === 'number' ? `, ${result.tradesDeleted} trade(s) deleted` : ''}.
          {/* A watch list that did not reach storage would come back on restart, so
              the difference is reported rather than folded into "success". */}
          {result.watchListPersisted === false ? (
            <span style={{ color: 'var(--warning)' }}>
              {' '}The watch list was cleared in memory but not in the database — a restart may
              restore it. Check the backend&apos;s database connection.
            </span>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--negative)' }}>
          {error}
        </div>
      ) : null}
    </Card>
  );
}

const inputStyle: React.CSSProperties = {
  background: 'var(--bg-surface-2)',
  border: '1px solid var(--border)',
  color: 'var(--text-primary)',
};
