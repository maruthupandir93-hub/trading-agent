import {
  DECISIONS_PAGE_LIMIT,
  countDecisions,
  listDecisions,
  getDecision,
  addDecision,
} from '@/lib/decisionStore.server';
import type { NewDecisionRecord } from '@/lib/decisionStore.server';

// Complete Audit Trail (Production Readiness Review #9) — every
// Supervisor decision, approved/rejected/pending, not just executed
// trades. Node runtime, file-backed, same shape as /api/trades.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const id = searchParams.get('id');
  if (id) {
    const record = await getDecision(id);
    return Response.json({ decision: record });
  }
  const symbol = searchParams.get('symbol') ?? undefined;
  const tab = searchParams.get('tab') ?? undefined;
  const outcome = searchParams.get('outcome') ?? undefined;
  // CAPPED. This route used to return every row, and the table had reached
  // 80,525 — all rejections from a single day, because the agent records a
  // decision on every evaluation it declines. The page fetched all of them and
  // then laid them out, which is what made it lag.
  //
  // `total` and `truncated` travel with the response so a capped view SAYS it is
  // capped. A silently shortened list is how a reader concludes the agent has
  // been idle.
  const requested = Number.parseInt(searchParams.get('limit') ?? '', 10);
  const limit = Number.isFinite(requested) && requested > 0 ? requested : DECISIONS_PAGE_LIMIT;

  const [all, total] = await Promise.all([
    listDecisions({ symbol, tab, outcome, limit }),
    countDecisions({ symbol, tab, outcome }),
  ]);

  return Response.json({
    decisions: all,
    limit,
    // null when there is no database to count in — not 0, which would claim the
    // table is empty.
    total,
    truncated: total !== null && total > all.length,
  });
}

export async function POST(req: Request) {
  let body: Partial<NewDecisionRecord>;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  if (!body.symbol || typeof body.symbol !== 'string') {
    return Response.json({ error: 'symbol is required' }, { status: 400 });
  }
  if (body.side !== 'buy' && body.side !== 'sell') {
    return Response.json({ error: 'side must be buy or sell' }, { status: 400 });
  }
  if (body.tab !== 'paper' && body.tab !== 'real') {
    return Response.json({ error: 'tab must be paper or real' }, { status: 400 });
  }
  if (!body.outcome || typeof body.outcome !== 'string') {
    return Response.json({ error: 'outcome is required' }, { status: 400 });
  }

  const record: NewDecisionRecord = {
    symbol: body.symbol,
    side: body.side,
    tab: body.tab,
    originTag: body.originTag ?? 'user-command',
    requestedQty: typeof body.requestedQty === 'number' ? body.requestedQty : 0,
    requestedPrice: typeof body.requestedPrice === 'number' ? body.requestedPrice : 0,
    outcome: body.outcome as NewDecisionRecord['outcome'],
    urgency: typeof body.urgency === 'string' ? body.urgency : 'normal',
    rejectionReasons: Array.isArray(body.rejectionReasons) ? body.rejectionReasons : [],
    conflictNotes: Array.isArray(body.conflictNotes) ? body.conflictNotes : [],
    cautionNotes: Array.isArray(body.cautionNotes) ? body.cautionNotes : [],
    riskChecks: body.riskChecks && typeof body.riskChecks === 'object' ? body.riskChecks : null,
    stopLoss: typeof body.stopLoss === 'number' ? body.stopLoss : null,
    takeProfit: typeof body.takeProfit === 'number' ? body.takeProfit : null,
    recommendedQty: typeof body.recommendedQty === 'number' ? body.recommendedQty : null,
    ensembleConsensus: typeof body.ensembleConsensus === 'string' ? body.ensembleConsensus : null,
    ensembleConfidencePct: typeof body.ensembleConfidencePct === 'number' ? body.ensembleConfidencePct : null,
    debateRecommendation: typeof body.debateRecommendation === 'string' ? body.debateRecommendation : null,
    debateConfidencePct: typeof body.debateConfidencePct === 'number' ? body.debateConfidencePct : null,
    rationale: typeof body.rationale === 'string' ? body.rationale : undefined,
    tradeLogEntryId: typeof body.tradeLogEntryId === 'string' ? body.tradeLogEntryId : undefined,
  };

  const saved = await addDecision(record);
  return Response.json({ decision: saved }, { status: 201 });
}
