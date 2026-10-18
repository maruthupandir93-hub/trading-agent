'use client';

// ---------------------------------------------------------------------
// /settings — runtime gates with an interactive live-trading toggle.
//
// The three execution gates:
//   GRAPH_EXECUTION_ENABLED      — may graph runs submit TARs (env var)
//   POSITION_MONITORING_ENABLED  — may monitoring decisions be applied (env var)
//   LIVE_TRADING                 — real money vs paper (TOGGLABLE from here)
//
// LIVE_TRADING is the only gate that can be changed from the browser.
// The other two are environment variables set in .env and shown read-only.
// The toggle calls POST /api/admin/live-trading/enable (with confirmation)
// or POST /api/admin/live-trading/disable.
// ---------------------------------------------------------------------

import { useState, useCallback } from 'react';
import { useAppState } from '@/components/AppState';
import { Badge } from '@/components/ui/Badge';
import { Card, NotAvailable, SectionTitle, TermTable } from '@/components/ui/primitives';
import { ResetPaperBookPanel } from '@/components/operator/ResetPaperBookPanel';
import { VenueSwitchPanel } from '@/components/operator/VenueSwitchPanel';
import { ExitRulesPanel } from '@/components/operator/ExitRulesPanel';
import { TestnetPanel } from '@/components/operator/TestnetPanel';
import { BACKEND_PATHS, backendProxyPath } from '@/lib/backendConfig';
import { describeWriteRefusal, writeBlocker, type BackendAuthStatus } from '@/lib/writeBlocker';
import { useBackend } from '@/lib/realtime/useRealtime';

interface TradingModeData {
  liveTradingEnabled?: boolean;
  graphExecutionEnabled?: boolean;
  positionMonitoringEnabled?: boolean;
  credentialsConfigured?: boolean;
  executionTab?: string;
  ordersRoutedTo?: string;
}

