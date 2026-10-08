"""The backtested prior: what it is allowed to do, and what it must never do.

THE GAP IT FILLS. `strategy_performance` needs MIN_SAMPLE (20) real closed
trades per strategy before a win rate may steer selection, and this account's
`trades` table is empty — so all eleven profiles scored the neutral 0.5 on the
track-record component and selection was decided purely by conditions-fit. Read
from the operator's live session on 2026-09-30:

    "DO NOT TRADE: the Grid setup is LONG but the specialist panel reads
     NEUTRAL at 0.05."

In a Range regime the scorer kept choosing Grid, Range and MeanReversion — the
three worst strategies in this project's own stored backtest — and the panel
then correctly refused to act on them. The evidence was sitting in `backtests/`
and nothing carried it to the choice.

WHAT THESE TESTS GUARD is not the arithmetic, which is trivial. It is the four
properties that keep this on the right side of invariant 5:

  1. a LIVE measurement always wins, so MIN_SAMPLE still governs promotion
  2. the prior is SHRUNK, because the backtest is in-sample and gross of fees
  3. no backtest means NEUTRAL, never zero
  4. it is deterministic — no model is consulted

Property 4 is asserted against the module's source, the same way
`tests/test_strategy_performance.py` asserts it for the realised path.
"""

from __future__ import annotations

import inspect
import json
import os

import pytest

from backend.services import strategy_priors as sp


@pytest.fixture(autouse=True)
def _clean():
    sp.reset()
    yield
    sp.reset()


# ---------------------------------------------------------------------------
# Property 4 — deterministic, no model
# ---------------------------------------------------------------------------

def test_no_model_is_consulted():
    """THIS IS THE INVARIANT-5 ARGUMENT, and it is the same one
    `strategy_performance` makes: a number derived by arithmetic from a stored
    file is not an LLM-authored hypothesis reaching production. It cannot
    invent, edit, disable or author a strategy; it supplies one field that
    Section 11.3 already lists as required."""
    src = inspect.getsource(sp)
    for forbidden in ("get_provider", "complete(", "ModelTier", "llm"):
        assert forbidden not in src, f"{forbidden} must not appear in a scoring input"


def test_the_same_file_always_gives_the_same_numbers():
    a = sp.load(force=True)
    b = sp.load(force=True)
    assert a["strategies"] == b["strategies"]


# ---------------------------------------------------------------------------
# Property 3 — absence is neutral, never a failure
# ---------------------------------------------------------------------------

def test_an_unknown_strategy_has_no_prior():
    """None must reach the scorer as the neutral 0.5, exactly like a strategy
    with no live record. Scoring an unmeasured strategy as a failure would
    permanently freeze out the one that would have worked."""
    assert sp.prior_win_rate("NoSuchStrategy") is None


def test_a_missing_backtest_directory_is_no_prior_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "BACKTEST_DIR", str(tmp_path / "nope"))
    sp.reset()
    data = sp.load(force=True)
    assert data["strategies"] == {}
    assert sp.prior_win_rate("Grid") is None


def test_an_unreadable_summary_is_no_prior_not_a_crash(monkeypatch, tmp_path):
    """Strategy scoring runs on every cycle. A corrupt file must degrade to the
    behaviour that existed before priors, not stop the agent deciding."""
    run = tmp_path / "2026-01-01"
    run.mkdir()
    (run / "summary.json").write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(sp, "BACKTEST_DIR", str(tmp_path))
    sp.reset()
    assert sp.load(force=True)["strategies"] == {}
    assert sp.prior_win_rate("Grid") is None


