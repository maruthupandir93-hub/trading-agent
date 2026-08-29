// Real Exchange Trading — the operator's own signed Binance/Bybit calls.
//
// THIS ROUTE NO LONGER SIGNS OR SENDS ANYTHING. It forwards to the backend,
// which holds the exchange conversation. `backend/api/operator_exchange.py`.
//
// WHY IT MOVED
//
// It signed and sent orders from Vercel. A Vercel handler executes in a
// Vercel-chosen region, and Binance refuses restricted regions with
// `451 Unavailable For Legal Reasons` — the same failure that broke /api/candles.
// On this path the consequence is worse than an empty chart: the operator's
// order would fail exactly when real money was on the line, and the reason
// ("your serverless function ran in the wrong country") is not one anybody would
// guess from a failed trade.
//
// WHAT DID NOT CHANGE
//
//   * The trust model. The client still holds the API key/secret
//     (components/ExchangeAccounts.tsx, localStorage) and sends them per request.
//     Nothing is stored or logged at either hop.
//   * The request and response shapes, so components/ExchangeAccounts.tsx is
//     untouched.
//   * That this path is UNSUPERVISED. CLAUDE.md invariant 1 keeps manual human
//     clicks out of the Supervisor's scope deliberately — this is the operator's
//     own hands, not an agent's.
//
// WHAT DID CHANGE, FOR THE BETTER
//
//   * The backend requires write auth on every one of these routes, and this
//     proxy attaches `TRADES_API_KEY` server-side. Order placement is no longer
//     reachable by anything that can merely reach the port.
//   * Every order is now persisted with `origin_tag='manual-click'` and logged
//     at WARNING. This route recorded nothing server-side.
//
// THE SECRET NOW CROSSES ONE MORE HOP: browser -> Vercel -> backend. Both are
// the operator's own infrastructure and the browser->Vercel leg is https. The
// Vercel->backend leg is plain http on this deployment, so it should run over a
// private network or a tunnel before this is used with mainnet keys — see
// docs/DEPLOYMENT_NETWORKING.md.

import { backendOrigin } from '@/lib/api/backendProxy.server';
import type { TradingExchangeId } from '@/lib/exchangeClients/types';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type ExchangeAction = 'balance' | 'placeOrder' | 'orderStatus' | 'cancelOrder';

type RequestBody = {
  exchange: TradingExchangeId;
  apiKey: string;
  apiSecret: string;
  testnet: boolean;
  action: ExchangeAction;
  params?: {
    symbol?: string;
    side?: 'buy' | 'sell';
    qty?: number;
    exchangeOrderId?: string;
    clientOrderId?: string; // idempotency key — see lib/executionQuality.ts
  };
};

/** Which backend route each action maps to. */
const ACTION_PATHS: Record<ExchangeAction, string> = {
  balance: '/api/operator/exchange/balance',
  placeOrder: '/api/operator/exchange/order',
  orderStatus: '/api/operator/exchange/order/status',
  cancelOrder: '/api/operator/exchange/order/cancel',
};

export async function POST(req: Request) {
  let body: Partial<RequestBody>;
  try {
    body = await req.json();
  } catch {
    return Response.json({ ok: false, error: 'Invalid JSON body' }, { status: 400 });
  }

  // Validated HERE, before the hop, and kept identical to what this route
  // rejected before. A malformed order should never reach a machine that can
  // sign it, and the caller gets the same message it always did.
  if (body.exchange !== 'binance' && body.exchange !== 'bybit') {
    return Response.json({ ok: false, error: 'exchange must be "binance" or "bybit"' }, { status: 400 });
  }
  if (!body.apiKey || !body.apiSecret) {
    return Response.json({ ok: false, error: 'apiKey and apiSecret are required' }, { status: 400 });
  }
  if (!body.action || !ACTION_PATHS[body.action]) {
    return Response.json(
      { ok: false, error: 'action must be one of balance, placeOrder, orderStatus, cancelOrder' },
      { status: 400 },
    );
  }

  const p = body.params;
  if (body.action === 'placeOrder') {
    if (!p?.symbol || (p.side !== 'buy' && p.side !== 'sell') || typeof p.qty !== 'number' || p.qty <= 0) {
      return Response.json(
        { ok: false, error: 'placeOrder needs params: { symbol, side: "buy"|"sell", qty > 0 }' },
        { status: 400 },
      );
    }
  }
  if ((body.action === 'orderStatus' || body.action === 'cancelOrder') && (!p?.symbol || !p.exchangeOrderId)) {
    return Response.json(
      { ok: false, error: `${body.action} needs params: { symbol, exchangeOrderId }` },
      { status: 400 },
    );
  }

  const payload: Record<string, unknown> = {
    exchange: body.exchange,
    apiKey: body.apiKey,
    apiSecret: body.apiSecret,
    testnet: body.testnet === true,
  };

  if (body.action === 'placeOrder') {
    payload.symbol = p!.symbol;
    payload.side = p!.side;
    payload.qty = p!.qty;
    // Passed straight through — this is the idempotency key the exchange uses to
    // reject a duplicate retry. Dropping it here would silently make retries
    // unsafe, which is the failure that produces two fills for one intent.
    if (p!.clientOrderId) payload.clientOrderId = p!.clientOrderId;
  } else if (body.action === 'orderStatus' || body.action === 'cancelOrder') {
    payload.symbol = p!.symbol;
    payload.exchangeOrderId = p!.exchangeOrderId;
  }

  try {
    const upstream = await fetch(`${backendOrigin()}${ACTION_PATHS[body.action]}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // Attached server-side. The browser performs a real-money action without
        // ever holding the backend's credential, and the key never enters the
        // client bundle.
        ...(process.env.TRADES_API_KEY ? { Authorization: `Bearer ${process.env.TRADES_API_KEY}` } : {}),
      },
      body: JSON.stringify(payload),
      cache: 'no-store',
    });

    const text = await upstream.text();

    if (!upstream.ok) {
      let message = text.slice(0, 500);
      try {
        const parsed = JSON.parse(text);
        if (typeof parsed?.detail === 'string') message = parsed.detail;
        else if (typeof parsed?.error === 'string') message = parsed.error;
      } catch {
        // Not JSON — the raw text is more useful than a guess.
      }
      return Response.json({ ok: false, error: message }, { status: upstream.status });
    }

    return new Response(text, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (err) {
    // AMBIGUOUS BY NATURE, AND SAID SO RATHER THAN GUESSED AT.
    //
    // A transport failure here cannot distinguish "the backend never received
    // the order" from "it placed the order and the reply was lost". Reporting a
    // clean failure would be a guess, and the wrong guess leads an operator to
    // retry an order that already exists. The idempotency key is what makes that
    // retry safe when one was supplied, which is why it is named here.
    const message = err instanceof Error ? err.message : 'Exchange request failed unexpectedly';
    return Response.json(
      {
        ok: false,
        error:
          `Could not reach the trading backend at ${backendOrigin()} (${message}). ` +
          `The order may or may not have been placed — this hop failed, not the exchange. ` +
          `CHECK THE EXCHANGE BEFORE RETRYING. A retry is only safe if it reuses the same ` +
          `clientOrderId, which the venue rejects as a duplicate.`,
      },
      { status: 502 },
    );
  }
}
