"""Batch 7: the review's remaining numbered Issues — 9, 11, 12, 13, 14.

These are not defects in the sense the earlier batches were. Nothing here
produces a wrong number; what each one produces is a TRUE statement that an
operator will read as a different, false one.

    "Real"                      means spot on one panel and a leveraged
                                perpetual on another (Issue 9)
    "position monitoring: off"  means the GRAPH review is off and says nothing
                                about the mechanical stop, which runs anyway
                                (Issue 11)
    "Scalping ranks best"       on a timeframe where the clock ends 76% of its
                                trades (Issue 12)
    "expectancy +0.16R"         gross of funding on multi-hour holds (Issue 13)
    "this strategy scores 0.62" is a claim about fit, read as a claim about
                                profitability (Issue 14)
"""

from __future__ import annotations

import inspect

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# Issue 9 — "Real" means two different instruments
# ---------------------------------------------------------------------------

def test_the_four_planes_are_described_separately():
    """The review: *"Do not let a generic 'Real' label hide these
    distinctions."* Paper simulation, testnet execution, live spot and live
    perpetual futures are four different things with four different answers to
    "what closes this position?"."""
    from backend.services.execution_planes import (
        AGENT_PERPETUAL, OPERATOR_SPOT, PAPER_MIRRORED, PAPER_SIMULATED, planes,
    )

    ids = [p["id"] for p in planes()]
    assert ids == [PAPER_SIMULATED, PAPER_MIRRORED, OPERATOR_SPOT, AGENT_PERPETUAL]


def test_every_plane_states_its_market_type_and_protection():
    """Each should expose its actual venue, market type, account, network and
    protective-order status -- the review's list, and the fields an operator
    needs to answer "what would happen if I pressed this"."""
    from backend.services.execution_planes import planes

    for plane in planes():
        for field in ("marketType", "network", "account", "positionMode",
                      "protectiveOrders", "closedBy", "venue"):
            assert field in plane, f"{plane['id']} does not state {field}"


def test_the_operator_plane_is_spot_and_the_agent_plane_is_not():
    """THE DISTINCTION THE REVIEW IS ABOUT, and it is load-bearing in code
    already: `operator_exchange` pins `defaultType: "spot"`, and its own route
    refuses to hand a spot holding to the PERPETUAL position monitor because
    the monitor cannot reduce one."""
    from backend.services.execution_planes import planes

    by_id = {p["id"]: p for p in planes()}
    assert by_id["operator-spot"]["marketType"] == "SPOT"
    assert by_id["operator-spot"]["leverageAvailable"] is False
    assert "perpetual" in by_id["agent-perpetual"]["marketType"]
    assert by_id["agent-perpetual"]["leverageAvailable"] is True


def test_a_spot_order_says_so_on_the_order_itself():
    """Stating it on the response means an operator reading one -- or a log of
    one -- never has to infer the instrument from a tab label."""
    import backend.api.operator_exchange as oe

    src = inspect.getsource(oe)
    assert '"plane": "operator-spot"' in src
    assert '"marketType": "SPOT"' in src


# ---------------------------------------------------------------------------
# Issue 11 — one status standing in for three
# ---------------------------------------------------------------------------

def test_three_independent_protection_states_are_reported():
    """*"monitor three independent states: mechanical stop active,
    exchange-side stop confirmed, and graph-based position review healthy. One
    status should not stand in for the others."*

    What was reported was `positionMonitoringEnabled`, which decides whether
    the GRAPH's review decisions are acted on. False does not mean nothing is
    watching, and true does not mean a stop rests at the venue."""
    from backend.api.monitoring import _protection_states

    out = _protection_states()
    assert "mechanical" in out
    assert "exchangeSide" in out
    assert "graphReview" in out


def test_the_mechanical_state_reports_whether_a_position_has_a_tick_feed():
    """`PositionMonitorAgent` enforces every stop by reacting to
    TICK_RECEIVED, so a position in an unsubscribed instrument is listed as
    watched while its stop can never fire. That happened once with a hardcoded
    three-symbol list and nothing reported it."""
    from backend.api.monitoring import _protection_states

    mech = _protection_states()["mechanical"]
    assert "positionsWithNoTickFeed" in mech
    assert "symbolsSubscribed" in mech