def test_a_strategy_under_the_trade_floor_is_reported_but_not_offered(monkeypatch, tmp_path):
    """A handful of simulated trades is exactly as unreliable as a handful of
    real ones, so the same floor applies."""
    run = tmp_path / "2026-01-01"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "runs": [{"strategies": [
            {"strategy": "Tiny", "trades": 3, "wins": 3, "total_r": 6.0},
        ]}],
    }), encoding="utf-8")
    monkeypatch.setattr(sp, "BACKTEST_DIR", str(tmp_path))
    sp.reset()

    entry = sp.load(force=True)["strategies"]["Tiny"]
    assert entry["usable"] is False
    assert entry["rawWinRate"] == 1.0          # reported honestly
    assert entry["shrunkWinRate"] is None
    assert sp.prior_win_rate("Tiny") is None   # and not offered as a prior


# ---------------------------------------------------------------------------
# Property 2 — shrunk, and shrunk toward the SCORER's neutral point
# ---------------------------------------------------------------------------

def test_the_shrinkage_target_is_the_scorers_own_neutral_rate():
    """IF THIS DRIFTS, TURNING PRIORS ON MOVES EVERY STRATEGY AT ONCE.

    The first version anchored on break-even (33.3%), which is BELOW the rate
    the scorer maps to 0.5 — so every strategy, including the good ones, scored
    lower than an unmeasured one and the whole field drifted toward
    MIN_SCORE_TO_SELECT. The agent would have traded LESS rather than better,
    which is the opposite of the point.
    """
    from backend.graphs.nodes.opportunity import _track_record_score

    neutral = sp._neutral_equivalent_win_rate()
    score, _ = _track_record_score(neutral)
    assert score == pytest.approx(0.5, abs=1e-9)


def test_a_backtest_moves_a_strategy_off_neutral_by_less_than_its_raw_rate_would():
    """*A backtest can move a strategy off neutral, but only half as far as a
    live measurement of the same size would.*

    The backtest is IN-SAMPLE, GROSS of fees and was measured over a mostly
    trending window — its own script says so. The range strategies losing there
    is partly that they were tested in a trend, which their regime gate would
    have muted live. Evidence, not a verdict.
    """
    neutral = sp._neutral_equivalent_win_rate()
    data = sp.load(force=True)["strategies"]

    # ANY LOSING STRATEGY, NOT A NAMED ONE. This used to pick "Grid", which was
    # a loser in the September window and is a WINNER in the October one — the
    # test was pinning a market regime while claiming to test the shrinkage.
    # Eight of eleven strategies changed sign between those two runs, so a
    # hardcoded name here is a timer, not an assertion.
    loser = next(
        (v for v in data.values()
         if v["usable"] and v["rawWinRate"] < neutral),
        None,
    )
    if loser is None:
        pytest.skip("the stored backtest has no usable below-neutral strategy")

    raw, shrunk = loser["rawWinRate"], loser["shrunkWinRate"]
    assert raw < shrunk < neutral, "a losing backtest must be pulled toward neutral"
    assert shrunk == pytest.approx((raw + neutral) / 2, abs=1e-3), (
        "halfway, because effective_n is capped at the same floor as the weight"
    )


