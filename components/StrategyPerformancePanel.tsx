'use client';

// ---------------------------------------------------------------------
// The learning loop, made visible.
//
// `historical_success_rate` was None on all nine profiles and the scorer said so
// on every run: the agent picked strategies purely on how well they FIT current
// conditions, and nothing it learned from an outcome ever reached that choice.
// That is closed now — but a loop nobody can see is indistinguishable from one
// that is still open, so this panel shows the measurement and, just as
// importantly, HOW FAR EACH STRATEGY IS FROM BEING TRUSTED.
//
// WHY THE SAMPLE COUNT IS AS PROMINENT AS THE WIN RATE
//
// A 100% win rate over three trades is noise wearing a percentage sign, and it
// is the single most misleading number this page could show. `usable` is the
// backend's own gate — below `minSample` a strategy's record is reported and
// explicitly NOT allowed to steer selection — so the row says which side of that
// line it sits on rather than leaving a reader to infer it from the count.
//
// EXPECTANCY LEADS, NOT WIN RATE. 33% at 2:1 and 60% at 0.5:1 are both roughly
// break-even; ranking on win rate would prefer the wrong one. The backend already
// ranks by expectancy and this renders that order.
// ---------------------------------------------------------------------

import { Badge } from '@/components/ui/Badge';
import { Card, NotAvailable, Num, SectionTitle, TermTable } from '@/components/ui/primitives';
import { useSameOrigin } from '@/lib/api/useSameOrigin';
import { backendProxyPath } from '@/lib/backendConfig';

type StrategyStat = {
  strategy: string;
  sampleSize: number;
  wins: number;
  losses: number;
  winRate: number;
  totalPnl: number;
  avgWin: number;
  avgLoss: number;
  expectancy: number;
  usable: boolean;
};

type Response = {
  available: boolean;
  reason?: string;
  minSample: number;
  strategies: StrategyStat[];
  usableCount?: number;
  meaning?: string;
};

export function StrategyPerformancePanel() {
  // 60s. These numbers change only when a position closes, and a closed position
  // is a rare event relative to any sane poll rate.
  const res = useSameOrigin<Response>(backendProxyPath('/api/graphs/strategy-performance'), {
    intervalMs: 60_000,
  });

  const data = res.data;
  const minSample = data?.minSample ?? 20;
  const strategies = data?.strategies ?? [];

  if (res.error) {
    return (
      <NotAvailable
        what="Strategy track record"
        reason={`the backend did not answer (${res.error}). This reads closed trades grouped by the strategy that opened them.`}
      />
    );
  }

  if (data && !data.available) {
    return (
      <NotAvailable
        what="Strategy track record"
        reason={data.reason ?? 'the trade database could not be read.'}
      />
    );
  }

  return (
    <Card>
      <SectionTitle
        action={
          <span className="font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
            {data?.usableCount ?? 0} of {strategies.length} steering selection
          </span>
        }
      >
        Strategy track record
      </SectionTitle>

      <TermTable
        columns={[
          { key: 's', label: 'Strategy' },
          { key: 'n', label: 'Closed', num: true },
          { key: 'w', label: 'Win rate', num: true },
          { key: 'e', label: 'Expectancy', num: true },
          { key: 'p', label: 'Total P&L', num: true },
          { key: 'u', label: 'Steering?' },
        ]}
        empty={
          res.state === 'loading'
            ? 'Loading…'
            : // Not an error, and worth saying plainly: this is what an agent that
              // has not yet closed an attributed trade correctly looks like.
              'No strategy has closed a trade yet. Selection runs on current conditions ' +
              'alone until one does — which is honest, not broken.'
        }
      >
        {strategies.map((s) => (
          <tr key={s.strategy}>
            <td className="mono text-[11.5px]">{s.strategy}</td>
            <td className="num">
              {/* THE COUNT SITS NEXT TO THE RATE ON PURPOSE. "100%" over three
                  trades is the most misleading number this page could show. */}
              <span className="mono">
                {s.sampleSize}
                {!s.usable ? (
                  <span style={{ color: 'var(--text-muted)' }}> / {minSample}</span>
                ) : null}
              </span>
            </td>
            <td className="num">
              <Num value={s.winRate * 100} digits={0} suffix="%" />
            </td>
            <td className="num">
              <Num value={s.expectancy} digits={2} prefix="$" colored signed />
            </td>
            <td className="num">
              <Num value={s.totalPnl} digits={2} prefix="$" colored signed />
            </td>
            <td>
              <Badge
                state={s.usable ? 'ACTIVE' : 'WAITING'}
                label={s.usable ? 'Yes' : `${minSample - s.sampleSize} more`}
              />
            </td>
          </tr>
        ))}
      </TermTable>

      <p className="mt-2 text-[11px] leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        Realised results per strategy, from closed trades only. A strategy needs{' '}
        <strong>{minSample}</strong> closed trades before its win rate is allowed to influence
        which strategy the agent picks — below that the rate is noise, and one lucky run would
        entrench a bad strategy. <strong>Expectancy</strong>, not win rate, is what says whether
        a strategy is worth running: 33% at 2:1 and 60% at 0.5:1 are both roughly break-even.
      </p>
    </Card>
  );
}
