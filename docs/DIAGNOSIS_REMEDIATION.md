# External review (October 2026) — remediation record

Covers every item in `docs/DIAGNOSIS.md`: the 14 numbered Issues and the
further 36. Each row names where the fix landed and, where the finding was
declined or already true, says so with the reason.

Nothing here makes a trade profitable. The measured edge on this system is
about **+0.05R and is not distinguishable from zero**; at a fixed 2:1 payoff
break-even is a ~33–36% win rate, so roughly two trades in three are designed
to lose and four losses in five is a ~1-in-6 routine outcome. These fixes
remove ways the system can be WRONG about what it is doing. That is a
different thing from an edge, and the distinction is the point of several of
them.

---

## The 14 numbered Issues

| # | Finding | Where it landed |
|---|---|---|
| 1 | Disabling live mode could simulate a REAL close | batch 2 — `close_routes_to_venue(tab)`; only an explicit simulation override blocks a real close |
| 2 | `/live-trading/enable` continued after a credential-check error | batch 1 — fails closed |
| 3 | A real position could stay unprotected if the venue stop failed | batch 5 — four explicit `PROTECTION_*` states, **and the resting quantity is checked**, since a venue can accept a stop for less than the position |
| 4 | Exchange credentials crossing a plain-HTTP hop | batch 7 — the proxy **refuses** a credential-bearing body over a non-https, non-loopback backend. `ALLOW_PLAINTEXT_EXCHANGE_SECRETS` is the named opt-out. **TLS on the backend and keys in browser localStorage remain operator decisions** — see below |
| 5 | An unmeasurable risk configuration passed the guard | batch 6 — `SAFE` / `UNSAFE` / `UNKNOWN`. `ok` keeps the permissive paper rule; a REAL session requires a measured `SAFE` |
| 6 | The guard does not choose a small risk amount for you | **Not a code fix — an operator decision.** See "Still owed" |
| 7 | One authoritative pre-trade validator | batch 7 — true of both automated paths and now pinned. The manual plane is **deliberately** unsupervised (invariant 1) and keeps the two hard limits |
| 8 | Duplicate protection was in memory only | batch 5 — `execution_submissions.basis` PRIMARY KEY; the claim *is* the insert |
| 9 | Several exchange-order interfaces, differently labelled | batch 7 — `services/execution_planes` states venue, market type, network, account, position mode and protective orders per plane |
| 10 | TWAP computed slices and placed one market order | batch 5 — **relabelled, not implemented.** The log says `NOT APPLIED` and names the depth cap as the real impact control |
| 11 | Exchange-side protection and local monitoring must agree | batch 7 — three independent states on `/api/monitoring`, plus `consecutive_failures` on the review worker |
| 12 | Scalping mismatched to the analysis timeframe | batch 7 — a strategy the clock dominates is `REFUSED` by `strategy_approval`, not ranked |
| 13 | Backtest costs improved but incomplete | batch 7 — funding modelled, settlements counted; gross, fee and funding reported separately |
| 14 | Strategy selection is not proof of profitability | batch 7 — `services/strategy_approval`; a backtest can never approve |

## The further 36

Batch 1 — #1 (reflection `asyncio`), #2 (fail-closed enable), #3 (retention
join), #4 (unauthenticated SSRF on `/api/ai/chat`), #5 (blended exit price),
#6 (repeating stop-order ids), #7 (margin never emitted), #8 (resume skipped
the risk check), #21 (the whole API publicly readable), plus the liquidity
depth cap.

Batch 2 — the P0 close-routing bug, #11, #12, #17, #18, #20.
Batch 3 — #15, #13, #34, #10.
Batch 4 — #32, #35, #36, #33, #28, #27.
Batch 5 — Issues 3, 8, 10.
Batch 6 — #14, #16, #19, #22, #23, #24, #25, #26, #29, #30, #31, Issue 5.
Batch 7 — Issues 4, 7, 9, 11, 12, 13, 14.

#2, #8 and #9 of that list are **settings**, not code. They are below.

---

## Section 6 — "What your trading bot should learn to do better"

Six measurement rows. Four already had something behind them, and that was
**checked rather than assumed**: regime and volatility are computed on every
run; `execution_quality` records slippage, partial fills and latency;
liquidation distance is not merely measured but ENFORCED by
`liquidation_safe_leverage`. Two did not.

| Row | Status |
|---|---|
| Market regime | already computed per run |
| Signal quality | HTF agreement and distance-from-invalidation already recorded |
| Execution quality | `execution_quality`: slippage, partial fills, latency |
| Risk | liquidation distance **enforced**, not just measured |
| Strategy performance | pooled existed; **by regime** added (`by_regime()`, with profit factor) |
| **Abstention** | **nothing at all → `services/abstention.py`** |