def test_the_exchange_side_state_counts_unprotected_positions_separately():
    """Batch 5 made protection an explicit per-position state precisely
    because `stop_order_id is None` was produced by three different outcomes.
    This is where that vocabulary becomes an operator-visible number."""
    from backend.api.monitoring import _protection_states

    venue = _protection_states()["exchangeSide"]
    for field in ("confirmedAtVenue", "partiallyCovered", "unprotected"):
        assert field in venue


def test_a_repeatedly_failing_review_loop_is_visible():
    """`start()` catches every cycle exception so one bad pass cannot kill the
    loop -- which is right, and it meant a loop failing on EVERY pass looked
    identical to a healthy idle one: `cycles_run` simply stopped advancing."""
    from backend.workers.position_worker import PositionMonitoringWorker

    worker = PositionMonitoringWorker()
    worker._running = True
    assert worker.health()["healthy"] is True

    worker.consecutive_failures = PositionMonitoringWorker.UNHEALTHY_AFTER
    worker.last_error = "boom"
    health = worker.health()
    assert health["healthy"] is False
    assert "boom" in health["note"]


def test_a_stopped_review_loop_does_not_claim_nothing_is_watching():
    """The mechanical stop is a SEPARATE mechanism and runs regardless. A note
    that read "not running" without saying so is how an operator concludes
    their positions are unprotected when they are not."""
    from backend.workers.position_worker import PositionMonitoringWorker

    note = PositionMonitoringWorker().health()["note"]
    assert "MECHANICAL" in note


# ---------------------------------------------------------------------------
# Issue 13 — funding, which this engine left out
# ---------------------------------------------------------------------------

