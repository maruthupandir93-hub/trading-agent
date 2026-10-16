import { getTrade, deleteTrade } from '@/lib/tradeStore.server';
import { writeAuthFailure } from '@/lib/apiAuth';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// The bespoke Bearer-only check that lived here is now the narrower half of
// `writeAuthFailure` — see the note in `app/api/trades/route.ts` for why
// accepting only a key the browser never holds was half of F35.

export async function GET(_req: Request, { params }: { params: { id: string } }) {
  const trade = await getTrade(params.id);
  if (!trade) return Response.json({ error: 'Trade not found' }, { status: 404 });
  return Response.json({ trade });
}

export async function DELETE(req: Request, { params }: { params: { id: string } }) {
  // F34: this route CHANGES STORED STATE, and `middleware.ts` excludes `/api`.
  // Without this the public deployment accepted the write from anyone.
  const unauthorized = writeAuthFailure(req);
  if (unauthorized) return unauthorized;

  const removed = await deleteTrade(params.id);
  if (!removed) return Response.json({ error: 'Trade not found' }, { status: 404 });
  return Response.json({ deleted: true });
}
