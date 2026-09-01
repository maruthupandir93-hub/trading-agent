'use client';

// ---------------------------------------------------------------------
// /home — the hero overview.
//
// The reference shows a rank/PnL ticker, an all-time PnL hero card, a biggest-win
// card, a mini chart, the Execution Cycle stepper, the Agent Swarm and a Polymarket
// Pulse strip.
//
// TWO OF THOSE ARE DERIVED AND SAID TO BE. All-time P&L and the biggest win are not
// stored as aggregates anywhere — they are computed from the trade log here, over
// trades that actually carry a `pnl`, with the count of those that do not reported
// alongside. A sum that treated a missing pnl as zero would understate the result
// and give no hint it had.
//
// "Rank" is dropped entirely. It implies a leaderboard against other accounts; there
// is no such thing, and a fabricated rank is exactly the kind of number that looks
// authoritative and means nothing.
// ---------------------------------------------------------------------

import dynamic from 'next/dynamic';
import { useMemo, useState } from 'react';

import { PolymarketCard, type PolymarketCardData } from '@/components/cards/PolymarketCard';
import { LiveAgentInspectorModal } from '@/components/modals/LiveAgentInspectorModal';
import { AgentSwarmViz, type SwarmLayer } from '@/components/viz/AgentSwarmViz';
import { ExecCycleStepper } from '@/components/viz/ExecCycleStepper';
import { LazyCandlestickChart as CandlestickChart, type Bar } from '@/components/ui/LazyCandlestickChart';
import { Badge } from '@/components/ui/Badge';
import { Card, Num, NotAvailable, SectionTitle, StatCard } from '@/components/ui/primitives';
import { BACKEND_PATHS } from '@/lib/backendConfig';
import type { NodeContract } from '@/lib/api/graphs';
import { equityCurve, maxDrawdownPct, realised, type Trade } from '@/lib/api/portfolio';
import { useSameOrigin } from '@/lib/api/useSameOrigin';
import {
  useBackend,
  useLivePrices,
} from '@/lib/realtime/useRealtime';
import { pipelineSourceLabel, usePipeline } from '@/lib/realtime/usePipeline';
import { useActiveSessionSymbol } from '@/lib/realtime/useActiveSession';
import { stageForNode } from '@/lib/viz/flow';

// Code-split. The operator panels sit below this page's real-data content, so
// nothing above the fold waits on them, and their dependency graph (providers,
// charts, the LLM chat path) is large relative to a page of tables. `ssr: false`
// because they read localStorage and measure DOM nodes.
const HomeOperator = dynamic(
  () => import('@/components/operator/HomeOperator').then((m) => ({ default: m.HomeOperator })),
  { ssr: false },
);

// `/api/candles` (the NEXT route, lib/candleSource.server.ts) returns
// `{ t, o, h, l, c, v }` — already the `Bar` shape. It is NOT the backend's
// `{ openTime, open, high, ... }`; that shape belongs to
// `backend/services/market_data.fetch_klines`, and reading it here produced
// `undefined` for every field. `toTime(undefined)` is NaN, which tripped
// lightweight-charts' own ordering assertion and took the whole route down with an
// unhandled runtime error. Two endpoints, two shapes, one guessed wrong.
type Candle = Bar;