def _series(n: int = 800):
    import random

    random.seed(7)
    out = []
    price = 100.0
    for i in range(n):
        drift = 0.0010 if (i // 60) % 2 == 0 else -0.0010
        price *= 1 + drift + random.gauss(0, 0.0012)
        out.append({
            "open": price, "high": price * 1.004, "low": price * 0.996,
            "close": price, "volume": 100, "openTime": i * 3600_000,
        })
    return out


def _any_strategy():
    from backend.agents.strategy_ensemble import STRATEGY_FUNCTIONS

    name = next(iter(STRATEGY_FUNCTIONS))
    return name, STRATEGY_FUNCTIONS[name]


def test_funding_is_charged_and_shows_up_in_the_net_expectancy(monkeypatch):
    """Funding was named as deliberately unmodelled, which was defensible
    while trades lasted minutes. `max_holding_minutes` made multi-hour holds
    routine, and funding settles every eight hours regardless of outcome."""
    from backend.core.strategy_backtest import backtest_strategy

    candles = _series()
    name, fn = _any_strategy()

    monkeypatch.setenv("BACKTEST_FUNDING_RATE", "0")
    free = backtest_strategy(candles, fn, name=name)

    monkeypatch.setenv("BACKTEST_FUNDING_RATE", "0.001")
    charged = backtest_strategy(candles, fn, name=name)

    assert free.funding_r_per_trade == 0.0
    assert charged.funding_r_per_trade > 0.0
    assert charged.expectancy_r < free.expectancy_r
    # The GROSS figure must not move: it is the risk model's own definition
    # (+2R for a target, -1R for a stop) and a cost leaking into it would make
    # every outcome a slightly different number with no obvious meaning.
    assert charged.gross_expectancy_r == free.gross_expectancy_r


def test_the_three_cost_lines_are_reported_separately(monkeypatch):
    """*"Report gross and net expectancy separately."* And the two costs apart
    from each other: the fee is paid once per round trip and shrinks as the
    stop widens, while funding accrues with TIME. Which one a strategy is
    losing to decides what to do about it."""
    from backend.core.strategy_backtest import backtest_strategy

    monkeypatch.setenv("BACKTEST_FUNDING_RATE", "0.001")
    name, fn = _any_strategy()
    out = backtest_strategy(_series(), fn, name=name)

    assert out.gross_expectancy_r != 0.0
    assert out.fee_r_per_trade > 0.0
    assert out.funding_r_per_trade > 0.0
    # Each figure is stored rounded for readability, so the identity holds to
    # the coarsest of those roundings (4 decimals) rather than exactly.
    assert out.expectancy_r == pytest.approx(
        out.gross_expectancy_r - out.fee_r_per_trade - out.funding_r_per_trade,
        abs=1e-3,
    )


def test_funding_can_be_turned_off_so_the_stored_results_stay_comparable():
    """`backtests/2026-09-06/` was measured without it. A study comparing
    against that run needs to be able to reproduce its cost model."""
    import backend.core.strategy_backtest as bt

    assert "BACKTEST_FUNDING_RATE" == bt.FUNDING_VAR


def test_funding_counts_settlements_rather_than_prorating():
    """A venue charges the whole rate at each eight-hour mark and nothing in
    between. Prorating would understate a short hold that straddles one and
    overstate a seven-hour one that does not."""
    import backend.core.strategy_backtest as bt

    src = code_only(inspect.getsource(bt))
    assert "FUNDING_INTERVAL_MINUTES" in src
    assert "settlements" in src


# ---------------------------------------------------------------------------
# Issues 12 and 14 — ranking well is not having been shown to make money
# ---------------------------------------------------------------------------

def test_a_backtest_can_never_approve_a_strategy_for_real_money():
    """CLAUDE.md's rule is "a backtest INFORMS; it does not deploy", and this
    module is where that sentence stops being advice. `strategy_priors` says in
    four places that its evidence is in-sample, gross of the costs measured
    since, and taken over a mostly trending window."""
    import backend.services.strategy_approval as sa

    src = code_only(inspect.getsource(sa))
    assert "strategy_priors" not in src, (
        "approval reads a backtest, which can only ever be in-sample evidence"
    )
    assert "strategy_performance" in src


def test_approval_makes_no_model_call():
    """The same invariant-5 argument `strategy_performance` and
    `strategy_priors` both make: deterministic arithmetic over closed trades,
    no model consulted, and it cannot invent, edit or disable a strategy."""
    import backend.services.strategy_approval as sa

    src = code_only(inspect.getsource(sa))
    for forbidden in ("complete(", "llm", "ModelTier", "openai"):
        assert forbidden not in src, f"approval reaches for {forbidden!r}"


def test_a_strategy_the_clock_dominates_is_refused_rather_than_ranked():
    """Issue 12. Measured on 5 instruments x 1000 real 15m candles: Scalping
    76%, Arbitrage 73%, even after the MIN_HOLDING_BARS floor. A longer bound
    does not fix it -- raising it further would just stop the strategy being a
    scalp."""
    from backend.services.strategy_approval import MAX_TIME_EXIT_SHARE, REFUSED, assess_cached

    out = assess_cached("Scalping", time_exit_share=0.76)
    assert out.status == REFUSED
    assert "clock ends 76%" in out.reason

    ok = assess_cached("Breakout", time_exit_share=MAX_TIME_EXIT_SHARE - 0.01)
    assert ok.status != REFUSED


def test_too_few_trades_is_a_candidate_not_a_refusal():
    """CANDIDATE is the normal state and is not a fault: it means the strategy
    may be the best available choice right now and has not yet demonstrated a
    net edge. Conflating that with "this loses money" would freeze out every
    strategy that has not traded yet, including the one that would have
    worked."""
    from backend.services.strategy_approval import CANDIDATE, _verdict

    out = _verdict("Breakout", {"sampleSize": 3, "expectancy": 12.0}, None)
    assert out.status == CANDIDATE
    assert "3 real closed trade(s)" in out.reason


def test_a_measured_loser_is_refused_however_well_it_ranks():
    from backend.services.strategy_approval import REFUSED, _verdict

    out = _verdict("Grid", {"sampleSize": 40, "expectancy": -3.5}, None)
    assert out.status == REFUSED
    assert "-3.50 per trade after costs" in out.reason


def test_a_measured_winner_with_enough_trades_is_approved():
    from backend.services.strategy_approval import APPROVED, _verdict

    out = _verdict("Breakout", {"sampleSize": 40, "expectancy": 7.25}, None)
    assert out.status == APPROVED
    assert out.approved_for_real_money is True
    assert "out-of-sample" in out.reason


def test_an_unreadable_record_is_not_a_refusal():
    """"Could not measure" and "measured and unprofitable" are different facts,
    and this codebase has now paid for conflating them five times."""
    from backend.services.strategy_approval import CANDIDATE, _verdict

    out = _verdict("Breakout", {}, None, record_available=False)
    assert out.status == CANDIDATE
    assert "could not be established" in out.reason


def test_a_trade_with_no_strategy_is_not_judged():
    """A manual position closes with `strategy` NULL by design and the event
    path writes none either. Inventing an attribution would poison the
    measurement this reads."""
    from backend.services.strategy_approval import CANDIDATE, assess_cached

    out = assess_cached(None)
    assert out.status == CANDIDATE
    assert "no strategy is attributed" in out.reason


def test_the_gate_is_off_by_default_and_read_at_call_time(monkeypatch):
    """No strategy here has twenty closed real trades, so a gate on by default
    stops real trading entirely until one does. That is a legitimate policy and
    the OPERATOR's to choose.

    Call time, never frozen at import: the `simulation_mode` rule."""
    from backend.services.strategy_approval import gate_enabled

    monkeypatch.delenv("REQUIRE_STRATEGY_APPROVAL", raising=False)
    assert gate_enabled() is False
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "true")
    assert gate_enabled() is True


