'use client';

// ---------------------------------------------------------------------
// The operator's manual trade ticket.
//
// WHAT THIS IS AND IS NOT
//
// A human pressing Buy or Sell. CLAUDE.md invariant 1 puts manual clicks
// deliberately outside the Supervisor: "supervising agents means supervising
// agents, not overriding the operator." So this does not go through the debate,
// the CRO or the risk gateway.
//
// The one thing it does NOT get to skip is the leverage ceiling. That is not a
// supervision rule, it is a hard limit on the account, and the backend enforces
// it with the same `check_leverage` the Risk Gateway calls. The slider here is
// clamped to whatever `/context` reports the ceiling to be rather than to a
// number hardcoded in this file, so the two cannot drift.
//
// TWO MODES, AND THE DIFFERENCE IS MADE UNMISSABLE
//
//   paper  -> POST /api/operator/trade/paper     no credentials, no venue
//   real   -> POST /api/operator/exchange/order  the operator's own keys
//
// The backend refuses to write a paper trade while LIVE_TRADING is on, because a
// Buy pressed in live mode means a real order and quietly booking a simulated
// one would leave the operator believing they hold a position they do not hold.
// This component mirrors that: in live mode the button says REAL and the form
// asks for keys.
//
// ONE FETCH PER SYMBOL CHANGE, NOT ONE PER KEYSTROKE
//
// Price, balance, the leverage ceiling and the instrument's size rules all come
// from a single `/context` call, and the authenticated balance behind it is
// cached server-side for 30s. Sizing controls recompute locally from the numbers
// already fetched; typing a quantity costs nothing.
// ---------------------------------------------------------------------

import { useCallback, useEffect, useMemo, useState } from 'react';

import { Badge } from '@/components/ui/Badge';
import { Card, Num, SectionTitle } from '@/components/ui/primitives';
import { backendProxyPath } from '@/lib/backendConfig';

type Balance = {
  available: number | null;
  currency: string;
  ageSeconds: number | null;
  cached: boolean;
  error: string | null;
};

type Rules = {
  minQty?: number | null;
  stepSize?: number | null;
  tickSize?: number | null;
  minNotional?: number | null;
  unavailable?: string | null;
};

type Context = {
  symbol: string;
  tab: 'paper' | 'real';
  liveTrading: boolean;
  price: number | null;
  priceNote: string | null;
  balance: Balance;
  leverageCeiling: number;
  leverageNote: string;
  instrumentRules: Rules;
  openPositions: { symbol: string; qty: number; avgCost: number }[];
  credentialsConfigured: boolean;
  balanceCacheSeconds: number;
};

// The symbols the backend's tick stream and agent actually watch. Offering a
// coin the backend does not price would produce a ticket that cannot be filled,
// which is a worse experience than a shorter list.
const SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT'] as const;

const SIZE_PRESETS = [0.1, 0.25, 0.5, 1.0] as const;

/** Round DOWN to the instrument's step. Rounding up can exceed the balance the
 *  size was computed against, and the venue would reject it after the operator
 *  had already committed. */
function toStep(qty: number, step: number | null | undefined): number {
  if (!step || step <= 0) return qty;
  return Math.floor(qty / step) * step;
}

function decimalsFor(step: number | null | undefined): number {
  if (!step || step <= 0) return 6;
  const s = step.toString();
  const dot = s.indexOf('.');
  return dot === -1 ? 0 : Math.min(8, s.length - dot - 1);
}