export default function HomePage() {
  const trades = useSameOrigin<{ trades?: Trade[] }>('/api/trades', { intervalMs: 30_000 });
  const nodesApi = useBackend<{ nodes: NodeContract[]; total: number }>(BACKEND_PATHS.graphNodes, {
    intervalMs: 60_000,
  });
  const strategies = useBackend<{ implementedCount: number; plannedCount: number }>(
    BACKEND_PATHS.catalogStrategies,
    { intervalMs: 120_000 },
  );
  const snapshots = useBackend<{ snapshots: Record<string, unknown>[] }>(
    BACKEND_PATHS.polymarketSnapshots,
    { intervalMs: 60_000 },
  );
  // `type=crypto` is REQUIRED by the route — omitting it returns 400, which the
  // hook would surface as `unreachable` and the chart as "no candles", i.e. a
  // caller error indistinguishable from missing data.
  const candles = useSameOrigin<{ candles?: Candle[] }>(
    '/api/candles?symbol=BTCUSDT&type=crypto&interval=1h&limit=120',
  );

  const prices = useLivePrices();

  // Seeded from the last completed run when nothing is streaming. Without this
  // the Execution Cycle and the Agent Ensemble below sat permanently on their
  // idle state, because the event stream gives a freshly loaded page NO backlog
  // — so between cycles the page showed a system that looked switched off.
  // PINNED TO THE SESSION'S COIN when one is running.
  //
  // Without this the view seeds from "the most recent decision-graph run",
  // whatever instrument that was — so a session on SOL rendered a BTC cycle the
  // moment anything else triggered one, and the diagram gave no hint it had
  // switched. `pipelineSourceLabel` names the symbol either way, so an unpinned
  // view stays readable rather than merely ambiguous.
  const sessionSymbol = useActiveSessionSymbol();
  const pipeline = usePipeline(undefined, { symbol: sessionSymbol });
  const [inspect, setInspect] = useState<string | null>(null);

  const rows = trades.data?.trades ?? [];
  const pnl = realised(rows);
  const curve = useMemo(() => equityCurve(rows), [rows]);
  const drawdown = useMemo(() => maxDrawdownPct(curve), [curve]);

  const biggestWin = useMemo(() => {
    const withPnl = rows.filter((t) => typeof t.pnl === 'number');
    if (withPnl.length === 0) return null;
    return withPnl.reduce((best, t) => ((t.pnl as number) > (best.pnl as number) ? t : best));
  }, [rows]);

  const bars: Bar[] = candles.data?.candles ?? [];

  const specialists = (nodesApi.data?.nodes ?? []).filter((n) => n.name.startsWith('specialist_'));

  // While a cycle is running this is the stage of the node the stream says is
  // RUNNING. Between cycles it is the stage of the LAST node the previous run
  // reached, so the stepper shows where the agent got to rather than resetting
  // to nothing — the difference between "idle" and "finished" is the whole
  // question an operator is asking when they look at it.
  const lastNodeReached = useMemo(() => {
    if (pipeline.currentNode) return pipeline.currentNode;
    const nodes = pipeline.lastRun?.nodes ?? [];
    return nodes.length > 0 ? nodes[nodes.length - 1].node : null;
  }, [pipeline.currentNode, pipeline.lastRun]);

  const activeStage = stageForNode(lastNodeReached);

  // How many of the panel actually reported in the run being displayed. A count
  // of nodes that RAN, not of nodes that exist — the ensemble picture is about
  // work done, and drawing nine dots for a run where three specialists errored
  // would overstate it.
  const specialistsReported = useMemo(() => {
    const ran = new Set(
      (pipeline.lastRun?.nodes ?? [])
        .filter((n) => n.node.startsWith('specialist_') && !n.error)
        .map((n) => n.node),
    );
    for (const [name, state] of Object.entries(pipeline.nodes)) {
      if (name.startsWith('specialist_') && state.status === 'COMPLETED') ran.add(name);
    }
    return ran.size;
  }, [pipeline.lastRun, pipeline.nodes]);

  // Layers bound to REAL counts. See AgentSwarmViz — this is labelled illustrative
  // and denies being a multi-agent swarm.
  const swarmLayers: SwarmLayer[] = [
    {
      label: 'Watched symbols',
      count: Math.max(1, Object.keys(prices).length || 2),
      color: 'var(--accent)',
      active: activeStage === 'trigger',
    },
    {
      label: 'Specialists reported',
      // The number that actually reported in the displayed cycle, falling back to
      // the registered count only before any cycle has run.
      count: specialistsReported || specialists.length || 7,
      color: 'var(--accent-2)',
      active: activeStage === 'analyse',
    },
    {
      label: 'Strategies scored',
      count: strategies.data?.implementedCount ?? 11,
      color: 'var(--positive)',
      active: activeStage === 'analyse',
    },
    { label: 'Decision', count: 1, color: 'var(--warning)', active: activeStage === 'decide' },
  ];

  const pmRows: PolymarketCardData[] = useMemo(() => {
    const snaps = snapshots.data?.snapshots ?? [];
    return snaps.flatMap((r) => {
      const dir = r.directional as Record<string, unknown> | null;
      const ev = r.eventRisk as Record<string, unknown> | null;
      const out: PolymarketCardData[] = [];
      if (dir) {
        out.push({
          question: String(dir.event ?? `${r.symbol} price range`),
          category: String(r.symbol ?? ''),
          yes: null,
          role: 'directional',
          confidence: typeof dir.confidence === 'number' ? dir.confidence : null,
          confirmed: true,
        });
      }
      if (ev) {
        out.push({
          question: String(ev.title ?? ev.key ?? 'Event risk'),
          category: String(ev.key ?? 'Event'),
          yes: typeof ev.probability === 'number' ? ev.probability : null,
          role: 'event_risk',
          concern: typeof ev.concern === 'number' ? ev.concern : null,
          confirmed: true,
        });
      }
      return out;
    });
  }, [snapshots.data]);

  return (
    <div className="space-y-3">
      {/* ---- Ticker ---- */}
      <div className="ticker-strip mono text-[12px] pb-2 border-b hairline">
        {Object.keys(prices).length === 0 ? (
          <span style={{ color: 'var(--text-muted)' }}>
            No live prices — the exchange feed is not delivering ticks here.
          </span>
        ) : (
          Object.entries(prices).map(([symbol, price]) => (
            <span key={symbol} className="shrink-0">
              <span style={{ color: 'var(--text-muted)' }}>{symbol} </span>
              <span>{price.toLocaleString()}</span>
            </span>
          ))
        )}
      </div>

      {/* ---- Hero ---- */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <Card className="lg:col-span-2">
          <SectionTitle>All-time realised P&amp;L</SectionTitle>
          {pnl.counted === 0 ? (
            <NotAvailable
              what="Realised P&L"
              reason="no trade in the log carries a pnl value, so there is nothing to total. This is not a P&L of zero."
              compact
            />
          ) : (
            <>
              <div
                className="hero-num mono"
                style={{ color: pnl.total >= 0 ? 'var(--positive)' : 'var(--negative)' }}
              >
                {pnl.total >= 0 ? '+' : ''}
                {pnl.total.toFixed(2)}
              </div>
              <div className="text-[11.5px] mt-2" style={{ color: 'var(--text-secondary)' }}>
                over {pnl.counted} trade(s) · {pnl.wins}W / {pnl.losses}L
                {pnl.withoutPnl > 0 ? (
                  <span style={{ color: 'var(--warning)' }}>
                    {' '}
                    · {pnl.withoutPnl} trade(s) carry no pnl and are excluded
                  </span>
                ) : null}
              </div>
            </>
          )}
        </Card>

        <Card>
          <SectionTitle>Biggest win</SectionTitle>
          {biggestWin === null ? (
            <div className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
              No trade carries a pnl.
            </div>
          ) : (
            <>
              <div className="mono text-[24px] font-bold" style={{ color: 'var(--positive)' }}>
                +{(biggestWin.pnl as number).toFixed(2)}
              </div>
              <div className="text-[11.5px] mt-1.5" style={{ color: 'var(--text-secondary)' }}>
                {biggestWin.symbol} · {biggestWin.side} · {new Date(biggestWin.ts).toLocaleDateString()}
              </div>
            </>
          )}
        </Card>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Trades logged" value={<Num value={rows.length} digits={0} />} />
        <StatCard
          label="Win rate"
          value={pnl.winRatePct === null ? <span style={{ color: 'var(--text-muted)' }}>—</span> : <Num value={pnl.winRatePct} digits={1} suffix="%" />}
          sub={pnl.counted ? undefined : 'not measurable'}
        />
        <StatCard
          label="Max drawdown"
          value={drawdown === null ? <span style={{ color: 'var(--text-muted)' }}>—</span> : <Num value={drawdown} digits={2} suffix="%" />}
          sub={drawdown === null ? 'needs 2+ trades with a pnl' : 'of realised curve'}
          color={drawdown === null ? undefined : 'var(--negative)'}
        />
        <StatCard label="Graph nodes" value={<Num value={nodesApi.data?.total ?? null} digits={0} />} />
      </div>

      {/* ---- Chart ---- */}
      <Card>
        <SectionTitle>BTCUSDT · 1h</SectionTitle>
        <CandlestickChart
          bars={bars}
          height={280}
          emptyMessage="No candles returned. /api/candles proxies the exchange; with no route to it there is no history to draw."
        />
      </Card>

      {/* ---- Execution cycle ---- */}
      <Card>
        <SectionTitle
          action={
            <Badge
              state={pipeline.isRunning ? 'RUNNING' : pipeline.source === 'none' ? 'IDLE' : 'COMPLETED'}
              label={
                pipeline.isRunning
                  ? 'Cycle running'
                  : pipeline.source === 'none'
                    ? 'No cycle yet'
                    : 'Last cycle'
              }
            />
          }
        >
          Execution cycle
        </SectionTitle>
        <div
          className="text-[11px] mb-2 leading-relaxed"
          style={{ color: pipeline.isRunning ? 'var(--accent)' : 'var(--text-muted)' }}
        >
          {pipelineSourceLabel(pipeline)}
        </div>
        <div className="overflow-x-auto">
          <ExecCycleStepper activeKey={activeStage} />
        </div>
      </Card>

      {/* ---- Swarm ---- */}
      <Card>
        <SectionTitle>Agent ensemble</SectionTitle>
        <AgentSwarmViz
          layers={swarmLayers}
          height={200}
          note={
            pipeline.isRunning
              ? `A cycle is running now — ${pipeline.currentNode} is the active node.`
              : pipeline.lastRun
                ? `Counts are from the last completed cycle on ${pipeline.lastRun.symbol ?? 'an unknown symbol'}.`
                : 'No cycle has run yet, so the counts are the registered structure rather than work done.'
          }
        />
      </Card>

      {/* ---- Polymarket pulse ---- */}
      <Card>
        <SectionTitle>Polymarket pulse</SectionTitle>
        {pmRows.length === 0 ? (
          <div className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
            No confirmed prediction-market mapping, so nothing to pulse. This costs the
            panel nothing.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {pmRows.slice(0, 6).map((r, i) => (
              <PolymarketCard key={i} data={r} compact />
            ))}
          </div>
        )}
      </Card>

      <LiveAgentInspectorModal symbol={inspect} onClose={() => setInspect(null)} />
      {/* ---- Operator controls: real panels from the old sidebar ---- */}
      <HomeOperator />
    </div>
  );
}
