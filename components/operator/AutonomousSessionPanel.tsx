'use client';

// ---------------------------------------------------------------------
// The autonomous session ticket.
//
// NOT AN ORDER FORM. There is no Buy button and no Long/Short choice, because
// the operator is not placing a trade — they are setting an objective and handing
// it to the agent. Which coin, how much leverage the agent may use, what the
// account starts at, what it stops at, and what it gives up at.
//
// EVERY AMOUNT HERE IS A WALLET BALANCE, NOT A COIN PRICE
//
// "$2 into $5" is a statement about the ACCOUNT. The session ends when the
// account reaches $5, whatever SOL is worth at that moment. That is deliberately
// not a price target: a price target says nothing about how much was staked, so
// the same move could double the account or barely touch it, and a session built
// on one would have no idea when it was finished. Both fields are labelled in
// dollars for that reason.
//
// THE STARTING AMOUNT IS TYPED ON PAPER AND FETCHED ON REAL
//
// On the real book it is the exchange's own free balance, read-only, because a
// typed figure would set the denominator of every percentage this session reports
// while the venue held a different number. On paper the operator sets it, and the
// backend writes it to the paper book's cash — so a $2 run is sized, marked and
// scored as a $2 run rather than looking like one while behaving like a $10,000
// one.
//
// Everything after "Start" is the agent's: it runs the full 23-node decision
// graph on a timer, and when the Supervisor decides to trade AND the Risk Gateway
// approves, the ordinary execution chain fills it and the position monitor
// enforces the stop. The panel's job after that is to show what it is doing.
//
// THE TARGET IS DISPLAYED AS A STOP CONDITION, DELIBERATELY
//
// It is labelled "stop when equity reaches", not "profit goal". The backend reads
// it in exactly one place — the check that ends the session — and never passes it
// to sizing. Presenting it as a goal the agent is pushing toward would suggest it
// takes more risk when behind, which is precisely what CLAUDE.md forbids and what
// `services/trading_session.py` is built not to do.
// ---------------------------------------------------------------------

import { useCallback, useEffect, useMemo, useState } from 'react';

