"""A risk configuration must survive an ordinary losing streak.

THE RUN THIS CAME FROM. The operator reported four losses in five trades with
"more amount losses" and a stopped agent, and read it as broken strategies. The
arithmetic says otherwise:

    MAX_PORTFOLIO_VAR_FRACTION  0.12   -> a stop-out costs ~12% of equity
    MAX_DRAWDOWN_FROM_HWM       0.30   -> the killswitch fires at 30%
    losses to halt              2.5

At the fixed 2:1 payoff this system places, break-even is a 33% win rate, so
the per-trade loss probability is ~0.64 and FOUR losses in a row happens about
one session in six. The strategies were not disproved; the account was sized so
that routine variance ended the session before enough trades existed to measure
anything.

`ceo_agent` already carried this arithmetic in a comment ("losses needed to
trip 3.8 -> FOUR consecutive stop-outs ... A limit that is certain to fire
within the first handful of trades stops being a disaster brake and becomes a
scheduled outage"). Nothing acted on it. This module does.
"""

from __future__ import annotations

import pytest

from backend.services import risk_coherence as rc


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

def test_the_operators_live_configuration_is_refused():
    """The exact numbers from the session that stopped."""
    verdict = rc.assess(var_fraction=0.12, drawdown_limit=0.30)

    assert verdict.ok is False
    assert verdict.losses_to_trip == pytest.approx(2.5)
    # The refusal must name every lever, because they are NOT equivalent: only
    # one of the three actually reduces risk.
    assert "MAX_PORTFOLIO_VAR_FRACTION" in verdict.reason
    assert "MAX_DRAWDOWN_FROM_HWM" in verdict.reason
    assert "fund the account" in verdict.reason.lower()


def test_a_survivable_configuration_passes():
    """0.03 against a 30% limit survives ten losses — a 1-in-87 streak."""
    verdict = rc.assess(var_fraction=0.03, drawdown_limit=0.30)

    assert verdict.ok is True
    assert verdict.losses_to_trip == pytest.approx(10.0)


def test_the_boundary_is_inclusive_at_the_required_streak():
    """Exactly `required` losses passes; a hair under does not.

    An off-by-one here would refuse a configuration the operator deliberately
    tuned to the stated bound, which reads as the check being arbitrary.
    """
    required = rc.min_losses_before_halt()
    exact = rc.assess(var_fraction=0.30 / required, drawdown_limit=0.30)
    assert exact.ok is True

    under = rc.assess(var_fraction=0.30 / (required - 0.5), drawdown_limit=0.30)
    assert under.ok is False


# ---------------------------------------------------------------------------
# "Could not measure" is not "unsafe"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var,dd", [(0.0, 0.30), (0.12, 0.0), (None, 0.30), (0.12, None)])
def test_an_unmeasurable_configuration_passes_with_its_reason_stated(var, dd):
    """The distinction this codebase has paid for three times.

    `fetch_usdt_perpetual_prices`, `open_positions()` and `venue_flat()` each
    conflated "empty" with "could not ask", and each cost a real bug. Refusing
    a session because a number was unreadable would halt trading on a config
    gap rather than on a risk one, and the operator would go looking for a
    dangerous setting that is not there.
    """
    verdict = rc.assess(var_fraction=var, drawdown_limit=dd)

    assert verdict.ok is True
    assert verdict.loss_fraction_per_stop is None
    assert verdict.losses_to_trip is None
    assert "could not be measured" in verdict.reason


# ---------------------------------------------------------------------------
# The override, and that it is read at call time
# ---------------------------------------------------------------------------

def test_the_requirement_is_read_at_call_time(monkeypatch):
    """The `simulation_mode` rule: a frozen constant means the operator changes
    the setting, is told it worked, and the process enforces the old one until
    a restart."""
    monkeypatch.setenv(rc.MIN_LOSSES_VAR, "3")
    assert rc.min_losses_before_halt() == 3
    # 0.12 against 0.30 survives 2.5 losses, so 3 still refuses...
    assert rc.assess(var_fraction=0.12, drawdown_limit=0.30).ok is False
    # ...and 2 accepts it. The operator can opt out, deliberately and visibly.
    monkeypatch.setenv(rc.MIN_LOSSES_VAR, "2")
    assert rc.assess(var_fraction=0.12, drawdown_limit=0.30).ok is True


@pytest.mark.parametrize("bad", ["", "abc", "0", "-4"])
def test_a_junk_override_falls_back_rather_than_disabling_the_check(monkeypatch, bad):
    """An unparseable value must not silently become "no requirement".

    Falling through to 0 would turn a typo into a disabled safety check that
    still reports itself as enabled — the failure mode this project calls worse
    than having no control at all.
    """
    monkeypatch.setenv(rc.MIN_LOSSES_VAR, bad)
    assert rc.min_losses_before_halt() == rc.MIN_LOSSES_BEFORE_HALT


# ---------------------------------------------------------------------------
# It reads the modules that OWN the numbers
# ---------------------------------------------------------------------------

def test_it_reads_the_owning_modules_rather_than_re_deriving_them():
    """A pre-check that re-reads the environment itself is how two numbers that
    must agree start disagreeing — the ATR-multiplier drift, and the venue
    pre-check that would have disagreed with the gate it stands in for."""
    from tests.sourceutil import code_only
    import inspect

    src = code_only(inspect.getsource(rc.assess_current))
    assert "max_drawdown_fraction" in src
    assert "max_portfolio_var_fraction" in src
    assert "getenv" not in src, "assess_current must not re-read the env itself"


def test_it_never_resizes_anything():
    """It refuses and explains; it must not quietly trade a smaller size.

    Silently sizing down would be the `simulation_mode` failure in a new place:
    the operator chooses one risk and the system runs another.
    """
    from tests.sourceutil import code_only
    import inspect

    src = code_only(inspect.getsource(rc))
    for forbidden in ("setenv", "environ[", "set_live_trading", "update_portfolio"):
        assert forbidden not in src, f"risk_coherence writes state via {forbidden}"


# ---------------------------------------------------------------------------
# The session refuses on it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_session_refuses_a_self_halting_configuration(monkeypatch):
    """At the moment the setting is chosen, not four losses later."""
    from backend.services import trading_session as ts

    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.12")
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")

    with pytest.raises(ValueError) as err:
        await ts.start_session(
            symbol="SOL/USDT", leverage=10, target_equity=100.0, start_amount=50.0,
        )

    assert "halts itself" in str(err.value)
    assert "MAX_PORTFOLIO_VAR_FRACTION" in str(err.value)


@pytest.mark.asyncio
async def test_the_check_runs_before_the_per_symbol_work(monkeypatch):
    """It is a property of the ACCOUNT, so no instrument choice can fix it.

    Checking it first is both cheaper and clearer: an operator told "BTC is not
    tradeable" would change the symbol and hit the real refusal anyway.
    """
    from backend.services import trading_session as ts

    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.12")
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "BTC/USDT")

    with pytest.raises(ValueError) as err:
        await ts.start_session(
            symbol="BTC/USDT", leverage=10, target_equity=100.0, start_amount=50.0,
        )

    # The RISK refusal, not the untradeable one.
    assert "halts itself" in str(err.value)
