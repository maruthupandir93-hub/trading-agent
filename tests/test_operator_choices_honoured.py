"""The operator chose 10x and 100%, and three things quietly changed both.

THE REPORT, VERBATIM: *"i choose the 10x leverage and allocation of 100% but it
choose different leverage on every trade"* and *"i choose 100% allocation from
my fund but it choose only some amount in likely 2/3 of balance"*.

Both were accurate, and neither was one bug. Three separate reducers sit
between the operator's selection and the order, and each was defensible on its
own:

    MAX_LEVERAGE_BY_REGIME   NORMAL 4x, HIGH 2x, EXTREME 1x -> a different
                             leverage on every cycle, which is exactly what
                             "different leverage on every trade" describes
    RISK_MULTIPLIER          HIGH 0.6 -> 60% of the size, applied AFTER the
                             VaR cap so the two compound
    the VaR cap              equity x VaR / (stop% x 1.5) -> at VaR 0.12 and a
                             1.3% stop that is 62% of the balance at 10x, which
                             is the "2/3" the operator measured

This codebase already treats "a control that silently delivers something other
than what was selected" as worse than a refusal — `simulation_mode`, the margin
buffer, the allocation. These are the same failure three more times.

WHAT STAYED, AND WHY THE LINE IS THERE. The first two are HEURISTICS and are
now the operator's to choose. The VaR cap, the liquidation cap and the mandatory
stop are BOUNDS and are untouched: past the liquidation cap the venue closes the
position before its stop fires, which is not more risk but UNBOUNDED risk, and
invariant 3 forbids it.
"""

from __future__ import annotations

import inspect

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# The chosen LEVERAGE is the one that trades
# ---------------------------------------------------------------------------

def test_the_regime_leverage_cap_is_off_by_default(monkeypatch):
    """NORMAL 4x / HIGH 2x / EXTREME 1x meant a 10x session traded at a
    different leverage depending on which regime the cycle happened to read."""
    import backend.graphs.nodes.risk_gateway as rg

    monkeypatch.delenv("VOLATILITY_LEVERAGE_CAP", raising=False)
    assert rg.volatility_leverage_cap_enabled() is False

    monkeypatch.setenv("VOLATILITY_LEVERAGE_CAP", "true")
    assert rg.volatility_leverage_cap_enabled() is True, "the old behaviour is unreachable"


def test_the_absolute_ceiling_is_untouched():
    """Invariant 2. The regime table is a preference; THIS is the bound, and it
    is not runtime-overridable by anything added here."""
    from backend.core.risk_manager import max_leverage_ceiling

    assert max_leverage_ceiling("real") <= 10
    assert max_leverage_ceiling("paper") <= 10


def test_the_liquidation_cap_is_NOT_optional():
    """THE LINE BETWEEN A PREFERENCE AND A BOUND.

    Past this cap the venue liquidates before the stop is touched, so the stop
    never fires, every protective mechanism built on it is bypassed at once, and
    the loss becomes the ENTIRE margin. Turning it off would not give the
    operator more risk — it would give them unbounded risk, which is what
    invariant 3 exists to prevent.
    """
    import backend.graphs.nodes.risk_gateway as rg

    src = code_only(inspect.getsource(rg.gate))
    assert "liquidation_safe_leverage" in src
    # No env switch may reach it.
    block = src[src.index("liquidation_safe_leverage") - 400:
                src.index("liquidation_safe_leverage") + 400]
    assert "getenv" not in block, "the liquidation cap became optional"


@pytest.mark.parametrize("stop_fraction,requested,at_least", [
    (0.013, 10, 10),   # a 1.3% stop sits well inside 10x's ~9.5% distance
    (0.025, 10, 10),   # and so does 2.5%
])
def test_an_ordinary_stop_does_not_trigger_the_liquidation_cap(stop_fraction, requested, at_least):
    """So the cap that STAYS does not quietly become the one that reduces the
    operator's leverage on every trade instead."""
    from backend.core.risk_manager import liquidation_safe_leverage

    assert liquidation_safe_leverage(stop_fraction, requested) >= at_least


def test_a_stop_wider_than_the_liquidation_distance_still_caps():
    """At 10x liquidation is ~9.5% away. A 12% stop cannot fit inside it, and
    that is the case the cap exists for."""
    from backend.core.risk_manager import liquidation_safe_leverage

    assert liquidation_safe_leverage(0.12, 10) < 10


# ---------------------------------------------------------------------------
# The chosen ALLOCATION is the one that is deployed
# ---------------------------------------------------------------------------

def test_the_volatility_size_haircut_is_off_by_default(monkeypatch):
    """The SECOND reduction, applied after the VaR cap so the two compound: a
    100% allocation VaR-capped to 62% became 37% in a HIGH regime."""
    import backend.graphs.nodes.risk_gateway as rg

    monkeypatch.delenv("VOLATILITY_SIZE_HAIRCUT", raising=False)
    assert rg.volatility_haircut_enabled() is False

    monkeypatch.setenv("VOLATILITY_SIZE_HAIRCUT", "true")
    assert rg.volatility_haircut_enabled() is True


