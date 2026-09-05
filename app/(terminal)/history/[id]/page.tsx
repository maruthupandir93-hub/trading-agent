'use client';

// ---------------------------------------------------------------------
// /history/[id] — one trade, its reflection, its hypothesis, and delete.
//
// The replacement for the old `/log/[id]`, and it exists because that route was
// NOT covered by anything else in the new design. It carried four things the
// history table does not: the structured reflection (why / failed signal / earlier
// exit / confidence / lesson), the Generate-Regenerate control, the per-trade
// HypothesisPanel, and the delete. Deleting `app/log/` without this would have
// removed all four while the migration ledger claimed `/history` replaced `/log`.
//
// The reflection comes from `useReflection()`, and `regenerate()` makes a real LLM
// call. It is advisory: nothing on this page can feed a lesson back into execution,
// and Apply on the hypothesis is the only path to production — by an explicit human
// click, never automatically.
// ---------------------------------------------------------------------

import dynamic from 'next/dynamic';
import { useRouter } from 'next/navigation';
import { useMemo, useState } from 'react';

import { usePortfolio } from '@/components/Portfolio';
import { useReflection } from '@/components/Reflection';
import { OperatorSection } from '@/components/operator/OperatorSection';
import { TradeJourney } from '@/components/viz/TradeJourney';
import { Badge } from '@/components/ui/Badge';
import { Card, Num, NotAvailable, SectionTitle, StatCard } from '@/components/ui/primitives';
import { buildJourney } from '@/lib/viz/journey';
import { annotateTrades, statusBadgeState, statusLabel } from '@/lib/tradeStatus';
import { parseEntryContext } from '@/lib/viz/entryContext';

// Code-split: HypothesisPanel is shown only after a row is selected, and it pulls
// in the hypothesis provider and the LLM path. Eager-importing it doubled this
// route's first load for a panel most visits never open.
const HypothesisPanel = dynamic(
  () => import('@/components/HypothesisPanel').then((m) => ({ default: m.HypothesisPanel })),
  { ssr: false, loading: () => <p className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>Loading…</p> },
);

