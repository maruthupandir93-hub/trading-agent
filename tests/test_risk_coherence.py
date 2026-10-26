"""A risk configuration must survive an ordinary losing streak.

THE RUN THIS CAME FROM. The operator reported four losses in five trades with
"more amount losses" and a stopped agent, and read it as broken strategies. The
arithmetic says otherwise:

    MAX_PORTFOLIO_VAR_FRACTION  0.12   -> a stop-out costs ~8% of equity
                                          (the budget DIVIDED by the 1.5x gap
                                          allowance -- this said 12% and that
                                          was a 50% overstatement, corrected
                                          when the guard turned out to refuse
                                          the shipped defaults too)
    MAX_DRAWDOWN_FROM_HWM       0.30   -> the killswitch fires at 30%
    losses to halt              3.75

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

import os

import pytest

from backend.services import risk_coherence as rc


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

def test_the_operators_live_configuration_is_refused():
    """The exact numbers from the session that stopped."""
    verdict = rc.assess(var_fraction=0.12, drawdown_limit=0.30)

    assert verdict.ok is False
    # 0.30 / (0.12 / 1.5). The budget is sized for a stop that GAPS; an
    # ordinary stop-out costs two thirds of it.
    assert verdict.losses_to_trip == pytest.approx(3.75)
    # The refusal must name every lever, because they are NOT equivalent: only
    # one of the three actually reduces risk.
    assert "MAX_PORTFOLIO_VAR_FRACTION" in verdict.reason
    assert "MAX_DRAWDOWN_FROM_HWM" in verdict.reason
    assert "fund the account" in verdict.reason.lower()


def test_a_survivable_configuration_passes():
    """0.03 against a 30% limit survives ten losses — a 1-in-87 streak."""
    verdict = rc.assess(var_fraction=0.03, drawdown_limit=0.30)

    assert verdict.ok is True
    assert verdict.losses_to_trip == pytest.approx(15.0)


def test_the_boundary_is_inclusive_at_the_required_streak():
    """Exactly `required` losses passes; a hair under does not.

    An off-by-one here would refuse a configuration the operator deliberately
    tuned to the stated bound, which reads as the check being arbitrary.
    """
    required = rc.min_losses_before_halt()
    # The VaR fraction that yields exactly `required` losses is
    # `drawdown x SLIPPAGE / required`, because a stop-out costs
    # `var / SLIPPAGE`. Derived from the module's own constant rather than
    # restated, so the two cannot drift.
    slippage = rc.DEFAULT_STOP_SLIPPAGE_MULTIPLIER
    exact = rc.assess(var_fraction=0.30 * slippage / required, drawdown_limit=0.30)
    assert exact.ok is True

    under = rc.assess(
        var_fraction=0.30 * slippage / (required - 0.5), drawdown_limit=0.30,
    )
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
    monkeypatch.setenv(rc.MIN_LOSSES_VAR, "4")
    assert rc.min_losses_before_halt() == 4
    # 0.12 against 0.30 survives 3.75 losses, so a requirement of 4 refuses...
    assert rc.assess(var_fraction=0.12, drawdown_limit=0.30).ok is False
    # ...and 3 accepts it. The operator can opt out, deliberately and visibly.
    monkeypatch.setenv(rc.MIN_LOSSES_VAR, "3")
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


# ---------------------------------------------------------------------------
# The regression that broke a real deployment
# ---------------------------------------------------------------------------

def test_the_SHIPPED_DEFAULTS_can_actually_start_a_session(monkeypatch):
    """THE BUG THIS FILE'S OWN GUARD CAUSED, and it reached production.

    `risk_coherence` refuses a configuration that cannot survive
    `MIN_LOSSES_BEFORE_HALT` stop-outs. That is right. What nobody checked is
    that the SHIPPED DEFAULTS pass it — and they did not:

        VaR 0.05, drawdown 0.10  ->  3.0 losses  ->  UNSAFE

    So on a fresh deployment every `POST /api/session/start` returned the
    refusal and the operator could not trade at all. A product whose own guard
    rejects its own defaults is broken out of the box, and it was found the
    worst way: by an operator redeploying and discovering nothing worked.

    The pair moves together now (drawdown 0.30 against the 0.05 VaR budget,
    9.0 losses). This test drives the REAL defaults through the REAL guard so
    the two can never drift apart again — asserting the property, not the
    numbers, because pinning either number is what caused this.
    """
    # The operator's own .env must not decide whether this passes: the question
    # is about the DEFAULTS. Same isolation rule as `isolate_write_auth`.
    for name in ("MAX_PORTFOLIO_VAR_FRACTION", "MAX_DRAWDOWN_FROM_HWM",
                 "MIN_LOSSES_BEFORE_HALT"):
        monkeypatch.delenv(name, raising=False)

    from backend.agents.ceo_agent import MAX_DRAWDOWN_FROM_HIGH_WATER_MARK
    from backend.agents.cro_agent import max_portfolio_var_fraction

    verdict = rc.assess(
        var_fraction=max_portfolio_var_fraction(),
        drawdown_limit=MAX_DRAWDOWN_FROM_HIGH_WATER_MARK,
    )
    assert verdict.verdict == rc.SAFE, (
        f"a fresh deployment cannot start a session: {verdict.reason}"
    )
    assert verdict.ok is True
    assert verdict.live_ok is True, "and a REAL session is refused too"


def test_the_loss_per_stop_is_the_budget_divided_by_the_gap_allowance():
    """It used to be the budget ITSELF, a 50% overstatement.

    The CRO permits `notional <= equity x var / (stop x SLIPPAGE)`, so a
    position stopped AT its stop loses `equity x var / SLIPPAGE`. The budget is
    sized for a stop that GAPS; the ordinary stop-out that a losing streak is
    made of costs two thirds of it at the 1.5x allowance.

    Overstating it made the guard half again as strict as the risk it models,
    which is how it came to refuse the defaults.
    """
    verdict = rc.assess(var_fraction=0.06, drawdown_limit=0.30)
    assert verdict.loss_fraction_per_stop == pytest.approx(
        0.06 / rc.DEFAULT_STOP_SLIPPAGE_MULTIPLIER
    )


def test_the_gap_allowance_is_the_CROs_own_constant():
    """A budget built with one multiplier and judged with another is the
    two-numbers-in-two-places failure this codebase keeps paying for."""
    from backend.agents.cro_agent import STOP_SLIPPAGE_MULTIPLIER

    assert rc.DEFAULT_STOP_SLIPPAGE_MULTIPLIER == STOP_SLIPPAGE_MULTIPLIER


# ---------------------------------------------------------------------------
# Making the refusal ACTIONABLE — the reason it kept recurring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_risk_pair_is_settable_without_touching_the_server(monkeypatch):
    """THE REFUSAL WAS CORRECT AND UNACTIONABLE, WHICH IS WHY IT RECURRED.

    The only way to change the pair was to SSH in, edit `.env` and restart —
    and `.env` is gitignored, so `git pull` carried the code fix and left the
    configuration exactly as it was. The operator redeployed twice and was
    still blocked. A control nobody can reach is the same failure as a value
    computed and never used.
    """
    from backend.api.admin import RiskConfigRequest, set_risk_config

    written: dict = {}
    monkeypatch.setattr(
        "backend.core.config.settings._persist_env",
        lambda k, v: written.__setitem__(k, v),
    )

    out = await set_risk_config(RiskConfigRequest(varFraction=0.05, drawdownLimit=0.30))
    assert out["ok"] is True
    assert written["MAX_PORTFOLIO_VAR_FRACTION"] == "0.05"
    assert written["MAX_DRAWDOWN_FROM_HWM"] == "0.3"
    # Written to the ENVIRONMENT too, or the operator is told it worked while
    # the running agent keeps the old number until a restart.
    assert os.environ["MAX_PORTFOLIO_VAR_FRACTION"] == "0.05"


@pytest.mark.asyncio
async def test_an_incoherent_pair_is_REFUSED_rather_than_saved(monkeypatch):
    """Persisting a setting that would then refuse every session puts the
    operator back where they started with no idea why. The refusal names the
    largest value that WOULD fit, because a limit that only says "no" is what
    made this feel arbitrary."""
    from fastapi import HTTPException

    from backend.api.admin import RiskConfigRequest, set_risk_config

    written: dict = {}
    monkeypatch.setattr(
        "backend.core.config.settings._persist_env",
        lambda k, v: written.__setitem__(k, v),
    )
    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.05")
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")

    with pytest.raises(HTTPException) as exc:
        await set_risk_config(RiskConfigRequest(varFraction=0.12))

    assert exc.value.status_code == 400
    assert "NOT SAVED" in exc.value.detail
    assert "largest VaR fraction" in exc.value.detail
    assert written == {}, "an incoherent pair was persisted anyway"
    assert os.environ["MAX_PORTFOLIO_VAR_FRACTION"] == "0.05", "the live value changed"


@pytest.mark.asyncio
async def test_the_panel_is_told_what_would_fit(monkeypatch):
    """Derived from the guard's own arithmetic, so the number the panel offers
    is the one it will accept — not a guess the operator then has to retry."""
    from backend.api.admin import get_risk_config

    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.12")
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")
    monkeypatch.delenv("MIN_LOSSES_BEFORE_HALT", raising=False)

    out = await get_risk_config()
    assert out["verdict"] == "UNSAFE"
    assert out["sessionAllowed"] is False

    offered = out["suggestions"]["largestVarFractionThatFits"]
    assert offered is not None
    # THE OFFER MUST ACTUALLY PASS. An offer the guard then refuses is worse
    # than no offer.
    assert rc.assess(var_fraction=offered, drawdown_limit=0.30).ok is True


@pytest.mark.asyncio
async def test_the_suggestion_names_both_levers_and_that_they_differ(monkeypatch):
    """They both make the refusal go away and they are opposites: lowering the
    VaR fraction reduces risk, raising the drawdown limit accepts a deeper
    loss. An operator should not have to infer that from two numbers."""
    from backend.api.admin import get_risk_config

    monkeypatch.setenv("MAX_PORTFOLIO_VAR_FRACTION", "0.12")
    monkeypatch.setenv("MAX_DRAWDOWN_FROM_HWM", "0.30")

    note = (await get_risk_config())["suggestions"]["note"]
    assert "reduces risk" in note
    assert "deeper" in note
