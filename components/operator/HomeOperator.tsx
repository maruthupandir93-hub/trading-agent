'use client';

// The autonomous session ticket. It belongs on /home because handing the agent an
// objective is the one thing an operator comes to this app to DO.
//
// WHAT THIS SLOT HAS HELD, AND WHY IT CHANGED TWICE
//
// It started as `MissionPlannerPanel` — a standing objective the other pages' numbers
// are read against. That moved to /risk, beside the limits it comments on.
//
// It then briefly held a manual order ticket: coin, side, size, Buy. That was the wrong
// shape for this project. A manual Buy is the OPERATOR trading, which puts the whole
// agent — the debate, the specialists, the risk gateway, the monitor — on the sidelines
// for the one action that matters. Replicating an exchange's order form makes this an
// exchange client with an agent attached, rather than an agent with a control surface.
//
// So the panel now sets an OBJECTIVE instead of an order: which coin, how much leverage
// the agent may use, and the equity to stop at. Everything after Start is the agent's.
// The manual ticket is still reachable — `TradePanel` is unchanged and mounted on
// /execution for when an operator genuinely wants to place one by hand.

import { AutonomousSessionPanel } from './AutonomousSessionPanel';

export function HomeOperator() {
  return <AutonomousSessionPanel />;
}