The A–D regime guidance was likewise checked: strategy/regime suitability is
scored from each profile's own `active_regimes` (A), a grid cannot accumulate
inventory because `MAX_CONCURRENT_POSITIONS` is 1 (B), and `Liquidity Crisis`
is in `BLOCKED_REGIMES` (D).

**Only 6C was genuinely missing — and 6C turned out to be WRONG for this
account.** The review asks for a gate on chasing an extended candle, and
`strategy_profiles.Breakout` names the same thing as its own worst condition.
`algorithms/entry_extension` measures it. Replayed through the real `assess`
against 17,157 signals on six instruments
(`scripts/measure_entry_extension.py`):

```
every signal          -0.0182R +/-0.0211   32.7%   17,157 trades
the gate would KEEP   -0.0503R +/-0.0259   31.7%   11,144 trades
the gate would BLOCK  +0.0412R +/-0.0361   34.7%    6,013 trades

gap -0.0915R, z = -4.04, against a smallest-detectable gap of 0.0371R
```

The gate removes the **better** half. `REQUIRE_ENTRY_PROXIMITY` therefore
defaults **OFF**; the extension is still measured and recorded on every entry,
so a future ledger can disagree. The result ran against the hypothesis, which
is the only direction this kind of result is worth much.

---

## Still owed by the operator — none of these is a code change

1. **Every configured LLM model must be verified alive.** The provider fails
   closed, so a dead model looks exactly like a quiet degradation. A model's
   state is a property of the ACCOUNT AND THE DATE, never of the name — this
   file has twice recorded the opposite of its own earlier statement about two
   specific models. `GET /api/monitoring`'s `llmHealth` is the standing
   version of that probe.

2. **`MAX_PORTFOLIO_VAR_FRACTION`.** At `0.12` a stop-out costs ~12% of
   equity, so against a 30% drawdown limit the killswitch fires after **2.5
   consecutive losses** — which at this payoff happens about one session in
   six. `risk_coherence` refuses such a session now and names all three levers;
   it never resizes, because silently trading a smaller size than configured is
   the same failure as a safety control that reports success while doing
   nothing. ≤ 0.0375 is the coherent setting for a 30% limit and an
   eight-loss requirement.

3. **`TRADES_API_KEY` and `DASHBOARD_PASSWORD` on BOTH sides.** The backend
   checks the key; the proxy supplies it and checks the password. Setting one
   side only does not change the refusal. Vercel bakes env at build time, so a
   redeploy is required. The Settings banner now names whichever half is
   missing — including both.

4. **Fund the book past the venue minimum.** Every listed pair has a $5
   minimum order and ETH is $20. At $2 of equity most ordinary decisions
   produce an order the venue refuses, which is silent. Past ~$25 the minimum
   stops being a consideration at all. **This is strictly safer than raising
   the VaR fraction to clear the same floor.**

5. **TLS on the backend, and keys out of browser localStorage** (Issue 4's
   remaining half). The proxy now refuses to forward secrets over the plain
   hop, which closes the transit exposure; `components/ExchangeAccounts.tsx`
   states the storage tradeoff plainly and it is still true. Exchange keys
   should carry trading permission only — never withdrawals — and be
   IP-restricted where practical.

6. **Rotate the credentials that have been exposed.** The Supabase password
   remains in git history from an earlier incident, and
   `LLM_CONSULT_KEY_NVIDIA` is plaintext in `.env`.

---

## What was deliberately NOT done

* **TWAP was relabelled rather than implemented.** A real schedule needs
  partial fills, cancellation, and an answer for the window where the position
  exists at a size neither the monitor nor the stop knows about. Half of that
  is worse than none.

* **`REQUIRE_STRATEGY_APPROVAL` defaults to OFF.** No strategy has twenty
  closed real trades, so switching it on stops real trading entirely until one
  does. That is a legitimate policy and the operator's to choose. The RECORD
  runs unconditionally, so the question stays answerable either way.

* **The manual trading plane is not supervised**, per invariant 1. It keeps
  the leverage ceiling and the stop-validity check, which are properties of
  the account rather than supervision rules.

* **F66 stays declined.** The volatility node computes a `STOP_ATR_MULTIPLE`
  that nothing applies. Applying it would set a 1.5x stop in low volatility,
  and the nine-SOL-trade finding is that five of six losses were stop-outs
  INSIDE the noise band at exactly that multiple. The unused value stays
  unused until there is evidence for it.

---

## Verification at the close of batch 8

```
backend     2520 passed, 2 skipped
frontend    522 passed across 37 files
tsc         clean
next build  passes
scripts/full_system_exercise.py   18/18, no database pool opened
```