import { Badge } from '@/components/ui/Badge';
import { Card, Num, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';

type Session = {
  id: string;
  symbol: string;
  leverage: number;
  start_equity: number;
  target_equity: number;
  floor_equity: number;
  capital_fraction: number;
  status: string;
  started_at: number;
  finished_at: number | null;
  stop_reason: string | null;
  cycles_run: number;
  trades_opened: number;
  last_cycle_at: number | null;
  last_decision: string | null;
  last_rationale: string | null;
  log: { ts: number; message: string; decision?: string }[];
};

type Status = {
  active: Session | null;
  recent: Session[];
  tab: 'paper' | 'real';
  liveTrading: boolean;
  currentEquity: number | null;
  equityNote: string | null;
  /** Real book only: the exchange's free USDT. null on paper, and null — never 0 — when unreadable. */
  realBalance: number | null;
  realBalanceError: string | null;
  /** False on the real book, where the amount comes from the venue. */
  startAmountEditable: boolean;
  startAmountMeaning: string;
  /** The three parts of equity. One flat number cannot distinguish an idle book
   *  from a stuck one; these can. */
  equityBreakdown?: {
    freeCash: number | null;
    lockedMargin: number;
    unrealized: number;
    equity: number | null;
    openPositions: number;
    unpricedSymbols: string[];
    meaning: string;
  };
  /** Progress toward the stop condition, computed server-side so it cannot
   *  disagree with the check that actually ends the session. */
  progress?: {
    fraction: number | null;
    percent: number | null;
    gained: number | null;
    remaining: number | null;
    reason: string | null;
  } | null;
  decisionIntervalSeconds: number;
  maxSessionHours: number;
  maxTrades: number;
  targetMeaning: string;
  floorMeaning: string;
  stopMeaning: string;
};

const SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT'] as const;

const STATUS_BADGE: Record<string, string> = {
  running: 'RUNNING',
  reached: 'PASS',
  floored: 'FAIL',
  stopped: 'IDLE',
  expired: 'WARN',
  failed: 'FAIL',
};

export function AutonomousSessionPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [symbol, setSymbol] = useState<string>('BTC/USDT');
  const [leverage, setLeverage] = useState(2);
  const [startAmount, setStartAmount] = useState('');
  const [target, setTarget] = useState('');
  const [floor, setFloor] = useState('');
  // How much of the balance this session may trade with. 100% = the whole
  // account (the pre-feature default). 25/50/75 keep more in reserve.
  const [capitalPct, setCapitalPct] = useState(100);

  const load = useCallback(async () => {
    try {
      const res = await fetch(backendProxyPath('/api/session'), { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStatus((await res.json()) as Status);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not read session status');
    }
  }, []);

  useEffect(() => {
    void load();
    // 5s. A decision cycle is a minute apart, but the operator wants to see the
    // cycle counter and the rationale move without reloading the page.
    const id = setInterval(() => void load(), 5_000);
    return () => clearInterval(id);
  }, [load]);

  const active = status?.active ?? null;
  const equity = status?.currentEquity ?? null;
  const isReal = status?.tab === 'real';
  const canEditStart = status?.startAmountEditable ?? true;

  const startNum = startAmount.trim() ? Number.parseFloat(startAmount) : null;
  const targetNum = Number.parseFloat(target);
  const floorNum = floor.trim() ? Number.parseFloat(floor) : null;

  // The equity the session will ACTUALLY start from, which is what the target and
  // floor must be checked against. On paper an entered starting amount replaces
  // the book's cash, so validating the target against the CURRENT balance instead
  // would reject a perfectly good "2 -> 5" whenever the paper book still held its
  // old figure — the exact request the operator is making.
  const effectiveStart = canEditStart && startNum !== null && startNum > 0 ? startNum : equity;

  // Suggested target once a starting figure is known. A suggestion only, never
  // auto-applied, and it follows `effectiveStart` so typing 2 proposes 2.20
  // rather than something derived from a balance about to be replaced.
  useEffect(() => {
    if (effectiveStart !== null && target === '') setTarget((effectiveStart * 1.1).toFixed(2));
  }, [effectiveStart, target]);

  const problems = useMemo(() => {
    const out: string[] = [];
    if (active) return out;

    if (canEditStart) {
      if (startAmount.trim() && (!Number.isFinite(startNum as number) || (startNum as number) <= 0)) {
        out.push('the starting amount must be a positive number');
      }
    } else if (equity === null) {
      // Real book: nothing is typeable, so an unreadable balance is terminal and
      // the reason has to name the exchange rather than the form.
      out.push(
        status?.realBalanceError
          ? `the exchange balance could not be read: ${status.realBalanceError}`
          : (status?.equityNote ?? 'the exchange balance is not readable'),
      );
    }

    if (effectiveStart === null) {
      out.push(status?.equityNote ?? 'a starting amount is needed before a session can define "done"');
    }
    if (!Number.isFinite(targetNum) || targetNum <= 0) out.push('enter a target amount');
    else if (effectiveStart !== null && targetNum <= effectiveStart) {
      out.push(`the target must be above the starting amount of $${effectiveStart.toFixed(2)}`);
    }
    if (floorNum !== null) {
      if (!Number.isFinite(floorNum) || floorNum < 0) out.push('the floor must be zero or more');
      else if (effectiveStart !== null && floorNum >= effectiveStart) {
        out.push('the floor must be below the starting amount');
      }
    }
    return out;
  }, [active, equity, effectiveStart, startAmount, startNum, targetNum, floorNum, canEditStart, status]);

  const start = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(backendProxyPath('/api/session/start'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol,
          leverage,
          targetEquity: targetNum,
          // The fraction of the account this session may deploy. 100% preserves
          // the old behaviour exactly.
          capitalFraction: capitalPct / 100,
          // Only sent when the operator actually chose one, and never on the real
          // book — the backend rejects it there rather than trusting this check
          // alone.
          ...(canEditStart && startNum !== null ? { startAmount: startNum } : {}),
          ...(floorNum !== null ? { floorEquity: floorNum } : {}),
        }),
      });
      const text = await res.text();
      if (!res.ok) throw new Error(text.slice(0, 400));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not start the session');
    } finally {
      setBusy(false);
    }
  }, [symbol, leverage, targetNum, floorNum, startNum, capitalPct, canEditStart, load]);

  const stop = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(backendProxyPath('/api/session/stop'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(await res.text());
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not stop the session');
    } finally {
      setBusy(false);
    }
  }, [load]);

  // COMPUTED SERVER-SIDE, not here. The bar an operator reads and the check that
  // ends the session must come from one place — a progress display that
  // disagreed with the stop condition would be worse than none at all.
  //
  // Still NOT shown as a promise: the label says "toward the stop condition",
  // because a progress bar implies the agent is on a schedule and it is not.
  const progress = status?.progress ?? null;
  const fraction = progress?.fraction ?? null;
  const breakdown = status?.equityBreakdown ?? null;

  return (
    <Card>
      <SectionTitle
        action={
          <div className="flex items-center gap-2">
            <Badge state={isReal ? 'CRITICAL' : 'INFO'} label={isReal ? 'REAL MONEY' : 'Paper'} />
            {active ? <Badge state="RUNNING" label={`cycle ${active.cycles_run}`} /> : null}
          </div>
        }
      >
        Autonomous trading session
      </SectionTitle>

      <div className="text-[11px] mb-3 leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
        Set an objective and the agent trades toward it on its own — full 23-node
        analysis, all nine specialists, prediction markets, news, and the language model,
        every cycle. It opens positions only when the Supervisor decides to and the Risk
        Gateway approves, and the position monitor enforces the stop on every tick.
      </div>

      {isReal ? (
        <div
          className="text-[11px] mb-3 p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--negative) 12%, transparent)',
            color: 'var(--negative)',
          }}
        >
          <strong>LIVE_TRADING is ON.</strong> A session started now places REAL orders
          with real funds, unattended, until it reaches its target or its floor.
        </div>
      ) : null}

      {/* ---- running session ---- */}
      {active ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
            <Stat label="Coin" value={<span className="mono">{active.symbol}</span>} />
            <Stat label="Started from" value={<Num value={active.start_equity} digits={2} prefix="$" />} />
            <Stat label="Leverage" value={<span className="mono">{active.leverage}x</span>} />
            <Stat label="Allocation" value={<span className="mono">{Math.round((active.capital_fraction ?? 1) * 100)}%</span>} />
            <Stat label="Account now" value={<Num value={equity} digits={2} prefix="$" />} />
            <Stat label="Trades opened" value={<Num value={active.trades_opened} digits={0} />} />
          </div>

          <div className="mb-3">
            <div className="flex items-baseline justify-between text-[10.5px] mb-1">
              <span style={{ color: 'var(--text-muted)' }}>
                floor ${active.floor_equity.toFixed(2)}
              </span>
              {/* The number, not just a bar. A bar alone cannot say how far. */}
              <span className="mono text-[12px]" style={{ color: 'var(--text-primary)' }}>
                {progress?.percent === null || progress?.percent === undefined
                  ? '—'
                  : `${progress.percent.toFixed(1)}%`}
              </span>
              <span style={{ color: 'var(--text-muted)' }}>
                target ${active.target_equity.toFixed(2)}
              </span>
            </div>
            <div className="h-1.5 rounded overflow-hidden" style={{ background: 'var(--bg-surface-2)' }}>
              <div
                className="h-full"
                style={{
                  width: `${(fraction ?? 0) * 100}%`,
                  background: 'var(--positive)',
                  transition: 'width 400ms ease',
                }}
              />
            </div>

            <div className="flex items-baseline justify-between text-[10.5px] mt-1.5">
              <span style={{ color: 'var(--text-muted)' }}>
                gained{' '}
                {/* SIGNED AND UNCLAMPED. The bar floors at 0% so it cannot render
                    backwards, but a session that is DOWN must say so — showing 0%
                    with no other signal would read as "no progress yet". */}
                <span
                  className="mono"
                  style={{
                    color:
                      progress?.gained == null
                        ? 'var(--text-muted)'
                        : progress.gained >= 0
                          ? 'var(--positive)'
                          : 'var(--negative)',
                  }}
                >
                  {progress?.gained == null
                    ? '—'
                    : `${progress.gained >= 0 ? '+' : ''}${progress.gained.toFixed(2)}`}
                </span>
              </span>
              <span style={{ color: 'var(--text-muted)' }}>
                still needed{' '}
                <span className="mono">
                  {progress?.remaining == null ? '—' : `$${progress.remaining.toFixed(2)}`}
                </span>
              </span>
            </div>

            <div className="text-[10px] mt-1 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
              {progress?.reason
                ? progress.reason
                : 'Progress toward the stop condition. The agent does not size up when behind — the target never reaches the Risk Gateway.'}
            </div>
          </div>

          {/* WHERE THE MONEY IS. One "Account now" figure sitting at its starting
              value looks identical whether the book is FLAT or the number is
              STUCK, and those have opposite responses. Cash also does NOT move
              while a position is open — the margin is locked, not spent — which
              reads as a frozen balance unless the parts are shown. */}
          {breakdown ? (
            <div className="grid grid-cols-3 gap-2 mb-3 p-2 rounded" style={{ background: 'var(--bg-surface-2)' }}>
              <Stat label="Free cash" value={<Num value={breakdown.freeCash} digits={2} prefix="$" />} />
              <Stat label="Locked margin" value={<Num value={breakdown.lockedMargin} digits={2} prefix="$" />} />
              <Stat
                label="Unrealised"
                value={<Num value={breakdown.unrealized} digits={2} prefix="$" colored signed />}
              />
            </div>
          ) : null}

          <div className="mb-3 p-2 rounded" style={{ background: 'var(--bg-surface-2)' }}>
            <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
              Latest decision
            </div>
            <div className="text-[11.5px] mono mb-1">{active.last_decision ?? 'first cycle in progress…'}</div>
            <div className="text-[11px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
              {active.last_rationale ?? 'The agent is running its first analysis cycle.'}
            </div>
          </div>

          {active.log.length > 0 ? (
            <div className="mb-3 max-h-[140px] overflow-y-auto font-mono text-[10.5px] space-y-1">
              {[...active.log].reverse().map((e, i) => (
                <div key={i} className="flex gap-2">
                  <span className="shrink-0" style={{ color: 'var(--text-muted)' }}>
                    {new Date(e.ts * 1000).toLocaleTimeString().slice(0, 8)}
                  </span>
                  <span style={{ color: 'var(--text-secondary)' }}>{e.message}</span>
                </div>
              ))}
            </div>
          ) : null}

          <button
            type="button"
            className="w-full py-2.5 rounded text-[13px] font-semibold uppercase tracking-wide"
            disabled={busy}
            onClick={() => void stop()}
            style={{
              background: 'color-mix(in srgb, var(--negative) 18%, transparent)',
              color: 'var(--negative)',
              border: '1px solid var(--negative)',
            }}
          >
            {busy ? 'Stopping…' : 'Stop session'}
          </button>
          <div className="text-[10px] mt-1.5 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            {status?.stopMeaning}
          </div>
        </>
      ) : (
        /* ---- start form ---- */
        <>
          <div className="grid grid-cols-2 gap-2 mb-3">
            <Field label="Coin">
              <select
                className="w-full mono text-[12px] px-2 py-1.5 rounded"
                style={inputStyle}
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
              >
                {SYMBOLS.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </Field>

            <Field label={`Max leverage — ${leverage}x`}>
              <input
                type="range"
                min={1}
                max={isReal ? 3 : 10}
                step={1}
                value={leverage}
                onChange={(e) => setLeverage(Number.parseInt(e.target.value, 10))}
                className="w-full"
              />
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-2 mb-1">
            <Field
              label={canEditStart ? 'Start amount ($)' : 'Start amount ($) — from exchange'}
            >
              {canEditStart ? (
                <input
                  type="text"
                  inputMode="decimal"
                  value={startAmount}
                  onChange={(e) => setStartAmount(e.target.value)}
                  placeholder={equity !== null ? equity.toFixed(2) : '2.00'}
                  className="w-full mono text-[13px] px-2 py-1.5 rounded"
                  style={inputStyle}
                />
              ) : (
                // READ-ONLY ON THE REAL BOOK. Rendered as text rather than a
                // disabled input so it does not look like a field the operator
                // has failed to fill in.
                <div className="mono text-[13px] py-1.5">
                  {status?.realBalance === null || status?.realBalance === undefined ? (
                    <span style={{ color: 'var(--warning)' }}>unreadable</span>
                  ) : (
                    <Num value={status.realBalance} digits={2} prefix="$" />
                  )}
                </div>
              )}
            </Field>

            <Field label="Target amount ($)">
              <input
                type="text"
                inputMode="decimal"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                placeholder="5.00"
                className="w-full mono text-[13px] px-2 py-1.5 rounded"
                style={inputStyle}
              />
            </Field>
          </div>

          <div className="text-[10px] mt-1 mb-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            {canEditStart
              ? 'Your wallet balance, not a coin price. The paper book is set to this amount, so the run is sized and scored at exactly that size. The session ends when the account reaches the target.'
              : 'Fetched from your exchange account and cached for 30s — it is not typeable, because a figure you entered would not be the money you actually have.'}
          </div>

          {/* CAPITAL ALLOCATION — how much of the balance this session may use.
              Scales the size of every trade AND caps the total capital the agent
              may commit at once. Not a leverage source: the leverage ceiling and
              mandatory stop still bound every trade, so 100% means "use the whole
              account as margin", never "use more leverage". */}
          <div className="mb-2">
            <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
              Capital to trade with — {capitalPct}% of the balance
            </label>
            <div className="grid grid-cols-4 gap-1.5">
              {[25, 50, 75, 100].map((pct) => (
                <button
                  key={pct}
                  type="button"
                  onClick={() => setCapitalPct(pct)}
                  className="py-1.5 rounded text-[12px] mono font-semibold"
                  style={{
                    background:
                      capitalPct === pct
                        ? 'color-mix(in srgb, var(--accent) 20%, transparent)'
                        : 'var(--bg-surface-2)',
                    color: capitalPct === pct ? 'var(--accent)' : 'var(--text-secondary)',
                    border: `1px solid ${capitalPct === pct ? 'var(--accent)' : 'var(--border)'}`,
                  }}
                >
                  {pct}%
                </button>
              ))}
            </div>
            <div className="text-[10px] mt-1 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
              The session trades with this share of the account and never commits more than
              it at once. Each trade is still sized by the risk rules within it, and the
              leverage cap and stop-loss are unchanged.
            </div>
          </div>

          <Field label="Give up if the account falls to ($, optional)">
            <input
              type="text"
              inputMode="decimal"
              value={floor}
              onChange={(e) => setFloor(e.target.value)}
              placeholder={
                effectiveStart !== null ? `defaults to ${(effectiveStart * 0.5).toFixed(2)}` : 'defaults to half'
              }
              className="w-full mono text-[12px] px-2 py-1.5 rounded"
              style={inputStyle}
            />
          </Field>
          <div className="text-[10px] mt-1 mb-3 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            {status?.floorMeaning}
          </div>

          {problems.length > 0 ? (
            <ul className="text-[11px] mb-2 space-y-0.5" style={{ color: 'var(--text-muted)' }}>
              {problems.map((p) => <li key={p}>· {p}</li>)}
            </ul>
          ) : null}

          <button
            type="button"
            disabled={problems.length > 0 || busy}
            onClick={() => void start()}
            className="w-full py-2.5 rounded text-[13px] font-semibold uppercase tracking-wide"
            style={{
              background: problems.length > 0
                ? 'var(--bg-surface-2)'
                : `color-mix(in srgb, var(--${isReal ? 'negative' : 'positive'}) 20%, transparent)`,
              color: problems.length > 0 ? 'var(--text-muted)' : `var(--${isReal ? 'negative' : 'positive'})`,
              border: `1px solid ${problems.length > 0 ? 'var(--border)' : `var(--${isReal ? 'negative' : 'positive'})`}`,
              cursor: problems.length > 0 ? 'not-allowed' : 'pointer',
            }}
          >
            {busy ? 'Starting…' : isReal ? 'Start trading — REAL FUNDS' : 'Start autonomous trading (paper)'}
          </button>

          <div className="text-[10px] mt-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            One decision cycle every {status?.decisionIntervalSeconds ?? 60}s. Hard limits:
            {' '}{status?.maxSessionHours ?? 72}h and {status?.maxTrades ?? 200} trades, because
            &ldquo;until the target&rdquo; is not a bound on its own.
          </div>
        </>
      )}

      {/* ---- recent sessions ---- */}
      {status?.recent?.length ? (
        <div className="mt-3 pt-3 border-t hairline">
          <div className="text-[10px] uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-muted)' }}>
            Recent sessions
          </div>
          <div className="space-y-1">
            {status.recent.filter((s) => s.status !== 'running').slice(0, 4).map((s) => (
              <div key={s.id} className="flex items-start gap-2 text-[10.5px]">
                <Badge state={(STATUS_BADGE[s.status] ?? 'INFO') as never} label={s.status} />
                <span className="mono" style={{ color: 'var(--text-secondary)' }}>
                  {s.symbol} {s.start_equity.toFixed(0)}→{s.target_equity.toFixed(0)}
                </span>
                <span style={{ color: 'var(--text-muted)' }}>
                  {s.cycles_run} cycles, {s.trades_opened} trade(s)
                  {s.stop_reason ? ` — ${s.stop_reason}` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--negative)' }}>
          {error}
        </div>
      ) : null}

      <div className="text-[10px] mt-3 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        {status?.targetMeaning}
      </div>
    </Card>
  );
}

const inputStyle: React.CSSProperties = {
  background: 'var(--bg-surface-2)',
  border: '1px solid var(--border)',
  color: 'var(--text-primary)',
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
        {label}
      </label>
      {children}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider mb-0.5" style={{ color: 'var(--text-muted)' }}>
        {label}
      </div>
      <div className="text-[13px]">{value}</div>
    </div>
  );
}
