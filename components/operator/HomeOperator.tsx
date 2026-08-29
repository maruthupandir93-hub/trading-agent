'use client';

// The operator's trade ticket. It belongs on /home because placing a trade is
// the one thing an operator comes to this app to DO, and it was previously three
// pages away behind a Mission planner.
//
// WHAT THIS REPLACED, AND WHY
//
// This slot held `MissionPlannerPanel`. A Mission is a standing objective the
// other pages' numbers are read against — useful, but not what the home page is
// for, and it is still reachable from its own route. The trade ticket is the
// thing that has to be one click away.
//
// The panel itself is mode-aware: paper by default, and it switches to the
// real-money path with the operator's own keys when LIVE_TRADING is on. See
// `TradePanel` for why those two are deliberately different code paths.

import { TradePanel } from './TradePanel';

export function HomeOperator() {
  return <TradePanel />;
}