def test_more_backtested_trades_do_not_buy_more_influence():
    """`effective_n` is capped at the floor on purpose: a longer backtest is more
    of the SAME window, not more independent evidence. Two strategies with the
    same win rate and very different trade counts must get the same prior."""
    run_dir = "2026-01-01"
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, run_dir))
        with open(os.path.join(tmp, run_dir, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump({"generatedAt": "2026-01-01T00:00:00+00:00", "runs": [{"strategies": [
                {"strategy": "Few", "trades": 40, "wins": 10, "total_r": -5.0},
                {"strategy": "Many", "trades": 4000, "wins": 1000, "total_r": -500.0},
            ]}]}, fh)
        old = sp.BACKTEST_DIR
        sp.BACKTEST_DIR = tmp
        try:
            sp.reset()
            d = sp.load(force=True)["strategies"]
            assert d["Few"]["shrunkWinRate"] == d["Many"]["shrunkWinRate"]
        finally:
            sp.BACKTEST_DIR = old
            sp.reset()


# ---------------------------------------------------------------------------
# Property 1 — a live measurement always wins
# ---------------------------------------------------------------------------

def test_the_scorer_asks_the_live_record_first():
    """MIN_SAMPLE still governs promotion. The prior fills the gap before a
    strategy has traded and is superseded the moment it has — otherwise a
    simulated window would keep steering an account that had its own answer."""
    import backend.graphs.nodes.opportunity as opp

    src = inspect.getsource(opp.score_strategies) if hasattr(opp, "score_strategies") else None
    if src is None:
        # The scorer node's name differs across versions; find the function that
        # defines `_rate` rather than guessing at the node's name.
        src = next(
            inspect.getsource(fn)
            for fn in vars(opp).values()
            if inspect.isfunction(fn) and "def _rate(" in (
                inspect.getsource(fn) if fn.__module__ == opp.__name__ else ""
            )
        )
    assert 'entry["usable"]' in src
    assert src.index('entry["usable"]') < src.index("strategy_priors.prior_win_rate"), (
        "the realised record must be consulted before the backtest prior"
    )


def test_the_evidence_string_says_which_source_it_used():
    """"38% backtested" and "38% realised" are very different claims about an
    account, and this node's output is what an operator reads to decide whether
    to trust a selection. The arithmetic is identical; only the label differs."""
    from backend.graphs.nodes.opportunity import _track_record_score

    live_score, live_detail = _track_record_score(0.38, "realised")
    prior_score, prior_detail = _track_record_score(0.38, "backtest")

    assert live_score == prior_score, "the discounting happens before this point"
    assert "realised" in live_detail and "BACKTEST" not in live_detail
    assert "BACKTESTED" in prior_detail and "superseded" in prior_detail


# ---------------------------------------------------------------------------
# The stored file, and what it actually says
# ---------------------------------------------------------------------------

def test_the_committed_backtest_ranks_the_losing_strategies_below_neutral():
    """The behavioural claim this whole change rests on, asserted against the
    real committed file rather than a fixture: the strategies the Range regime
    keeps selecting are the ones the backtest says lose."""
    neutral = sp._neutral_equivalent_win_rate()
    data = sp.load(force=True)["strategies"]
    if not data:
        pytest.skip("no backtest is committed in this checkout")

    # SIGN-CONSISTENCY ACROSS WHATEVER THE FILE SAYS, not a named ranking.
    #
    # This used to assert Grid/Range/MeanReversion below neutral and
    # Scalping/Breakout/Momentum above — true of the September window and
    # EXACTLY INVERTED in the October one. Eight of eleven strategies changed
    # sign between the two runs, so the names were encoding a market regime
    # that the file is supposed to be the authority on.
    #
    # The property that must hold in every regime: a strategy the backtest
    # says LOSES must not score above neutral, and one it says WINS must not
    # score below. If that ever breaks, the prior is arguing against its own
    # evidence, which is the only way this feature can do harm.
    checked = 0
    for name, entry in data.items():
        if not entry["usable"]:
            continue
        checked += 1
        if entry["expectancyR"] < 0:
            assert entry["shrunkWinRate"] < neutral, (
                f"{name} loses ({entry['expectancyR']:+.3f}R) but scores at or "
                f"above neutral"
            )
        elif entry["expectancyR"] > 0:
            assert entry["shrunkWinRate"] >= neutral - 1e-9, (
                f"{name} wins ({entry['expectancyR']:+.3f}R) but is penalised"
            )
    assert checked >= 5, f"only {checked} usable strategies — is the file real?"


def test_the_status_carries_the_caveats_with_the_numbers():
    """A ranked table with no context reads as a verdict. `run_backtests.py`
    prints these warnings and they do not survive into a JSON file on their
    own."""
    st = sp.status()
    joined = " ".join(st["caveats"]).lower()
    assert "in-sample" in joined
    assert "gross of fees" in joined
    assert "informs; it does not deploy" in joined