export default function TradeDetailPage({ params }: { params: { id: string } }) {
  const { tradeLog, tradeLogLoaded, deleteTradeLogEntry } = usePortfolio();
  const { getReflection, isGenerating, regenerate } = useReflection();
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);

  // ANNOTATED OVER THE WHOLE LEDGER, not just this row.
  //
  // A fill row does not say whether its position is still open — `trades` is a
  // fill log, not a position log. Status, hold time, direction and the entry
  // price all come from walking the ledger, so the annotation has to see every
  // row even though only one is being displayed.
  const annotated = useMemo(() => annotateTrades(tradeLog), [tradeLog]);
  const trade = annotated.find((t) => t.id === params.id);

  // The other half of the round trip: an entry's exit, or an exit's entry.
  const counterpartId = trade
    ? (trade.role === 'close' ? trade.openedByTradeId : trade.closedByTradeId)
    : null;
  const counterpart = counterpartId ? annotated.find((t) => t.id === counterpartId) ?? null : null;

  // Which of the two legs is the entry and which the exit, so the times below
  // are labelled by what they MEAN rather than by which row was clicked.
  const entryLeg = trade ? (trade.role === 'close' ? counterpart : trade) : null;
  const exitLeg = trade ? (trade.role === 'close' ? trade : counterpart) : null;

  // The snapshot lives on the ENTRY leg — it describes the decision to open, and
  // an exit is not a decision the gateway made.
  const entryContextRaw = entryLeg?.entryContext ?? trade?.entryContext ?? null;
  const context = parseEntryContext(entryContextRaw);
  const reflection = trade ? getReflection(trade.id) : undefined;
  const generating = trade ? isGenerating(trade.id) : false;

  if (!trade) {
    return (
      <div className="max-w-[820px] space-y-3">
        <h1 className="text-[17px] font-semibold">Trade Detail</h1>
        <NotAvailable
          what="This trade"
          reason={
            tradeLogLoaded
              ? 'no trade in the log carries that id. Trade ids are per-store, so a link from another machine will not resolve here.'
              : 'the trade log has not finished loading.'
          }
        />
        <button type="button" className="chip" onClick={() => router.push('/history')}>
          Back to trade history
        </button>
      </div>
    );
  }

  const sections = reflection?.sections;
  const hasSections =
    sections &&
    (sections.whyOutcome ||
      sections.failedSignal ||
      sections.earlierExit ||
      sections.confidenceAssessment ||
      sections.lesson);

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <h1 className="text-[17px] font-semibold">
          <span className="mono">{trade.symbol}</span>{' '}
          <span
            className="mono text-[13px]"
            style={{ color: trade.side === 'buy' ? 'var(--positive)' : 'var(--negative)' }}
          >
            {trade.side.toUpperCase()}
          </span>
        </h1>
        <span className="flex items-center gap-2">
          {/* Whether this position is still running. The page used to show a
              price and a quantity with no indication of that at all. */}
          <Badge state={statusBadgeState(trade)} label={statusLabel(trade)} />
          <Badge
            state={trade.tab === 'real' ? 'CRITICAL' : 'INFO'}
            label={trade.tab === 'real' ? 'Real ledger' : 'Paper'}
          />
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Quantity" value={<Num value={trade.qty} digits={8} />} />
        <StatCard label="Price" value={<Num value={trade.price} prefix="$" />} />
        <StatCard label="Notional" value={<Num value={trade.qty * trade.price} prefix="$" />} />
        <StatCard
          label="Realised P&L"
          value={
            typeof trade.pnl === 'number' ? (
              <Num value={trade.pnl} prefix="$" colored signed />
            ) : (
              <span style={{ color: 'var(--text-muted)' }}>&mdash;</span>
            )
          }
          sub={
            typeof trade.pnl === 'number'
              ? undefined
              : trade.status === 'OPEN'
                // Not missing data — an open position HAS no realised result yet.
                ? 'still running — no result yet'
                : 'this is the entry leg; the result is on the exit'
          }
        />
      </div>

      <Card>
        <SectionTitle>Lifecycle</SectionTitle>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-[11.5px]">
          <Field
            label="Opened"
            value={entryLeg ? new Date(entryLeg.ts).toLocaleString() : 'not in this log'}
            muted={!entryLeg}
          />
          <Field
            label="Closed"
            value={
              exitLeg
                ? new Date(exitLeg.ts).toLocaleString()
                : trade.status === 'OPEN'
                  ? 'still open'
                  : 'not in this log'
            }
            muted={!exitLeg}
          />
          <Field
            label="Held for"
            value={trade.holdMs === null ? 'unknown' : formatHold(trade.holdMs)}
            muted={trade.holdMs === null}
          />
          <Field
            label="Direction"
            value={trade.direction === 'unknown' ? 'unknown' : trade.direction}
            muted={trade.direction === 'unknown'}
          />
          <Field
            label="Entry price"
            value={entryLeg ? `$${entryLeg.price}` : 'not in this log'}
            muted={!entryLeg}
            mono
          />
          <Field
            label="Exit price"
            value={exitLeg ? `$${exitLeg.price}` : trade.status === 'OPEN' ? 'still open' : 'not in this log'}
            muted={!exitLeg}
            mono
          />
          <Field label="Origin" value={trade.originTag ?? 'unknown'} muted={!trade.originTag} mono />
          <Field label="This fill" value={trade.role === 'close' ? 'exit' : 'entry'} />
          <Field label="Trade id" value={trade.id} mono />
          {counterpart ? (
            <Field
              label={trade.role === 'close' ? 'Entry fill' : 'Exit fill'}
              value={counterpart.id}
              mono
            />
          ) : null}
          {trade.note ? <Field label="Note" value={trade.note} /> : null}
        </div>

        {/* Stated rather than left to be inferred from the dashes above. An
            unpaired leg is ordinary after a restart or a retention trim, and it
            is not the same thing as missing data. */}
        {!counterpart && trade.status === 'CLOSED' ? (
          <p className="text-[10.5px] mt-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The other leg of this round trip is not in the current log — ordinary after a
            restart or a retention trim. The realised P&amp;L above is still authoritative: it
            was computed at close time against the real entry.
          </p>
        ) : null}
      </Card>

      <Card>
        <SectionTitle>How this trade happened</SectionTitle>
        <TradeJourney
          steps={buildJourney({
            symbol: trade.symbol,
            // The ENTRY leg's price, not this row's — a journey starts where the
            // position was opened, and on an exit row `trade.price` is the exit.
            price: entryLeg?.price ?? trade.price,
            // RECORDED AT DECISION TIME by the Risk Gateway, which is the last
            // node holding the indicators, the regime and the volatility reading
            // together. Before this existed the middle of the journey was not
            // lost — it was never written down.
            indicators:
              context.rsi !== null || context.atr !== null
                ? { rsi: context.rsi, atr: context.atr }
                : null,
            regime: context.regime ? { regime: context.regime } : null,
            strategy: context.strategy,
            execution: { submitted: true, status: 'filled' },
            outcome: typeof trade.pnl === 'number' ? { pnl: trade.pnl } : { status: 'unknown' },
          })}
        />

        {context.volatility || context.trend ? (
          <div className="flex gap-4 mt-2 text-[11px]" style={{ color: 'var(--text-secondary)' }}>
            {context.trend ? <span>structure trend: <span className="mono">{context.trend}</span></span> : null}
            {context.volatility ? (
              <span>volatility: <span className="mono">{context.volatility}</span></span>
            ) : null}
          </div>
        ) : null}

        <div className="text-[10.5px] mt-2 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          {entryContextRaw
            ? 'Indicators and regime are what the agent actually saw when it decided this trade, recorded by the Risk Gateway at decision time.'
            : 'The middle steps are unknown: this trade predates the entry-context snapshot, so no record exists of what the agent saw. Trades taken from now on carry it.'}
        </div>
      </Card>

      {typeof trade.pnl === 'number' ? (
        <OperatorSection
          title="AI reflection"
          note="Advisory analysis of this trade's entry and exit. It is read by the chat system prompt and never re-fed into execution."
          action={
            <button type="button" className="chip" onClick={() => regenerate(trade.id)} disabled={generating}>
              {generating ? 'Generating…' : reflection ? 'Regenerate' : 'Generate'}
            </button>
          }
        >
          {reflection ? (
            <div className="space-y-2">
              {hasSections ? (
                <>
                  {sections?.whyOutcome ? <Labelled label="Why" text={sections.whyOutcome} /> : null}
                  {sections?.failedSignal ? (
                    <Labelled label="Failed signal" text={sections.failedSignal} />
                  ) : null}
                  {sections?.earlierExit ? (
                    <Labelled label="Earlier exit?" text={sections.earlierExit} />
                  ) : null}
                  {sections?.confidenceAssessment ? (
                    <Labelled label="Confidence too high?" text={sections.confidenceAssessment} />
                  ) : null}
                  {sections?.lesson ? <Labelled label="Lesson" text={sections.lesson} accent /> : null}
                </>
              ) : (
                <p className="text-[12px] whitespace-pre-wrap" style={{ color: 'var(--text-secondary)' }}>
                  {reflection.content}
                </p>
              )}

              {reflection.finishReason === 'length' ? (
                <p className="text-[10.5px] mono" style={{ color: 'var(--warning)' }}>
                  Cut off at the model&apos;s token limit — this reflection may be incomplete.
                </p>
              ) : null}

              <details className="text-[10.5px] mono" style={{ color: 'var(--text-muted)' }}>
                <summary className="cursor-pointer">Context used</summary>
                <p className="mt-1">Entry: {reflection.entryContextUsed ?? 'not captured for this trade'}</p>
                <p className="mt-1">Exit: {reflection.exitContextUsed}</p>
              </details>
            </div>
          ) : generating ? (
            <p className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
              Analysing this trade&apos;s entry and exit context…
            </p>
          ) : (
            <p className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
              No reflection yet. One is generated automatically for a closed trade; Generate
              runs it now.
            </p>
          )}
        </OperatorSection>
      ) : (
        <div className="text-[10.5px] leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          No reflection or hypothesis for this row: both need a realised P&amp;L to reason
          about, and this record carries none.
        </div>
      )}

      {typeof trade.pnl === 'number' ? (
        <OperatorSection
          title="Hypothesis"
          note="Apply is the only path from a lesson to production, and it is a human click. Nothing here can write to risk config or strategy selection on its own."
        >
          <HypothesisPanel tradeId={trade.id} />
        </OperatorSection>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="chip" onClick={() => router.push('/history')}>
          ← Trade history
        </button>
        {confirming ? (
          <>
            <span className="text-[11.5px]" style={{ color: 'var(--negative)' }}>
              Delete this {trade.side} {trade.symbol} entry? Every derived figure changes and
              this cannot be undone.
            </span>
            <button
              type="button"
              className="chip"
              style={{ color: 'var(--negative)', borderColor: 'var(--negative)' }}
              onClick={async () => {
                await deleteTradeLogEntry(trade.id);
                router.push('/history');
              }}
            >
              Delete
            </button>
            <button type="button" className="chip" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="chip" onClick={() => setConfirming(true)}>
            Delete entry
          </button>
        )}
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  mono,
  muted,
}: {
  label: string;
  value: string;
  mono?: boolean;
  /** Dims a value that is an ABSENCE ("unknown", "still open") rather than a
   *  measurement, so the two do not read alike at a glance. */
  muted?: boolean;
}) {
  return (
    <div>
      <div
        className="font-mono text-[10px] uppercase tracking-wider mb-0.5"
        style={{ color: 'var(--text-muted)' }}
      >
        {label}
      </div>
      <div
        className={mono ? 'mono text-[11px] break-all' : ''}
        style={{ color: muted ? 'var(--text-muted)' : 'var(--text-secondary)' }}
      >
        {value}
      </div>
    </div>
  );
}

/** Hold time as something a human reads at a glance, not raw milliseconds. */
function formatHold(ms: number): string {
  const minutes = ms / 60_000;
  if (minutes < 1) return `${Math.round(ms / 1000)}s`;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

function Labelled({ label, text, accent }: { label: string; text: string; accent?: boolean }) {
  return (
    <p
      className="text-[12px] leading-relaxed"
      style={{ color: accent ? 'var(--accent)' : 'var(--text-secondary)' }}
    >
      <span
        className="font-mono text-[10px] uppercase tracking-wider mr-1.5"
        style={{ color: 'var(--text-muted)' }}
      >
        {label}
      </span>
      {text}
    </p>
  );
}