def test_the_haircut_still_applies_to_the_UNATTENDED_path():
    """THE DISTINCTION IS WHO CHOSE THE SIZE. With no session nobody chose
    anything — the agent is unattended and sizes itself, so a volatility
    haircut is the system exercising its own judgement rather than overriding
    someone's selection."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    block = src[src.index("volatility_multiplier = 1.0"):]
    block = block[: block.index("if size <= 0")]
    assert "session_leverage is not None" in block, (
        "the haircut no longer distinguishes an operator-chosen size from an "
        "unattended one"
    )


def test_the_multiplier_is_still_COMPUTED_when_it_is_not_applied():
    """Turning a reduction off must not also stop recording what it would have
    done — the arrangement REQUIRE_HTF_ALIGNMENT and the entry-extension gate
    both use."""
    import backend.graphs.nodes.risk_gateway as rg

    src = inspect.getsource(rg.gate)
    block = src[src.index("volatility_multiplier = 1.0"):]
    assert "volatility_haircut_note" in block
    assert "AllocationHonoured" in src, "the operator cannot see what was skipped"


def test_the_VaR_cap_is_NOT_optional():
    """It is a BOUND, not a preference, and removing it would not even deliver
    the bigger position: the CRO re-checks independently and would refuse the
    plan, which is the two-gates-disagree outage this node's own comment
    records (9,926 cycles, 0 trades, 75 GLOBAL_VAR_LIMIT events)."""
    import backend.graphs.nodes.risk_gateway as rg

    src = code_only(inspect.getsource(rg.gate))
    assert "max_portfolio_var_fraction" in src
    assert "VAR_HEADROOM" in src


def test_the_deployment_arithmetic_is_what_the_operator_must_change():
    """The allocation an operator actually gets at L leverage is

        margin / equity  =  VaR / (stop_fraction x SLIPPAGE x leverage)

    so 100% at 10x with a 1.3% stop needs VaR ~= 0.19 — and that is the number
    to change, not the allocation slider. Asserted so the figure quoted to the
    operator stays derived from the code rather than from a message.
    """
    from backend.agents.cro_agent import STOP_SLIPPAGE_MULTIPLIER

    deployed = lambda var, stop, lev: var / (stop * STOP_SLIPPAGE_MULTIPLIER * lev)

    assert deployed(0.12, 0.013, 10) == pytest.approx(0.615, abs=0.02)   # the "2/3"
    assert deployed(0.05, 0.013, 10) == pytest.approx(0.256, abs=0.02)
    assert deployed(0.19, 0.013, 10) == pytest.approx(0.974, abs=0.03)


# ---------------------------------------------------------------------------
# The REAL balance comes from the venue the operator connected
# ---------------------------------------------------------------------------

def test_the_real_balance_reads_the_configured_venue():
    """It read `services/exchange_client`, which hardcodes `ccxt.binance(...)`,
    so a BYBIT deployment reported a BINANCE account. The same defect CLAUDE.md
    records for `POST /live-trading/enable`, in a second place.

    It is the DENOMINATOR of every percentage a real session reports — the
    progress bar, the drawdown check, the floor check and the capital pool are
    all computed from it.
    """
    import backend.services.trading_session as ts

    src = inspect.getsource(ts.real_account_balance)
    assert "from backend.services.venue import get_venue" in src
    assert "free_usdt" in src
    # The old client REMAINS as a fallback: it answers correctly on Binance,
    # which is every existing install, and a transport failure on one path
    # should not blank a balance the other can still produce.
    assert "get_exchange_client" in src


def test_bybit_is_asked_for_its_UNIFIED_account():
    """A Unified Trading Account holds one balance for spot, margin and
    derivatives, and ccxt needs `accountType` to know which book to report.
    Asking the Binance way returns the CONTRACT wallet on a classic account and
    nothing useful on a unified one."""
    import backend.services.venue as venue

    src = inspect.getsource(venue.Venue.free_usdt)
    assert "UNIFIED" in src
    assert 'self.id == "bybit"' in src


def test_there_is_exactly_ONE_free_usdt_definition():
    """A second `async def free_usdt` in the same class is DEAD CODE — Python
    keeps the last one — and that is how this fix nearly shipped as a no-op: a
    method was added without checking whether one already existed, the new body
    never ran, and only a test reading the live source caught it."""
    import backend.services.venue as venue
    import inspect as _i

    src = _i.getsource(venue)
    assert src.count("async def free_usdt") == 1


def test_an_unreadable_balance_is_None_and_never_zero():
    """"No money" and "could not ask" are different facts, and this codebase has
    paid for conflating them five times. A zero here would read as a wiped
    account and would trip the session's floor check."""
    import backend.services.venue as venue

    src = inspect.getsource(venue.Venue.free_usdt)
    assert "return None" in src
    assert "0.0" not in src.split("except")[-1], "a failure path returns a number"
