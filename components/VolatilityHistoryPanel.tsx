'use client';

// ---------------------------------------------------------------------
// The volatility layer, made visible.
//
// The engine already gates entries, scales position size and sets stop distance,
// but until this panel existed the only evidence of any of that was a rejection
// reason buried in a risk assessment. An operator watching the agent decline to
// trade had no way to see that the market was in an EXTREME regime — which reads
// as the agent being broken.
//
// WHY THE PERCENTILE IS THE HEADLINE NUMBER AND ATR% IS THE FOOTNOTE
//
// ATR% is not comparable between instruments: SOL's ordinary session is a wider
// percentage range than BTC's, so a table sorted on ATR% ranks them by which coin
// is intrinsically choppier rather than by which one is behaving unusually today.
// The percentile answers the question actually being asked — "is this market
// unusual FOR ITSELF right now?" — so it leads, and `basis` says whenever the
// reading fell back to absolute thresholds and is therefore NOT comparable.
//
// SOURCED FROM THE 15-ENTRY FILE, NOT FROM THE BACKEND DIRECTLY. The readings are
// collected and capped by `/api/volatility-history`; see that route and
// `lib/volatilityHistoryStore.server.ts` for why the durable record lives here
// rather than in the operator's database.
// ---------------------------------------------------------------------

import { Badge } from '@/components/ui/Badge';
import { Card, NotAvailable, Num, SectionTitle, TermTable } from '@/components/ui/primitives';
import { useSameOrigin } from '@/lib/api/useSameOrigin';
import type { VolatilityHistoryEntry } from '@/lib/volatilityHistoryStore.server';

type Response = {
  history?: VolatilityHistoryEntry[];
  maxEntries?: number;
  /** null means the backend could not be reached — distinct from "it measured nothing". */
  collected?: number | null;
};

/**
 * Regime -> shared badge vocabulary.
 *
 * EXTREME is the only regime that blocks entry outright, so it alone gets the
 * negative colour; HIGH still trades, at reduced size. A null regime is UNKNOWN
 * and maps to 'UNAVAILABLE' rather than to a calm colour — the engine treats an
 * unmeasurable volatility as a refusal, and the badge must not read as safe.
 */
function badgeStateFor(regime: string | null): string {
  switch (regime) {
    case 'EXTREME':
      return 'CRITICAL';
    case 'HIGH':
      return 'WARN';
    case 'NORMAL':
    case 'LOW':
    case 'VERY_LOW':
      return 'PASS';
    default:
      return 'UNAVAILABLE';
  }
}

export function VolatilityHistoryPanel() {
  // 20s: the readings are produced per graph cycle, and polling faster only
  // re-reads a file that has not changed.
  const res = useSameOrigin<Response>('/api/volatility-history', { intervalMs: 20_000 });
  const history = res.data?.history ?? [];
  const max = res.data?.maxEntries ?? 15;

  if (res.error) {
    return (
      <NotAvailable
        what="Volatility history"
        reason={`/api/volatility-history did not respond (${res.error}). It is a Next.js route reading .data/volatility-history.json.`}
      />
    );
  }

  return (
    <Card>
      <SectionTitle
        action={
          <span className="font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
            last {max} readings
          </span>
        }
      >
        Volatility regime
      </SectionTitle>

      <TermTable
        columns={[
          { key: 'symbol', label: 'Symbol' },
          { key: 'regime', label: 'Regime' },
          { key: 'pct', label: 'Percentile', num: true },
          { key: 'atr', label: 'ATR %', num: true },
          { key: 'size', label: 'Size ×', num: true },
          { key: 'lev', label: 'Max lev', num: true },
          { key: 'stop', label: 'Stop × ATR', num: true },
          { key: 'gate', label: 'Entry' },
        ]}
        empty={
          // Three genuinely different states, and an empty table would render all
          // three identically. Which one it is decides where to look next.
          res.data?.collected === null
            ? 'The backend could not be reached, so no readings could be collected.'
            : history.length === 0
              ? 'No volatility reading yet — the agent records one on each analysis cycle.'
              : null
        }
      >
        {history.map((r) => (
          <tr key={r.id}>
            <td className="mono">{r.symbol}</td>
            <td>
              <Badge state={badgeStateFor(r.regime)} label={r.regime ?? 'UNKNOWN'} />
              {/* An absolute reading is a thin-history fallback and is NOT
                  comparable across instruments. Marked, so a row is never
                  compared against another that was ranked a different way. */}
              {r.basis === 'absolute' && (
                <span className="ml-1.5 text-[10px]" style={{ color: 'var(--text-muted)' }}>
                  absolute
                </span>
              )}
              {r.volatilityShock && (
                <span className="ml-1.5 text-[10px]" style={{ color: 'var(--danger)' }}>
                  shock&nbsp;
                  <Num value={r.expansionRatio} digits={1} suffix="×" />
                </span>
              )}
            </td>
            <td className="num">
              <Num value={r.percentile} digits={0} />
            </td>
            <td className="num">
              <Num value={r.atrPercent} digits={3} suffix="%" />
            </td>
            <td className="num">
              <Num value={r.riskMultiplier} digits={2} />
            </td>
            <td className="num">
              <Num value={r.maxLeverage} digits={0} suffix="x" />
            </td>
            <td className="num">
              <Num value={r.stopAtrMultiple} digits={1} />
            </td>
            <td>
              <Badge
                state={r.tradingAllowed ? 'PASS' : 'BLOCKED'}
                label={r.tradingAllowed ? 'Allowed' : 'Blocked'}
              />
            </td>
          </tr>
        ))}
      </TermTable>

      <p className="mt-2 text-[11px] leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        Regime is ranked against each instrument&apos;s <strong>own</strong> recent ATR%
        distribution, so a reading transfers between coins that a fixed threshold table would
        rank backwards. <strong>Size ×</strong> multiplies position size and is never above 1.
        <strong> Max lev</strong> combines with the absolute ceiling by taking the lower of the
        two. Only the {max} most recent readings are kept.
      </p>
    </Card>
  );
}