export default function SettingsPage() {
  const { config, setConfig, activeProvider, resolvedModel, hasKey } = useAppState();
  const tradingMode = useBackend<TradingModeData>(BACKEND_PATHS.tradingMode, { intervalMs: 5_000 });
  const exchange = useBackend<Record<string, unknown>>(BACKEND_PATHS.exchangeStatus, { intervalMs: 30_000 });
  const admin = useBackend<{ isPaused?: boolean; emergencyStop?: boolean; auth?: BackendAuthStatus }>(
    BACKEND_PATHS.adminStatus, { intervalMs: 15_000 },
  );

  // THE BLOCKER IS READ BEFORE ANYTHING IS CLICKED.
  //
  // Every control on this page is a write, and with TRADES_API_KEY unset the
  // backend refuses all of them with a 503 whose JSON used to land in a toast
  // one button at a time. The refusal is correct -- writes fail closed on a
  // real-money service -- but discovering it from a toast after clicking is the
  // failure `VenueSwitchPanel` already avoids by rendering its blocker above
  // its control. Same rule here, for the same reason.
  const blocker = writeBlocker(admin.data?.auth);
  const polymarket = useBackend<{ enabled?: boolean; gateMeaning?: string }>(BACKEND_PATHS.polymarket, { intervalMs: 60_000 });

  const live = tradingMode.data?.liveTradingEnabled === true;
  const graphExec = tradingMode.data?.graphExecutionEnabled === true;
  const posMon = tradingMode.data?.positionMonitoringEnabled === true;
  const hasCreds = tradingMode.data?.credentialsConfigured === true;

  const [toggling, setToggling] = useState(false);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const toggleLiveTrading = useCallback(async () => {
    if (toggling) return;
    setToggleError(null);

    if (!live) {
      // Enabling — require confirmation
      const confirmed = window.confirm(
        '⚠️ ENABLE REAL-MONEY TRADING?\n\n' +
        'This will route orders to a REAL exchange with REAL funds.\n\n' +
        '• Ensure your Binance API keys are configured\n' +
        '• Ensure you understand the risk limits (10× leverage, 3% per trade)\n' +
        '• The stop-loss only exists while the backend process is alive\n\n' +
        'Click OK to enable live trading.'
      );
      if (!confirmed) return;
    }

    setToggling(true);
    try {
      const url = backendProxyPath(live ? BACKEND_PATHS.liveTradingDisable : BACKEND_PATHS.liveTradingEnable);
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: live ? '{}' : JSON.stringify({ confirm: 'I understand this uses real funds' }),
      });
      // `res.ok` WAS NEVER CHECKED (F33). A 401 or 403 from the proxy, or a
      // 409 from the backend, answers `{detail: ...}` rather than
      // `{status: 'error'}` — so a REFUSED toggle showed no message at all and
      // the badge kept its previous state. The operator was told nothing and
      // had every reason to believe live trading had switched.
      //
      // A safety control that reports success while doing nothing is the
      // `simulation_mode` failure, and this is that control's own switch.
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          detail = body?.detail || body?.message || body?.error || detail;
        } catch {
          /* a non-JSON refusal is still a refusal */
        }
        // Rewritten rather than echoed: the backend's own text names an
        // escape hatch (ALLOW_UNAUTHENTICATED_WRITES) that is not an equal
        // option on a public deployment, and says nothing about the frontend
        // half of the same configuration. Anything unrecognised falls through
        // to the backend's wording unchanged.
        setToggleError(`Refused: ${describeWriteRefusal(res.status, detail)}`);
        // Re-read the real state rather than assuming the old one is still
        // right — the refusal may have come after a partial change.
        tradingMode.reload();
        return;
      }
      const data = await res.json();
      if (data.status === 'error') {
        setToggleError(data.message);
      } else {
        // Force refetch of trading mode
        tradingMode.reload();
      }
    } catch (e) {
      setToggleError(`Failed to toggle: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setToggling(false);
    }
  }, [live, toggling, tradingMode]);

  return (
    <div className="space-y-3">
      <h1 className="text-[17px] font-semibold">Settings</h1>

      {blocker ? (
        <Card>
          <div className="flex items-center gap-2 mb-2">
            <Badge state="CRITICAL" label="Writes disabled" />
            <span className="text-[12px] font-semibold">{blocker.title}</span>
          </div>
          <p className="text-[11.5px] leading-relaxed mb-2" style={{ color: 'var(--text-secondary)' }}>
            Reads still work, which is why this page renders while every control on it is
            refused. Nothing is open — writes fail closed on purpose. Until this is set the
            agent cannot start a session, enable live trading, switch venue, or fire the
            emergency stop.
          </p>
          <ol className="text-[11.5px] leading-relaxed list-decimal pl-4 space-y-1"
              style={{ color: 'var(--text-secondary)' }}>
            {blocker.steps.map((step) => (
              <li key={step} className="mono text-[11px]">{step}</li>
            ))}
          </ol>
        </Card>
      ) : null}

      {/* ─── Trading Mode Toggle ─── */}
      <Card>
        <SectionTitle>Trading Mode</SectionTitle>
        <div className="flex items-center gap-4 mb-3">
          <div className="flex-1">
            <div className="flex items-center gap-2 mb-1">
              <Badge
                state={live ? 'CRITICAL' : 'PASS'}
                label={live ? '🔴 LIVE — REAL MONEY' : '🟢 Paper Trading — Safe'}
              />
              {toggling && <span className="text-[10px] text-txt2 animate-pulse">switching…</span>}
            </div>
            <p className="text-[11px]" style={{ color: 'var(--text-secondary)' }}>
              {live
                ? `Orders routed to: ${tradingMode.data?.ordersRoutedTo ?? 'exchange'}. Tab: ${tradingMode.data?.executionTab ?? 'real'}.`
                : 'Orders are simulated. No real exchange calls. All trades logged as paper.'}
            </p>
          </div>
          <button
            onClick={toggleLiveTrading}
            disabled={toggling}
            className={`
              relative px-4 py-2 rounded-lg text-[12px] font-mono font-semibold border-2 transition-all duration-200
              ${live
                ? 'border-red bg-red/10 text-red hover:bg-red/20'
                : 'border-green bg-green/10 text-green hover:bg-green/20'}
              disabled:opacity-40
              ${live ? 'shadow-[0_0_12px_rgba(239,68,68,0.15)]' : ''}
            `}
          >
            {live ? 'Switch to Paper' : 'Enable Live Trading'}
          </button>
        </div>
        {toggleError && (
          <div className="text-[11px] text-red bg-red/5 border border-red/20 rounded px-2.5 py-1.5 mb-2">
            {toggleError}
          </div>
        )}
        {!hasCreds && !live && (
          <div className="text-[10.5px] text-amber bg-amber/5 border border-amber/20 rounded px-2.5 py-1.5 mb-2">
            ⚠️ No exchange credentials configured. Set BINANCE_API_KEY and BINANCE_SECRET in .env before enabling live trading.
          </div>
        )}
      </Card>

      {/* ─── Pipeline Gates ─── */}
      <Card>
        <SectionTitle>Pipeline Gates</SectionTitle>
        <TermTable columns={[{ key: 'g', label: 'Gate' }, { key: 'v', label: 'Status' }, { key: 'e', label: 'Effect' }]}>
          <tr>
            <td className="mono text-[11.5px]">GRAPH_EXECUTION_ENABLED</td>
            <td>
              <Badge state={graphExec ? 'PASS' : 'IDLE'} label={graphExec ? 'ON — TARs submitted' : 'OFF — dry run'} />
            </td>
            <td className="text-[11px] whitespace-normal max-w-[480px]" style={{ color: 'var(--text-secondary)' }}>
              Graph reasoning runs can submit Trade Action Requests through the CRO for approval.
            </td>
          </tr>
          <tr>
            <td className="mono text-[11.5px]">POSITION_MONITORING_ENABLED</td>
            <td>
              <Badge state={posMon ? 'PASS' : 'IDLE'} label={posMon ? 'ON — applied' : 'OFF — logged only'} />
            </td>
            <td className="text-[11px] whitespace-normal max-w-[480px]" style={{ color: 'var(--text-secondary)' }}>
              Position monitoring decisions (stop tightening, REDUCE, EXIT) are applied to open positions.
            </td>
          </tr>
          <tr>
            <td className="mono text-[11.5px]">LIVE_TRADING</td>
            <td><Badge state={live ? 'CRITICAL' : 'INFO'} label={live ? 'true — REAL MONEY' : 'false — paper'} /></td>
            <td className="text-[11px] whitespace-normal max-w-[480px]" style={{ color: 'var(--text-secondary)' }}>
              Toggleable above. Routes orders to the real exchange when on.
            </td>
          </tr>
          <tr>
            <td className="mono text-[11.5px]">POLYMARKET_ENABLED</td>
            <td><Badge state={polymarket.data?.enabled ? 'PASS' : 'IDLE'} label={String(polymarket.data?.enabled ?? '—')} /></td>
            <td className="text-[11px] whitespace-normal max-w-[480px]" style={{ color: 'var(--text-secondary)' }}>
              Registers two supplementary specialists and widens the panel from 7 nodes to 9,
              which changes every confidence number.
            </td>
          </tr>
          <tr>
            <td className="mono text-[11.5px]">Paused / Emergency stop</td>
            <td>
              <span className="flex gap-1.5">
                <Badge state={admin.data?.isPaused ? 'PAUSED' : 'ACTIVE'} />
                {admin.data?.emergencyStop ? <Badge state="CRITICAL" label="Stopped" /> : null}
              </span>
            </td>
            <td className="text-[11px] whitespace-normal max-w-[480px]" style={{ color: 'var(--text-secondary)' }}>
              Both are togglable from the top bar. Closing a position is never blocked by either.
            </td>
          </tr>
        </TermTable>
        <div className="text-[10.5px] mt-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          GRAPH_EXECUTION_ENABLED and POSITION_MONITORING_ENABLED are set in <span className="mono">.env</span> and
          read at call time. LIVE_TRADING can be toggled from the button above.
        </div>
      </Card>

      {admin.data?.auth?.note ? (
        <Card>
          <SectionTitle>API auth</SectionTitle>
          <div className="flex items-center gap-2 mb-2">
            {/* "All endpoints open" WAS A FALSE CLAIM and it is the one that
                wastes a day: it was true while `require_write_auth` was a
                no-op, and became the exact opposite of the truth when writes
                began failing closed. An operator reading it concluded
                everything was permitted while every button was being refused. */}
            <Badge
              state={
                admin.data.auth.writeAuthEnabled
                  ? 'PASS'
                  : admin.data.auth.openWritesAllowed
                    ? 'WARN'
                    : 'CRITICAL'
              }
              label={
                admin.data.auth.writeAuthEnabled
                  ? 'Write auth on'
                  : admin.data.auth.openWritesAllowed
                    ? 'Writes open — no key required'
                    : 'Writes refused — no key set'
              }
            />
          </div>
          <div className="text-[11.5px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
            {admin.data.auth.note}
          </div>
          {admin.data.auth.remedy ? (
            <div className="text-[11.5px] leading-relaxed mt-2" style={{ color: 'var(--text-secondary)' }}>
              {admin.data.auth.remedy}
            </div>
          ) : null}
        </Card>
      ) : null}

      <Card>
        <SectionTitle>Chat provider — stored in this browser</SectionTitle>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-[11px]" style={{ color: 'var(--text-secondary)' }}>
            <div className="mb-1">Provider</div>
            <select
              value={config.provider}
              onChange={(e) => setConfig((c) => ({ ...c, provider: e.target.value }))}
              className="px-2 py-1.5 text-[12px] rounded"
            >
              <option value={config.provider}>{activeProvider?.id ?? config.provider}</option>
            </select>
          </label>
          <label className="text-[11px]" style={{ color: 'var(--text-secondary)' }}>
            <div className="mb-1">Model</div>
            <input
              value={config.model ?? ''}
              placeholder={resolvedModel}
              onChange={(e) => setConfig((c) => ({ ...c, model: e.target.value }))}
              className="mono px-2 py-1.5 text-[12px] rounded w-[220px]"
            />
          </label>
          <label className="text-[11px]" style={{ color: 'var(--text-secondary)' }}>
            <div className="mb-1">API key</div>
            <input
              type="password"
              value={config.apiKeys?.[config.provider] ?? ''}
              onChange={(e) =>
                setConfig((c) => ({ ...c, apiKeys: { ...c.apiKeys, [c.provider]: e.target.value } }))
              }
              className="mono px-2 py-1.5 text-[12px] rounded w-[260px]"
              placeholder="sk-…"
            />
          </label>
          <Badge state={hasKey ? 'PASS' : 'WARN'} label={hasKey ? 'Key set' : 'No key'} />
        </div>
        <div className="text-[10.5px] mt-2.5 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          The key stays in this browser and is sent to <span className="mono">/api/chat</span>,
          which proxies to your provider. The backend has no LLM provider of its own — its
          resolver recognises only <span className="mono">null</span> — so without a key here
          there is no chat at all, and no server-side fallback.
        </div>
      </Card>

      {/* Directly under Trading Mode: which exchange the agent trades on is the
          same class of decision as whether it trades for real, and reading them
          together is how an operator knows what the next order actually does. */}
      {/* Placed above the venue switch: these are the numbers an operator
          adjusts while watching the agent trade, and every one of them applies
          on the next tick with no restart. */}
      <ExitRulesPanel />

      {/* Directly under the exit rules and above the venue switch: this decides
          whether a PAPER fill is simulated or comes from a real exchange, which
          is the same class of decision as which venue the agent trades on and
          belongs beside it rather than buried further down. */}
      <TestnetPanel />

      <VenueSwitchPanel />

      {/* Housed here, beside the live-trading toggle, because this is already the
          page for controls that change state irreversibly rather than display it. */}
      <ResetPaperBookPanel liveTrading={live} />

      <NotAvailable
        what="Notification settings"
        reason="there is no notification subsystem — no email, webhook or push transport exists in the backend, so there is nothing to configure."
      />
    </div>
  );
}