def test_the_gateway_records_approval_even_with_the_gate_off():
    """The separation is lost if the distinction only exists when a flag is on.
    Every real entry's evidence states whether the strategy behind it had
    demonstrated an edge, so the question stays answerable afterwards."""
    import backend.graphs.nodes.risk_gateway as rg

    src = code_only(inspect.getsource(rg.gate))
    assert "strategy_approval" in src
    assert "StrategyApproval" in inspect.getsource(rg.gate)


def test_the_gate_never_applies_to_paper():
    """Paper research is how a strategy earns a record in the first place.
    Refusing the trades that would produce the evidence is a loop with no
    exit."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    block = src[src.index("strategy_approval.assess_cached"):]
    block = block[: block.index("THE FEE MUST NOT EAT")] if "THE FEE MUST NOT EAT" in block else block[:2000]
    assert 'tab == "real"' in block


def test_the_gateway_reads_the_cache_rather_than_querying(monkeypatch):
    """A node that queried the ledger mid-run would not be replay-safe: a
    resumed checkpoint would reason over a different set of closed trades than
    the original decision (Section 39.4)."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    assert "assess_cached" in src
    assert "await strategy_approval" not in src


# ---------------------------------------------------------------------------
# Issue 7 — one authoritative pre-trade validator
# ---------------------------------------------------------------------------

def test_both_automated_originators_reach_the_same_validator():
    """*"One authoritative pre-trade validator should govern every automated
    and manual entry."*

    For the AUTOMATED paths this already holds and the reason it holds is
    worth pinning: `trade_scope`'s header records that the scope rules used to
    live in the gateway, which meant they applied to the GRAPH path only --
    and the event path traded 4,080 times in five days with none of them. "A
    limit that only one of two execution paths respects is not a limit."
    """
    import backend.agents.supervisor_agent as ev
    import backend.graphs.nodes.risk_gateway as rg

    for module in (ev, rg):
        src = code_only(inspect.getsource(module))
        assert "validate_trade" in src, f"{module.__name__} has its own rules"


def test_the_gateway_runs_the_validator_in_strict_mode():
    """A check that could NOT run is a failure here, not a pass. The gateway
    holds every input, so an unrunnable check is a bug in this node rather
    than a caller limitation."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    assert "strict=True" in src


def test_the_manual_plane_is_deliberately_not_supervised_but_keeps_the_hard_limits():
    """THE ONE PLACE THIS REVIEW ITEM CONFLICTS WITH A STATED DECISION, and
    the decision wins: CLAUDE.md invariant 1 scopes supervision to
    AI-originated trades -- *"Manual human clicks are deliberately out of
    scope: supervising agents means supervising agents, not overriding the
    operator."*

    What the manual plane does NOT get to skip is the two HARD limits, which
    are properties of the account rather than supervision rules: the leverage
    ceiling (invariant 2) and a stop that can actually fire (invariant 3).
    """
    import backend.api.operator_trade as ot

    src = code_only(inspect.getsource(ot))
    # Invariant 2: the same function the Risk Gateway calls, not a copy.
    assert "check_leverage" in src
    # Invariant 3, via batch 6: the side check that had never run.
    assert "stop_side_fault" in src


def test_the_operator_spot_route_has_no_leverage_to_cap():
    """It is SPOT (Issue 9), so invariant 2 has nothing to bound -- which is a
    reason the ceiling is absent there, not an omission. Asserted so a future
    reader does not "fix" it by adding a futures client."""
    import backend.api.operator_exchange as oe

    # RAW, not `code_only`: both halves of this live in string literals, which
    # `code_only` strips.
    src = inspect.getsource(oe)
    assert '"defaultType": "spot"' in src
    assert '"marketType": "SPOT"' in src
