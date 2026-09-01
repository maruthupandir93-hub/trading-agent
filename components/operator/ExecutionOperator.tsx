'use client';

import { OrderFlowPanel } from '@/components/OrderFlowPanel';
import { OperatorSection } from './OperatorSection';
import { TradePanel } from './TradePanel';

export function ExecutionOperator() {
  return (
    <>
      <OperatorSection title="Order flow" note="Live book pressure, from the exchange depth stream.">
        <OrderFlowPanel />
      </OperatorSection>

      {/* MOVED HERE FROM /home, which now carries the autonomous session ticket.
          A manual order belongs on the execution page: it is the operator taking
          the trade themselves, outside the Supervisor gate by design (CLAUDE.md
          invariant 1). It is kept because an operator sometimes genuinely wants
          to place one by hand — but it is no longer the first thing the app
          offers, because an agent with a manual order form as its front door is
          an exchange client with an agent attached. */}
      <OperatorSection
        title="Manual trade — the operator's own order"
        note="Placed by you, NOT by the agent: no debate, no CRO, no Supervisor. The leverage ceiling and (for paper) the stop-loss watcher still apply. In live mode it uses your own API keys, sent per request and never stored."
      >
        <TradePanel />
      </OperatorSection>
    </>
  );
}