export function TradePanel() {
  const [symbol, setSymbol] = useState<string>('BTC/USDT');
  const [ctx, setCtx] = useState<Context | null>(null);
  const [ctxError, setCtxError] = useState<string | null>(null);

  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [leverage, setLeverage] = useState(1);
  const [qtyInput, setQtyInput] = useState('');
  const [stopLoss, setStopLoss] = useState('');
  const [takeProfit, setTakeProfit] = useState('');

  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (refreshBalance = false) => {
      try {
        const res = await fetch(
          backendProxyPath(
            `/api/operator/trade/context?symbol=${encodeURIComponent(symbol)}` +
              (refreshBalance ? '&refreshBalance=true' : ''),
          ),
          { cache: 'no-store' },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as Context;
        setCtx(data);
        setCtxError(null);
        // Clamp rather than reset: an operator who chose 5x and switched symbol
        // keeps 5x unless the new tab's ceiling is lower.
        setLeverage((l) => Math.min(l, data.leverageCeiling));
      } catch (e) {
        setCtxError(e instanceof Error ? e.message : 'could not load trade context');
      }
    },
    [symbol],
  );

  useEffect(() => {
    void load();
    // Price moves; balance does not need re-fetching on this timer because the
    // backend serves it from a 30s cache and only refreshes on demand.
    const id = setInterval(() => void load(), 10_000);
    return () => clearInterval(id);
  }, [load]);

  const price = ctx?.price ?? null;
  const available = ctx?.balance.available ?? null;
  const step = ctx?.instrumentRules?.stepSize ?? null;
  const qtyDecimals = decimalsFor(step);

  const qty = useMemo(() => {
    const n = Number.parseFloat(qtyInput);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }, [qtyInput]);

  const notional = price !== null ? qty * price : null;
  const margin = notional !== null && leverage > 0 ? notional / leverage : null;

  const applyPreset = useCallback(
    (fraction: number) => {
      if (price === null || available === null || price <= 0) return;
      // Size against MARGIN, not notional: at 5x, "50%" means committing half the
      // balance as margin, which is a 2.5x-balance position. Sizing the notional
      // instead would silently cap leverage at 1x and make the slider a no-op.
      const budget = available * fraction;
      const raw = (budget * leverage) / price;
      setQtyInput(toStep(raw, step).toFixed(qtyDecimals));
    },
    [available, leverage, price, step, qtyDecimals],
  );

  const held = useMemo(
    () => (ctx?.openPositions ?? []).find((p) => p.symbol === symbol) ?? null,
    [ctx, symbol],
  );

  const problems = useMemo(() => {
    const out: string[] = [];
    if (price === null) out.push(ctx?.priceNote ?? 'no live price for this symbol');
    if (qty <= 0) out.push('enter a quantity');
    if (step && qty > 0 && Math.abs(qty / step - Math.round(qty / step)) > 1e-9) {
      out.push(`quantity must be a multiple of the ${step} step`);
    }
    const minQty = ctx?.instrumentRules?.minQty;
    if (minQty && qty > 0 && qty < minQty) out.push(`below the ${minQty} minimum quantity`);
    const minNotional = ctx?.instrumentRules?.minNotional;
    if (minNotional && notional !== null && notional > 0 && notional < minNotional) {
      out.push(`notional ${notional.toFixed(2)} is below the ${minNotional} minimum`);
    }
    if (side === 'buy' && margin !== null && available !== null && margin > available) {
      out.push(`needs ${margin.toFixed(2)} margin, book holds ${available.toFixed(2)}`);
    }
    if (side === 'sell' && ctx?.tab === 'paper' && (!held || held.qty < qty)) {
      out.push('the paper book does not hold that much — a sell closes an existing long');
    }
    if (ctx?.tab === 'real' && (!apiKey.trim() || !apiSecret.trim())) {
      out.push('real mode needs your exchange API key and secret');
    }
    return out;
  }, [price, ctx, qty, step, notional, side, margin, available, held, apiKey, apiSecret]);

  const submit = useCallback(async () => {
    if (problems.length > 0 || ctx === null) return;
    setSubmitting(true);
    setError(null);
    setResult(null);

    try {
      const isReal = ctx.tab === 'real';
      const path = isReal ? '/api/operator/exchange/order' : '/api/operator/trade/paper';
      const body = isReal
        ? {
            exchange: 'binance',
            apiKey: apiKey.trim(),
            apiSecret: apiSecret.trim(),
            symbol,
            side,
            qty,
            // Sent on the real path too. These do NOT place resting orders at
            // the venue — they register the fill with the position monitor, and
            // the response says plainly whether that succeeded. Omitting them
            // here was the gap: a real trade went through completely unwatched.
            stopLoss: stopLoss.trim() ? Number.parseFloat(stopLoss) : null,
            takeProfit: takeProfit.trim() ? Number.parseFloat(takeProfit) : null,
          }
        : {
            symbol,
            side,
            qty,
            leverage,
            stopLoss: stopLoss.trim() ? Number.parseFloat(stopLoss) : null,
            takeProfit: takeProfit.trim() ? Number.parseFloat(takeProfit) : null,
          };

      const res = await fetch(backendProxyPath(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const text = await res.text();
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0, 400)}`);
      setResult(JSON.parse(text));
      setQtyInput('');
      void load(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'order failed');
    } finally {
      setSubmitting(false);
    }
  }, [problems, ctx, apiKey, apiSecret, symbol, side, qty, leverage, stopLoss, takeProfit, load]);

  const isReal = ctx?.tab === 'real';

  return (
    <Card>
      <SectionTitle
        action={
          <div className="flex items-center gap-2">
            <Badge
              state={isReal ? 'CRITICAL' : 'INFO'}
              label={isReal ? 'REAL MONEY' : 'Paper'}
            />
            <button
              type="button"
              className="chip"
              onClick={() => void load(true)}
              title="Re-read the balance from the exchange. Cached for 30s otherwise, to stay inside the venue's rate limit."
            >
              Refresh balance
            </button>
          </div>
        }
      >
        Trade execution
      </SectionTitle>

      {ctxError ? (
        <div className="text-[11.5px] mb-2" style={{ color: 'var(--negative)' }}>
          Could not load the trade context: {ctxError}
        </div>
      ) : null}

      {isReal ? (
        <div
          className="text-[11px] mb-3 p-2 rounded leading-relaxed"
          style={{
            background: 'color-mix(in srgb, var(--negative) 12%, transparent)',
            color: 'var(--negative)',
          }}
        >
          <strong>LIVE_TRADING is ON.</strong> This ticket places a REAL order with your own
          keys against real funds. Keys are sent per request and never stored. Every order is
          persisted with <span className="mono">origin_tag=&apos;manual-click&apos;</span>.
        </div>
      ) : null}

      {/* ---- market + balance ---- */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
        <div>
          <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
            Symbol
          </label>
          <select
            className="w-full mono text-[12px] px-2 py-1.5 rounded"
            style={{ background: 'var(--bg-surface-2)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          >
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
            Mark price
          </div>
          <div className="mono text-[14px] py-1">
            {price === null ? (
              <span style={{ color: 'var(--warning)' }}>unavailable</span>
            ) : (
              <Num value={price} digits={2} />
            )}
          </div>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
            {isReal ? 'Exchange balance' : 'Paper cash'}
          </div>
          <div className="mono text-[14px] py-1">
            {available === null ? (
              <span style={{ color: 'var(--warning)' }}>unreadable</span>
            ) : (
              <Num value={available} digits={2} prefix="$" />
            )}
          </div>
          {ctx?.balance.cached && ctx.balance.ageSeconds !== null ? (
            <div className="text-[9.5px]" style={{ color: 'var(--text-muted)' }}>
              cached {Math.round(ctx.balance.ageSeconds)}s ago
            </div>
          ) : null}
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
            Holding
          </div>
          <div className="mono text-[14px] py-1">
            {held ? <Num value={held.qty} digits={qtyDecimals} /> : <span style={{ color: 'var(--text-muted)' }}>—</span>}
          </div>
        </div>
      </div>

      {ctx?.balance.error ? (
        <div className="text-[11px] mb-2 leading-relaxed" style={{ color: 'var(--warning)' }}>
          {ctx.balance.error}
        </div>
      ) : null}

      {/* ---- side ---- */}
      <div className="flex gap-2 mb-3">
        {(['buy', 'sell'] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSide(s)}
            className="flex-1 py-2 rounded text-[12px] font-semibold uppercase tracking-wide"
            style={{
              background:
                side === s
                  ? `color-mix(in srgb, var(--${s === 'buy' ? 'positive' : 'negative'}) 20%, transparent)`
                  : 'var(--bg-surface-2)',
              color: side === s ? `var(--${s === 'buy' ? 'positive' : 'negative'})` : 'var(--text-secondary)',
              border: `1px solid ${side === s ? `var(--${s === 'buy' ? 'positive' : 'negative'})` : 'var(--border)'}`,
            }}
          >
            {s === 'buy' ? 'Buy / Long' : 'Sell / Close'}
          </button>
        ))}
      </div>

      {/* ---- leverage ---- */}
      <div className="mb-3">
        <div className="flex items-baseline justify-between mb-1">
          <label className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>
            Leverage
          </label>
          <span className="mono text-[12px]">{leverage}x</span>
        </div>
        <input
          type="range"
          min={1}
          max={ctx?.leverageCeiling ?? 1}
          step={1}
          value={leverage}
          onChange={(e) => setLeverage(Number.parseInt(e.target.value, 10))}
          className="w-full"
        />
        <div className="text-[10px] mt-1 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          {ctx?.leverageNote ?? 'ceiling not loaded'}
        </div>
      </div>

      {/* ---- quantity ---- */}
      <div className="mb-3">
        <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
          Quantity {step ? <span className="mono">(step {step})</span> : null}
        </label>
        <input
          type="text"
          inputMode="decimal"
          value={qtyInput}
          onChange={(e) => setQtyInput(e.target.value)}
          placeholder="0.000"
          className="w-full mono text-[13px] px-2 py-1.5 rounded"
          style={{ background: 'var(--bg-surface-2)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
        />
        <div className="flex gap-1.5 mt-1.5">
          {SIZE_PRESETS.map((f) => (
            <button
              key={f}
              type="button"
              className="chip"
              disabled={price === null || available === null}
              onClick={() => applyPreset(f)}
              title="Percentage of available balance committed as MARGIN, then multiplied by leverage."
            >
              {f * 100}%
            </button>
          ))}
        </div>
      </div>

      {/* ---- stop / target ----
           Shown in BOTH modes. They were paper-only, which meant a REAL order
           could be placed with no stop and nothing watching it — the one case
           where an unmonitored position costs actual money. */}
      <div className="grid grid-cols-2 gap-2 mb-3">
          <div>
            <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
              Stop-loss <span style={{ color: 'var(--warning)' }}>(else unmonitored)</span>
            </label>
            <input
              type="text"
              inputMode="decimal"
              value={stopLoss}
              onChange={(e) => setStopLoss(e.target.value)}
              placeholder="optional"
              className="w-full mono text-[12px] px-2 py-1.5 rounded"
              style={{ background: 'var(--bg-surface-2)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
            />
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-muted)' }}>
              Take-profit
            </label>
            <input
              type="text"
              inputMode="decimal"
              value={takeProfit}
              onChange={(e) => setTakeProfit(e.target.value)}
              placeholder="optional"
              className="w-full mono text-[12px] px-2 py-1.5 rounded"
              style={{ background: 'var(--bg-surface-2)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
            />
          </div>
      </div>

      {isReal ? (
        <div className="text-[10.5px] mb-3 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          A stop here is enforced by this backend against every tick — it is NOT a
          resting order at the exchange. If this process is down, nothing is watching.
          Closing also uses the backend&apos;s own API keys, so it only works when those
          are configured for the same account.
        </div>
      ) : null}

      {/* ---- real credentials ---- */}
      {isReal ? (
        <div className="grid grid-cols-2 gap-2 mb-3">
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="API key"
            autoComplete="off"
            className="mono text-[12px] px-2 py-1.5 rounded"
            style={{ background: 'var(--bg-surface-2)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
          />
          <input
            type="password"
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            placeholder="API secret"
            autoComplete="off"
            className="mono text-[12px] px-2 py-1.5 rounded"
            style={{ background: 'var(--bg-surface-2)', border: '1px solid var(--border)', color: 'var(--text-primary)', padding: '6px 8px' }}
          />
        </div>
      ) : null}

      {/* ---- order summary ---- */}
      <div
        className="grid grid-cols-3 gap-2 mb-3 p-2 rounded text-[11px]"
        style={{ background: 'var(--bg-surface-2)' }}
      >
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Notional</div>
          <div className="mono">{notional === null ? '—' : `$${notional.toFixed(2)}`}</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Margin</div>
          <div className="mono">{margin === null ? '—' : `$${margin.toFixed(2)}`}</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>After</div>
          <div className="mono">
            {margin === null || available === null ? '—' : `$${(available - margin).toFixed(2)}`}
          </div>
        </div>
      </div>

      {problems.length > 0 ? (
        <ul className="text-[11px] mb-2 space-y-0.5" style={{ color: 'var(--text-muted)' }}>
          {problems.map((p) => (
            <li key={p}>· {p}</li>
          ))}
        </ul>
      ) : null}

      <button
        type="button"
        disabled={problems.length > 0 || submitting}
        onClick={() => void submit()}
        className="w-full py-2.5 rounded text-[13px] font-semibold uppercase tracking-wide"
        style={{
          background:
            problems.length > 0
              ? 'var(--bg-surface-2)'
              : `color-mix(in srgb, var(--${side === 'buy' ? 'positive' : 'negative'}) 22%, transparent)`,
          color:
            problems.length > 0
              ? 'var(--text-muted)'
              : `var(--${side === 'buy' ? 'positive' : 'negative'})`,
          border: `1px solid ${problems.length > 0 ? 'var(--border)' : `var(--${side === 'buy' ? 'positive' : 'negative'})`}`,
          cursor: problems.length > 0 ? 'not-allowed' : 'pointer',
        }}
      >
        {submitting
          ? 'Submitting…'
          : `${side === 'buy' ? 'Buy' : 'Sell'} ${qty > 0 ? qty : ''} ${symbol.split('/')[0]} ${isReal ? '— REAL' : '(paper)'}`}
      </button>

      {error ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--negative)' }}>
          {error}
        </div>
      ) : null}

      {result ? (
        <div className="text-[11px] mt-2 leading-relaxed" style={{ color: 'var(--positive)' }}>
          Filled {String(result.qty ?? '')} {String(result.symbol ?? '')} at{' '}
          <span className="mono">{String(result.fillPrice ?? result.price ?? '—')}</span>.
          {typeof result.monitorNote === 'string' ? (
            <div
              className="mt-1"
              style={{ color: result.monitored ? 'var(--text-secondary)' : 'var(--warning)' }}
            >
              {result.monitorNote}
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}
